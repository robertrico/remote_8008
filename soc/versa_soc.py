# versa_soc.py -- the b8008_net SoC target (Task 7). Builds a Versa-ECP5
# bitstream that pairs a minimal VexRiscv + Etherbone/ethmac hybrid network
# stack with the GHDL-converted Intel-8008 monitor core (B8008Core). The
# RISC-V runs firmware (Task 8) that DHCP-leases an IP and pushes it into the
# eb_ip CSR, which retargets the Etherbone UDP/IP core at runtime.
#
# The comment block below is the preserved Task 2 spike result: the exact
# invocation that gets a CSR-driven (runtime-settable) IP address onto the
# Etherbone UDP/IP core in hybrid mode (with_ethmac=True), confirmed both
# ELABORATED OK and Verilog generation OK against litex/liteeth/litex_boards
# pinned tag 2026.04. add_etherbone_dynamic_ip() below is the reconstruction.
#
# ── Background ──────────────────────────────────────────────────────────
# The stock helper `SoCCore.add_etherbone(...)` cannot take a Migen Signal
# for `ip_address` when `with_ethmac=True` (which this design requires --
# hybrid mode, so the RISC-V CPU also gets a MAC/network stack alongside
# Etherbone). litex/soc/integration/soc.py add_etherbone() (~line 2388 at
# this tag) unconditionally runs:
#     if ip_address == ethmac_local_ip:
# whenever with_ethmac=True. ip_address as a Signal and ethmac_local_ip as a
# string makes Migen's Signal.__eq__ try to wrap the string into a Migen
# _Operator operand, which raises immediately:
#     TypeError: Object '0.0.0.0' of type <class 'str'> is not a Migen value
# This is NOT a runtime/simulation issue -- it happens at Python
# elaboration time, unconditionally, every time. There is no kwarg that
# avoids it; add_etherbone() must be hand-rolled with that one check
# skipped (or guarded) when ip_address is a Migen Value. Everything else in
# add_etherbone() -- the with_ethmac branch, ethmac CSR/bus wiring, IRQ,
# timing constraints -- works unmodified with a Signal ip_address; the
# `LiteEthUDPIPCore(ip_address=<Signal>)` call underneath just passes the
# Signal straight through `convert_ip()` (liteeth/common.py), which only
# special-cases `str` input and returns anything else (Signal, int)
# untouched.
#
# ── Working invocation (proven in spike_dynamic_ip.py, now deleted) ─────
#
#   from migen import Mux, C
#   from migen.fhdl.structure import _Value as MigenValue
#   from litex.soc.interconnect.csr import CSRStorage
#   from litex.gen import LiteXModule
#
#   class _EbIP(LiteXModule):        # must be a Module (LiteXModule =
#       def __init__(self):          # Module+AutoCSR+AutoDoc), NOT bare
#           self.ip = CSRStorage(32, reset=0)   # AutoCSR -- see note below
#
#   soc.submodules.eb_ip = _EbIP()
#   soc.submodules.ethphy = LiteEthPHYRGMII(*phy_pads, tx_delay=0e-9)
#
#   add_etherbone_dynamic_ip(soc,                 # hand-rolled, see below
#       phy               = soc.ethphy,
#       mac_address       = 0x10e2d5000001,
#       # CSR-driven IP, with REQUIRED default-address gating (see the
#       # "ARP gating" section below for why the bare CSR is not safe):
#       # while the CSR still holds its reset value 0, park the core on
#       # 240.0.0.1 (reserved class-E space, never ARP-queried on a real
#       # LAN) so it stays silent until firmware writes the real address.
#       ip_address        = Mux(soc.eb_ip.ip.storage == 0,
#                               C(0xF0000001, 32),          # 240.0.0.1
#                               soc.eb_ip.ip.storage),
#       udp_port          = 1234,
#       buffer_depth      = 255,      # REQUIRED: default (16) overflows on
#                                     # RemoteClient's 255-word write bursts
#                                     # (liteeth/frontend/etherbone.py
#                                     # asserts <= 256)
#       with_ethmac       = True,
#       ethmac_address    = 0x10e2d5000002,
#       ethmac_local_ip   = "0.0.0.0",   # ethmac's own IP stays a build-time
#       ethmac_remote_ip  = "0.0.0.0")   # constant; only etherbone's IP is dynamic
#
#   soc.finalize()                                          # -> no error
#   Builder(soc, output_dir=..., compile_software=False).build(run=False)
#   # -> build/.../gateware/*.v written, contains eb_ip_storage wired into
#   #    the ARP/IP core (see "internals worth knowing" below)
#
# (The spike itself proved elaboration + Verilog generation with the bare
# `ip_address=soc.eb_ip.ip.storage` Signal; the Mux wrapper above is the
# same _Value pathway -- Mux/C are Migen _Value subclasses, and liteeth's
# convert_ip() passes any non-str through untouched -- added as a REQUIRED
# fix after review, because the bare CSR at reset would answer ARP queries
# for 0.0.0.0, which the spec forbids.)
#
# `add_etherbone_dynamic_ip()` is `SoCCore.add_etherbone()` copied verbatim
# from litex/soc/integration/soc.py lines 2290-2430 (tag 2026.04 -- Task 7
# reconstructs the copy from that exact source range; the spike's copy was
# never committed), with only the with_ethmac duplicate-IP check changed
# from:
#     if ip_address == ethmac_local_ip:
# to:
#     from migen.fhdl.structure import _Value as MigenValue
#     ...
#     if not isinstance(ip_address, MigenValue) and \
#             ip_address == ethmac_local_ip:
# (`_Value` is Signal's -- and Mux's -- base class; it is not exported from
# the top-level `migen` namespace, hence the explicit submodule import.)
# i.e. the static duplicate-address check is skipped when ip_address is a
# Migen value -- collision between the CSR value and ethmac_local_ip
# becomes a runtime property, not something provable at elaboration time.
#
# ── Two other gaps found while iterating (not about the Signal at all) ──
# 1. `_EbIP(AutoCSR)` (bare AutoCSR, per the brief's Step-1 snippet) is not
#    a Migen Module. `soc.submodules.eb_ip = _EbIP()` elaborates fine but
#    `soc.finalize()` then throws AttributeError: '_EbIP' object has no
#    attribute 'get_fragment_called' inside Module._collect_submodules().
#    Fix: subclass `litex.gen.LiteXModule` (= Module + AutoCSR + AutoDoc),
#    which is what every other module in this codebase already does.
# 2. `versa.BaseSoC(..., with_ethernet=False, with_etherbone=False)` alone
#    (no other kwargs) leaves `integrated_rom_size=0` (SoCCore's default),
#    so there's no Region at the CPU's reset address 0x0 and
#    `soc.finalize()` raises "CPU needs reset address 0x00000000 to be in a
#    defined Region." Needs `integrated_rom_size=0x8000` (or larger --
#    0x8000 was too small once ethmac/sdram/etc. drivers got linked into
#    the BIOS; that's a linker-script sizing problem for whoever builds the
#    real BIOS/firmware in a later task, not an elaboration blocker since
#    the spike used `compile_software=False`).
#
# ── liteeth internals worth knowing (ARP gating: REQUIRED for Task 7) ────
# Grepping the generated Verilog for `eb_ip_storage` (the CSR output) shows
# it reaches the core in two structurally different ways:
#   - ARP RX match (liteeth/core/arp.py:~127, LiteEthARPRX): the accept
#     condition is a plain, UNCONDITIONAL AND-reduction ending in
#         ... & (target_ip == eb_ip_storage)
#     and the ARP table FSM then fires SEND_REPLY unconditionally on any
#     accepted request. NOTHING in liteeth gates this on the address being
#     valid/nonzero: with the CSR at its reset value of 0 (0.0.0.0), an
#     inbound ARP query for 0.0.0.0 WOULD match and the board WOULD answer
#     it. The spec forbids answering ARP for the default address, so bare
#     `ip_address=eb_ip.ip.storage` is NOT acceptable for the real SoC.
#     REQUIRED mechanism (recorded in the invocation above, no liteeth
#     patching needed since Task 7 hand-copies the add_etherbone body
#     anyway): feed the UDP/IP core
#         Mux(eb_ip.ip.storage == 0, C(0xF0000001, 32), eb_ip.ip.storage)
#     240.0.0.1 is reserved class-E space that is never ARP-queried on a
#     real LAN, so the core stays silent until the CSR is written; the
#     moment firmware writes a real address, the Mux switches over and ARP
#     answers for it. Stricter alternative for anyone patching liteeth
#     directly instead: qualify LiteEthARPRX's accept condition with
#     `(ip_address != 0)` so a zero address can never match at all.
#   - IP RX match (liteeth/core/ip.py, `with_ip_broadcast=True`, the
#     add_etherbone default we kept): the equivalent target-IP check is
#     ORed with a hardwired constant 1:
#         ((target_ip == eb_ip_storage) | 1'd1) & (version==4) & (ihl==5) & ...
#     i.e. with_ip_broadcast makes the IP layer accept every IPv4 packet
#     regardless of destination address -- it never gates on eb_ip_storage
#     at all, dynamic or otherwise. This means a peer that has already
#     resolved our MAC (e.g. a stale/static ARP entry, or by sniffing) could
#     reach the UDP/Etherbone layer even before the IP CSR is set. ARP is
#     therefore the only real gate on "is this device reachable yet"; if
#     Task 7 wants the IP layer itself to also gate strictly, pass
#     `with_ip_broadcast=False` through to `add_etherbone_dynamic_ip()`.
#   - ARP TX (gratuitous ARP / replies) and IP TX (packetizer's source-IP
#     field) both read `eb_ip_storage` directly as their outgoing
#     sender/source IP, so once firmware writes the CSR, replies and
#     transmitted packets immediately carry the new address -- no
#     additional plumbing needed.

