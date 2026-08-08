"""Console TX path behavior. DUT is ConsoleBridge, pure cd_sys."""
from migen import run_simulation

from console_bridge import ConsoleBridge

# clk/baud = 10 -> 10 sys cycles per bit. Mirrors test_console_rx.py's FAST;
# used here only by the live-drain test, which needs the PHY to actually
# move bytes while writes keep arriving.
FAST = dict(sys_clk_freq=1_000_000, baudrate=100_000)
BIT = 10


def _new_dut(**kwargs):
    """ConsoleBridge with console_tx wired for simulation.

    Same gap as test_console_rx.py's _new_dut(): LiteXModule excludes
    _CSRBase instances from auto-submodule registration, so a bare
    ConsoleBridge(...) never runs CSRStatus.status's internal comb (the
    logic that composes .status from .fields.*), and `.status` reads stuck
    at its reset value even though `.fields.*` are already correct. Confirmed
    directly here: without this finalize, level/full both read back as 0
    from `.status` immediately after a write that the DUT's own
    `.fields.level`/`.fields.full` already show as 1/correct. Finalizing
    console_tx (test-side only, as console_rx does) fixes it.
    """
    dut = ConsoleBridge(**kwargs)
    dut.console_tx.finalize(32, "big")
    dut.submodules += dut.console_tx
    return dut


def _write_tx(dut, byte):
    yield dut.console_tx_data.r.eq(byte)
    yield dut.console_tx_data.re.eq(1)
    yield
    yield dut.console_tx_data.re.eq(0)
    yield
    yield


def _read_tx(dut):
    w = yield dut.console_tx.status
    return w & 0x1FF, (w >> 9) & 1


def _fill_tx(dut, n):
    for i in range(n):
        yield from _write_tx(dut, i & 0xFF)


def test_tx_fifo_depth_is_256():
    """tx_fifo depth is exactly 256.

    VPLAN: TX-1
    """
    assert ConsoleBridge(sys_clk_freq=75e6).tx_fifo.fifo.depth == 256


def test_write_increases_level_by_one():
    """A write while not full increases level by exactly 1.

    VPLAN: TX-2
    """
    dut = _new_dut(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        out.append((yield from _read_tx(dut)))
        yield from _write_tx(dut, 0x5A)
        out.append((yield from _read_tx(dut)))

    run_simulation(dut, gen())
    assert out == [(0, 0), (1, 0)]


def test_full_flag_tracks_level():
    """full == (level == 256) after filling the FIFO with no drain.

    VPLAN: TX-7
    """
    dut = _new_dut(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        yield from _fill_tx(dut, 256)
        out.append((yield from _read_tx(dut)))

    run_simulation(dut, gen())
    assert out == [(256, 1)]


def test_full_flag_consistent_during_live_drain():
    """full == (level == depth) every single cycle, including cycles where a
    write and a PHY drain land back to back -- not just after a fill.

    Regression for a boundary bug in the naive `full = ~tx_fifo.sink.ready`
    formulation (SPEC.md S-TX-5): stream.SyncFIFO(buffered=True) wraps
    migen.genlib.fifo.SyncFIFOBuffered, whose exposed `sink.ready` is the
    INNER (depth-256) SyncFIFO's `writable`, while `level` is
    `inner.level + readable` -- the inner occupancy PLUS the wrapper's
    extra one-word output register ("Increases latency by one cycle").
    Verified directly in sim: filling continuously with no drain hits a
    cycle where level==256 while sink.ready is still 1, i.e. `full=0`
    while the FIFO is already at its spec'd capacity. Accepting a write
    that cycle pushes level to 257, permanently exceeding TX_DEPTH -- an
    over-report of available space, the same class of bug Task 3 fixed on
    the RX side (see console_bridge.py's console_rx comment). The fix
    makes `full` a pure decode of `tx_fifo.level` (the same register
    `console_tx.fields.level` reports) instead of `sink.ready`, and gates
    writes off that same decode -- so the pair can't disagree in any
    cycle, by construction, not by getting lucky on timing.

    A fill-then-check test (test_full_flag_tracks_level above) cannot
    observe this: it only samples once the FIFO has been sitting full for
    a while, after any transient has already resolved. This drives writes
    at CSR-cycle rate (each ~3 cycles) against a PHY draining at FAST's
    10-cycles/bit rate (100 cycles/byte), so the FIFO repeatedly saturates
    and drains by one while writes keep landing, and samples every single
    cycle throughout.

    VPLAN: TX-7
    """
    dut = _new_dut(**FAST)
    bad = []
    max_level = [0]

    def driver():
        yield dut.pads.rx.eq(1)
        for i in range(600):
            yield from _write_tx(dut, i & 0xFF)

    def monitor():
        for _ in range(2000):
            level, full = yield from _read_tx(dut)
            max_level[0] = max(max_level[0], level)
            expect_full = 1 if level == 256 else 0
            if full != expect_full:
                bad.append((level, full, expect_full))
            yield

    run_simulation(dut, [driver(), monitor()])
    assert not bad, f"full/level disagreed during live drain: {bad}"
    assert max_level[0] == 256, "test never actually reached full -- weak"


def test_write_when_full_is_rejected():
    """A write while full leaves level at 256 and pulses the error signal.

    Dropped a `CSR-8` tag this test previously carried: `CSR-8` is "a read
    of console_err leaves all its bits unchanged," and this test never
    reads `console_err` at all -- it only checks `console_tx`'s level/full
    fields and `err_tx_write_when_full`. `CSR-8` is already legitimately
    discharged elsewhere (`test_console_err.py::test_reading_err_does_not_
    clear_it`).

    VPLAN: TX-4, TX-5, TX-6
    """
    dut = _new_dut(sys_clk_freq=75e6)
    out, fired = [], []

    def monitor():
        for _ in range(4000):
            if (yield dut.err_tx_write_when_full):
                fired.append(1)
            yield

    def gen():
        yield
        yield from _fill_tx(dut, 256)
        yield from _write_tx(dut, 0xFF)      # rejected
        out.append((yield from _read_tx(dut)))

    run_simulation(dut, [gen(), monitor()])
    assert out == [(256, 1)]
    assert fired, "err_tx_write_when_full never asserted"


def test_write_when_full_does_not_displace_queued_bytes():
    """A rejected write leaves every byte already queued unchanged: the
    front-of-queue byte popped after the rejection is still the first byte
    written, not the rejected one and not corrupted.

    VPLAN: TX-4, TX-6
    """
    dut = _new_dut(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        yield from _write_tx(dut, 0x11)      # accepted, first in queue
        yield from _fill_tx(dut, 255)        # fills to 256
        yield from _write_tx(dut, 0xFF)      # rejected -- must not displace 0x11
        out.append((yield dut.tx_fifo.source.data))

    run_simulation(dut, gen())
    assert out == [0x11]


def test_tx_data_ignores_high_bits():
    """Bits 31:8 of a console_tx_data write do not reach the FIFO.

    VPLAN: CSR-7
    """
    dut = _new_dut(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        yield from _write_tx(dut, 0x41)
        out.append((yield dut.tx_fifo.source.data))

    run_simulation(dut, gen())
    assert out == [0x41]
