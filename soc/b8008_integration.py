#
# b8008_integration.py -- B8008Core: Migen/LiteX wrapper around the GHDL-
# converted Intel-8008 monitor netlist (build/b8008_net_core.v, Task 5).
#
# Wraps the VHDL core with:
#   - dual-clock RAM (b8008-domain port A for the CPU, sys-domain port B on a
#     wishbone window; word index == absolute 14-bit 8008 address),
#   - a b8008-domain ROM read port fed from a 4096-entry init image,
#   - a console UART bridge (RS232 PHY + rx/tx SyncFIFOs + CSRs), copied strobe-
#     for-strobe from litex.soc.cores.uart at the pinned tag,
#   - control-CSR clock-domain crossing (sys -> b8008): pulse fields via
#     PulseSynchronizer, the interrupt vector via MultiReg,
#   - status CDC (b8008 -> sys) via MultiReg.
#
# The RISC-V/Etherbone SoC (Task 7) drives the CSRs and the wishbone window and
# routes self.dbg to physical pads.
#
import os

from migen import (
    Signal, Instance, Memory, Record, ClockSignal, ResetSignal, READ_FIRST,
)
from migen.genlib.cdc import MultiReg, PulseSynchronizer

from litex.gen import LiteXModule
from litex.soc.interconnect import wishbone, stream
from litex.soc.interconnect.csr import CSRStorage, CSRStatus, CSR, CSRField, AutoCSR
from litex.soc.cores.uart import RS232PHY


_HERE = os.path.dirname(os.path.abspath(__file__))
# The GHDL netlist instantiates gate_mdff / gate_midff primitives defined here;
# without this source the LiteX/yosys build fails its hierarchy check. Resolve
# absolutely from this file so it works regardless of the caller's cwd.
_GHDL_GATES = os.path.abspath(os.path.join(_HERE, "..", "build", "ghdl_gates.v"))


def load_mem_file(path):
    """Read a whitespace/newline separated hex byte image (one byte per line)."""
    return [int(l.strip(), 16) for l in open(path) if l.strip()]


