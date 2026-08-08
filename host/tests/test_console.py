# Task 1 (make-login-console-client) -- rewritten against the new console
# CSR bank (SPEC.md S11): non-destructive console_rx {data,valid,level},
# an explicit console_rx_pop that is the only thing that consumes a byte,
# a console_tx {level,full} pair, a console_tx_data that rejects rather
# than queues while full, and a sticky/W1C console_err/console_err_clear
# pair. All against a FakeBoard; no real board, no real tty (termios/
# select stay untested here -- see console.py's console_loop, which is
# thin plumbing around the pump body tested below).
import io

import pytest

from b8008net import console


class _Reg:
    """Stands in for a litex CSRRegister: optional .read()/.write()."""

    def __init__(self, read=None, write=None):
        self._read = read
        self._write = write

    def read(self):
        return self._read()

    def write(self, value):
        self._write(value)


# console_rx bit layout (SPEC.md S11.1): data[7:0], valid[8], level[21:9]
_RX_VALID_BIT = 1 << 8
_RX_LEVEL_SHIFT = 9
# console_tx bit layout (SPEC.md S11.3): level[8:0], full[9]
_TX_FULL_BIT = 1 << 9
# console_err bits (SPEC.md S11.5)
_ERR_RX_OVERFLOW = 1 << 0
_ERR_TX_WRITE_WHEN_FULL = 1 << 1
_ERR_RX_POP_WHEN_EMPTY = 1 << 2


class FakeBoard:
    """Stands in for b8008net.board.Board's console-relevant surface:
    .regs.b8008_console_console_{rx,rx_pop,tx,tx_data,err,err_clear}.

    `infinite=True` models an infinitely chatty board for the drain-bound
    test: console_rx always reports a byte waiting, and pops never
    exhaust it -- the loop can only be stopped by drain()'s own bound.
    """

    def __init__(self, rx_bytes=b"", infinite=False, tx_capacity=256):
        self._rx_queue = bytearray(rx_bytes)
        self._infinite = infinite
        self.pop_calls = 0

        self._tx_capacity = tx_capacity
        self._tx_accepted = bytearray()
        self.tx_write_attempts = 0

        self._err = 0
        self.err_clear_writes = []

        self.regs = type("Regs", (), {})()
        self.regs.b8008_console_console_rx = _Reg(read=self._read_rx)
        self.regs.b8008_console_console_rx_pop = _Reg(write=self._write_rx_pop)
        self.regs.b8008_console_console_tx = _Reg(read=self._read_tx)
        self.regs.b8008_console_console_tx_data = _Reg(write=self._write_tx_data)
        self.regs.b8008_console_console_err = _Reg(read=lambda: self._err)
        self.regs.b8008_console_console_err_clear = _Reg(write=self._write_err_clear)

    # -- console_rx / console_rx_pop ---------------------------------------
    def _read_rx(self):
        if self._infinite:
            data, valid, level = 0x42, 1, 1
        else:
            level = len(self._rx_queue)
            valid = 1 if level else 0
            data = self._rx_queue[0] if level else 0
        return (data & 0xFF) | (valid << 8) | ((level & 0x1FFF) << _RX_LEVEL_SHIFT)

    def _write_rx_pop(self, _value):
        self.pop_calls += 1
        if self._infinite:
            return  # an infinitely chatty board: popping never empties it
        if self._rx_queue:
            del self._rx_queue[0]
        else:
            self._err |= _ERR_RX_POP_WHEN_EMPTY

    # -- console_tx / console_tx_data ---------------------------------------
    def _read_tx(self):
        level = len(self._tx_accepted)
        full = 1 if level >= self._tx_capacity else 0
        return (level & 0x1FF) | (full << 9)

    def _write_tx_data(self, value):
        self.tx_write_attempts += 1
        if len(self._tx_accepted) >= self._tx_capacity:
            self._err |= _ERR_TX_WRITE_WHEN_FULL
            return
        self._tx_accepted.append(value & 0xFF)

    def drain_tx_hardware(self, n=1):
        """Simulate RS232PHYTX draining n bytes off tx_fifo at line rate --
        lets tests script "space opens up after a wait"."""
        del self._tx_accepted[:n]

    # -- console_err / console_err_clear -------------------------------------
    def _write_err_clear(self, value):
        self.err_clear_writes.append(value)
        self._err &= ~value & 0xFFFFFFFF


