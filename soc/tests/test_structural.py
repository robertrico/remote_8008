"""Structural conformance: claims checked by inspecting the elaborated design."""
from conftest import needs_netlist  # noqa: F401


@needs_netlist
def test_no_pulse_synchronizers(core_verilog):
    """No PulseSynchronizer exists; the retired ctl pulses are gone.

    VPLAN: CDC-5
    """
    assert "pulsesynchronizer" not in core_verilog.lower()
