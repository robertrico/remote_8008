# Task 10 -- FakeBoard unit tests for the pure + board-interaction helpers in
# soc/host_selftest.py (written in Task 9, updated in Task 2 of
# make-login-console-client for the new console CSR bank and the retirement
# of the wishbone RAM window -- SPEC.md S-PROD-8/D-10).
#
# host_selftest.py lives in soc/ (repo root's soc/ dir), outside the
# b8008net package, and its top-level imports are stdlib-only (argparse,
# sys, time) -- `from litex...` is deferred inside main(). We load it by
# file path with importlib so this test file never touches sys.path (the
# repo root also holds the litex/migen *clones*; adding it to sys.path
# would shadow the editable-installed litex package used by test_discovery.py
# -- see versa_soc.py's sys.path guard comment for the same collision).
import importlib.util
import os

import pytest

_HOST_SELFTEST_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "soc", "host_selftest.py")
)
_spec = importlib.util.spec_from_file_location("host_selftest", _HOST_SELFTEST_PATH)
host_selftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host_selftest)


# ── Fake RemoteClient -------------------------------------------------------
# console_rx bit layout (SPEC.md S11.1): data[7:0], valid[8], level[21:9] --
# same layout host/tests/test_console.py's FakeBoard models for
# b8008net.console itself; drain_rx()/send_command() now delegate straight
# to that module, so this fake only needs to speak its register contract.
_RX_VALID_BIT = 1 << 8
_RX_LEVEL_SHIFT = 9


class _Reg:
    """Stands in for a litex CSRRegister: optional .read()/.write()."""

    def __init__(self, read=None, write=None):
        self._read = read
        self._write = write

    def read(self):
        return self._read()

    def write(self, value):
        self._write(value)


class _Bases:
    pass


class FakeBoard:
    """Stands in for litex's RemoteClient: .bases.identifier_mem + .read()/
    .write() for get_identifier(), and the console_rx/console_rx_pop/
    console_tx/console_tx_data registers b8008net.console.drain()/send()
    read/write for drain_rx()/send_command()."""

    def __init__(self, identifier_mem_base=None, rx_bytes=b""):
        self.bases = _Bases()
        if identifier_mem_base is not None:
            self.bases.identifier_mem = identifier_mem_base
        self._mem = {}
        self._rx_queue = bytearray(rx_bytes)
        self._tx_sent = bytearray()
        self.pop_calls = 0

        self.regs = type("Regs", (), {})()
        self.regs.b8008_console_console_rx = _Reg(read=self._read_rx)
        self.regs.b8008_console_console_rx_pop = _Reg(write=self._write_rx_pop)
        self.regs.b8008_console_console_tx = _Reg(read=lambda: 0)  # never full
        self.regs.b8008_console_console_tx_data = _Reg(write=self._write_tx_data)

    def read(self, addr):
        return self._mem.get(addr, 0)

    def write(self, addr, value):
        self._mem[addr] = value & 0xFF

    def feed_rx(self, data):
        self._rx_queue.extend(data)

    def _read_rx(self):
        level = len(self._rx_queue)
        valid = 1 if level else 0
        data = self._rx_queue[0] if level else 0
        return (data & 0xFF) | (valid << 8) | ((level & 0x1FFF) << _RX_LEVEL_SHIFT)

    def _write_rx_pop(self, _value):
        self.pop_calls += 1
        if self._rx_queue:
            del self._rx_queue[0]

    def _write_tx_data(self, value):
        self._tx_sent.append(value & 0xFF)


def _write_identifier(board, base, text):
    for i, ch in enumerate(text.encode("ascii") + b"\x00"):
        board.write(base + 4 * i, ch)


# ── pure helpers --------------------------------------------------------------
def test_compare_bytes_equal():
    ok, idx = host_selftest.compare_bytes(b"abc", b"abc")
    assert ok is True
    assert idx == -1


def test_compare_bytes_mismatch_reports_first_index():
    ok, idx = host_selftest.compare_bytes(b"abcd", b"abXd")
    assert ok is False
    assert idx == 2


def test_compare_bytes_length_mismatch():
    ok, idx = host_selftest.compare_bytes(b"abc", b"ab")
    assert ok is False
    assert idx == 2


def test_batches_yields_fixed_size_chunks():
    chunks = list(host_selftest.batches(list(range(10)), 4))
    assert chunks == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]


def test_batches_exact_multiple():
    chunks = list(host_selftest.batches(b"abcdef", 3))
    assert chunks == [b"abc", b"def"]


