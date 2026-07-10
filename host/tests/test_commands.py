# Task 12 -- load/peek/poke/run/reset/stop/step against a FakeBoard.
#
# RAM window (binding facts, Task 4/9): word_addr = ram_base + 4*a8008 --
# word index IS the absolute 14-bit 8008 address, no offset subtraction.
# Valid load/peek/poke range is 0x1000-0x3FFF (the monitor's RAM region).
#
# ctl fields (b8008_integration.py): run_stop=bit0, step_cycle=bit1,
# step_sync=bit2, int_req=bit3, int_vector=bits4-6. status: is_running=bit0.
import pytest

from b8008net import commands


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
    """Stands in for b8008net.board.Board's RAM/ctl/status/console surface.

    `banner_on_run` scripts the monitor's boot behavior: those bytes land in
    the console RX FIFO when a run_stop pulse transitions the core from
    stopped to running (the ~400 ms banner-after-run, minus the 400 ms).
    `events` records the interleaving ("ctl"/"drain"/"tx") so tests can
    assert ordering (e.g. banner drained BEFORE the G command is sent)."""

    RAM_BASE = 0x90000000
    RXTX_ADDR = 0xF0000008

    def __init__(self, running=0, stuck=False, banner_on_run=b""):
        self.ram_base = self.RAM_BASE
        self._ram = {}
        self._corrupt = {}          # word_addr -> one-shot override on next read
        self._running = 1 if running else 0
        self._stuck = stuck         # run_stop pulses never change _running
        self._banner_on_run = banner_on_run
        self._rx_queue = bytearray()
        self._ctl_writes = []
        self._tx_sent = bytearray()
        self.write_calls = []
        self.read_calls = []
        self.events = []

        self.regs = type("Regs", (), {})()
        self.regs.b8008_status = _Reg(read=lambda: self._running)
        self.regs.b8008_ctl = _Reg(write=self._write_ctl)
        self.regs.b8008_rxtx = _Reg(addr=self.RXTX_ADDR, write=self._write_rxtx)
        self.regs.b8008_txfull = _Reg(read=lambda: 0)
        self.regs.b8008_rxlevel = _Reg(read=lambda: len(self._rx_queue))

    def _write_ctl(self, value):
        self._ctl_writes.append(value)
        self.events.append("ctl")
        if not self._stuck and (value & 0x1):
            self._running = 0 if self._running else 1
            if self._running:
                self._rx_queue.extend(self._banner_on_run)

    def _write_rxtx(self, value):
        self._tx_sent.append(value & 0xFF)
        self.events.append("tx")

    def write(self, addr, values):
        self.write_calls.append((addr, values))
        if isinstance(values, list):
            for i, v in enumerate(values):
                self._ram[addr + 4 * i] = v & 0xFF
        else:
            self._ram[addr] = values & 0xFF

    def read(self, addr, n=1, burst="incr"):
        self.read_calls.append((addr, n, burst))
        if addr == self.RXTX_ADDR and burst == "fixed":
            self.events.append("drain")
            chunk = list(self._rx_queue[:n])
            del self._rx_queue[:n]
            return chunk
        if n == 1:
            return self._corrupt.pop(addr, self._ram.get(addr, 0))
        return [self._corrupt.pop(addr + 4 * i, self._ram.get(addr + 4 * i, 0))
                for i in range(n)]


# ── load() ───────────────────────────────────────────────────────────────
def test_load_writes_segment_as_burst_at_ram_base_plus_4_times_addr():
    board = FakeBoard(running=0)
    commands.load(board, [(0x1000, bytes([0x11, 0x22, 0x33]))])
    assert board.write_calls == [(board.ram_base + 4 * 0x1000, [0x11, 0x22, 0x33])]


def test_load_reads_back_to_verify():
    board = FakeBoard(running=0)
    commands.load(board, [(0x1000, bytes([0x11, 0x22, 0x33]))])
    assert (board.ram_base + 4 * 0x1000, 3, "incr") in board.read_calls


def test_load_raises_verify_error_with_offset_expected_got_on_mismatch():
    board = FakeBoard(running=0)
    mismatch_word_addr = board.ram_base + 4 * 0x1002
    board._corrupt[mismatch_word_addr] = 0xFF  # one-shot: overrides the verify read

    with pytest.raises(commands.VerifyError) as exc_info:
        commands.load(board, [(0x1000, bytes([0x01, 0x02, 0x03, 0x04]))])

    err = exc_info.value
    assert err.offset == 0x1002
    assert err.expected == 0x03
    assert err.got == 0xFF


def test_load_refuses_when_running_without_force():
    board = FakeBoard(running=1)
    with pytest.raises(commands.NotStoppedError):
        commands.load(board, [(0x1000, bytes([0x01]))])
    assert board.write_calls == []