# ============================================================================
# Implementation (Task 7)
# ============================================================================
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# The litex / liteeth / migen / litex_boards *clones* live at the repo root,
# not next to this file (this script is in soc/). setuptools' editable-install
# finder runs AFTER PathFinder, so any sys.path entry that contains a
# `litex/` (etc.) directory shadows the editable install as a bare namespace
# package (ImportError: cannot import name 'get_data_mod'). Running
# `python versa_soc.py` puts this dir on sys.path[0]; since this dir doesn't
# hold those clones the drop below is a no-op today, but it's kept as a guard
# in case that ever changes. Drop it before importing litex, then re-append
# for the local imports.
_drop = [p for p in sys.path if os.path.abspath(p or os.getcwd()) == _HERE]
sys.path = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]

from migen import (
    Signal, ClockDomain, ClockSignal, ResetSignal, ClockDomainsRenamer,
    Instance, If, Mux, C,
)
from migen.fhdl.structure import _Value as MigenValue
from migen.genlib.resetsync import AsyncResetSynchronizer

from litex.gen import LiteXModule, colorer

from litex.build.generic_platform import Subsignal, Pins, IOStandard
from litex_boards.platforms import lattice_versa_ecp5

from litex.soc.cores.clock import ECP5PLL
from liteeth.phy.ecp5rgmii import LiteEthPHYRGMII
from litex.soc.integration.soc import (
    SoCError, SoCRegion, add_ip_address_constants, add_mac_address_constants,
)
from litex.soc.integration.soc_core import SoCCore
from litex.soc.integration.builder import Builder
from litex.soc.interconnect.csr import CSRStorage

