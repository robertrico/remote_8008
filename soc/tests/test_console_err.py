"""Sticky error bits and their clear semantics."""
from migen import run_simulation

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
    dut = _new_dut()
    out = []

    def gen():
        yield
        yield dut.err_rx_overflow.eq(1)
        yield
        yield dut.err_rx_overflow.eq(0)
        yield from _pulse_pop_empty(dut)
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
