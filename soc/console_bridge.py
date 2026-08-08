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
        self.comb += [
            # data reads 0x00 when empty -- S-RX-7's table, not "don't care".
            If(self.rx_fifo.source.valid,
                self.console_rx.fields.data.eq(self.rx_fifo.source.data)
            ).Else(
                self.console_rx.fields.data.eq(0)
            ),
            self.console_rx.fields.valid.eq(self.rx_fifo.source.valid),
            self.console_rx.fields.level.eq(self.rx_fifo.level),
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