# litex/liteeth/migen now resolved and cached; safe to expose local modules.
sys.path.append(_HERE)
from b8008_integration import B8008Core, load_mem_file


# ── add_etherbone_dynamic_ip ────────────────────────────────────────────────
# SoCCore.add_etherbone() copied verbatim from litex/soc/integration/soc.py
# lines 2290-2431 (tag 2026.04), re-homed as a free function taking `soc` in
# place of `self`, with exactly ONE behavioural change: the with_ethmac
# duplicate-IP guard is skipped when ip_address is a Migen value (a Signal /
# Mux), because a CSR-vs-constant collision is only knowable at runtime, not at
# elaboration time. See the comment block above for the full rationale.
def add_etherbone_dynamic_ip(soc, name="etherbone", phy=None, phy_cd=None, data_width=8,
    mac_address             = 0x10e2d5000000,
    ip_address              = "192.168.1.50",
    arp_entries             = 1,
    udp_port                = 1234,
    buffer_depth            = 16,
    with_ip_broadcast       = True,
    with_timing_constraints = True,
    with_ethmac             = False,
    ethmac_address          = 0x10e2d5000001,
    ethmac_local_ip         = "192.168.1.51",
    ethmac_remote_ip        = "192.168.1.100",
    with_igmp               = False,
    igmp_groups             = None,
    igmp_interval           = 10):
    if phy is None:
        soc.logger.error("Etherbone requires {}.".format(
            colorer("phy", color="red")))
        raise SoCError()

    # Imports
    from liteeth.core import LiteEthUDPIPCore
    from liteeth.frontend.etherbone import LiteEthEtherbone
    from liteeth.phy.model import LiteEthPHYModel

    # Core
    if data_width not in [8, 32, 64]:
        soc.logger.error("Etherbone {} {}: must be 8, 32 or 64.".format(
            colorer("data_width"), colorer(data_width, color="red")))
        raise SoCError()
    with_sys_datapath = (data_width == 32)
    soc.check_if_exists(name + "_ethcore")
    ethcore = LiteEthUDPIPCore(
        phy         = phy,
        mac_address = mac_address,
        ip_address  = ip_address,
        clk_freq    = soc.clk_freq,
        arp_entries = arp_entries,
        dw          = data_width,
        with_ip_broadcast = with_ip_broadcast,
        with_sys_datapath = with_sys_datapath,
        with_igmp     = with_igmp,
        igmp_groups   = igmp_groups,
        igmp_interval = igmp_interval,
        interface   = {True :            "hybrid", False: "crossbar"}[with_ethmac],
        endianness  = {True : soc.cpu.endianness, False:      "big"}[with_ethmac],
    )
    if not with_sys_datapath:
        # Use PHY's eth_tx/eth_rx clock domains.
        if phy_cd is None:
            eth_tx_clk_name = getattr(phy, "crg", phy).cd_eth_tx.name
            eth_rx_clk_name = getattr(phy, "crg", phy).cd_eth_rx.name
        else:
            eth_tx_clk_name = phy_cd + "_tx"
            eth_rx_clk_name = phy_cd + "_rx"
        ethcore_cd = {True: "sys", False: eth_rx_clk_name}[with_ethmac]
        ethcore = ClockDomainsRenamer({
            "eth_tx": eth_tx_clk_name,
            "eth_rx": eth_rx_clk_name,
            "sys"   : ethcore_cd,
        })(ethcore)
    else:
        ethcore_cd = "sys"
    ethcore.cd = ethcore_cd
    soc.add_module(name=f"ethcore_{name}", module=ethcore)

    etherbone_cd = "sys"
    if not with_sys_datapath:
        # Create Etherbone clock domain and run it from sys clock domain.
        etherbone_cd = name
        setattr(soc, f"cd_{name}", ClockDomain(name))
        soc.comb += getattr(soc, f"cd_{name}").clk.eq(ClockSignal("sys"))
        soc.comb += getattr(soc, f"cd_{name}").rst.eq(ResetSignal("sys"))

    # Etherbone
    soc.check_if_exists(name)
    etherbone = LiteEthEtherbone(ethcore.udp, udp_port, buffer_depth=buffer_depth, cd=etherbone_cd)
    soc.add_module(name=name, module=etherbone)
    soc.bus.add_master(name=name, master=etherbone.wishbone.bus)

    # Timing constraints
    if with_timing_constraints:
        eth_rx_clk = getattr(phy, "crg", phy).cd_eth_rx.clk
        eth_tx_clk = getattr(phy, "crg", phy).cd_eth_tx.clk
        if not isinstance(phy, LiteEthPHYModel) and not getattr(phy, "model", False):
            soc.platform.add_period_constraint(eth_rx_clk, 1e9/phy.rx_clk_freq)
            if not eth_rx_clk is eth_tx_clk:
                soc.platform.add_period_constraint(eth_tx_clk, 1e9/phy.tx_clk_freq)
                soc.platform.add_false_path_constraints(soc.crg.cd_sys.clk, eth_rx_clk, eth_tx_clk)
            else:
                soc.platform.add_false_path_constraints(soc.crg.cd_sys.clk, eth_rx_clk)

    # Ethernet MAC (CPU).
    if with_ethmac:
        if mac_address == ethmac_address:
            soc.logger.error("Etherbone {} and {} must differ.".format(
                colorer("mac_address"), colorer("ethmac_address", color="red")))
            raise SoCError()
        # NOTE (Task 7 deviation from stock add_etherbone): the stock body runs
        # `if ip_address == ethmac_local_ip:` unconditionally. With ip_address a
        # Migen Mux/Signal, Signal.__eq__ builds a comparison operand against a
        # str and raises TypeError at elaboration. A dynamic-vs-static-IP
        # collision is a runtime property, so we only run the guard for a
        # build-time (str/int) ip_address.
        if not isinstance(ip_address, MigenValue) and ip_address == ethmac_local_ip:
            soc.logger.error("Etherbone {} and {} must differ.".format(
                colorer("ip_address"), colorer("ethmac_local_ip", color="red")))
            raise SoCError()

        soc.check_if_exists("ethmac")
        ethcore.autocsr_exclude = {"mac"}
        # Software Interface.
        soc.ethmac = ethmac = ethcore.mac
        ethmac_rx_region_size = ethmac.rx_slots.constant*ethmac.slot_size.constant
        ethmac_tx_region_size = ethmac.tx_slots.constant*ethmac.slot_size.constant
        ethmac_region_size    = ethmac_rx_region_size + ethmac_tx_region_size
        soc.bus.add_region("ethmac", SoCRegion(
            origin = soc.mem_map.get("ethmac", None),
            size   = ethmac_region_size,
            linker = True,
            cached = False,
        ))
        ethmac_rx_region = SoCRegion(
            origin = soc.bus.regions["ethmac"].origin + 0,
            size   = ethmac_rx_region_size,
            mode   = "r",
            linker = False,
            cached = False,
        )
        soc.bus.add_slave(name=f"ethmac_rx", slave=ethmac.bus_rx, region=ethmac_rx_region)
        ethmac_tx_region = SoCRegion(
            origin = soc.bus.regions["ethmac"].origin + ethmac_rx_region_size,
            size   = ethmac_tx_region_size,
            linker = False,
            cached = False,
        )
        soc.bus.add_slave(name=f"ethmac_tx", slave=ethmac.bus_tx, region=ethmac_tx_region)

        # Add IRQs (if enabled).
        if soc.irq.enabled:
            soc.irq.add("ethmac", use_loc_if_exists=True)

        soc.add_constant("ETH_PHY_NO_RESET") # Disable reset from BIOS to avoid disabling Hardware Interface.

        add_ip_address_constants(soc,  "LOCALIP",  ethmac_local_ip)
        add_ip_address_constants(soc,  "REMOTEIP", ethmac_remote_ip)
        add_mac_address_constants(soc, "MACADDR",  ethmac_address)


