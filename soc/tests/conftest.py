"""Shared fixtures for the VPLAN conformance suite.

Every test in this package declares the VPLAN row(s) it discharges in its
docstring as `VPLAN: <ID>`. test_vplan_coverage.py enforces that.
"""
import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.abspath(os.path.join(_HERE, ".."))
_ROOT = os.path.abspath(os.path.join(_SOC, ".."))

# soc/ is not a package; B8008Core is imported by module name, as
# soc/test_integration.py already does.
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

# soc/tests/__init__.py makes pytest import this file as `tests.conftest`
# (rootdir-relative package import), so sibling test modules' plain
# `from conftest import ...` needs this directory on sys.path too.
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# `make convert` writes the netlist to the repo-root build/ dir (see
# Makefile's NETLIST_V and versa_soc.py's _HERE-relative core_v), not
# soc/build/ -- go up one more level than soc/test_integration.py's (buggy,
# untouched) _CORE_V does.
CORE_V = os.path.join(_ROOT, "build", "b8008_net_core.v")
VPLAN_MD = os.path.join(_ROOT, "docs", "VPLAN.md")

VPLAN_RE = re.compile(r"VPLAN:\s*([A-Z0-9\-, ]+)")

needs_netlist = pytest.mark.skipif(
    not os.path.exists(CORE_V),
    reason="build/b8008_net_core.v missing -- run `make convert` first",
)


def _fake_platform():
    from litex.build.generic_platform import Pins
    from litex.build.lattice import LatticeECP5Platform

    class P(LatticeECP5Platform):
        default_clk_name = "clk"

        def __init__(self):
            super().__init__(
                "LFE5UM5G-45F-8BG381C",
                [("clk", 0, Pins("P3"))],
                toolchain="trellis",
            )

    return P()


@pytest.fixture
def core_verilog():
    """Elaborate B8008Core and return its converted Verilog as a string."""
    from migen import ClockDomain
    from migen.fhdl.verilog import convert
    from litex.gen import LiteXModule

    from b8008_integration import B8008Core

    class Top(LiteXModule):
        def __init__(self, platform):
            self.cd_sys = ClockDomain("sys")
            self.cd_b8008 = ClockDomain("b8008")
            self.core = B8008Core(
                platform, sys_clk_freq=75e6, core_v=CORE_V, rom_init=[0] * 4096
            )

    return str(convert(Top(_fake_platform()), ios=set()))


@pytest.fixture
def core_module():
    """A finalized B8008Core instance, for inspecting its CSRs and submodules."""
    from b8008_integration import B8008Core

    return B8008Core(
        _fake_platform(), sys_clk_freq=75e6, core_v=CORE_V, rom_init=[0] * 4096
    )