# ── console_rx: non-destructive read ────────────────────────────────────
def test_read_is_not_destructive():
    board = FakeBoard(rx_bytes=b"AB")
    word1 = board.regs.b8008_console_console_rx.read()
    word2 = board.regs.b8008_console_console_rx.read()
    assert word1 == word2
    assert word1 & 0xFF == ord("A")
    assert word1 & _RX_VALID_BIT
    assert board.pop_calls == 0


# ── drain() ──────────────────────────────────────────────────────────────
def test_drain_pops_exactly_once_per_byte():
    board = FakeBoard(rx_bytes=b"hello")
    out = console.drain(board)
    assert out == b"hello"
    assert board.pop_calls == 5


def test_drain_stops_at_empty():
    board = FakeBoard(rx_bytes=b"")
    out = console.drain(board)
    assert out == b""
    assert board.pop_calls == 0


def test_drain_is_bounded():
    board = FakeBoard(infinite=True)
    out = console.drain(board)
    assert len(out) == console.DRAIN_MAX_BYTES
    assert board.pop_calls == console.DRAIN_MAX_BYTES


# ── send() ───────────────────────────────────────────────────────────────
def test_send_respects_full(monkeypatch):
    board = FakeBoard(tx_capacity=1)
    board._tx_accepted.append(0xFF)  # fifo starts full

    def fake_sleep(_s):
        board.drain_tx_hardware(1)  # hardware frees a slot while we wait

    monkeypatch.setattr(console.time, "sleep", fake_sleep)

    accepted = console.send(board, b"A", gap_s=0)

    assert accepted == 1
    assert bytes(board._tx_accepted) == b"A"
    assert board.tx_write_attempts == 1  # never attempted a write while full


def test_send_reports_short_write(monkeypatch):
    board = FakeBoard(tx_capacity=1)
    board._tx_accepted.append(0xFF)  # full, and stays full -- never drains

    monkeypatch.setattr(console.time, "sleep", lambda s: None)

    accepted = console.send(board, b"ABCDE", gap_s=0)

    assert accepted == 0  # the fifo never had room; nothing silently dropped
    assert accepted < 5
    assert board.tx_write_attempts == 0  # never wrote while full


def test_send_writes_every_byte_when_never_full():
    board = FakeBoard()
    accepted = console.send(board, b"G 2000\r", gap_s=0)
    assert accepted == 7
    assert bytes(board._tx_accepted) == b"G 2000\r"


# ── check_errors() ───────────────────────────────────────────────────────
def test_check_errors_reports_and_clears():
    board = FakeBoard()
    board._err = _ERR_RX_OVERFLOW | _ERR_RX_POP_WHEN_EMPTY

    err = console.check_errors(board, clear=True)

    assert err == (_ERR_RX_OVERFLOW | _ERR_RX_POP_WHEN_EMPTY)
    assert board.err_clear_writes == [_ERR_RX_OVERFLOW | _ERR_RX_POP_WHEN_EMPTY]
    assert board._err == 0  # W1C cleared exactly the bits observed


def test_check_errors_no_clear_leaves_bits_set():
    board = FakeBoard()
    board._err = _ERR_TX_WRITE_WHEN_FULL

    err = console.check_errors(board, clear=False)

    assert err == _ERR_TX_WRITE_WHEN_FULL
    assert board.err_clear_writes == []
    assert board._err == _ERR_TX_WRITE_WHEN_FULL


def test_check_errors_zero_does_not_write_clear():
    board = FakeBoard()
    err = console.check_errors(board, clear=True)
    assert err == 0
    assert board.err_clear_writes == []


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
    assert bytes(board._tx_accepted) == b"L\r"