# ── CSR-driven Etherbone IP register ────────────────────────────────────────
# LiteXModule (= Module + AutoCSR + AutoDoc), NOT a bare AutoCSR -- see the
# Task 2 note above: a bare AutoCSR is not a Migen Module and finalize() throws.
class _EbIP(LiteXModule):
    def __init__(self):
        self.ip = CSRStorage(32, reset=0, name="ip")


# ── b8008 debug bus pin-out (X3 expansion) ──────────────────────────────────
# Sites copied verbatim from projects/b8008_monitor/constraints/b8008_monitor.lpf
# (the serial monitor's logic-analyzer header). All LVCMOS33.
_b8008_dbg_io = [
    ("b8008_dbg", 0,
        Subsignal("d",    Pins("E9 D9 B8 C8 D8 E8 C7 C6")),  # cpu_d[0..7]
        Subsignal("s0",   Pins("D7")),
        Subsignal("s1",   Pins("B11")),
        Subsignal("s2",   Pins("B6")),
        Subsignal("sync", Pins("E7")),
        Subsignal("phi1", Pins("E6")),
        Subsignal("phi2", Pins("D6")),
        Subsignal("int",  Pins("B9")),
        IOStandard("LVCMOS33")),
]


# ── CRG ─────────────────────────────────────────────────────────────────────
# Stock lattice_versa_ecp5 _CRG shape (sys via ECLKSYNCB/CLKDIVF at the 75 MHz
# stock default), extended with a 25 MHz cd_b8008 for the Intel-8008 core. The
# DDR-only cd_init is dropped (no SDRAM in this target) to keep the PLL to two
# outputs (sys2x_i + b8008).
class _CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq):
        self.rst        = Signal()
        self.cd_por     = ClockDomain()
        self.cd_sys     = ClockDomain()
        self.cd_sys2x   = ClockDomain()
        self.cd_sys2x_i = ClockDomain()
        self.cd_b8008   = ClockDomain()

        # # #

        self.stop  = Signal()
        self.reset = Signal()

        # Clk / Rst
        clk100 = platform.request("clk100")
        rst_n  = platform.request("rst_n")

        # Power on reset
        por_count = Signal(16, reset=2**16-1)
        por_done  = Signal()
        self.comb += self.cd_por.clk.eq(clk100)
        self.comb += por_done.eq(por_count == 0)
        self.sync.por += If(~por_done, por_count.eq(por_count - 1))

        # PLL
        self.pll = pll = ECP5PLL()
        self.comb += pll.reset.eq(~por_done | ~rst_n | self.rst)
        pll.register_clkin(clk100, 100e6)
        pll.create_clkout(self.cd_sys2x_i, 2*sys_clk_freq)
        pll.create_clkout(self.cd_b8008, 25e6)
        self.specials += [
            Instance("ECLKSYNCB",
                i_ECLKI = self.cd_sys2x_i.clk,
                i_STOP  = self.stop,
                o_ECLKO = self.cd_sys2x.clk),
            Instance("CLKDIVF",
                p_DIV     = "2.0",
                i_ALIGNWD = 0,
                i_CLKI    = self.cd_sys2x.clk,
                i_RST     = self.reset,
                o_CDIVX   = self.cd_sys.clk),
            AsyncResetSynchronizer(self.cd_sys, ~pll.locked | self.reset),
        ]