class B8008Core(LiteXModule, AutoCSR):
    def __init__(self, platform, sys_clk_freq, core_v="build/b8008_net_core.v", rom_init=None):
        # ---- control CSRs (sys domain) -> pulses / level in b8008 domain ----
        # pulse=True fields drive the CSR's field output high for exactly one
        # sys cycle on write (csr.py: `If(self.re, field_assign)`); each is fed
        # through a PulseSynchronizer to a single-cycle b8008-domain pulse.
        self.ctl = CSRStorage(name="ctl", fields=[
            CSRField("run_stop",   size=1, pulse=True),
            CSRField("step_cycle", size=1, pulse=True),
            CSRField("step_sync",  size=1, pulse=True),
            CSRField("int_req",    size=1, pulse=True),
            CSRField("int_vector", size=3)])
        ctl_pulses = {}
        for name in ["run_stop", "step_cycle", "step_sync", "int_req"]:
            ps = PulseSynchronizer("sys", "b8008")
            self.submodules += ps
            self.comb += ps.i.eq(getattr(self.ctl.fields, name))
            ctl_pulses[name] = ps.o
        int_vec_b = Signal(3)
        self.specials += MultiReg(self.ctl.fields.int_vector, int_vec_b, "b8008")

        # ---- status: b8008 domain -> sys via MultiReg -----------------------
        is_running_b, triggered_b, tx_busy_b = Signal(), Signal(), Signal()
        self.status = CSRStatus(name="status", fields=[
            CSRField("is_running", size=1),
            CSRField("triggered",  size=1),
            CSRField("tx_busy",    size=1)])
        self.specials += [
            MultiReg(is_running_b, self.status.fields.is_running),
            MultiReg(triggered_b,  self.status.fields.triggered),
            MultiReg(tx_busy_b,    self.status.fields.tx_busy)]

        # ---- RAM: 16384 bytes, dual-clock, byte per 32-bit wishbone word ----
        # Monitor uses b8008_top DEFAULT map with RAM_ADDR_BITS=14 and ABSOLUTE
        # addressing (Task 4 finding). Window convention: wishbone word index ==
        # absolute 14-bit 8008 address. READ_FIRST pins the read-during-write
        # contract (old data on read); a sync read every b8008 edge is exactly
        # what a Migen sync read port gives.
        ram = Memory(8, 16384)
        pa = ram.get_port(write_capable=True, clock_domain="b8008", mode=READ_FIRST)  # CPU
        pb = ram.get_port(write_capable=True, clock_domain="sys",   mode=READ_FIRST)  # wishbone
        self.specials += ram, pa, pb

        self.bus_ram = wishbone.Interface(data_width=32, adr_width=30)
        self.sync += self.bus_ram.ack.eq(self.bus_ram.cyc & self.bus_ram.stb & ~self.bus_ram.ack)
        self.comb += [
            pb.adr.eq(self.bus_ram.adr[:14]),
            pb.dat_w.eq(self.bus_ram.dat_w[:8]),
            pb.we.eq(self.bus_ram.cyc & self.bus_ram.stb & ~self.bus_ram.ack
                     & self.bus_ram.we & self.bus_ram.sel[0]),
            self.bus_ram.dat_r.eq(pb.dat_r)]

        # ---- ROM: 4096 bytes, b8008-domain read port ------------------------
        rom = Memory(8, 4096, init=rom_init or [0] * 4096)
        pr = rom.get_port(clock_domain="b8008")
        self.specials += rom, pr

        # ---- console bridge: RS232-level serial <-> CSR FIFOs ---------------
        # Strobe wiring copied verbatim from litex.soc.cores.uart.UART at this
        # tag: TX pushes tx_fifo on _rxtx.re (read-enable-as-write for the CSR
        # write side), RX pops rx_fifo on _rxtx.we (rx_fifo_rx_we path). PHY is
        # clocked in the sys domain at sys_clk_freq (explicit -- platforms carry
        # no freq; a hasattr fallback would silently mis-clock the baud divisor).
        pads = Record([("tx", 1), ("rx", 1)])
        self.submodules.phy = RS232PHY(pads, clk_freq=sys_clk_freq, baudrate=115200)
        rx_fifo = stream.SyncFIFO([("data", 8)], 4096, buffered=True)
        tx_fifo = stream.SyncFIFO([("data", 8)], 256, buffered=True)
        self.submodules += rx_fifo, tx_fifo

        self._rxtx    = CSR(8, name="rxtx")
        self._rxlevel = CSRStatus(13, name="rxlevel")
        self._txfull  = CSRStatus(name="txfull")
        self._rxempty = CSRStatus(name="rxempty")
        self.comb += [
            # PHY <-> FIFOs.
            self.phy.source.connect(rx_fifo.sink),   # PHY RX -> rx_fifo
            tx_fifo.source.connect(self.phy.sink),   # tx_fifo -> PHY TX
            # CSR write (re) --> TX FIFO.
            tx_fifo.sink.valid.eq(self._rxtx.re),
            tx_fifo.sink.data.eq(self._rxtx.r),
            # RX FIFO --> CSR read (w), pop on we.
            self._rxtx.w.eq(rx_fifo.source.data),
            rx_fifo.source.ready.eq(self._rxtx.we),
            # Status.
            self._rxlevel.status.eq(rx_fifo.level),
            self._txfull.status.eq(~tx_fifo.sink.ready),
            self._rxempty.status.eq(~rx_fifo.source.valid)]

        # ---- debug bus: expose as a Record for the SoC to route to pads -----
        self.dbg = Record([
            ("d", 8), ("s0", 1), ("s1", 1), ("s2", 1),
            ("sync", 1), ("phi1", 1), ("phi2", 1), ("int", 1)])

        # ---- RAM port-A write enable from the core's rw_n / cs_n ------------
        ram_rw_n = Signal(name="ram_rw_n")
        ram_cs_n = Signal(name="ram_cs_n")
        self.comb += pa.we.eq(~ram_cs_n & ~ram_rw_n)

        # ---- the VHDL core --------------------------------------------------
        platform.add_source(core_v)
        platform.add_source(_GHDL_GATES)  # gate_mdff / gate_midff definitions
        self.specials += Instance("b8008_net_core",
            i_clk=ClockSignal("b8008"), i_rst=ResetSignal("b8008"),
            o_uart_tx=pads.rx,  # core TX -> bridge RX
            i_uart_rx=pads.tx,  # bridge TX -> core RX
            i_ctl_run_stop=ctl_pulses["run_stop"],
            i_ctl_step_cycle=ctl_pulses["step_cycle"],
            i_ctl_step_sync=ctl_pulses["step_sync"],
            i_ctl_int=ctl_pulses["int_req"],
            i_ctl_int_vector=int_vec_b,
            o_sts_is_running=is_running_b, o_sts_triggered=triggered_b,
            o_sts_tx_busy=tx_busy_b,
            o_ram_addr=pa.adr, o_ram_wdata=pa.dat_w, i_ram_rdata=pa.dat_r,
            o_ram_rw_n=ram_rw_n, o_ram_cs_n=ram_cs_n,
            o_rom_addr=pr.adr, i_rom_data=pr.dat_r,
            o_dbg_d=self.dbg.d, o_dbg_s0=self.dbg.s0, o_dbg_s1=self.dbg.s1,
            o_dbg_s2=self.dbg.s2, o_dbg_sync=self.dbg.sync,
            o_dbg_phi1=self.dbg.phi1, o_dbg_phi2=self.dbg.phi2,
            o_dbg_int=self.dbg.int)
