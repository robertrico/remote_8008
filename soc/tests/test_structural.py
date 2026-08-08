"""Structural conformance: claims checked by inspecting the elaborated design."""
import re

from conftest import CORE_V, needs_netlist  # noqa: F401


@needs_netlist
def test_no_pulse_synchronizers(core_verilog):
    """No PulseSynchronizer exists; the retired ctl pulses are gone.

    VPLAN: CDC-5
    """
    assert "pulsesynchronizer" not in core_verilog.lower()


@needs_netlist
def test_no_retired_control_csrs(core_module):
    """No ctl/status CSR and no retired control fields exist.

    VPLAN: STR-1, STR-2
    """
    names = {csr.name for csr in core_module.get_csrs()}
    retired = {"ctl", "status", "rxtx", "rxlevel", "txfull", "rxempty"}
    assert not (names & retired), f"retired CSRs still present: {names & retired}"


@needs_netlist
def test_no_host_ram_window(core_module):
    """B8008Core exposes no wishbone RAM window to the host.

    VPLAN: STR-3
    """
    assert not hasattr(core_module, "bus_ram")


@needs_netlist
def test_console_bank_has_exactly_six_registers(core_module):
    """The console bank contains exactly the six specified registers.

    VPLAN: CSR-13
    """
    expected = {
        "console_rx", "console_rx_pop", "console_tx",
        "console_tx_data", "console_err", "console_err_clear",
    }
    got = {csr.name for csr in core_module.console.get_csrs()}
    assert got == expected, f"extra={got - expected} missing={expected - got}"


@needs_netlist
def test_stall_crosses_into_b8008_domain(core_verilog):
    """The stall reaches the core through a synchronizer, not combinationally.

    VPLAN: CDC-2
    """
    assert "ext_hold" in core_verilog


@needs_netlist
def test_all_core_input_ports_are_connected(core_verilog):
    """Every input port `b8008_net_core` declares is wired by the Migen
    Instance() -- not merely present in the source.

    Task 7 fix round 1 finding: `ctl_run_stop`/`ctl_step_cycle`/
    `ctl_step_sync`/`ctl_int`/`ctl_int_vector` were dropped from the
    Instance() call while still declared as VHDL entity inputs with no
    default value. migen.fhdl.specials.Instance.emit_verilog only emits a
    port association for kwargs actually passed, so an omitted input isn't
    tied off -- it is left completely unconnected at the netlist boundary.
    `ctl_run_stop` fed the run/stop toggle and `ctl_int` fed live interrupt
    injection, so this was a silent hazard, not a cosmetic one, and nothing
    in the rest of this suite (all of which only inspects CSR/register-level
    Python state or greps for specific substrings) would have caught it.

    This test reads the module's actual port declaration straight out of
    the converted netlist (`build/b8008_net_core.v`, not just what the
    wrapper *intends* to connect) and cross-checks it against the
    instantiation Migen actually emits, so a future edit that drops a
    connection -- for any port, not just the ones retired here -- fails
    here instead of reaching place-and-route with a floating net.

    Declared VPLAN: CDC-1 rather than STR-6: STR-6 asserts something
    different (no *multi-bit bus* crosses the domain boundary at all).
    CDC-1's assertion -- that the crossings into b8008_net_core are exactly
    the inventoried set (X1/X2/X3) and nothing else -- is the row this test
    actually backs: an unconnected input is either an undocumented fourth
    crossing (a floating net masquerading as a signal) or a broken X1/X2/X3,
    both of which a complete connection inventory rules out.

    VPLAN: CDC-1
    """
    with open(CORE_V) as f:
        netlist = f.read()

    mod = re.search(r"module\s+b8008_net_core\s*\((.*?)\);", netlist, re.S)
    assert mod, "could not find b8008_net_core's module port declaration"
    declared_inputs = set(re.findall(
        r"\binput\s+(?:\[[^\]]+\]\s+)?(\w+)", mod.group(1)))
    assert declared_inputs, "found no declared input ports to check"

    inst = re.search(r"\bb8008_net_core\s+\S+\s*\((.*?)\);", core_verilog, re.S)
    assert inst, "no b8008_net_core instantiation found in the elaborated top"
    connected = set(re.findall(r"\.(\w+)\(", inst.group(1)))

    missing = declared_inputs - connected
    assert not missing, f"declared input ports left unconnected by Instance(): {missing}"