# ── BaseSoC ─────────────────────────────────────────────────────────────────
class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=75e6, device="LFE5UM5G", toolchain="trellis", **kwargs):
        platform = lattice_versa_ecp5.Platform(toolchain=toolchain, device=device)
        platform.add_extension(_b8008_dbg_io)

        # CRG --------------------------------------------------------------------------------------
        self.crg = _CRG(platform, sys_clk_freq)

        # SoCCore ----------------------------------------------------------------------------------
        # Force the b8008_net SoC identity. These override any CLI-provided
        # defaults carried in soc_argdict (which already contains cpu_type,
        # integrated_rom_size, uart_name, ... -- passing them again positionally
        # would raise "multiple values for keyword argument").
        kwargs.update(dict(
            cpu_type             = "vexriscv",
            cpu_variant          = "minimal",
            # Task 7 note for Task 8: the etherbone+ethmac BIOS overflows the
            # brief's 0x8000 ROM (`.rodata will not fit in region rom` at link),
            # and ROM auto-size can't measure a BIOS that fails to link -- so the
            # initial size must already hold it. 0x10000 (64KB) links clean.
            # Firmware/integrated_rom_init in Task 8 can revisit this.
            integrated_rom_size  = 0x10000,
            integrated_sram_size = 0x2000,
            uart_name            = "stub",
            ident                = "b8008_net",
            ident_version        = True,
        ))
        SoCCore.__init__(self, platform, sys_clk_freq, **kwargs)

        # Ethernet PHY + Etherbone/ethmac hybrid stack ---------------------------------------------
        self.ethphy = LiteEthPHYRGMII(
            clock_pads = platform.request("eth_clocks", 0),
            pads       = platform.request("eth", 0),
            tx_delay   = 0e-9,
            rx_delay   = 0e-9)

        # CSR-driven, ARP-gated Etherbone IP (parks on class-E 240.0.0.1 until
        # firmware writes the real address into eb_ip -- see the top comment).
        self.eb_ip = _EbIP()
        add_etherbone_dynamic_ip(self,
            phy              = self.ethphy,
            mac_address      = 0x10e2d5000001,
            ip_address       = Mux(self.eb_ip.ip.storage == 0,
                                   C(0xF0000001, 32),   # 240.0.0.1
                                   self.eb_ip.ip.storage),
            udp_port         = 1234,
            buffer_depth     = 255,          # REQUIRED: default (16) overflows on 255-word bursts
            with_ip_broadcast= False,        # stricter: IP layer does not blanket-accept every IPv4 frame
            with_ethmac      = True,
            ethmac_address   = 0x10e2d5000002,
            ethmac_local_ip  = "0.0.0.0",
            ethmac_remote_ip = "0.0.0.0")

        # Intel-8008 monitor core ------------------------------------------------------------------
        # Repo-root build/ (same convention as NETLIST_V/GHDL_GATES/VERSA_DIR in
        # the Makefile, and the same _HERE-relative fix applied in bench_core.py's
        # main()) -- not soc/build/. _HERE is soc/ post-move, so this must go up
        # one level to land where `make convert` (NETLIST_V := build/b8008_net_core.v)
        # actually writes the netlist.
        self.b8008 = B8008Core(platform,
            sys_clk_freq = sys_clk_freq,
            core_v       = os.path.join(_HERE, "..", "build", "b8008_net_core.v"),
            # regenerated by `make rom-freeze` in the core repo's projects/b8008_monitor
            rom_init     = load_mem_file(os.path.join(_HERE, "..", "src", "rom_baked.mem")))
        # SPEC.md S-PROD-8 (D-10): no host-facing wishbone window onto the
        # 8008's RAM. B8008Core exposes no bus_ram any more (Task 7).

        # Route the b8008 debug bus to the X3 expansion pads.
        dbg = platform.request("b8008_dbg")
        self.comb += [
            dbg.d.eq(self.b8008.dbg.d),
            dbg.s0.eq(self.b8008.dbg.s0),
            dbg.s1.eq(self.b8008.dbg.s1),
            dbg.s2.eq(self.b8008.dbg.s2),
            dbg.sync.eq(self.b8008.dbg.sync),
            dbg.phi1.eq(self.b8008.dbg.phi1),
            dbg.phi2.eq(self.b8008.dbg.phi2),
            dbg.int.eq(self.b8008.dbg.int),
        ]


