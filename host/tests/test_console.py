# Task 11 -- interactive console: batched FIFO drain, txfull-gated send,
# and the stdin/stdout pump loop body. All against a FakeBoard; no real
# board, no real tty (termios/select stay untested here -- see console.py's
# console_loop, which is thin plumbing around the pump body tested below).
import io

import pytest

from b8008net import console


class _Reg:
    """Stands in for a litex CSRRegister: optional .addr, .read(), .write()."""

    def __init__(self, addr=None, read=None, write=None):
        self.addr = addr
        self._read = read
        self._write = write

    def read(self):
        return self._read()

    def write(self, value):
        self._write(value)


class FakeBoard:
    """Stands in for b8008net.board.Board's console-relevant surface:
    .regs.b8008_rxlevel/b8008_rxtx/b8008_txfull and .read(addr, n, burst)."""

    RXTX_ADDR = 0xF0000008

    def __init__(self, rx_bytes=b"", txfull_script=None):
        self._rx_queue = bytearray(rx_bytes)
        self._tx_sent = bytearray()
        self.read_calls = []  # (addr, n, burst) per board.read() call
        self._txfull_script = list(txfull_script or [])

        self.regs = type("Regs", (), {})()
        self.regs.b8008_rxlevel = _Reg(read=lambda: len(self._rx_queue))
        self.regs.b8008_rxtx = _Reg(addr=self.RXTX_ADDR, write=self._write_rxtx)
        self.regs.b8008_txfull = _Reg(read=self._read_txfull)

    def read(self, addr, n=1, burst="incr"):
        assert addr == self.RXTX_ADDR
        assert burst == "fixed"
        assert n <= 256
        self.read_calls.append((addr, n, burst))
        chunk = list(self._rx_queue[:n])
        del self._rx_queue[:n]
        return chunk

    def _write_rxtx(self, value):
        self._tx_sent.append(value & 0xFF)

    def _read_txfull(self):
        if self._txfull_script:
            return self._txfull_script.pop(0)
        return 0


# ── drain() ──────────────────────────────────────────────────────────────
def test_drain_returns_scripted_bytes():
    board = FakeBoard(rx_bytes=b"hello")
    assert console.drain(board) == b"hello"
    assert board.read_calls == [(FakeBoard.RXTX_ADDR, 5, "fixed")]


def test_drain_returns_empty_and_skips_read_when_rxlevel_zero():
    board = FakeBoard(rx_bytes=b"")
    assert console.drain(board) == b""
    assert board.read_calls == []


def test_drain_batches_at_most_256_bytes_per_call():
    board = FakeBoard(rx_bytes=bytes(range(256)) * 2)  # 512 bytes queued
    out = console.drain(board)
    assert len(out) == 256
    assert board.read_calls == [(FakeBoard.RXTX_ADDR, 256, "fixed")]
    # A second call drains the remainder -- proves batching, not truncation.
    out2 = console.drain(board)
    assert len(out2) == 256
    assert out + out2 == (bytes(range(256)) * 2)


# ── send() ───────────────────────────────────────────────────────────────
def test_send_writes_every_byte_in_order():
    board = FakeBoard()
    console.send(board, b"G 2000\r")
    assert bytes(board._tx_sent) == b"G 2000\r"


def test_send_stalls_on_txfull_before_writing(monkeypatch):
    board = FakeBoard(txfull_script=[1, 1, 0])  # busy twice, then clear
    sleeps = []
    monkeypatch.setattr(console.time, "sleep", lambda s: sleeps.append(s))

    console.send(board, b"A")

    assert bytes(board._tx_sent) == b"A"
    assert sleeps == [0.001, 0.001]


# ── console_pump() -- the testable inner loop body ──────────────────────
def test_pump_drains_board_to_stdout():
    board = FakeBoard(rx_bytes=b"8008 Monitor\r\n")
    stdout = io.BytesIO()
    cont = console.console_pump(board, io.BytesIO(b""), stdout, stdin_ready=False)
    assert cont is True
    assert stdout.getvalue() == b"8008 Monitor\r\n"


def test_pump_forwards_stdin_to_board_when_ready():
    board = FakeBoard()
    stdin = io.BytesIO(b"L\r")
    cont = console.console_pump(board, stdin, io.BytesIO(), stdin_ready=True)
    assert cont is True
    assert bytes(board._tx_sent) == b"L\r"


def test_pump_ignores_stdin_when_not_ready():
    board = FakeBoard()
    stdin = io.BytesIO(b"L\r")
    cont = console.console_pump(board, stdin, io.BytesIO(), stdin_ready=False)
    assert cont is True
    assert bytes(board._tx_sent) == b""


def test_pump_exits_on_ctrl_bracket():
    board = FakeBoard()
    stdin = io.BytesIO(b"\x1d")
    cont = console.console_pump(board, stdin, io.BytesIO(), stdin_ready=True)
    assert cont is False
    assert bytes(board._tx_sent) == b""


def test_pump_sends_bytes_preceding_exit_byte_in_same_chunk():
    board = FakeBoard()
    stdin = io.BytesIO(b"ab\x1dcd")
    cont = console.console_pump(board, stdin, io.BytesIO(), stdin_ready=True)
    assert cont is False
    assert bytes(board._tx_sent) == b"ab"


def test_pump_returns_false_on_stdin_eof():
    board = FakeBoard()
    stdin = io.BytesIO(b"")
    cont = console.console_pump(board, stdin, io.BytesIO(), stdin_ready=True)
    assert cont is False


def test_pump_does_both_directions_in_one_call():
    board = FakeBoard(rx_bytes=b"ok\r\n")
    stdin = io.BytesIO(b"L\r")
    stdout = io.BytesIO()
    cont = console.console_pump(board, stdin, stdout, stdin_ready=True)
    assert cont is True
    assert stdout.getvalue() == b"ok\r\n"
    assert bytes(board._tx_sent) == b"L\r"
