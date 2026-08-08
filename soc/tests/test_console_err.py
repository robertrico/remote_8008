"""Sticky error bits and their clear semantics."""
from migen import ClockDomain, run_simulation

from console_bridge import ConsoleBridge

BIT_OVERFLOW = 0
BIT_TX_FULL = 1
BIT_POP_EMPTY = 2


def _new_dut():
    """ConsoleBridge(sys_clk_freq=75e6), with console_err wired for simulation.

    Same gap as test_console_rx.py's/test_console_tx.py's _new_dut():
    LiteXModule.__setattr__ deliberately excludes _CSRBase instances from its
    auto-submodule magic (litex/gen/fhdl/module.py), so a bare
    ConsoleBridge(...) never runs CSRStatus.status's internal comb -- the
    logic that composes .status from .fields.* -- and `.status` reads stuck
    at its reset value (0) even once the sticky bits underneath are set.
    Confirmed directly: without this finalize, every test below that expects
    a nonzero read gets 0 instead and fails outright (not a silent false
    pass -- the sticky bit going 0->1 changes `.status` from its reset value,
    so a stuck read is loudly wrong here, unlike a hazard that would hide
    behind an already-correct value). Finalizing+registering console_err
    here (test-side only) fixes it, exactly as _new_dut() does for
    console_rx/console_tx elsewhere in this suite.
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    dut.console_err.finalize(32, "big")
    dut.submodules += dut.console_err
    return dut


def _read_err(dut):
    return (yield dut.console_err.status) & 0x7


def _clear(dut, mask):
    yield dut.console_err_clear.r.eq(mask)
    yield dut.console_err_clear.re.eq(1)
    yield
    yield dut.console_err_clear.re.eq(0)
    yield
    yield


def _pulse_pop_empty(dut):
    yield dut.console_rx_pop.re.eq(1)
    yield
    yield dut.console_rx_pop.re.eq(0)
    yield
    yield


# Task 6 makes err_rx_overflow comb-driven from a genuine
# rx_fifo.sink.valid & ~rx_fifo.sink.ready condition (SPEC.md S-BP-9). Before
# that it was Task 5's undriven placeholder Signal(), so the tests below used
# to poke it directly with `yield dut.err_rx_overflow.eq(1)`. That is now
# silently overridden every cycle by the comb driver -- the same
# testbench-write-to-comb-signal hazard Task 3 found on rx_fifo.sink -- so a
# real overflow has to be provoked instead. Doing that at the production
# 4096-depth config would take ~26M cycles (test_backpressure.py's SMALL
# bridge exists for the same reason), so `rx_depth` is shrunk drastically
# here purely to trip the fault quickly -- the sticky-bit behavior under
# test does not depend on the RX FIFO's depth.
#
# The baud rate is kept at the REAL sys_clk_freq=75e6/115200 ratio, though,
# not sped up: sped up once (1MHz/100kHz, matching test_backpressure.py's
# SMALL) it silently broke test_sticky_bits_survive_reset's tx_fifo control
# check -- at that ratio the PHY's TX side drains ~5 bytes for real out of
# tx_fifo during the very write loop that is trying to fill it to exactly
# 256, because a UART frame (100 cycles at clk/baud=10) becomes cheap enough
# to complete within the loop's own 512-cycle span. At the real 651-cycle
# bit period a frame (6510 cycles) so outlasts that loop that no draining
# happens, exactly as in the original test. `rx_depth` shrinks independently
# of baud rate, so a handful of real frames (fifo.depth + 3) still overflows
# a tiny rx_fifo quickly even at the real baud rate.
#
# rx_depth=4 also makes this DUT's hwm/lwm negative (RX_HEADROOM/
# RX_HYSTERESIS default to 64 each) -- harmless and unused here: this file
# never reads .stall, only the overflow canary and the sticky bits it feeds.
OVERFLOW = dict(sys_clk_freq=75e6, baudrate=115200, rx_depth=4)
OVF_BIT = round(75e6 / 115200)


def _send_overflow_frame(dut, byte):
    def bit(v):
        yield dut.pads.rx.eq(v)
        for _ in range(OVF_BIT):
            yield
    yield from bit(0)
    for i in range(8):
        yield from bit((byte >> i) & 1)
    yield from bit(1)


def _overflow(dut):
    """Send enough real 8N1 frames to present a byte to a full rx_fifo,
    tripping err_rx_overflow for real (not by poking it)."""
    n = dut.rx_fifo.fifo.depth + 3
    for i in range(n):
        yield from _send_overflow_frame(dut, i & 0xFF)


def _new_overflow_dut():
    """A tiny ConsoleBridge with console_err wired for simulation."""
    dut = ConsoleBridge(**OVERFLOW)
    dut.console_err.finalize(32, "big")
    dut.submodules += dut.console_err
    return dut


def test_err_is_zero_at_reset():
    """All error bits read 0 before any fault.

    VPLAN: RST-11a
    """
    dut = _new_dut()
    out = []

    def gen():
        yield
        out.append((yield from _read_err(dut)))

    run_simulation(dut, gen())
    assert out == [0]


def test_pop_empty_sets_sticky_bit():
    """An illegal pop latches bit 2 and it stays set.

    VPLAN: RX-11, CSR-5
    """
    dut = _new_dut()
    out = []

    def gen():
        yield
        yield from _pulse_pop_empty(dut)
        for _ in range(50):
            out.append((yield from _read_err(dut)))

    run_simulation(dut, gen())
    assert set(out) == {1 << BIT_POP_EMPTY}


def test_reading_err_does_not_clear_it():
    """100 reads of console_err leave every bit unchanged.

    VPLAN: CSR-8, CSR-4
    """
    dut = _new_dut()
    out = []

    def gen():
        yield
        yield from _pulse_pop_empty(dut)
        for _ in range(100):
            out.append((yield from _read_err(dut)))

    run_simulation(dut, gen())
    assert len(set(out)) == 1 and out[0] == 1 << BIT_POP_EMPTY


def test_write_one_clears_only_that_bit():
    """Writing 1 to a clear bit clears exactly that bit.

    VPLAN: CSR-9
    """
    dut = _new_overflow_dut()
    out = []

    def gen():
        yield dut.pads.rx.eq(1)
        for _ in range(OVF_BIT):
            yield
        yield from _pulse_pop_empty(dut)   # bit 2, fifo still empty here
        yield from _overflow(dut)          # bit 0, real overflow
        out.append((yield from _read_err(dut)))
        yield from _clear(dut, 1 << BIT_POP_EMPTY)
        out.append((yield from _read_err(dut)))
        yield from _clear(dut, 1 << BIT_OVERFLOW)
        out.append((yield from _read_err(dut)))

    run_simulation(dut, gen())
    both = (1 << BIT_POP_EMPTY) | (1 << BIT_OVERFLOW)
    assert out == [both, 1 << BIT_OVERFLOW, 0]


def test_write_zero_leaves_bit_set():
    """Writing 0 to a clear bit leaves the corresponding bit unchanged.

    VPLAN: CSR-10
    """
    dut = _new_dut()
    out = []

    def gen():
        yield
        yield from _pulse_pop_empty(dut)
        yield from _clear(dut, 0)
        out.append((yield from _read_err(dut)))

    run_simulation(dut, gen())
    assert out == [1 << BIT_POP_EMPTY]


def test_set_wins_over_concurrent_clear():
    """A clear coinciding with the set condition leaves the bit SET.

    VPLAN: CSR-11
    """
    dut = _new_dut()
    out = []

    def gen():
        yield
        # Assert the clear and the fault on the SAME cycle.
        yield dut.console_err_clear.r.eq(1 << BIT_POP_EMPTY)
        yield dut.console_err_clear.re.eq(1)
        yield dut.console_rx_pop.re.eq(1)
        yield
        yield dut.console_err_clear.re.eq(0)
        yield dut.console_rx_pop.re.eq(0)
        yield
        yield
        out.append((yield from _read_err(dut)))

    run_simulation(dut, gen())
    assert out == [1 << BIT_POP_EMPTY], "a concurrent clear swallowed a fresh fault"


def _new_dut_with_reset():
    """_new_overflow_dut(), plus a real 'sys' ClockDomain so a reset can be driven.

    ConsoleBridge itself never declares its own ClockDomain("sys") -- in a
    real SoC build that domain (and its `rst` signal) is supplied by the CRG
    one level up (see versa_soc.py's `cd_sys.rst.eq(ResetSignal("sys"))`).
    Verified directly (scratch experiment against this exact DUT) that
    without adding one here, migen.sim.core.Simulator auto-creates a
    *reset_less* "sys" ClockDomain of its own for any clock named in its
    `clocks=` dict that the fragment doesn't already declare
    (migen/sim/core.py:293: `ClockDomain(name=clock, reset_less=True)`) --
    that domain's `.rst` is `None` (ClockDomain's `reset_less` -- a
    domain-level "no reset signal exists at all", not to be confused with
    Signal's `reset_less` used on the sticky bits themselves). Driving
    `ResetSignal("sys")` directly from a generator against that raised
    `NotImplementedError` in the Evaluator: there is no signal to write to.
    Adding a real `ClockDomain("sys")` here (test-side only, the same shape
    production wiring already takes) gives `dut.cd_sys.rst` an actual
    Signal, so the simulator's own `insert_resets()` (migen/fhdl/tools.py)
    has a genuine reset to insert and this test can drive one for real.
    """
    dut = _new_overflow_dut()
    dut.clock_domains.cd_sys = ClockDomain("sys")
    return dut


def test_sticky_bits_survive_reset():
    """All three sticky bits, once latched, are still set after a real
    `cd_sys` reset pulse -- SPEC.md S-CSR-9/S-CSR-10: a fault that provokes
    a power cycle must still be visible afterward.

    A control in the same test proves the reset actually took effect: tx_fifo
    is filled to depth (which is what also trips the tx_write_when_full
    fault), so its `level` is a piece of ordinary, genuinely-reset state
    sitting at a known nonzero value (256) at the exact moment of the reset.
    If the reset assertion below were a no-op (e.g. `dut.cd_sys.rst` silently
    not wired into the sync logic, as this codebase's testbench-write hazard
    has produced before), `level` would still read 256 afterward and this
    test would catch it -- a reset that doesn't run makes the sticky-survival
    assertion trivially true, so this guards against that exact failure mode.

    VPLAN: RST-12
    """
    dut = _new_dut_with_reset()
    out = {}

    def gen():
        yield dut.pads.rx.eq(1)
        for _ in range(OVF_BIT):
            yield
        # Trip all three fault sources.
        yield from _pulse_pop_empty(dut)                       # bit 2
        yield from _overflow(dut)                              # bit 0, real overflow
        for i in range(256):                                   # fill tx_fifo
            yield dut.console_tx_data.r.eq(i & 0xFF)
            yield dut.console_tx_data.re.eq(1)
            yield
            yield dut.console_tx_data.re.eq(0)
            yield
        yield dut.console_tx_data.r.eq(0xFF)                   # rejected: bit 1
        yield dut.console_tx_data.re.eq(1)
        yield
        yield dut.console_tx_data.re.eq(0)
        yield
        yield

        all_bits = (1 << BIT_OVERFLOW) | (1 << BIT_TX_FULL) | (1 << BIT_POP_EMPTY)
        out["err_before"] = (yield from _read_err(dut))
        out["tx_level_before"] = (yield dut.tx_fifo.level)

        yield dut.cd_sys.rst.eq(1)
        yield
        yield
        yield dut.cd_sys.rst.eq(0)
        yield
        yield

        out["err_after"] = (yield from _read_err(dut))
        out["tx_level_after"] = (yield dut.tx_fifo.level)
        out["all_bits"] = all_bits

    run_simulation(dut, gen())

    assert out["err_before"] == out["all_bits"], "setup failed to latch all 3 bits"
    assert out["tx_level_before"] == 256, "setup failed to actually fill tx_fifo"
    assert out["err_after"] == out["all_bits"], \
        "a sticky bit did NOT survive reset"
    assert out["tx_level_after"] == 0, \
        "control state did not clear -- the reset was never actually applied"
