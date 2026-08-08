#
# console_bridge.py -- ConsoleBridge: the host-facing console byte path.
#
# Lives entirely in cd_sys. Contains no 8008 and no clock-domain crossing:
# SPEC.md S-ARCH-1 puts the sys<->b8008 boundary on the serial line itself,
# so this module is a single-domain design that Migen can simulate directly.
#
# Serial convention, stated once because the names invert across the boundary:
#   pads.rx  is this bridge's INPUT  -- driven by the core's uart_tx
#   pads.tx  is this bridge's OUTPUT -- drives the core's uart_rx
#
from migen import Record, Signal, If

from litex.gen import LiteXModule
from litex.soc.interconnect import stream
from litex.soc.interconnect.csr import AutoCSR, CSR, CSRStatus, CSRField
from litex.soc.cores.uart import RS232PHY

RX_DEPTH = 4096   # SPEC.md S-RX-1
TX_DEPTH = 256    # SPEC.md S-TX-1


class ConsoleBridge(LiteXModule, AutoCSR):
    def __init__(self, sys_clk_freq, baudrate=115200,
                 rx_depth=RX_DEPTH, tx_depth=TX_DEPTH):
        self.pads = Record([("tx", 1), ("rx", 1)])

        # PHY is clocked in cd_sys at an explicitly passed frequency --
        # SPEC.md S-CLK-6 forbids inferring it from the platform.
        self.phy = RS232PHY(self.pads, clk_freq=sys_clk_freq, baudrate=baudrate)

        self.rx_fifo = stream.SyncFIFO([("data", 8)], rx_depth, buffered=True)
        self.tx_fifo = stream.SyncFIFO([("data", 8)], tx_depth, buffered=True)
        self.submodules += self.rx_fifo, self.tx_fifo

        self.comb += [
            self.phy.source.connect(self.rx_fifo.sink),   # PHY RX -> rx_fifo
            self.tx_fifo.source.connect(self.phy.sink),   # tx_fifo -> PHY TX
        ]

        # ---- console_rx: atomic {data, valid, level} (SPEC.md S-RX-8) --------
        # One read carries everything the host needs, so it can never observe a
        # torn level/data pair and never needs two round trips on a transport
        # that costs ~1 RTT per read (S-WIRE-3, S-WIRE-4).
        self.console_rx = CSRStatus(name="console_rx", fields=[
            CSRField("data",  size=8),
            CSRField("valid", size=1),
            CSRField("level", size=13)])
        # SyncFIFOBuffered (migen/genlib/fifo.py) has a one-cycle window on the
        # empty->non-empty edge where its `level` (= inner fifo.level +
        # outer readable) already counts the incoming word combinationally,
        # but `source.valid` (the outer `readable` register) only catches up
        # on the next clock: `fifo.re` fires the same cycle the inner FIFO
        # becomes readable, while `self.readable.eq(1)` is a `sync` update
        # gated on that same `fifo.re`. So for that one cycle,
        # rx_fifo.level == 1 while rx_fifo.source.valid == 0 -- reporting a
        # byte the host cannot yet actually pop. Exporting raw
        # rx_fifo.level here would let `console_rx.level != 0` disagree with
        # `console_rx.valid` on that cycle, breaking S-RX-8's atomicity
        # requirement (RX-8/RX-10). Gate the exported level on `valid` so
        # they can never disagree; `rx_fifo.level` itself (what Task 6's
        # backpressure logic reads directly, not through this CSR) is
        # unchanged.
        self.comb += [
            # data reads 0x00 when empty -- S-RX-7's table, not "don't care".
            If(self.rx_fifo.source.valid,
                self.console_rx.fields.data.eq(self.rx_fifo.source.data),
                self.console_rx.fields.level.eq(self.rx_fifo.level),
            ).Else(
                self.console_rx.fields.data.eq(0),
                self.console_rx.fields.level.eq(0),
            ),
            self.console_rx.fields.valid.eq(self.rx_fifo.source.valid),
        ]

        # ---- console_rx_pop: the ONLY consuming action (SPEC.md S-RX-4) ------
        # Reads are non-destructive so a retried UDP read cannot eat a byte
        # (S-RX-5). Consumption moves to the write path, which is not retried.
        self.console_rx_pop = CSR(name="console_rx_pop")
        self.err_rx_pop_when_empty = Signal()
        self.comb += [
            self.rx_fifo.source.ready.eq(
                self.console_rx_pop.re & self.rx_fifo.source.valid),
            self.err_rx_pop_when_empty.eq(
                self.console_rx_pop.re & ~self.rx_fifo.source.valid),
        ]

        # ---- console_tx / console_tx_data (SPEC.md S-TX-2, S-TX-3) ----------
        # A write while full is REJECTED and flagged, not silently discarded.
        #
        # `full` must NOT be derived from `~tx_fifo.sink.ready`. That signal
        # is stream.SyncFIFO's `sink.ready`, which for a *buffered* FIFO
        # (migen/genlib/fifo.py SyncFIFOBuffered) equals the INNER SyncFIFO's
        # `writable` -- true whenever the inner depth-256 memory has fewer
        # than 256 entries, with no regard for the extra one-word output
        # register the wrapper adds ("Increases latency by one cycle" via
        # `self.readable`/`self.dout`). `tx_fifo.level` (what this CSR
        # reports) is `fifo.level + self.readable`, i.e. inner memory
        # occupancy PLUS that extra register, so it can reach up to 257 even
        # though sink.ready only gates on the inner 256. Verified in sim: a
        # continuous fill with no drain hits a cycle where level==256 while
        # sink.ready is still 1 -- full=0 while level says the FIFO is
        # already at its spec'd capacity (S-TX-1). Accepting the write that
        # cycle (the brief's naive `full = ~sink.ready` implementation does)
        # pushes level to 257, permanently exceeding TX_DEPTH: an
        # over-report of available space, the exact class of bug forbidden
        # here (same as Task 3's RX-side fix; see console_rx above).
        #
        # Fix: make `full` a pure decode of `tx_fifo.level` -- the same
        # register `console_tx.fields.level` reports -- and gate writes off
        # that same decode, not off sink.ready. This makes `full ==
        # (level == 256)` true by construction, every cycle, with no race:
        # both fields come from one registered signal read in the same
        # comb evaluation. It also caps real occupancy at 256, never lets
        # level reach 257, and is safe against the hardware's true
        # capacity: level >= tx_fifo.fifo.level (inner occupancy) always,
        # so level < 256 guarantees the inner FIFO is not full and
        # sink.ready is genuinely 1 -- this never claims room that isn't
        # there, it only ever holds back the harmless extra 257th slot
        # (under-report, which Task 3's precedent allows).
        tx_full = Signal()
        self.comb += tx_full.eq(self.tx_fifo.level == tx_depth)

        self.console_tx = CSRStatus(name="console_tx", fields=[
            CSRField("level", size=9),
            CSRField("full",  size=1)])
        self.comb += [
            self.console_tx.fields.level.eq(self.tx_fifo.level),
            self.console_tx.fields.full.eq(tx_full),
        ]

        self.console_tx_data = CSR(8, name="console_tx_data")
        self.err_tx_write_when_full = Signal()
        self.comb += [
            self.tx_fifo.sink.valid.eq(
                self.console_tx_data.re & ~tx_full),
            self.tx_fifo.sink.data.eq(self.console_tx_data.r),
            self.err_tx_write_when_full.eq(
                self.console_tx_data.re & tx_full),
        ]

        # ---- sticky error bits (SPEC.md §11.5, S-CSR-9, S-CSR-12) -----------
        # Sticky, and survive every reset: a fault that provokes a power cycle
        # must still be visible afterward (S-CSR-10). Set beats a coinciding
        # clear -- losing a fresh fault to a concurrent clear would make these
        # unreliable in exactly the case they exist for.
        self.err_rx_overflow = Signal()   # driven by the backpressure task

        self.console_err = CSRStatus(name="console_err", fields=[
            CSRField("rx_overflow",        size=1),
            CSRField("tx_write_when_full", size=1),
            CSRField("rx_pop_when_empty",  size=1)])
        self.console_err_clear = CSR(3, name="console_err_clear")

        sources = [
            (0, self.err_rx_overflow,          self.console_err.fields.rx_overflow),
            (1, self.err_tx_write_when_full,   self.console_err.fields.tx_write_when_full),
            (2, self.err_rx_pop_when_empty,    self.console_err.fields.rx_pop_when_empty),
        ]
        for idx, src, field in sources:
            sticky = Signal(name=f"sticky_err_{idx}", reset_less=True)
            self.sync += [
                If(src,
                    sticky.eq(1)                       # set wins
                ).Elif(self.console_err_clear.re & self.console_err_clear.r[idx],
                    sticky.eq(0)
                )
            ]
            self.comb += field.eq(sticky)