def test_load_proceeds_when_running_with_force():
    board = FakeBoard(running=1)
    commands.load(board, [(0x1000, bytes([0x01]))], force=True)
    assert board.write_calls == [(board.ram_base + 4 * 0x1000, [0x01])]


def test_load_rejects_address_below_window():
    board = FakeBoard(running=0)
    with pytest.raises(commands.AddressRangeError):
        commands.load(board, [(0x0FFF, bytes([0x01]))])


def test_load_rejects_segment_extending_above_window():
    board = FakeBoard(running=0)
    with pytest.raises(commands.AddressRangeError):
        commands.load(board, [(0x3FFF, bytes([0x01, 0x02]))])  # end = 0x4000


def test_load_multiple_segments_all_written():
    board = FakeBoard(running=0)
    commands.load(board, [(0x1000, bytes([0x01])), (0x2000, bytes([0x02, 0x03]))])
    assert board.write_calls == [
        (board.ram_base + 4 * 0x1000, [0x01]),
        (board.ram_base + 4 * 0x2000, [0x02, 0x03]),
    ]


# ── peek() ───────────────────────────────────────────────────────────────
def test_peek_returns_bytes_read_from_ram_window():
    board = FakeBoard(running=0)
    board.write(board.ram_base + 4 * 0x1000, [0x41, 0x42, 0x43, 0x44])
    data = commands.peek(board, 0x1000, 4)
    assert data == bytes([0x41, 0x42, 0x43, 0x44])
    assert (board.ram_base + 4 * 0x1000, 4, "incr") in board.read_calls


def test_peek_single_byte_length():
    board = FakeBoard(running=0)
    board.write(board.ram_base + 4 * 0x1000, [0x7F])
    assert commands.peek(board, 0x1000, 1) == bytes([0x7F])


def test_peek_rejects_out_of_window_address():
    board = FakeBoard(running=0)
    with pytest.raises(commands.AddressRangeError):
        commands.peek(board, 0x0FFF, 1)
    with pytest.raises(commands.AddressRangeError):
        commands.peek(board, 0x3FFF, 2)


def test_format_hexdump_layout():
    data = bytes(range(16)) + bytes([0x41, 0x42])  # second row: 'AB' printable
    text = commands.format_hexdump(0x1000, data)
    lines = text.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("1000")
    assert "00 01 02 03" in lines[0]
    assert lines[1].startswith("1010")
    assert "|AB|" in lines[1]


# ── poke() ───────────────────────────────────────────────────────────────
def test_poke_writes_single_words_and_verifies():
    board = FakeBoard(running=0)
    commands.poke(board, 0x2000, [0xAA, 0xBB])
    assert board.write_calls == [
        (board.ram_base + 4 * 0x2000, 0xAA),
        (board.ram_base + 4 * 0x2001, 0xBB),
    ]


def test_poke_raises_verify_error_on_mismatch():
    board = FakeBoard(running=0)
    board._corrupt[board.ram_base + 4 * 0x2000] = 0xEE
    with pytest.raises(commands.VerifyError) as exc_info:
        commands.poke(board, 0x2000, [0xAA])
    assert exc_info.value.offset == 0x2000
    assert exc_info.value.expected == 0xAA
    assert exc_info.value.got == 0xEE


def test_poke_rejects_out_of_window_address():
    board = FakeBoard(running=0)
    with pytest.raises(commands.AddressRangeError):
        commands.poke(board, 0x0FFF, [0x01])


# ── run() ────────────────────────────────────────────────────────────────
def test_run_sends_go_command_via_console():
    board = FakeBoard(running=1)
    commands.run(board, 0x2000)
    assert bytes(board._tx_sent) == b"G 2000\r"


def test_run_zero_pads_short_addresses_to_four_hex_digits():
    board = FakeBoard(running=1)
    commands.run(board, 0x100)
    assert bytes(board._tx_sent) == b"G 0100\r"


def test_run_on_running_core_sends_without_any_ctl_pulse():
    board = FakeBoard(running=1)
    commands.run(board, 0x2000)
    assert board._ctl_writes == []
    assert bytes(board._tx_sent) == b"G 2000\r"


def test_run_on_stopped_core_restarts_awaits_banner_then_sends_g():
    # A stopped core's USART never consumes console bytes -- run() must
    # first restart the core (which re-bootstraps the monitor), then wait
    # for the banner/prompt to arrive before sending G.
    board = FakeBoard(running=0, banner_on_run=b"\r\n8008 Monitor\r\n>")
    commands.run(board, 0x2000)

    assert board._ctl_writes == [1]           # one run_stop pulse to start
    assert board._running == 1
    assert bytes(board._tx_sent) == b"G 2000\r"
    # Ordering: restart pulse, then banner drained, then G bytes sent.
    assert "drain" in board.events
    assert board.events.index("ctl") < board.events.index("drain")
    assert board.events.index("drain") < board.events.index("tx")
    # The banner was actually consumed (not left to garble later output).
    assert board._rx_queue == bytearray()


