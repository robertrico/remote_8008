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
from migen import Record

from litex.gen import LiteXModule
from litex.soc.interconnect import stream
from litex.soc.interconnect.csr import AutoCSR
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
