#
# bench_core.py -- Verilator bench harness around B8008Core (Task 9).
# ----------------------------------------------------------------------------
# Pre-hardware gate. macOS host has no TAP interface, so we do NOT drive the
# monitor over simulated Ethernet; instead this emits a standalone Verilog top
# (build/bench_core.v) that exposes B8008Core's *CSR bus* and *wishbone bus*
# directly, plus both clock domains (sys + b8008) and both resets, as ports.
# The C++ driver (sim/bench_tb.cpp) then exercises exactly the custom logic:
#   - wishbone RAM window (shim + BRAM port B),
#   - console bridge CSR mechanics (rxtx / rxlevel FIFO strobes),
#   - control-CSR clock-domain crossing (ctl pulses sys -> b8008; status back),
#   - the converted GHDL netlist itself (auto-start boot to UART banner).
#
# CSR bus: B8008Core's CSR objects only materialise an adr/we/dat_w/dat_r bus
# through litex.soc.interconnect.csr_bus.CSRBankArray + Interconnect (exactly
# how SoCCore does it). We build a single bank ("core") at offset 0 with a
# 32-bit data bus -- mirrors the real SoC's csr_data_width=32, so each CSR is
# one word and its word address == its index in B8008Core.get_csrs(sort=True):
#     0 ctl   1 status   2 rxtx   3 rxlevel   4 txfull   5 rxempty
#
# Run:  python bench_core.py   ->  build/bench_core.v
# ----------------------------------------------------------------------------
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# Same sys.path guard as versa_soc.py: the litex/migen *clones* live next to
# this file; drop this dir before importing them so the editable install wins,
# then re-append for the local b8008_integration import.
sys.path = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]

from migen import Signal, ClockDomain
from migen.fhdl.verilog import convert

from litex.gen import LiteXModule
from litex.soc.interconnect import csr_bus

sys.path.append(_HERE)
from b8008_integration import B8008Core, load_mem_file


class _FakePlatform:
    """Minimal stand-in: B8008Core only calls platform.add_source()."""
    def __init__(self):
        self.sources = []

    def add_source(self, path, *args, **kwargs):
        self.sources.append(path)


class BenchTop(LiteXModule):
    # Single CSR bank for the b8008 core, mapped at CSR-bus offset 0.
    def address_map(self, name, memory):
        return 0

    def __init__(self, platform, rom_init, core_v):
        # Two clock domains, explicitly named (Python-3.14 migen name tracer
        # needs the explicit ClockDomain name). The C++ bench drives both at
        # 25 MHz but phase-offset so the sys<->b8008 CDC paths are genuinely
        # crossed.
        self.cd_sys   = ClockDomain("sys")
        self.cd_b8008 = ClockDomain("b8008")

        # The device under test.
        self.submodules.core = B8008Core(
            platform, sys_clk_freq=25e6, core_v=core_v, rom_init=rom_init)

        # ---- CSR bus: build the adr/we/dat_w/dat_r fabric ------------------
        # 32-bit data width mirrors the real SoC (csr_data_width=32): every
        # B8008Core CSR is <= 32 bits, so each occupies a single word address.
        self.csr = csr_bus.Interface(data_width=32, address_width=14)
        self.submodules.csrbankarray = csr_bus.CSRBankArray(
            self, self.address_map, data_width=32, address_width=14)
        self.submodules.csrcon = csr_bus.Interconnect(
            self.csr, self.csrbankarray.get_buses())

        # ---- exposed, hard-named IO signals -------------------------------
        # name_override pins the exact Verilog port names the C++ driver uses.
        self.io_csr_adr   = Signal(14, name_override="csr_adr")
        self.io_csr_re    = Signal(name_override="csr_re")
        self.io_csr_we    = Signal(name_override="csr_we")
        self.io_csr_dat_w = Signal(32, name_override="csr_dat_w")
        self.io_csr_dat_r = Signal(32, name_override="csr_dat_r")
        self.comb += [
            self.csr.adr.eq(self.io_csr_adr),
            self.csr.re.eq(self.io_csr_re),
            self.csr.we.eq(self.io_csr_we),
            self.csr.dat_w.eq(self.io_csr_dat_w),
            self.io_csr_dat_r.eq(self.csr.dat_r),
        ]

        wb = self.core.bus_ram
        self.io_wb_adr   = Signal(30, name_override="wb_adr")
        self.io_wb_dat_w = Signal(32, name_override="wb_dat_w")
        self.io_wb_dat_r = Signal(32, name_override="wb_dat_r")
        self.io_wb_sel   = Signal(4,  name_override="wb_sel")
        self.io_wb_cyc   = Signal(name_override="wb_cyc")
        self.io_wb_stb   = Signal(name_override="wb_stb")
        self.io_wb_we    = Signal(name_override="wb_we")
        self.io_wb_ack   = Signal(name_override="wb_ack")
        self.comb += [
            wb.adr.eq(self.io_wb_adr),
            wb.dat_w.eq(self.io_wb_dat_w),
            wb.sel.eq(self.io_wb_sel),
            wb.cyc.eq(self.io_wb_cyc),
            wb.stb.eq(self.io_wb_stb),
            wb.we.eq(self.io_wb_we),
            self.io_wb_dat_r.eq(wb.dat_r),
            self.io_wb_ack.eq(wb.ack),
        ]

    def ios(self):
        return {
            self.cd_sys.clk, self.cd_sys.rst,
            self.cd_b8008.clk, self.cd_b8008.rst,
            self.io_csr_adr, self.io_csr_re, self.io_csr_we,
            self.io_csr_dat_w, self.io_csr_dat_r,
            self.io_wb_adr, self.io_wb_dat_w, self.io_wb_dat_r, self.io_wb_sel,
            self.io_wb_cyc, self.io_wb_stb, self.io_wb_we, self.io_wb_ack,
        }


def main():
    # Repo-root build/ (same convention as NETLIST_V/GHDL_GATES/VERSA_DIR in
    # the Makefile, and the same _HERE-relative fix already applied to the
    # rom_baked.mem lookup below) -- not soc/build/. _HERE is soc/ post-move,
    # so this must go up one level to land where the Makefile's BENCH_V and
    # `cd build && ./obj_bench/bench_tb` expect it.
    build_dir = os.path.join(_HERE, "..", "build")
    os.makedirs(build_dir, exist_ok=True)

    # regenerated by `make rom-freeze` in the core repo's projects/b8008_monitor
    rom_init = load_mem_file(
        os.path.join(_HERE, "..", "src", "rom_baked.mem"))
    core_v = os.path.join(build_dir, "b8008_net_core.v")

    platform = _FakePlatform()
    top = BenchTop(platform, rom_init=rom_init, core_v=core_v)

    # migen emits the ROM contents to a sibling "rom.init" ($readmemh) using a
    # bare relative path, so both bench_core.v and rom.init must land in build/
    # and the Verilator binary must run from there. chdir keeps them together.
    os.chdir(build_dir)
    convert(top, ios=top.ios(), name="bench_core").write("bench_core.v")
    print(f"wrote {os.path.join(build_dir, 'bench_core.v')} (+ rom.init)")


if __name__ == "__main__":
    main()