# ── Task 8: reset synchronization and ordering (S-RST-4, S-RST-6) ──────────
#
# These tests elaborate the real `_CRG` from versa_soc.py (not a stand-in),
# so they exercise the same code path `make build` does. Importing
# versa_soc.py pulls in `litex.soc.integration.builder`, which needs a real
# `litex` package (not the vendored repo-root clone directory shadowing it
# as a namespace package) already resolved -- see conftest.py's `_ROOT`
# strip, which this relies on and which must run before this module's first
# `import litex` anywhere in the session, not just before `_crg()`.
def _crg():
    """Elaborate the real Versa-ECP5 `_CRG`, for inspecting its reset wiring."""
    import os
    import sys

    soc_dir = os.path.join(os.path.dirname(__file__), "..")
    if soc_dir not in sys.path:
        sys.path.insert(0, soc_dir)
    from versa_soc import _CRG
    from litex_boards.platforms import lattice_versa_ecp5

    platform = lattice_versa_ecp5.Platform(device="LFE5UM5G", toolchain="trellis")
    return _CRG(platform, 75e6)


def _comb_driver(fragment, target_signal):
    """The RHS expression of the `fragment.comb` assignment whose target is
    `target_signal`, or None. `Module.specials`/`.comb` are proxy objects
    (migen.fhdl.module._ModuleSpecials/_ModuleComb) re-created on every
    attribute access and not themselves iterable -- the real, iterable
    storage a finalized module's contents end up in is `fragment.specials`
    (a set) and `fragment.comb` (a list), reached via `Module.get_fragment()`.
    """
    from migen.fhdl.structure import _Assign

    for stmt in fragment.comb:
        if isinstance(stmt, _Assign) and stmt.l is target_signal:
            return stmt.r
    return None


class _ResetSignalFinder:
    """Collects the clock-domain names of every `ResetSignal(...)` reachable
    inside a Migen expression tree.

    `migen.fhdl.tools.list_signals` only collects plain `Signal` nodes;
    `ResetSignal` is a distinct `_Value` subclass (it isn't resolved to a
    concrete per-domain Signal until a later clock-domain-lowering pass), so
    it is invisible to `list_signals` and needs its own tiny walk.
    """

    def __init__(self):
        self.domains = set()

    def visit(self, node):
        from migen.fhdl.structure import ResetSignal, _Operator, _Slice, _Part, Cat, Replicate

        if isinstance(node, ResetSignal):
            self.domains.add(node.cd)
        elif isinstance(node, _Operator):
            for o in node.operands:
                self.visit(o)
        elif isinstance(node, Cat):
            for o in node.l:
                self.visit(o)
        elif isinstance(node, _Slice):
            self.visit(node.value)
        elif isinstance(node, _Part):
            self.visit(node.value)
            self.visit(node.offset)
        elif isinstance(node, Replicate):
            self.visit(node.v)


def test_b8008_reset_is_synchronized():
    """cd_b8008 has exactly one AsyncResetSynchronizer, and it is genuinely
    gated on the same base condition as cd_sys's (~pll.locked | reset) --
    not merely present under some attribute name.

    A bare "does *a* synchronizer exist for cd_b8008" check would have
    passed on unmodified versa_soc.py for the wrong reason:
    `ECP5PLL.create_clkout(self.cd_b8008, 25e6)` attaches its *own*
    `AsyncResetSynchronizer(cd_b8008, ~pll.locked)` by default
    (litex/soc/cores/clock/common.py's connect_clkout(), with_reset=True),
    confirmed by converting the pre-fix CRG to Verilog: b8008_rst was
    already driven, just by a synchronizer missing the self.reset (POR/
    rst_n) term cd_sys's equivalent has, and with no ordering gate at all --
    SPEC.md S-RST-3's "cd_b8008 currently has no AsyncResetSynchronizer" is
    imprecise about *that*. The actual, spec-relevant defects were the
    missing self.reset term and the missing S-RST-6 ordering gate.
    Layering an explicit, correctly-gated synchronizer on *top of* that
    default (rather than replacing it) elaborates and converts to Verilog
    without complaint, but produces two separate FD1S3BX flip-flop pairs
    both driving the same b8008_rst net -- a multi-driver hazard invisible
    until synthesis. versa_soc.py disables the default (with_reset=False)
    so the explicit synchronizer is cd_b8008's sole driver; the count check
    below guards against that regressing.

    VPLAN: RST-2
    """
    from migen.fhdl.tools import list_signals
    from migen.genlib.resetsync import AsyncResetSynchronizer

    crg = _crg()
    fragment = crg.get_fragment()

    b8008_syncs = [
        s for s in fragment.specials
        if isinstance(s, AsyncResetSynchronizer) and s.cd is crg.cd_b8008
    ]
    assert len(b8008_syncs) == 1, (
        f"expected exactly one AsyncResetSynchronizer driving cd_b8008.rst, "
        f"found {len(b8008_syncs)} -- more than one is a multi-driver hazard "
        f"on the same net, not mere redundancy"
    )

    gate = _comb_driver(fragment, b8008_syncs[0].async_reset)
    assert gate is not None, (
        "cd_b8008's synchronizer input is not driven by any comb assignment "
        "this suite can find -- it may be wired to a constant"
    )
    fanin = list_signals(gate)
    assert crg.pll.locked in fanin, "cd_b8008 sync not gated on pll.locked"
    assert crg.reset in fanin, "cd_b8008 sync not gated on self.reset (POR/rst_n)"