# ── Build ───────────────────────────────────────────────────────────────────
def main():
    from litex.build.parser import LiteXArgumentParser
    parser = LiteXArgumentParser(platform=lattice_versa_ecp5.Platform,
        description="b8008_net SoC on Versa ECP5.")
    parser.add_target_argument("--sys-clk-freq", default=75e6, type=float, help="System clock frequency.")
    parser.add_target_argument("--device",       default="LFE5UM5G",       help="FPGA device (LFE5UM5G or LFE5UM).")
    args = parser.parse_args()

    # Task 8: `--integrated-rom-init firmware/build/firmware.bin` (a stock
    # LiteXArgumentParser flag from soc_core_args -- no target-specific CLI
    # plumbing needed here) replaces the BIOS in integrated ROM with the
    # b8008_net firmware. Optional and off by default: SoCCore's own
    # integrated_rom_init default is None, which still compiles/loads the
    # BIOS as before, so a plain `make build` with no firmware built keeps
    # working. When given, it also overrides BaseSoC's hardcoded
    # integrated_rom_size=0x10000 below -- SoCCore sizes the ROM to the
    # actual firmware image once integrated_rom_init is a path
    # (soc_core.py: integrated_rom_size = len(data)*(bus_width//8)), so the
    # ROM shrinks to fit the firmware instead of staying at the
    # BIOS-sized 64KB.
    soc = BaseSoC(
        sys_clk_freq = args.sys_clk_freq,
        device       = args.device,
        toolchain    = args.toolchain,
        **parser.soc_argdict)
    builder = Builder(soc, **parser.builder_argdict)
    if args.build:
        # build_name is honoured only by soc.build() (Builder forwards **kwargs
        # to it); passing it to SoCCore does nothing. This names the gateware
        # artifacts versa_soc.* (the host-tool contract: versa_soc.bit).
        builder.build(build_name="versa_soc", **parser.toolchain_argdict)


if __name__ == "__main__":
    main()