def test_pump_ignores_stdin_when_not_ready():
    board = FakeBoard()
    stdin = io.BytesIO(b"L\r")
    cont = console.console_pump(board, stdin, io.BytesIO(), stdin_ready=False)
    assert cont is True
    assert bytes(board._tx_accepted) == b""


def test_pump_exits_on_ctrl_bracket():
    board = FakeBoard()
    stdin = io.BytesIO(b"\x1d")
    cont = console.console_pump(board, stdin, io.BytesIO(), stdin_ready=True)
    assert cont is False
    assert bytes(board._tx_accepted) == b""


def test_pump_sends_bytes_preceding_exit_byte_in_same_chunk():
    board = FakeBoard()
    stdin = io.BytesIO(b"ab\x1dcd")
    cont = console.console_pump(board, stdin, io.BytesIO(), stdin_ready=True)
    assert cont is False
    assert bytes(board._tx_accepted) == b"ab"


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
    assert bytes(board._tx_accepted) == b"L\r"


def test_pump_reports_errors_on_stderr_without_polluting_stdout(capsys):
    board = FakeBoard()
    board._err = _ERR_TX_WRITE_WHEN_FULL
    stdout = io.BytesIO()

    cont = console.console_pump(board, io.BytesIO(b""), stdout, stdin_ready=False)

    assert cont is True
    assert stdout.getvalue() == b""  # the fault line must never enter the transcript
    err_out = capsys.readouterr().err
    assert "tx_write_when_full" in err_out
    assert board._err == 0  # check_errors() cleared it after reporting
    assert board.err_clear_writes == [_ERR_TX_WRITE_WHEN_FULL]


def test_pump_reports_dropped_bytes_when_tx_stays_full(monkeypatch, capsys):
    """This is the failure mode neither software nor hardware can catch on
    its own: a well-behaved client never writes while console_tx.full is
    set, so tx_write_when_full never fires, and send() already returns
    the true accepted count -- the bug is a caller (console_pump) that
    discards it. A test that only calls console.send() directly and
    checks its return value would have passed while this bug was live;
    it has to go through console_pump to catch a dropped return value."""
    board = FakeBoard(tx_capacity=1)
    board._tx_accepted.append(0xFF)  # full, and stays full -- nothing drains it
    monkeypatch.setattr(console.time, "sleep", lambda s: None)
    stdin = io.BytesIO(b"hello")

    cont = console.console_pump(board, stdin, io.BytesIO(), stdin_ready=True)

    assert cont is True
    assert bytes(board._tx_accepted) == b"\xff"  # none of "hello" landed
    err_out = capsys.readouterr().err
    assert "5 of 5" in err_out
    assert "dropped" in err_out


def test_pump_reports_dropped_bytes_in_ctrl_bracket_branch(monkeypatch, capsys):
    """Same short-write scenario, but through the exit_idx > 0 branch
    (bytes preceding a Ctrl-] in the same chunk) -- send()'s return must
    be checked against exit_idx there too, not just against len(data) in
    the no-exit-byte branch."""
    board = FakeBoard(tx_capacity=1)
    board._tx_accepted.append(0xFF)  # full, and stays full
    monkeypatch.setattr(console.time, "sleep", lambda s: None)
    stdin = io.BytesIO(b"ab\x1dcd")

    cont = console.console_pump(board, stdin, io.BytesIO(), stdin_ready=True)

    assert cont is False
    err_out = capsys.readouterr().err
    assert "2 of 2" in err_out
    assert "dropped" in err_out


def test_pump_silent_on_stderr_when_all_typed_bytes_accepted(capsys):
    board = FakeBoard()
    stdin = io.BytesIO(b"L\r")
    console.console_pump(board, stdin, io.BytesIO(), stdin_ready=True)
    assert capsys.readouterr().err == ""


def test_pump_silent_on_stderr_when_no_errors(capsys):
    board = FakeBoard(rx_bytes=b"x")
    console.console_pump(board, io.BytesIO(b""), io.BytesIO(), stdin_ready=False)
    assert capsys.readouterr().err == ""
