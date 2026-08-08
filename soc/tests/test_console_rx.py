"""Console RX path behavior. DUT is ConsoleBridge, pure cd_sys."""
from migen import run_simulation

from console_bridge import ConsoleBridge


# clk/baud = 10 -> 10 sys cycles per bit, 100 per 10-bit frame. Keeps sims fast.
FAST = dict(sys_clk_freq=1_000_000, baudrate=100_000)
BIT = 10
FRAME = 10 * BIT

# console_rx/console_rx_pop tests below construct ConsoleBridge(sys_clk_freq=75e6)
# (the CSR-focused default, not FAST) so the bit period here matches that config.
RX_BIT = round(75e6 / 115200)
RX_FRAME = 10 * RX_BIT


def serial_send(dut, byte, bit=BIT):
    """Drive one 8N1 frame onto dut.pads.rx at the given bit period."""
    def _bit(v):
        yield dut.pads.rx.eq(v)
        for _ in range(bit):
            yield

    yield from _bit(0)                       # start
    for i in range(8):
        yield from _bit((byte >> i) & 1)     # LSB first
    yield from _bit(1)                       # stop


def test_bridge_receives_a_byte():
    """A byte clocked into pads.rx lands in rx_fifo.

    VPLAN: RX-1
    """
    dut = ConsoleBridge(**FAST)
    seen = []

    def gen():
        yield dut.pads.rx.eq(1)
        for _ in range(BIT):
            yield
        yield from serial_send(dut, 0xA5)
        for _ in range(FRAME):
            yield
        seen.append((yield dut.rx_fifo.level))
        seen.append((yield dut.rx_fifo.source.data))

    run_simulation(dut, gen())
    assert seen == [1, 0xA5]


def test_rx_fifo_depth_is_4096():
    """rx_fifo depth is exactly 4096.

    VPLAN: RX-1
    """
    assert ConsoleBridge(sys_clk_freq=75e6).rx_fifo.fifo.depth == 4096


def _preload(dut, payload):
    """Push bytes into rx_fifo over the real serial line.

    The task-3 brief specified pushing straight into rx_fifo.sink to bypass
    the PHY for speed. That doesn't work against this ConsoleBridge:
    `self.comb += [self.phy.source.connect(self.rx_fifo.sink), ...]` already
    makes rx_fifo.sink.valid/.data comb targets, and Migen's simulator
    re-executes every comb statement to a fixed point on every settle --
    verified directly against soc/console_bridge.py's fragment
    (`rx_fifo.sink.valid in list_targets(dut.get_fragment().comb)` is True).
    A generator write to a signal with an existing comb driver is silently
    overridden the same cycle; `phy.source` itself is equally unwritable
    (RS232PHYRX drives `source.valid` from FSM-gated comb logic). So this
    drives dut.pads.rx with real 8N1 framing instead, timed to this file's
    default DUT config (sys_clk_freq=75e6, baudrate=115200) via RX_BIT.
    """
    yield dut.pads.rx.eq(1)
    for _ in range(RX_BIT):
        yield
    for b in payload:
        yield from serial_send(dut, b, bit=RX_BIT)
    for _ in range(RX_FRAME):
        yield


