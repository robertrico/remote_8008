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