def test_run_on_stopped_core_drains_stale_prompt_before_restart():
    # The RX FIFO is 4096 deep and persists across a stop: a '>' left over
    # from before the stop must not satisfy _await_monitor_prompt on the
    # very first poll (that would send G into the monitor's ~400 ms reboot
    # window, where the just-reset USART isn't listening -- bytes silently
    # lost, CLI reports success anyway). run() must drain the FIFO to empty
    # BEFORE pulsing run_stop, and only then await the FRESH banner/prompt
    # that shows up after the actual restart.
    board = FakeBoard(running=0, banner_on_run=b"\r\n8008 Monitor\r\n>")
    board._rx_queue.extend(b">")  # stale prompt, queued before the restart

    commands.run(board, 0x2000)

    assert board._ctl_writes == [1]
    assert bytes(board._tx_sent) == b"G 2000\r"
    # Ordering: stale-drain happens before the ctl pulse; the fresh-banner
    # drain happens after it and before G is sent.
    ctl_idx = board.events.index("ctl")
    assert board.events.index("drain") < ctl_idx
    assert "drain" in board.events[ctl_idx:]
    assert board.events.index("tx") > ctl_idx
    # Nothing left over in the FIFO -- both the stale byte and the banner
    # were fully consumed, not left to garble whatever comes next.
    assert board._rx_queue == bytearray()


def test_run_on_stopped_core_no_banner_warns_and_sends_anyway(capsys):
    # Banner never arrives (fake scripts none): after the timeout run()
    # proceeds with a warning rather than failing -- the hardware stage
    # calibrates the real boot delay.
    board = FakeBoard(running=0)  # no banner_on_run
    commands.run(board, 0x2000, banner_timeout=0.05)

    assert board._ctl_writes == [1]
    assert bytes(board._tx_sent) == b"G 2000\r"
    err = capsys.readouterr().err
    assert "warning" in err.lower()


# ── stop() / set_run_state() ────────────────────────────────────────────
def test_stop_pulses_run_stop_once_then_rereads_status():
    board = FakeBoard(running=1)
    commands.stop(board)
    assert board._ctl_writes == [1]
    assert board._running == 0


def test_stop_is_noop_when_already_stopped():
    board = FakeBoard(running=0)
    commands.stop(board)
    assert board._ctl_writes == []


def test_stop_raises_after_three_attempts_when_stuck():
    board = FakeBoard(running=1, stuck=True)
    with pytest.raises(commands.RunStateError):
        commands.stop(board)
    assert board._ctl_writes == [1, 1, 1]


# ── reset() ──────────────────────────────────────────────────────────────
def test_reset_does_stop_then_run_cycle():
    board = FakeBoard(running=1)
    commands.reset(board)
    assert board._ctl_writes == [1, 1]
    assert board._running == 1


def test_reset_from_already_stopped_still_ends_running():
    board = FakeBoard(running=0)
    commands.reset(board)
    assert board._ctl_writes == [1]
    assert board._running == 1


# ── step() ───────────────────────────────────────────────────────────────
def test_step_cycle_pulses_bit_1():
    board = FakeBoard(running=0)
    commands.step(board)
    assert board._ctl_writes == [1 << 1]


def test_step_sync_pulses_bit_2():
    board = FakeBoard(running=0)
    commands.step(board, sync=True)
    assert board._ctl_writes == [1 << 2]


def test_step_warns_but_still_pulses_when_running(capsys):
    board = FakeBoard(running=1)
    commands.step(board)
    assert board._ctl_writes == [1 << 1]
    err = capsys.readouterr().err
    assert "running" in err.lower()


# ── measure_rtt() ────────────────────────────────────────────────────────
def test_measure_rtt_returns_average_seconds_as_float():
    board = FakeBoard(running=1)
    rtt = commands.measure_rtt(board, reads=5)
    assert isinstance(rtt, float)
    assert rtt >= 0.0


def test_measure_rtt_reads_status_reads_times():
    board = FakeBoard(running=1)
    read_count = {"n": 0}
    real_read = board.regs.b8008_status.read

    def counting_read():
        read_count["n"] += 1
        return real_read()

    board.regs.b8008_status.read = counting_read
    commands.measure_rtt(board, reads=7)
    assert read_count["n"] == 7