def test_console_reset_precedes_core_reset():
    """cd_b8008's reset-release gate genuinely has ResetSignal("sys") in its
    fan-in -- not merely an attribute that exists (SPEC.md S-RST-6: the
    console must be able to accept a byte before the core can emit one, or
    the boot banner is lost).

    What this test does NOT establish: that the release is separated by
    >=1 cd_sys period (VPLAN RST-7's full statement), only that it cannot
    happen *before* cd_sys releases. Proving the >=1-cycle gap needs a
    cycle-accurate simulation of the real _CRG, and that is not reachable
    from Migen's simulator for two independent reasons, both confirmed
    directly rather than assumed:
      1. ECP5PLL's `locked` output comes from `Instance("EHXPLLL", ...)`, an
         opaque vendor primitive. Migen's simulator has no behavioral model
         for arbitrary Instance()s (migen/sim/core.py's own comment: "TODO:
         instances via Iverilog/VPI") -- lower_specials() would raise
         "Could not lower all specials" before simulation could start.
      2. Even given a stand-in for the PLL, Migen's Simulator's *default*
         override for AsyncResetSynchronizer -- DummyAsyncResetSynchronizer,
         migen/sim/core.py -- lowers it to a single combinational
         `cd.rst.eq(async_reset)`, not the real 2-FF synchronous-release
         chain (that only exists in the ECP5-specific lowering, e.g.
         FD1S3BX pairs, which is a synthesis-time transform, not something
         the plain simulator exercises). Under that default, cd_sys.rst and
         cd_b8008.rst would release in the same delta-cycle whenever the
         gate clears -- a simulation "pass" here would demonstrate ordering
         that is not what the real 2-FF-synchronized hardware actually does,
         which is worse than not simulating at all.
      3. This is also why VPLAN.md declares RST-7's method as `SBY` (an
         unbounded formal property), not `PYTEST` or a `COCOTB` testbench --
         the row was never expected to be discharged by a Python-level
         elaboration check. This test is static corroboration that the
         *structural* prerequisite for that property (ResetSignal("sys") is
         actually in the gate's fan-in) holds; it is not the property itself.

    VPLAN: RST-7
    """
    from migen.genlib.resetsync import AsyncResetSynchronizer

    crg = _crg()
    fragment = crg.get_fragment()

    b8008_syncs = [
        s for s in fragment.specials
        if isinstance(s, AsyncResetSynchronizer) and s.cd is crg.cd_b8008
    ]
    assert b8008_syncs, "no AsyncResetSynchronizer found for cd_b8008"

    gate = _comb_driver(fragment, b8008_syncs[0].async_reset)
    assert gate is not None, "cd_b8008's synchronizer input is not comb-driven"

    finder = _ResetSignalFinder()
    finder.visit(gate)
    assert "sys" in finder.domains, (
        f"ResetSignal(\"sys\") not found in cd_b8008's reset-gate fan-in "
        f"(found domains: {finder.domains}); cd_b8008 can release before "
        f"cd_sys does, which SPEC.md S-RST-6 forbids"
    )
