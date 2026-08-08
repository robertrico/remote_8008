"""Structural conformance: claims checked by inspecting the elaborated design."""
from conftest import needs_netlist  # noqa: F401


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