def test_batches_rejects_nonpositive_size():
    with pytest.raises(ValueError):
        list(host_selftest.batches([1, 2, 3], 0))


def test_find_prefix_true():
    assert host_selftest.find_prefix(b"8008 Monitor\r\n", b"8008 ") is True


def test_find_prefix_false():
    assert host_selftest.find_prefix(b"nope", b"8008 ") is False


def test_find_prefix_buf_shorter_than_prefix():
    assert host_selftest.find_prefix(b"80", b"8008 ") is False


def test_decode_printable_escapes_control_and_nonprintable():
    out = host_selftest.decode_printable(b"Hi\r\n\x01\xff!")
    assert out == "Hi\\r\\n\\x01\\xff!"


# ── board-interaction helpers --------------------------------------------------
def test_get_identifier_reads_until_nul():
    board = FakeBoard(identifier_mem_base=0x1000)
    _write_identifier(board, 0x1000, "b8008_net 2026-07-09")
    assert host_selftest.get_identifier(board) == "b8008_net 2026-07-09"


def test_get_identifier_no_identifier_mem():
    board = FakeBoard()
    assert host_selftest.get_identifier(board) == "<no identifier_mem>"


def test_get_identifier_delegates_to_shared_helper(monkeypatch):
    """MINOR 2 (review): get_identifier and Board.identifier must share one
    implementation -- b8008net.board.read_identifier. Prove get_identifier
    actually routes through it by swapping it out."""
    import b8008net.board as board_mod

    seen = []

    def fake_read_identifier(client):
        seen.append(client)
        return "swapped"

    monkeypatch.setattr(board_mod, "read_identifier", fake_read_identifier)
    board = FakeBoard(identifier_mem_base=0x1000)
    assert host_selftest.get_identifier(board) == "swapped"
    assert seen == [board]


def test_drain_rx_delegates_to_console_module(monkeypatch):
    """drain_rx() must not re-derive the read/pop protocol -- prove it
    actually routes through b8008net.console.drain()."""
    import b8008net.console as console_mod

    seen = []

    def fake_drain(client):
        seen.append(client)
        return b"swapped"

    monkeypatch.setattr(console_mod, "drain", fake_drain)
    board = FakeBoard()
    assert host_selftest.drain_rx(board) == b"swapped"
    assert seen == [board]


def test_drain_rx_stops_at_empty():
    board = FakeBoard(rx_bytes=b"hello")
    assert host_selftest.drain_rx(board) == b"hello"
    assert board.pop_calls == 5
    assert host_selftest.drain_rx(board) == b""
    assert board.pop_calls == 5  # no pop against an empty FIFO


def test_send_command_writes_each_byte():
    board = FakeBoard()
    host_selftest.send_command(board, b"H\r")
    assert bytes(board._tx_sent) == b"H\r"


def test_poll_banner_finds_prefix():
    board = FakeBoard(rx_bytes=b"8008 Monitor\r\n")
    banner = host_selftest.poll_banner(board, timeout_s=0.2, poll_s=0.01)
    assert host_selftest.find_prefix(banner, host_selftest.BANNER_PREFIX)
    assert banner == b"8008 Monitor\r\n"


def test_poll_banner_times_out_without_prefix():
    board = FakeBoard(rx_bytes=b"garbage")
    banner = host_selftest.poll_banner(board, timeout_s=0.05, poll_s=0.01)
    assert banner == b"garbage"
    assert not host_selftest.find_prefix(banner, host_selftest.BANNER_PREFIX)


def test_poll_marker_finds_marker_anywhere():
    board = FakeBoard(rx_bytes=b"...Help menu...")
    resp = host_selftest.poll_marker(board, host_selftest.HELP_MARKER,
                                      timeout_s=0.2, poll_s=0.01)
    assert host_selftest.HELP_MARKER in resp


def test_poll_marker_times_out_without_marker():
    board = FakeBoard(rx_bytes=b"nothing here")
    resp = host_selftest.poll_marker(board, host_selftest.HELP_MARKER,
                                      timeout_s=0.05, poll_s=0.01)
    assert host_selftest.HELP_MARKER not in resp


def test_ram_window_check_is_retired():
    """SPEC.md S-PROD-8/D-10: the host-facing wishbone RAM window is gone
    from the gateware, and host_selftest.py no longer has a check for it."""
    assert not hasattr(host_selftest, "ram_window_base")
    assert not hasattr(host_selftest, "ram_write_readback")