def _new_dut():
    """ConsoleBridge(sys_clk_freq=75e6), with console_rx wired for simulation.

    LiteXModule.__setattr__ deliberately excludes _CSRBase instances from its
    auto-submodule magic (litex/gen/fhdl/module.py): in a real SoC build,
    csr_bus.CSRBankArray finalizes and registers each CSR while building the
    CSR bus. A bare `run_simulation(ConsoleBridge(...))` never builds that
    bus, so CSRStatus.status's own internal comb -- which composes .status
    from .fields.* -- never runs, and `.status` reads stuck at its reset
    value even though .fields.* (set directly by ConsoleBridge's own comb)
    are correct. This is not novel: LiteX's own test suite hits the identical
    gap and self-skips rather than fixing it (litex/test/test_csr.py,
    TestCSR.test_csr_fields, the `.status`/`.read()` assertions wrapped in
    "skip known failure"). Finalizing+registering console_rx here (test-side
    only) fixes it for these tests without baking a CSR-bus busword
    assumption into console_bridge.py's production `__init__` -- that
    belongs to the real SoC integration, not to this module.
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    dut.console_rx.finalize(32, "big")
    dut.submodules += dut.console_rx
    return dut


def _read_rx(dut):
    """Read console_rx and unpack it into (data, valid, level)."""
    w = yield dut.console_rx.status
    return w & 0xFF, (w >> 8) & 1, (w >> 9) & 0x1FFF


def _pop(dut):
    yield dut.console_rx_pop.re.eq(1)
    yield
    yield dut.console_rx_pop.re.eq(0)
    yield
    yield


def test_read_is_non_destructive():
    """Two reads with no intervening pop return identical values.

    VPLAN: RX-2, RX-3
    """
    dut = _new_dut()
    got = []

    def gen():
        yield from _preload(dut, [0x41, 0x42])
        for _ in range(1000):
            got.append((yield from _read_rx(dut)))

    run_simulation(dut, gen())
    assert len(set(got)) == 1, f"read mutated state: {set(got)}"
    assert got[0] == (0x41, 1, 2)


def test_pop_consumes_exactly_one():
    """One pop decreases level by exactly 1 and advances to the next byte.

    VPLAN: RX-4, RX-5, CSR-5
    """
    dut = _new_dut()
    seq = []

    def gen():
        yield from _preload(dut, [0x10, 0x20, 0x30])
        for _ in range(3):
            data, valid, level = yield from _read_rx(dut)
            seq.append((data, valid, level))
            yield from _pop(dut)
        seq.append((yield from _read_rx(dut)))

    run_simulation(dut, gen())
    assert seq == [(0x10, 1, 3), (0x20, 1, 2), (0x30, 1, 1), (0x00, 0, 0)]


def test_empty_reads_zero():
    """When level is 0, data reads 0x00 and valid reads 0.

    VPLAN: RX-6, RX-7
    """
    dut = _new_dut()
    out = []

    def gen():
        yield
        out.append((yield from _read_rx(dut)))

    run_simulation(dut, gen())
    assert out == [(0x00, 0, 0)]


def test_valid_tracks_level():
    """valid == (level != 0) across a full fill and drain.

    VPLAN: RX-8
    """
    dut = _new_dut()
    bad = []

    def gen():
        yield from _preload(dut, list(range(8)))
        for _ in range(9):
            data, valid, level = yield from _read_rx(dut)
            if valid != (1 if level else 0):
                bad.append((data, valid, level))
            yield from _pop(dut)

    run_simulation(dut, gen())
    assert not bad, f"valid/level disagreed: {bad}"


def test_retried_read_then_pop_advances_one():
    """k reads followed by one pop advance the stream by exactly one byte.

    VPLAN: RX-14
    """
    dut = _new_dut()
    seen = []

    def gen():
        yield from _preload(dut, [0xAA, 0xBB])
        for k in (1, 5, 10):
            for _ in range(k):
                seen.append((yield from _read_rx(dut))[0])
            yield from _pop(dut)
            if k == 5:
                break

    run_simulation(dut, gen())
    assert seen == [0xAA] * 1 + [0xBB] * 5


def test_pop_when_empty_pulses_error():
    """A pop at level 0 consumes nothing and pulses the error signal.

    VPLAN: RX-11, RX-12
    """
    dut = _new_dut()
    fired = []

    def monitor():
        for _ in range(40):
            if (yield dut.err_rx_pop_when_empty):
                fired.append(1)
            yield

    def gen():
        yield
        yield from _pop(dut)                    # illegal: empty
        yield from _preload(dut, [0x7E])
        data, valid, level = yield from _read_rx(dut)
        assert (data, valid, level) == (0x7E, 1, 1), (data, valid, level)

    run_simulation(dut, [gen(), monitor()])
    assert fired, "err_rx_pop_when_empty never asserted"


def test_read_during_live_push_stays_consistent():
    """console_rx sampled every single cycle during live serial arrivals
    never disagrees on valid vs level, and every popped byte matches what
    the read immediately before it reported.

    Regression for a transient in SyncFIFOBuffered (migen/genlib/fifo.py):
    on the empty->non-empty edge, `fifo.re` fires combinationally the same
    cycle the inner FIFO becomes readable, but the outer `readable` register
    (== rx_fifo.source.valid) only catches up on the *next* clock edge. For
    that one cycle, rx_fifo.level reads 1 while rx_fifo.source.valid reads
    0 -- console_rx.level != 0 while console_rx.valid == 0, which violates
    S-RX-8. A fill-then-drain access pattern (as in test_valid_tracks_level)
    never lands on that cycle, which is exactly why it didn't catch this.
    This test samples every cycle while a byte is still arriving, and pops
    opportunistically mid-stream, so it must cross that transient directly.

    Uses FAST (clk/baud = 10), not the 75 MHz/115200 default: the realistic
    ratio is ~65x slower in sim for no added fidelity to this property.

    VPLAN: RX-8, RX-9, RX-10
    """
    dut = ConsoleBridge(**FAST)
    dut.console_rx.finalize(32, "big")
    dut.submodules += dut.console_rx
    bad = []
    popped = []

    def driver():
        yield dut.pads.rx.eq(1)
        for _ in range(BIT):
            yield
        for b in (0x11, 0x22, 0x33, 0x44):
            yield from serial_send(dut, b)
            for _ in range(2 * BIT):     # idle gap between bytes
                yield

    def monitor():
        last_data = None
        for cyc in range(700):
            data, valid, level = yield from _read_rx(dut)
            if valid != (1 if level else 0):
                bad.append((cyc, data, valid, level))
            if valid:
                last_data = data
            # Pop opportunistically while data is available, landing pops
            # both mid-arrival and in the gaps between bytes.
            pop_now = bool(valid) and (cyc % 7 == 3)
            yield dut.console_rx_pop.re.eq(1 if pop_now else 0)
            if pop_now:
                popped.append(last_data)
            yield                         # exactly one sys cycle/iteration

    run_simulation(dut, [driver(), monitor()])
    assert not bad, f"valid/level disagreed while a byte was arriving: {bad}"
    assert popped == [0x11, 0x22, 0x33, 0x44], popped
