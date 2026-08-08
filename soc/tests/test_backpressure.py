"""Backpressure thresholds and hysteresis. DUT is ConsoleBridge."""
from migen import run_simulation

from console_bridge import ConsoleBridge, RX_DEPTH, RX_HEADROOM, RX_HYSTERESIS

# A small, fast bridge. rx_fifo.sink is comb-driven from phy.source, so a
# testbench CANNOT poke it -- Migen silently overrides writes to comb-driven
# signals (found the hard way in Task 3). Bytes must arrive as real 8N1 frames
# on pads.rx, which makes frame cost the limiting factor: clk/baud = 10 gives
# 100 cycles per byte. Filling the default 4096-deep FIFO at the real 115200/
# 75 MHz ratio would need ~26 million cycles, so the test scales the depth down
# and lets the thresholds scale with it.
SMALL = dict(sys_clk_freq=1_000_000, baudrate=100_000, rx_depth=256)
BIT = 10
S_HWM = 256 - RX_HEADROOM      # 192
S_LWM = S_HWM - RX_HYSTERESIS  # 128


def _send(dut, byte):
    """Drive one 8N1 frame onto dut.pads.rx."""
    def bit(v):
        yield dut.pads.rx.eq(v)
        for _ in range(BIT):
            yield
    yield from bit(0)
    for i in range(8):
        yield from bit((byte >> i) & 1)
    yield from bit(1)


def _fill(dut, n):
    for i in range(n):
        yield from _send(dut, i & 0xFF)


def _drain(dut, n):
    for _ in range(n):
        yield dut.console_rx_pop.re.eq(1)
        yield
        yield dut.console_rx_pop.re.eq(0)
        yield


def test_default_thresholds_match_the_spec():
    """At the shipped 4096 depth the thresholds are exactly 4032 and 3968.

    VPLAN: BP-5
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    assert (dut.hwm, dut.lwm) == (4032, 3968)
    assert RX_DEPTH == 4096


def test_thresholds_leave_headroom():
    """Headroom above HWM exceeds the max bytes that can still be in flight.

    VPLAN: BP-6
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    assert RX_DEPTH - dut.hwm >= 4, "headroom must exceed max bytes in flight"
    assert dut.lwm < dut.hwm, "hysteresis band must be non-empty"


def test_stall_asserts_at_hwm():
    """Stall is low below HWM and high once level reaches HWM.

    VPLAN: BP-1
    """
    dut = ConsoleBridge(**SMALL)
    out = []

    def gen():
        yield dut.pads.rx.eq(1)
        for _ in range(BIT):
            yield
        yield from _fill(dut, S_HWM - 1)
        out.append((yield dut.stall))
        yield from _fill(dut, 1)
        for _ in range(4):
            yield
        out.append((yield dut.stall))

    run_simulation(dut, gen())
    assert out == [0, 1]


def test_stall_holds_through_the_band():
    """Stall stays asserted while LWM < level < HWM on the way down.

    VPLAN: BP-2, BP-3
    """
    dut = ConsoleBridge(**SMALL)
    out = []

    def gen():
        yield dut.pads.rx.eq(1)
        for _ in range(BIT):
            yield
        yield from _fill(dut, S_HWM)
        for _ in range(4):
            yield
        out.append((yield dut.stall))                  # asserted
        yield from _drain(dut, (S_HWM - S_LWM) - 1)    # still inside the band
        out.append((yield dut.stall))                  # must still be asserted
        yield from _drain(dut, 1)                      # now at LWM
        for _ in range(4):
            yield
        out.append((yield dut.stall))                  # released

    run_simulation(dut, gen())
    assert out == [1, 1, 0]


def test_no_overflow_under_sustained_pressure():
    """No byte is ever presented to a full rx_fifo; the canary stays clear.

    VPLAN: BP-6
    """
    dut = ConsoleBridge(**SMALL)
    violations, err = [], []

    def monitor():
        for _ in range(S_HWM * 100 + 5000):
            if (yield dut.rx_fifo.sink.valid) and not (yield dut.rx_fifo.sink.ready):
                violations.append(1)
            if (yield dut.err_rx_overflow):
                err.append(1)
            yield

    def gen():
        yield dut.pads.rx.eq(1)
        for _ in range(BIT):
            yield
        yield from _fill(dut, S_HWM + 8)   # keep sending past the threshold
        for _ in range(200):
            yield

    run_simulation(dut, [gen(), monitor()])
    assert not violations, "a byte was presented to a full rx_fifo"
    assert not err, "the overflow canary fired"


def test_exported_rx_level_never_exceeds_depth():
    """console_rx.level saturates at rx_depth even though the FIFO holds depth+1.

    VPLAN: RX-7
    """
    dut = ConsoleBridge(**SMALL)
    # CSRStatus.status's internal comb (composing .status from .fields.*) is
    # only wired up when a CSR bus finalizes and registers the CSR -- a real
    # SoC build does this, but a bare run_simulation() never does. Without
    # this, .status reads stuck at its reset value (0) and the test below
    # would pass vacuously, "silently reading zeros" against a signal that
    # never moves. See test_console_rx.py's _new_dut() for the same fix.
    dut.console_rx.finalize(32, "big")
    dut.submodules += dut.console_rx
    seen = []

    def monitor():
        for _ in range((256 + 16) * 100 + 2000):
            w = yield dut.console_rx.status
            seen.append((w >> 9) & 0x1FFF)
            yield

    def gen():
        yield dut.pads.rx.eq(1)
        for _ in range(BIT):
            yield
        yield from _fill(dut, 256 + 8)   # push past nominal capacity
        for _ in range(200):
            yield

    run_simulation(dut, [gen(), monitor()])
    assert max(seen) <= 256, f"exported level reached {max(seen)}, exceeds depth"
