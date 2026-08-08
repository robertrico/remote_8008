#
# b8008_integration.py -- B8008Core: Migen/LiteX wrapper around the GHDL-
# converted Intel-8008 monitor netlist (build/b8008_net_core.v, Task 5).
#
# Wraps the VHDL core with:
#   - the core's own 16384-byte RAM, b8008-domain port only -- SPEC.md
#     S-PROD-8 retired the host-facing wishbone window onto it (D-10),
#   - a b8008-domain ROM read port fed from a 4096-entry init image,
#   - a console UART bridge (see console_bridge.ConsoleBridge: RS232 PHY +
#     rx/tx SyncFIFOs, sys-domain only per SPEC.md S-ARCH-1),
#   - a single clock-domain crossing (sys -> b8008): the console bridge's
#     backpressure stall, a LEVEL synchronized via MultiReg (SPEC.md
#     S-CDC-1 X3, S-CDC-3). The retired ctl/status CSRs and their four
#     PulseSynchronizers (D-8/D-9/D-10) are gone as of Task 7.
#
# The RISC-V/Etherbone SoC routes self.dbg to physical pads.
#
import os

from migen import (
    Signal, Instance, Memory, Record, ClockSignal, ResetSignal, READ_FIRST,
)
from migen.genlib.cdc import MultiReg

from litex.gen import LiteXModule
from litex.soc.interconnect.csr import AutoCSR


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
        # ---- RAM: 16384 bytes, b8008-domain port only ------------------------
        # Monitor uses b8008_top DEFAULT map with RAM_ADDR_BITS=14 and ABSOLUTE
        # addressing (Task 4 finding). SPEC.md S-PROD-8 retires the host-facing
        # wishbone window (D-10): the 8008 still needs its own RAM, but nothing
        # outside this core reads or writes it directly any more. READ_FIRST
        # pins the read-during-write contract (old data on read); a sync read
        # every b8008 edge is exactly what a Migen sync read port gives.
        ram = Memory(8, 16384)
        pa = ram.get_port(write_capable=True, clock_domain="b8008", mode=READ_FIRST)  # CPU
        self.specials += ram, pa

        # ---- ROM: 4096 bytes, b8008-domain read port ------------------------
        rom = Memory(8, 4096, init=rom_init or [0] * 4096)
        pr = rom.get_port(clock_domain="b8008")
        self.specials += rom, pr

        # ---- console bridge (SPEC.md S-ARCH-1: sys-domain, serial crossing) ---
        from console_bridge import ConsoleBridge

        self.console = ConsoleBridge(sys_clk_freq=sys_clk_freq)
        pads = self.console.pads

        # ---- debug bus: expose as a Record for the SoC to route to pads -----
        self.dbg = Record([
            ("d", 8), ("s0", 1), ("s1", 1), ("s2", 1),
            ("sync", 1), ("phi1", 1), ("phi2", 1), ("int", 1)])

        # ---- RAM port-A write enable from the core's rw_n / cs_n ------------
        ram_rw_n = Signal(name="ram_rw_n")
        ram_cs_n = Signal(name="ram_cs_n")
        self.comb += pa.we.eq(~ram_cs_n & ~ram_rw_n)

        # ---- X3: backpressure stall, cd_sys -> cd_b8008 (SPEC.md S-CDC-1) ---
        # A LEVEL, not a pulse (S-CDC-3): the synchronizer's 2-cycle latency
        # (80 ns) is negligible against the 86.805 us byte time.
        stall_b = Signal()
        self.specials += MultiReg(self.console.stall, stall_b, "b8008")

        # ---- the VHDL core --------------------------------------------------
        platform.add_source(core_v)
        platform.add_source(_GHDL_GATES)  # gate_mdff / gate_midff definitions
        self.specials += Instance("b8008_net_core",
            i_clk=ClockSignal("b8008"), i_rst=ResetSignal("b8008"),
            o_uart_tx=pads.rx,  # core TX -> bridge RX
            i_uart_rx=pads.tx,  # bridge TX -> core RX
            i_ext_hold=stall_b,
            o_ram_addr=pa.adr, o_ram_wdata=pa.dat_w, i_ram_rdata=pa.dat_r,
            o_ram_rw_n=ram_rw_n, o_ram_cs_n=ram_cs_n,
            o_rom_addr=pr.adr, i_rom_data=pr.dat_r,
            o_dbg_d=self.dbg.d, o_dbg_s0=self.dbg.s0, o_dbg_s1=self.dbg.s1,
            o_dbg_s2=self.dbg.s2, o_dbg_sync=self.dbg.sync,
            o_dbg_phi1=self.dbg.phi1, o_dbg_phi2=self.dbg.phi2,
            o_dbg_int=self.dbg.int)
