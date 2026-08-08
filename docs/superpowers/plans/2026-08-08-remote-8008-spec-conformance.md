# remote_8008 Spec Conformance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the gateware into conformance with `SPEC.md` by resolving all 12 recorded divergences, behind a regression suite that turns red before each fix and green after.

**Architecture:** Extract the console byte path out of `B8008Core` into a standalone `ConsoleBridge` module that lives entirely in `cd_sys` and contains no 8008. That single change makes the product's whole behavioral surface simulatable in milliseconds with Migen's own simulator — no netlist, no gate-level boot, no external simulator. `B8008Core` shrinks to: instantiate the netlist, instantiate a `ConsoleBridge`, and connect the three clock-domain crossings between them. Structural claims (register set, reset topology, crossing count) are checked by elaborating the design and inspecting it.

**Tech Stack:** Python 3.14, Migen, LiteX `2026.04` (pinned), `migen.sim.run_simulation`, pytest, VHDL-2008 (for `src/b8008_net_core.vhdl` only).

## Global Constraints

- LiteX is pinned to tag `2026.04`. Do not upgrade it, and do not import LiteX APIs that do not exist at that tag.
- All Python runs through `.venv/bin/python`. Never system `python3`.
- **`intel-8008-vhdl` is read-only.** `SPEC.md` `S-CORE-3` freezes the core's port list. `src/b8008_net_core.vhdl` belongs to *this* repo and may be modified; nothing under `~/Development/intel-8008-vhdl` may be.
- `SPEC.md` is authoritative over all RTL. Where existing code disagrees, the code is wrong.
- Every new test carries the VPLAN row ID(s) it discharges in its docstring, formatted exactly `VPLAN: <ID>[, <ID>...]`. Task 1 builds a checker that depends on this format.
- Etherbone `buffer_depth` stays at **255** (`S-WIRE-2`). Do not revert it to a LiteX default.
- Commit after every task. Never commit with a failing suite unless the task explicitly says the failure is the deliverable.

## Check-type decision, and a VPLAN amendment

`VPLAN.md` assigns `COCOTB-D`/`COCOTB-R` to the behavioral console rows. This plan uses **Migen's `run_simulation`** for every row whose device under test is `ConsoleBridge`, and amends the VPLAN accordingly in Task 1.

Reasoning: `ConsoleBridge` is pure `cd_sys` Migen. Migen sim runs it directly as Python — no Verilog conversion, no simulator install, milliseconds per test. It is also the idiom LiteX itself uses for exactly this kind of module (`litex/test/test_uart.py`, `litex/test/test_csr_bus.py`, both vendored in this repo). Adding cocotb here would mean converting to Verilog and installing a simulator to test logic that Migen can already execute.

**cocotb is not cancelled.** It remains the right tool for the rows this plan does not cover: anything involving the converted netlist, the `cd_b8008` domain, or real CDC phase relationships (`CDC-3`, `CDC-4`, `CDC-6`, `RST-3`, `RST-4`, `BP-7`, `BP-12`, `CLK-7`, `CLK-8`). Those, plus all 16 `SBY` rows and both `EQY` rows, are **out of scope for this plan** and get their own plan once this one is green. They need a formal/simulation toolchain stand-up that is a project in its own right, and none of them blocks the work here.

**Rows this plan turns green:** the 26 `PYTEST` rows, plus the `ConsoleBridge` behavioral rows RX-2..RX-7, RX-11, RX-12, RX-14, TX-2..TX-6, TX-8, BP-1, BP-2, BP-6, CSR-1, CSR-2, CSR-5, CSR-7..CSR-10, X-4, X-5. **Roughly 50 of 119.**

## File structure

| File | Responsibility |
|---|---|
| `soc/console_bridge.py` | **New.** `ConsoleBridge`: RS232 PHY, both FIFOs, all six console CSRs, sticky error logic, backpressure threshold logic. Entirely `cd_sys`. Knows nothing about the 8008. |
| `soc/b8008_integration.py` | **Shrinks.** `B8008Core`: instantiate the netlist, instantiate `ConsoleBridge`, wire X1/X2/X3. Retired CSRs and the RAM window are deleted. |
| `src/b8008_net_core.vhdl` | Gains one `ext_hold` input that gates `run_enable`. |
| `soc/versa_soc.py` | `cd_b8008` gains an `AsyncResetSynchronizer`; the build declares clock groups. |
| `soc/tests/__init__.py` | **New.** Makes `soc/tests` a package. |
| `soc/tests/conftest.py` | **New.** Shared elaboration fixtures. |
| `soc/tests/test_structural.py` | **New.** Rows checked by inspecting the elaborated design. |
| `soc/tests/test_console_rx.py` | **New.** RX path behavioral rows. |
| `soc/tests/test_console_tx.py` | **New.** TX path behavioral rows. |
| `soc/tests/test_console_err.py` | **New.** Sticky error and clear rows. |
| `soc/tests/test_backpressure.py` | **New.** Threshold and hysteresis rows. |
| `soc/tests/test_vplan_coverage.py` | **New.** Asserts every test declares a VPLAN row, and that declared rows exist. |
| `Makefile` | Gains a `vplan` target. |

`soc/test_integration.py` stays where it is; Task 1 makes the new `make vplan` target collect it too.

---

### Task 1: Test scaffolding and the first red row

**Files:**
- Create: `soc/tests/__init__.py`
- Create: `soc/tests/conftest.py`
- Create: `soc/tests/test_structural.py`
- Create: `soc/tests/test_vplan_coverage.py`
- Modify: `Makefile` (add `vplan` target)
- Modify: `docs/VPLAN.md` (record the check-type amendment)

**Interfaces:**
- Consumes: nothing.
- Produces: `elaborate_core(tmp_path) -> str` in `conftest.py`, returning converted Verilog for `B8008Core` as a string. `VPLAN_RE`, a compiled regex matching `VPLAN: ID[, ID...]` in a docstring.

- [ ] **Step 1: Create the package and fixtures**

Create `soc/tests/__init__.py` as an empty file.

Create `soc/tests/conftest.py`:

```python
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

CORE_V = os.path.join(_SOC, "build", "b8008_net_core.v")
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
```

- [ ] **Step 2: Write the coverage enforcer**

Create `soc/tests/test_vplan_coverage.py`:

```python
"""Meta-tests: the suite must stay tied to VPLAN.md.

VPLAN: (meta)
"""
import importlib
import inspect
import os
import pkgutil
import re

from conftest import VPLAN_MD, VPLAN_RE

ROW_ID = re.compile(
    r"^(CLK|RST|CDC|RX|TX|BP|CSR|WIRE|E2E|EQ|STR|X|A)-\d+[a-z]?$"
)


def _vplan_row_ids():
    ids = set()
    with open(VPLAN_MD) as f:
        for line in f:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            if cells and ROW_ID.match(cells[0]):
                ids.add(cells[0])
    return ids


def _suite_tests():
    """(module, function, docstring) for every test in this directory."""
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    for mod in pkgutil.iter_modules([here]):
        if not mod.name.startswith("test_"):
            continue
        m = importlib.import_module(mod.name)
        for name, fn in inspect.getmembers(m, inspect.isfunction):
            if name.startswith("test_") and inspect.getmodule(fn) is m:
                out.append((mod.name, name, fn.__doc__ or ""))
    return out


def test_vplan_md_parses():
    """VPLAN.md yields a non-trivial set of row IDs.

    VPLAN: (meta)
    """
    assert len(_vplan_row_ids()) > 100


def test_every_test_declares_a_row():
    """Every test function's docstring names the VPLAN row it discharges.

    VPLAN: (meta)
    """
    missing = [
        f"{mod}::{name}"
        for mod, name, doc in _suite_tests()
        if not VPLAN_RE.search(doc)
    ]
    assert not missing, f"tests without a `VPLAN:` declaration: {missing}"


def test_declared_rows_exist_in_vplan():
    """Every declared row ID is a real row in VPLAN.md.

    VPLAN: (meta)
    """
    known = _vplan_row_ids()
    bad = []
    for mod, name, doc in _suite_tests():
        m = VPLAN_RE.search(doc)
        if not m:
            continue
        for rid in (x.strip() for x in m.group(1).split(",")):
            if rid and rid != "(meta)" and rid not in known:
                bad.append(f"{mod}::{name} -> {rid}")
    assert not bad, f"tests citing unknown VPLAN rows: {bad}"
```

- [ ] **Step 3: Write the first structural test — it must FAIL**

Create `soc/tests/test_structural.py`:

```python
"""Structural conformance: claims checked by inspecting the elaborated design."""
from conftest import needs_netlist  # noqa: F401


@needs_netlist
def test_no_pulse_synchronizers(core_verilog):
    """No PulseSynchronizer exists; the retired ctl pulses are gone.

    VPLAN: CDC-5
    """
    assert "pulsesynchronizer" not in core_verilog.lower()
```

- [ ] **Step 4: Add the `vplan` make target**

Append to `Makefile`:

```makefile
.PHONY: vplan
vplan:
	$(PY) -m pytest soc/tests soc/test_integration.py -v
```

- [ ] **Step 5: Run it and confirm the red**

Run: `make vplan`
Expected: `test_no_pulse_synchronizers` **FAILS** — `b8008_integration.py` currently instantiates four `PulseSynchronizer`s for the retired `ctl` fields. All meta-tests PASS.

This red is the deliverable. It pins divergence D-8 as a failing test before any RTL moves.

- [ ] **Step 6: Amend the VPLAN check types**

In `docs/VPLAN.md`, add a row to the check-type legend table, immediately after the `COCOTB-R` row:

```markdown
| `MIGENSIM` | Migen `run_simulation` unit test | `litex/test/test_uart.py`, `litex/test/test_csr_bus.py` |
```

Then in the same file, immediately below the legend table, add:

```markdown
**On `MIGENSIM` vs `COCOTB-D`.** Rows whose device under test is `ConsoleBridge`
use `MIGENSIM`: that module is pure `cd_sys` Migen, so Migen's own simulator runs
it directly as Python in milliseconds, with no Verilog conversion and no simulator
install. It is the idiom LiteX uses for equivalent modules. `COCOTB-*` is retained
for rows needing the converted netlist, the `cd_b8008` domain, or a real CDC phase
relationship — CDC-3, CDC-4, CDC-6, RST-3, RST-4, BP-7, BP-12, CLK-7, CLK-8.
```

- [ ] **Step 7: Commit**

```bash
git add soc/tests Makefile docs/VPLAN.md
git commit -m "test: VPLAN conformance scaffolding; CDC-5 red

Adds soc/tests/ with shared elaboration fixtures, a make vplan target,
and meta-tests that enforce every test declaring the VPLAN row it
discharges. The first structural row (CDC-5, no PulseSynchronizer) fails
against current RTL, which is the intent: divergence D-8 is now a
failing test rather than a note in a document.

Amends VPLAN check types to use Migen run_simulation for ConsoleBridge
rows, keeping cocotb for netlist and cd_b8008 rows.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Extract `ConsoleBridge` (pure refactor)

No behavior changes. This exists so every later task has something it can simulate in milliseconds.

**Files:**
- Create: `soc/console_bridge.py`
- Create: `soc/tests/test_console_rx.py`
- Modify: `soc/b8008_integration.py:100-129` (console section)

**Interfaces:**
- Consumes: nothing.
- Produces: `ConsoleBridge(sys_clk_freq, baudrate=115200, rx_depth=4096, tx_depth=256)`, a `LiteXModule` + `AutoCSR` exposing:
  - `.pads` — a `Record([("tx", 1), ("rx", 1)])`. `pads.rx` is the bridge's serial input (driven by the core's `uart_tx`); `pads.tx` is its serial output (drives the core's `uart_rx`).
  - `.rx_fifo`, `.tx_fifo` — the `stream.SyncFIFO` instances, exposed for test introspection.
  - CSRs added in later tasks.

- [ ] **Step 1: Write the failing test**

Create `soc/tests/test_console_rx.py`:

```python
"""Console RX path behavior. DUT is ConsoleBridge, pure cd_sys."""
from migen import run_simulation

from console_bridge import ConsoleBridge


# clk/baud = 10 -> 10 sys cycles per bit, 100 per 10-bit frame. Keeps sims fast.
FAST = dict(sys_clk_freq=1_000_000, baudrate=100_000)
BIT = 10
FRAME = 10 * BIT


def serial_send(dut, byte):
    """Drive one 8N1 frame onto dut.pads.rx at the FAST bit period."""
    def bit(v):
        yield dut.pads.rx.eq(v)
        for _ in range(BIT):
            yield

    yield from bit(0)                       # start
    for i in range(8):
        yield from bit((byte >> i) & 1)     # LSB first
    yield from bit(1)                       # stop


def test_bridge_receives_a_byte():
    """A byte clocked into pads.rx lands in rx_fifo.

    VPLAN: RX-1
    """
    dut = ConsoleBridge(**FAST)
    seen = []

    def gen():
        yield dut.pads.rx.eq(1)
        for _ in range(BIT):
            yield
        yield from serial_send(dut, 0xA5)
        for _ in range(FRAME):
            yield
        seen.append((yield dut.rx_fifo.level))
        seen.append((yield dut.rx_fifo.source.data))

    run_simulation(dut, gen())
    assert seen == [1, 0xA5]


def test_rx_fifo_depth_is_4096():
    """rx_fifo depth is exactly 4096.

    VPLAN: RX-1
    """
    assert ConsoleBridge(sys_clk_freq=75e6).rx_fifo.fifo.depth == 4096
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest soc/tests/test_console_rx.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'console_bridge'`

- [ ] **Step 3: Write `ConsoleBridge`**

Create `soc/console_bridge.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest soc/tests/test_console_rx.py -v`
Expected: both PASS.

- [ ] **Step 5: Use `ConsoleBridge` inside `B8008Core`**

In `soc/b8008_integration.py`, replace the whole console section (the block from the `# ---- console bridge` comment through the `self._rxempty.status.eq(...)` line, currently lines 100-129) with:

```python
        # ---- console bridge (SPEC.md S-ARCH-1: sys-domain, serial crossing) ---
        from console_bridge import ConsoleBridge

        self.console = ConsoleBridge(sys_clk_freq=sys_clk_freq)
        pads = self.console.pads
```

Leave the `Instance("b8008_net_core", ...)` call untouched — it already refers to
`pads.rx` and `pads.tx`, and those names now resolve to the bridge's pads with the
same directions.

- [ ] **Step 6: Verify the whole suite still behaves as before**

Run: `make vplan`
Expected: `CDC-5` still FAILS (unchanged, Task 1's deliverable). Every other test PASSES, including `soc/test_integration.py::test_elaborates`.

If `test_elaborates` fails, the refactor changed something. Fix it before committing — this task must be behavior-neutral.

- [ ] **Step 7: Commit**

```bash
git add soc/console_bridge.py soc/tests/test_console_rx.py soc/b8008_integration.py
git commit -m "refactor: extract ConsoleBridge from B8008Core

Pure refactor, no behavior change. The console byte path moves into a
single-domain cd_sys module with no 8008 in it, which makes the product's
behavioral surface simulatable with Migen's own simulator in milliseconds
instead of requiring a gate-level netlist boot.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `console_rx` and `console_rx_pop` — non-destructive read (D-3, D-4)

**Files:**
- Modify: `soc/console_bridge.py`
- Modify: `soc/tests/test_console_rx.py`

**Interfaces:**
- Consumes: `ConsoleBridge` from Task 2.
- Produces: `ConsoleBridge.console_rx` (`CSRStatus`, fields `data[7:0]`, `valid[8]`, `level[21:9]`) and `ConsoleBridge.console_rx_pop` (`CSR`, write-any-value pops one). Also `ConsoleBridge.err_rx_pop_when_empty` — a `Signal()` pulsing high for one cycle on an illegal pop, consumed by Task 5.

- [ ] **Step 1: Write the failing tests**

Append to `soc/tests/test_console_rx.py`:

```python
def _preload(dut, payload):
    """Push bytes straight into rx_fifo, bypassing the PHY (fast)."""
    for b in payload:
        yield dut.rx_fifo.sink.valid.eq(1)
        yield dut.rx_fifo.sink.data.eq(b)
        yield
        while not (yield dut.rx_fifo.sink.ready):
            yield
    yield dut.rx_fifo.sink.valid.eq(0)
    yield
    yield


def _read_rx(dut):
    """Read console_rx and unpack it into (data, valid, level)."""
    w = yield dut.console_rx.status
    return w & 0xFF, (w >> 8) & 1, (w >> 9) & 0x1FFF


def _pop(dut):
    yield dut.console_rx_pop.re.eq(1)
    yield
    yield dut.console_rx_pop.re.eq(0)
    yield
    yield


def test_read_is_non_destructive():
    """Two reads with no intervening pop return identical values.

    VPLAN: RX-2, RX-3
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    got = []

    def gen():
        yield from _preload(dut, [0x41, 0x42])
        for _ in range(1000):
            got.append((yield from _read_rx(dut)))

    run_simulation(dut, gen())
    assert len(set(got)) == 1, f"read mutated state: {set(got)}"
    assert got[0] == (0x41, 1, 2)


def test_pop_consumes_exactly_one():
    """One pop decreases level by exactly 1 and advances to the next byte.

    VPLAN: RX-4, RX-5, CSR-5
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    seq = []

    def gen():
        yield from _preload(dut, [0x10, 0x20, 0x30])
        for _ in range(3):
            data, valid, level = yield from _read_rx(dut)
            seq.append((data, valid, level))
            yield from _pop(dut)
        seq.append((yield from _read_rx(dut)))

    run_simulation(dut, gen())
    assert seq == [(0x10, 1, 3), (0x20, 1, 2), (0x30, 1, 1), (0x00, 0, 0)]


def test_empty_reads_zero():
    """When level is 0, data reads 0x00 and valid reads 0.

    VPLAN: RX-6, RX-7
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        out.append((yield from _read_rx(dut)))

    run_simulation(dut, gen())
    assert out == [(0x00, 0, 0)]


def test_valid_tracks_level():
    """valid == (level != 0) across a full fill and drain.

    VPLAN: RX-8
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    bad = []

    def gen():
        yield from _preload(dut, list(range(8)))
        for _ in range(9):
            data, valid, level = yield from _read_rx(dut)
            if valid != (1 if level else 0):
                bad.append((data, valid, level))
            yield from _pop(dut)

    run_simulation(dut, gen())
    assert not bad, f"valid/level disagreed: {bad}"


def test_retried_read_then_pop_advances_one():
    """k reads followed by one pop advance the stream by exactly one byte.

    VPLAN: RX-14
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    seen = []

    def gen():
        yield from _preload(dut, [0xAA, 0xBB])
        for k in (1, 5, 10):
            for _ in range(k):
                seen.append((yield from _read_rx(dut))[0])
            yield from _pop(dut)
            if k == 5:
                break

    run_simulation(dut, gen())
    assert seen == [0xAA] * 1 + [0xBB] * 5


def test_pop_when_empty_pulses_error():
    """A pop at level 0 consumes nothing and pulses the error signal.

    VPLAN: RX-11, RX-12
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    fired = []

    def monitor():
        for _ in range(40):
            if (yield dut.err_rx_pop_when_empty):
                fired.append(1)
            yield

    def gen():
        yield
        yield from _pop(dut)                    # illegal: empty
        yield from _preload(dut, [0x7E])
        data, valid, level = yield from _read_rx(dut)
        assert (data, valid, level) == (0x7E, 1, 1), (data, valid, level)

    run_simulation(dut, [gen(), monitor()])
    assert fired, "err_rx_pop_when_empty never asserted"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest soc/tests/test_console_rx.py -v`
Expected: the six new tests FAIL with `AttributeError: 'ConsoleBridge' object has no attribute 'console_rx'`.

- [ ] **Step 3: Implement the registers**

In `soc/console_bridge.py`, add to the imports:

```python
from migen import Record, Signal, If
from litex.soc.interconnect.csr import AutoCSR, CSR, CSRStatus, CSRField
```

Then append to `ConsoleBridge.__init__`:

```python
        # ---- console_rx: atomic {data, valid, level} (SPEC.md S-RX-8) --------
        # One read carries everything the host needs, so it can never observe a
        # torn level/data pair and never needs two round trips on a transport
        # that costs ~1 RTT per read (S-WIRE-3, S-WIRE-4).
        self.console_rx = CSRStatus(name="console_rx", fields=[
            CSRField("data",  size=8),
            CSRField("valid", size=1),
            CSRField("level", size=13)])
        self.comb += [
            # data reads 0x00 when empty -- S-RX-7's table, not "don't care".
            If(self.rx_fifo.source.valid,
                self.console_rx.fields.data.eq(self.rx_fifo.source.data)
            ).Else(
                self.console_rx.fields.data.eq(0)
            ),
            self.console_rx.fields.valid.eq(self.rx_fifo.source.valid),
            self.console_rx.fields.level.eq(self.rx_fifo.level),
        ]

        # ---- console_rx_pop: the ONLY consuming action (SPEC.md S-RX-4) ------
        # Reads are non-destructive so a retried UDP read cannot eat a byte
        # (S-RX-5). Consumption moves to the write path, which is not retried.
        self.console_rx_pop = CSR(name="console_rx_pop")
        self.err_rx_pop_when_empty = Signal()
        self.comb += [
            self.rx_fifo.source.ready.eq(
                self.console_rx_pop.re & self.rx_fifo.source.valid),
            self.err_rx_pop_when_empty.eq(
                self.console_rx_pop.re & ~self.rx_fifo.source.valid),
        ]
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest soc/tests/test_console_rx.py -v`
Expected: all PASS.

If `test_valid_tracks_level` fails with an off-by-one, the cause is `SyncFIFOBuffered`'s `level` counting its output register differently than `source.valid` implies. Fix it in `ConsoleBridge` by deriving `level` so the invariant holds — do **not** relax the test. `S-RX-8` is the reason the atomic register exists.

- [ ] **Step 5: Commit**

```bash
git add soc/console_bridge.py soc/tests/test_console_rx.py
git commit -m "feat(console): non-destructive console_rx + explicit pop (D-3, D-4)

Resolves divergences D-3 and D-4. The old rxtx register popped on read,
so a retried UDP read -- which CommUDP does on any timed-out reply --
consumed and discarded a byte the host never saw. That is a loss inside
the product boundary and violates S-PROD-3.

Reads are now idempotent and consumption is an explicit write. console_rx
also packs {data, valid, level} into one word so the host cannot observe
a torn pair and does not spend two round trips per byte.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: TX path — reject-when-full instead of silent discard (D-7)

**Files:**
- Modify: `soc/console_bridge.py`
- Create: `soc/tests/test_console_tx.py`

**Interfaces:**
- Consumes: `ConsoleBridge` from Task 3.
- Produces: `ConsoleBridge.console_tx` (`CSRStatus`, fields `level[8:0]`, `full[9]`), `ConsoleBridge.console_tx_data` (`CSR(8)`, write pushes), and `ConsoleBridge.err_tx_write_when_full` — a one-cycle `Signal()` consumed by Task 5.

- [ ] **Step 1: Write the failing tests**

Create `soc/tests/test_console_tx.py`:

```python
"""Console TX path behavior. DUT is ConsoleBridge, pure cd_sys."""
from migen import run_simulation

from console_bridge import ConsoleBridge


def _write_tx(dut, byte):
    yield dut.console_tx_data.r.eq(byte)
    yield dut.console_tx_data.re.eq(1)
    yield
    yield dut.console_tx_data.re.eq(0)
    yield
    yield


def _read_tx(dut):
    w = yield dut.console_tx.status
    return w & 0x1FF, (w >> 9) & 1


def _fill_tx(dut, n):
    for i in range(n):
        yield from _write_tx(dut, i & 0xFF)


def test_tx_fifo_depth_is_256():
    """tx_fifo depth is exactly 256.

    VPLAN: TX-1
    """
    assert ConsoleBridge(sys_clk_freq=75e6).tx_fifo.fifo.depth == 256


def test_write_increases_level_by_one():
    """A write while not full increases level by exactly 1.

    VPLAN: TX-2
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        out.append((yield from _read_tx(dut)))
        yield from _write_tx(dut, 0x5A)
        out.append((yield from _read_tx(dut)))

    run_simulation(dut, gen())
    assert out == [(0, 0), (1, 0)]


def test_full_flag_tracks_level():
    """full == (level == 256).

    VPLAN: TX-7
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        yield from _fill_tx(dut, 256)
        out.append((yield from _read_tx(dut)))

    run_simulation(dut, gen())
    assert out == [(256, 1)]


def test_write_when_full_is_rejected():
    """A write while full leaves level at 256 and pulses the error signal.

    VPLAN: TX-4, TX-5, TX-6, CSR-8
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out, fired = [], []

    def monitor():
        for _ in range(4000):
            if (yield dut.err_tx_write_when_full):
                fired.append(1)
            yield

    def gen():
        yield
        yield from _fill_tx(dut, 256)
        yield from _write_tx(dut, 0xFF)      # rejected
        out.append((yield from _read_tx(dut)))

    run_simulation(dut, [gen(), monitor()])
    assert out == [(256, 1)]
    assert fired, "err_tx_write_when_full never asserted"


def test_tx_data_ignores_high_bits():
    """Bits 31:8 of a console_tx_data write do not reach the FIFO.

    VPLAN: CSR-7
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        yield from _write_tx(dut, 0x41)
        out.append((yield dut.tx_fifo.source.data))

    run_simulation(dut, gen())
    assert out == [0x41]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest soc/tests/test_console_tx.py -v`
Expected: five FAIL with `AttributeError` on `console_tx_data` (the depth test passes).

- [ ] **Step 3: Implement**

Append to `ConsoleBridge.__init__` in `soc/console_bridge.py`:

```python
        # ---- console_tx / console_tx_data (SPEC.md S-TX-2, S-TX-3) ----------
        # A write while full is REJECTED and flagged, not silently discarded.
        # Silent discard would be a loss inside the product boundary.
        self.console_tx = CSRStatus(name="console_tx", fields=[
            CSRField("level", size=9),
            CSRField("full",  size=1)])
        self.comb += [
            self.console_tx.fields.level.eq(self.tx_fifo.level),
            self.console_tx.fields.full.eq(~self.tx_fifo.sink.ready),
        ]

        self.console_tx_data = CSR(8, name="console_tx_data")
        self.err_tx_write_when_full = Signal()
        self.comb += [
            self.tx_fifo.sink.valid.eq(
                self.console_tx_data.re & self.tx_fifo.sink.ready),
            self.tx_fifo.sink.data.eq(self.console_tx_data.r),
            self.err_tx_write_when_full.eq(
                self.console_tx_data.re & ~self.tx_fifo.sink.ready),
        ]
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest soc/tests/test_console_tx.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add soc/console_bridge.py soc/tests/test_console_tx.py
git commit -m "feat(console): reject TX writes when full, flag them (D-7)

Resolves divergence D-7. The old path pushed unconditionally, so a write
arriving at a full FIFO vanished with no indication. The host had no way
to distinguish a delivered byte from a dropped one.

Writes while full are now rejected without displacing queued bytes, and
raise a signal that Task 5 latches into a sticky bit.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Sticky error register with set-wins arbitration (D-11)

**Files:**
- Modify: `soc/console_bridge.py`
- Create: `soc/tests/test_console_err.py`

**Interfaces:**
- Consumes: `err_rx_pop_when_empty` (Task 3), `err_tx_write_when_full` (Task 4).
- Produces: `ConsoleBridge.console_err` (`CSRStatus`, fields `rx_overflow[0]`, `tx_write_when_full[1]`, `rx_pop_when_empty[2]`), `ConsoleBridge.console_err_clear` (`CSR(3)`, write-1-to-clear), and `ConsoleBridge.err_rx_overflow` — a `Signal()` input that Task 6 drives.

- [ ] **Step 1: Write the failing tests**

Create `soc/tests/test_console_err.py`:

```python
"""Sticky error bits and their clear semantics."""
from migen import run_simulation

from console_bridge import ConsoleBridge

BIT_OVERFLOW = 0
BIT_TX_FULL = 1
BIT_POP_EMPTY = 2


def _read_err(dut):
    return (yield dut.console_err.status) & 0x7


def _clear(dut, mask):
    yield dut.console_err_clear.r.eq(mask)
    yield dut.console_err_clear.re.eq(1)
    yield
    yield dut.console_err_clear.re.eq(0)
    yield
    yield


def _pulse_pop_empty(dut):
    yield dut.console_rx_pop.re.eq(1)
    yield
    yield dut.console_rx_pop.re.eq(0)
    yield
    yield


def test_err_is_zero_at_reset():
    """All error bits read 0 before any fault.

    VPLAN: RST-11a
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        out.append((yield from _read_err(dut)))

    run_simulation(dut, gen())
    assert out == [0]


def test_pop_empty_sets_sticky_bit():
    """An illegal pop latches bit 2 and it stays set.

    VPLAN: RX-11, CSR-5
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        yield from _pulse_pop_empty(dut)
        for _ in range(50):
            out.append((yield from _read_err(dut)))

    run_simulation(dut, gen())
    assert set(out) == {1 << BIT_POP_EMPTY}


def test_reading_err_does_not_clear_it():
    """100 reads of console_err leave every bit unchanged.

    VPLAN: CSR-8, CSR-4
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        yield from _pulse_pop_empty(dut)
        for _ in range(100):
            out.append((yield from _read_err(dut)))

    run_simulation(dut, gen())
    assert len(set(out)) == 1 and out[0] == 1 << BIT_POP_EMPTY


def test_write_one_clears_only_that_bit():
    """Writing 1 to a clear bit clears exactly that bit.

    VPLAN: CSR-9
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        yield dut.err_rx_overflow.eq(1)
        yield
        yield dut.err_rx_overflow.eq(0)
        yield from _pulse_pop_empty(dut)
        out.append((yield from _read_err(dut)))
        yield from _clear(dut, 1 << BIT_POP_EMPTY)
        out.append((yield from _read_err(dut)))
        yield from _clear(dut, 1 << BIT_OVERFLOW)
        out.append((yield from _read_err(dut)))

    run_simulation(dut, gen())
    both = (1 << BIT_POP_EMPTY) | (1 << BIT_OVERFLOW)
    assert out == [both, 1 << BIT_OVERFLOW, 0]


def test_write_zero_leaves_bit_set():
    """Writing 0 to a clear bit leaves the corresponding bit unchanged.

    VPLAN: CSR-10
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        yield from _pulse_pop_empty(dut)
        yield from _clear(dut, 0)
        out.append((yield from _read_err(dut)))

    run_simulation(dut, gen())
    assert out == [1 << BIT_POP_EMPTY]


def test_set_wins_over_concurrent_clear():
    """A clear coinciding with the set condition leaves the bit SET.

    VPLAN: CSR-11
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out = []

    def gen():
        yield
        # Assert the clear and the fault on the SAME cycle.
        yield dut.console_err_clear.r.eq(1 << BIT_POP_EMPTY)
        yield dut.console_err_clear.re.eq(1)
        yield dut.console_rx_pop.re.eq(1)
        yield
        yield dut.console_err_clear.re.eq(0)
        yield dut.console_rx_pop.re.eq(0)
        yield
        yield
        out.append((yield from _read_err(dut)))

    run_simulation(dut, gen())
    assert out == [1 << BIT_POP_EMPTY], "a concurrent clear swallowed a fresh fault"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest soc/tests/test_console_err.py -v`
Expected: all six FAIL with `AttributeError` on `console_err`.

- [ ] **Step 3: Implement**

Append to `ConsoleBridge.__init__` in `soc/console_bridge.py`:

```python
        # ---- sticky error bits (SPEC.md §11.5, S-CSR-9, S-CSR-12) -----------
        # Sticky, and survive every reset: a fault that provokes a power cycle
        # must still be visible afterward (S-CSR-10). Set beats a coinciding
        # clear -- losing a fresh fault to a concurrent clear would make these
        # unreliable in exactly the case they exist for.
        self.err_rx_overflow = Signal()   # driven by the backpressure task

        self.console_err = CSRStatus(name="console_err", fields=[
            CSRField("rx_overflow",        size=1),
            CSRField("tx_write_when_full", size=1),
            CSRField("rx_pop_when_empty",  size=1)])
        self.console_err_clear = CSR(3, name="console_err_clear")

        sources = [
            (0, self.err_rx_overflow,          self.console_err.fields.rx_overflow),
            (1, self.err_tx_write_when_full,   self.console_err.fields.tx_write_when_full),
            (2, self.err_rx_pop_when_empty,    self.console_err.fields.rx_pop_when_empty),
        ]
        for idx, src, field in sources:
            sticky = Signal(name=f"sticky_err_{idx}", reset_less=True)
            self.sync += [
                If(src,
                    sticky.eq(1)                       # set wins
                ).Elif(self.console_err_clear.re & self.console_err_clear.r[idx],
                    sticky.eq(0)
                )
            ]
            self.comb += field.eq(sticky)
```

`reset_less=True` is what makes the bits survive a reset, per `S-CSR-9`. The `If`/`Elif`
order is what makes set beat clear, per `S-CSR-12`. Neither is incidental.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest soc/tests/test_console_err.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add soc/console_bridge.py soc/tests/test_console_err.py
git commit -m "feat(console): sticky error register, set-wins on clear (D-11)

Resolves divergence D-11. Three faults that were previously invisible --
RX overflow, TX write while full, pop while empty -- now latch into
sticky bits that survive every reset and clear only on an explicit
write-1-to-clear.

Set beats a coinciding clear (S-CSR-12): a clear racing a fresh fault
must not swallow it, or the bits are unreliable in precisely the
situation they exist for.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Backpressure with hysteresis (D-5, D-6)

**Files:**
- Modify: `soc/console_bridge.py`
- Create: `soc/tests/test_backpressure.py`

**Interfaces:**
- Consumes: `ConsoleBridge` from Task 5.
- Produces: `ConsoleBridge.stall` — a `Signal()` in `cd_sys`, high when the 8008 must be frozen. Task 7 crosses it to `cd_b8008`. Also module constants `HWM = 4032` and `LWM = 3968`.

- [ ] **Step 1: Write the failing tests**

Create `soc/tests/test_backpressure.py`:

```python
"""Backpressure thresholds and hysteresis. DUT is ConsoleBridge."""
from migen import run_simulation

from console_bridge import ConsoleBridge, HWM, LWM


def _preload(dut, n):
    for i in range(n):
        yield dut.rx_fifo.sink.valid.eq(1)
        yield dut.rx_fifo.sink.data.eq(i & 0xFF)
        yield
        while not (yield dut.rx_fifo.sink.ready):
            yield
    yield dut.rx_fifo.sink.valid.eq(0)
    yield
    yield


def _drain(dut, n):
    for _ in range(n):
        yield dut.console_rx_pop.re.eq(1)
        yield
        yield dut.console_rx_pop.re.eq(0)
        yield


def test_thresholds_leave_headroom():
    """HWM leaves at least 4 entries of headroom below the 4096 depth.

    VPLAN: BP-6
    """
    assert 4096 - HWM >= 4, "headroom must exceed the max bytes in flight"
    assert LWM < HWM, "hysteresis band must be non-empty"


def test_stall_asserts_at_hwm():
    """Stall is low below HWM and high at HWM.

    VPLAN: BP-1
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out = []

    def gen():
        yield from _preload(dut, HWM - 1)
        out.append((yield dut.stall))
        yield from _preload(dut, 1)
        out.append((yield dut.stall))

    run_simulation(dut, gen())
    assert out == [0, 1]


def test_stall_holds_through_the_band():
    """Stall stays asserted while LWM < level < HWM on the way down.

    VPLAN: BP-2, BP-3
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    out = []

    def gen():
        yield from _preload(dut, HWM)
        out.append((yield dut.stall))            # asserted
        yield from _drain(dut, (HWM - LWM) - 1)  # still inside the band
        out.append((yield dut.stall))            # must still be asserted
        yield from _drain(dut, 1)                # now at LWM
        yield
        out.append((yield dut.stall))            # released

    run_simulation(dut, gen())
    assert out == [1, 1, 0]


def test_no_overflow_under_sustained_pressure():
    """rx_fifo never reports a write-while-full; the canary stays clear.

    VPLAN: BP-6
    """
    dut = ConsoleBridge(sys_clk_freq=75e6)
    violations = []

    def monitor():
        for _ in range(20000):
            if (yield dut.rx_fifo.sink.valid) and not (yield dut.rx_fifo.sink.ready):
                violations.append(1)
            yield

    def gen():
        yield from _preload(dut, HWM)
        for _ in range(200):
            yield

    run_simulation(dut, [gen(), monitor()])
    assert not violations, "a byte was presented to a full rx_fifo"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest soc/tests/test_backpressure.py -v`
Expected: FAIL with `ImportError: cannot import name 'HWM'`.

- [ ] **Step 3: Implement**

In `soc/console_bridge.py`, add near the existing depth constants:

```python
# SPEC.md S-BP-5. The 64-entry gap below RX_DEPTH is headroom for bytes still
# in flight when the stall asserts -- at most 3 (core USART shift register,
# PHY receive shift register, and the X3 synchronizer's 2-cycle latency), so
# this carries roughly 20x margin. S-BP-6.
HWM = 4032
LWM = 3968
```

Append to `ConsoleBridge.__init__`:

```python
        # ---- backpressure (SPEC.md §10) -------------------------------------
        # S-BP-1: never drop a byte. When the host is slow, the 8008 STOPS.
        # Legal only because S-PROD-4 makes no real-time promise.
        # Hysteresis prevents oscillation at the threshold (S-BP-5).
        self.stall = Signal()
        self.sync += [
            If(self.rx_fifo.level >= HWM,
                self.stall.eq(1)
            ).Elif(self.rx_fifo.level <= LWM,
                self.stall.eq(0)
            )
        ]

        # S-BP-9: the canary. Under a correct implementation S-BP-8 makes this
        # unreachable; it exists to catch a regression in the mechanism that
        # makes it unreachable, not to handle the condition.
        self.comb += self.err_rx_overflow.eq(
            self.rx_fifo.sink.valid & ~self.rx_fifo.sink.ready)
```

Move the `self.err_rx_overflow = Signal()` declaration from Task 5's block up to
before the sticky-bit loop if it is not already there — it must be declared before
both the loop that reads it and the `comb` that drives it.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest soc/tests/test_backpressure.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add soc/console_bridge.py soc/tests/test_backpressure.py
git commit -m "feat(console): backpressure with hysteresis (D-5, D-6)

Resolves divergences D-5 and D-6. The RX FIFO previously dropped bytes
silently when full. It now asserts a stall at 4032 and releases at 3968;
Task 7 crosses that stall into cd_b8008 to freeze the phase generator.

The 64-entry headroom exceeds the maximum bytes that can still arrive
after the stall asserts (at most 3), so overflow becomes unreachable
rather than handled. The overflow bit stays as a canary for a regression
in the mechanism that makes it unreachable.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Retire the removed surface and wire the stall through (D-8, D-9, D-10)

**Files:**
- Modify: `soc/b8008_integration.py`
- Modify: `src/b8008_net_core.vhdl:36-50` (entity ports), `:417` (run_enable wiring)
- Modify: `soc/versa_soc.py` (drop the RAM window's bus/region registration)
- Modify: `soc/tests/test_structural.py`

**Interfaces:**
- Consumes: `ConsoleBridge.stall` (Task 6).
- Produces: `B8008Core` with no `ctl`/`status` CSRs, no `bus_ram`, and a `MultiReg`-synchronized `stall` reaching the core's new `ext_hold` port.

- [ ] **Step 1: Write the failing structural tests**

Append to `soc/tests/test_structural.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest soc/tests/test_structural.py -v`
Expected: `test_no_retired_control_csrs`, `test_no_host_ram_window`, and
`test_stall_crosses_into_b8008_domain` FAIL. `CDC-5` still FAILS.

- [ ] **Step 3: Add `ext_hold` to the wrapper VHDL**

In `src/b8008_net_core.vhdl`, inside the `entity b8008_net_core` port list, add after
the `ctl_*` inputs (around line 43):

```vhdl
        -- Backpressure hold from the sys-domain console bridge, already
        -- synchronized into this clock domain by a 2-FF MultiReg on the
        -- Migen side (SPEC.md S-CDC-1 X3). '1' freezes the phase generator.
        ext_hold       : in std_logic := '0';
```

Then change line 417 from:

```vhdl
            run_enable  => dbg_run_enable,        -- Debug hold: '0' freezes phi state machine
```

to:

```vhdl
            -- Debug hold OR host backpressure: '0' freezes the phi state
            -- machine. SPEC.md S-BP-4 -- a hold, never a gated clock.
            run_enable  => (dbg_run_enable and not ext_hold),
```

- [ ] **Step 4: Delete the retired surface and wire the stall**

In `soc/b8008_integration.py`:

1. Delete the entire `# ---- control CSRs` block (the `self.ctl = CSRStorage(...)`
   declaration, the `ctl_pulses` loop, and the `int_vec_b` `MultiReg`).
2. Delete the entire `# ---- status:` block (`is_running_b`, `triggered_b`,
   `tx_busy_b`, and `self.status`).
3. Delete the `self.bus_ram = wishbone.Interface(...)` declaration and the
   `self.sync += self.bus_ram.ack...` and `self.comb += [pb.adr...]` blocks that
   follow it. Keep the `Memory` and port `pa`; the 8008 still needs its RAM. Change
   the port `pb` line to be deleted as well, since nothing drives it now.
4. Add the stall crossing immediately before the `Instance(...)` call:

```python
        # ---- X3: backpressure stall, cd_sys -> cd_b8008 (SPEC.md S-CDC-1) ---
        # A LEVEL, not a pulse (S-CDC-3): the synchronizer's 2-cycle latency
        # (80 ns) is negligible against the 86.805 us byte time.
        stall_b = Signal()
        self.specials += MultiReg(self.console.stall, stall_b, "b8008")
```

5. In the `Instance("b8008_net_core", ...)` call, delete every `i_ctl_*` argument and
   every `o_sts_*` argument, and add:

```python
            i_ext_hold=stall_b,
```

6. Remove now-unused imports: `PulseSynchronizer`, `CSRStorage`, `wishbone`, and
   `READ_FIRST` if the remaining `Memory` port does not use it.

In `soc/versa_soc.py`, delete the `add_memory_region` / bus-attach call for the b8008
RAM window (search for `bus_ram`), and delete any reference to `self.b8008.bus_ram`.

- [ ] **Step 5: Retire the obsolete host commands**

The `host/b8008net` package still exports `load`, `peek`, `poke`, `run`, `reset`, and
`step`, which drive CSRs that no longer exist. Do not delete them in this task —
that is a separate change with its own test churn. Instead, mark them, so nothing
silently depends on a retired surface. At the top of `host/b8008net/commands.py`,
insert after the existing module docstring block:

```python
# RETIRED SURFACE. SPEC.md S-PROD-8 removed host-side load/peek/poke/run/step
# from the product: the monitor provides L/D/W/G in-band over the console.
# The functions below target CSRs that no longer exist in the gateware and
# will fail against a conformant build. They are kept only until the console
# workflow replaces them. Do not add callers.
```

- [ ] **Step 6: Run the full suite**

Run: `make vplan`
Expected: **all tests PASS**, including `CDC-5`, which has been red since Task 1.
`soc/test_integration.py::test_elaborates` must still pass.

- [ ] **Step 7: Commit**

```bash
git add soc/b8008_integration.py soc/versa_soc.py src/b8008_net_core.vhdl \
        soc/tests/test_structural.py host/b8008net/commands.py
git commit -m "feat: retire non-product surface, wire backpressure (D-8, D-9, D-10)

Resolves divergences D-8, D-9, D-10. Deletes the ctl/status CSRs, the
four PulseSynchronizers that served them, the permanently-zero triggered
bit, and the host-facing wishbone RAM window. All of that reimplemented
from outside what the monitor already provides in-band via L/D/W/G.

Adds an ext_hold input to the wrapper VHDL, gated into run_enable, and
crosses ConsoleBridge.stall into cd_b8008 through a MultiReg -- the only
new clock-domain crossing in the design.

Marks host/b8008net/commands.py as retired surface.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Reset synchronizer and ordering (D-1, D-2)

**Files:**
- Modify: `soc/versa_soc.py:388-431` (`_CRG`)
- Modify: `soc/tests/test_structural.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: a `cd_b8008` whose reset is asserted asynchronously and released synchronously, strictly after `cd_sys`.

- [ ] **Step 1: Write the failing tests**

Append to `soc/tests/test_structural.py`:

```python
def _crg():
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from versa_soc import _CRG
    from litex_boards.platforms import lattice_versa_ecp5
    platform = lattice_versa_ecp5.Platform(device="LFE5UM5G", toolchain="trellis")
    return _CRG(platform, 75e6)


def test_b8008_reset_is_synchronized():
    """cd_b8008 has an AsyncResetSynchronizer; its reset is actually driven.

    VPLAN: RST-2
    """
    from migen.genlib.resetsync import AsyncResetSynchronizer

    crg = _crg()
    targets = [
        s.cd.name for s in crg.specials
        if isinstance(getattr(s, "_inst", s), AsyncResetSynchronizer)
        or isinstance(s, AsyncResetSynchronizer)
    ]
    assert "b8008" in targets, f"cd_b8008 unsynchronized; found: {targets}"


def test_console_reset_precedes_core_reset():
    """cd_b8008 release is gated on cd_sys already being out of reset.

    VPLAN: RST-7
    """
    crg = _crg()
    assert hasattr(crg, "b8008_rst_gate"), (
        "no explicit gate ordering cd_b8008's release after cd_sys's "
        "(SPEC.md S-RST-6: the console must be able to accept a byte before "
        "the core can emit one, or the boot banner is lost)"
    )
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest soc/tests/test_structural.py -k reset -v`
Expected: both FAIL. `cd_b8008` currently has no synchronizer at all.

- [ ] **Step 3: Implement**

In `soc/versa_soc.py`, inside `_CRG.__init__`, find this existing block (it ends the
method today):

```python
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
```

Replace it **in full** with:

```python
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

        # SPEC.md S-RST-4 / S-RST-6. cd_b8008 previously had NO reset
        # synchronizer at all, so ResetSignal("b8008") was never driven and the
        # core was reset only by its own internal POR counter.
        #
        # The gate is what orders R6 before R7: cd_b8008 is held in reset until
        # cd_sys is out of reset, so the console path can accept a byte before
        # the core can emit one. Without it the boot banner -- the product's
        # only power-on liveness evidence -- can be emitted into a FIFO that is
        # still in reset, and lost.
        self.b8008_rst_gate = Signal()
        self.comb += self.b8008_rst_gate.eq(
            ~pll.locked | self.reset | ResetSignal("sys"))
        self.specials += AsyncResetSynchronizer(
            self.cd_b8008, self.b8008_rst_gate)
```

`ResetSignal` and `Signal` are already in the `migen` import list at the top of the
file, alongside `ClockSignal`. Confirm before running — if either is missing, add it.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest soc/tests/test_structural.py -k reset -v`
Expected: both PASS.

- [ ] **Step 5: Run the whole suite**

Run: `make vplan`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add soc/versa_soc.py soc/tests/test_structural.py
git commit -m "fix: synchronize and order cd_b8008 reset (D-1, D-2)

Resolves divergences D-1 and D-2. cd_b8008 had no AsyncResetSynchronizer,
so ResetSignal('b8008') was never driven and the value fed to the core's
i_rst was tied low. The core was reset only by its own internal POR.

Reset is now asserted asynchronously and released synchronously to
cd_b8008, gated on cd_sys already being released so the console path can
accept a byte before the core can emit one. Without that ordering the
boot banner can be emitted into a FIFO still held in reset -- and the
banner is the only power-on liveness evidence the product has.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Clock groups and elaboration guards (D-12)

**Files:**
- Modify: `soc/versa_soc.py` (`BaseSoC.__init__`)
- Modify: `soc/tests/test_structural.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a generated `.lpf` declaring `cd_sys` and `cd_b8008` as unrelated clocks.

- [ ] **Step 1: Write the failing test**

Append to `soc/tests/test_structural.py`:

```python
def test_clock_groups_declared():
    """The build declares cd_sys and cd_b8008 as an asynchronous group.

    VPLAN: CLK-3, CLK-4
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from versa_soc import BaseSoC

    soc = BaseSoC(sys_clk_freq=75e6)
    constraints = "\n".join(
        str(c) for c in soc.platform.toolchain.false_paths
    )
    assert "b8008" in constraints, (
        "no false-path/clock-group constraint between cd_sys and cd_b8008; "
        "the timing tool will try to close paths that S-CDC-1 says do not exist"
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest soc/tests/test_structural.py -k clock_groups -v`
Expected: FAIL — no constraint mentions `b8008`.

- [ ] **Step 3: Implement**

In `soc/versa_soc.py`, in `BaseSoC.__init__`, immediately after the `self.b8008 = B8008Core(...)`
assignment, add:

```python
        # SPEC.md S-CLK-3. cd_sys and cd_b8008 both descend from one ECP5PLL
        # but no fixed phase relationship between them is declared or relied
        # upon. The three crossings of S-CDC-1 are all synchronized, so the
        # timing tool must not attempt to close paths between the domains.
        platform.add_false_path_constraints(
            self.crg.cd_sys.clk, self.crg.cd_b8008.clk)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest soc/tests/test_structural.py -k clock_groups -v`
Expected: PASS.

If `toolchain.false_paths` is not the attribute name at LiteX `2026.04`, find the
real one by inspecting `soc.platform.toolchain.__dict__` after
`add_false_path_constraints`, and update the test to read it. Do not weaken the
assertion to something that would pass without the constraint.

- [ ] **Step 5: Commit**

```bash
git add soc/versa_soc.py soc/tests/test_structural.py
git commit -m "build: declare cd_sys/cd_b8008 as unrelated clocks (D-12)

Resolves divergence D-12. Both domains descend from one ECP5PLL, so
without an explicit constraint the timing tool treats them as related and
tries to close paths that S-CDC-1 says do not exist -- the three real
crossings are all synchronized.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Update VPLAN statuses and confirm the build

**Files:**
- Modify: `docs/VPLAN.md`
- Modify: `README.md` (status table)

- [ ] **Step 1: Build the bitstream**

Run: `make build`
Expected: completes and produces `build/versa/gateware/versa_soc.bit` with timing met.

If synthesis fails on a deleted signal, a stale reference to `bus_ram`, `ctl`, or
`status` survives somewhere. Find it with
`grep -rn "bus_ram\|\.ctl\|sts_is_running" soc/ src/` and remove it.

- [ ] **Step 2: Flip the statuses of rows this plan discharged**

For every row whose test now exists and passes, change `UNIMPLEMENTED` to `PASS` in
`docs/VPLAN.md`. Get the authoritative list from the suite itself:

```bash
.venv/bin/python -m pytest soc/tests -q 2>/dev/null | head -1
.venv/bin/python - <<'EOF'
import importlib, inspect, os, re, sys
sys.path.insert(0, "soc"); sys.path.insert(0, "soc/tests")
rows = set()
for fn in sorted(os.listdir("soc/tests")):
    if not fn.startswith("test_") or not fn.endswith(".py"):
        continue
    m = importlib.import_module(fn[:-3])
    for _, f in inspect.getmembers(m, inspect.isfunction):
        d = f.__doc__ or ""
        g = re.search(r"VPLAN:\s*([A-Z0-9\-, ]+)", d)
        if g:
            rows |= {x.strip() for x in g.group(1).split(",") if x.strip() != "(meta)"}
print(sorted(rows))
EOF
```

- [ ] **Step 3: Update the two summary counts**

Re-run the count script embedded in `docs/VPLAN.md` §4 and update the §8 totals
table, including the `Rows currently PASS` line.

- [ ] **Step 4: Update the README status table**

In `README.md`, replace the status table's `Rows passing | **0**` line with the real
number, and change `Divergences from current RTL | 12` to `| 0 (all resolved)`.

- [ ] **Step 5: Commit**

```bash
git add docs/VPLAN.md README.md
git commit -m "docs: mark rows discharged by the conformance work

All 12 divergences resolved. Updates VPLAN row statuses and the README
status table to the counts the suite actually reports.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Out of scope, and why

**Deferred to a follow-on plan — formal and netlist verification.** The 16 `SBY`
rows, both `EQY` rows, and the 9 `COCOTB` rows need a formal/simulation toolchain
stand-up (SymbiYosys, an SMT solver, `eqy`, a cocotb-compatible simulator) that is a
project in its own right. None of them blocks this plan, and this plan's green suite
is the sane starting point for them.

The most valuable single row in the whole document lives there: **BP-4**, proving
`rx_fifo` overflow unreachable across all reachable states. Task 6's
`test_no_overflow_under_sustained_pressure` samples that claim; it does not prove it.
That gap is deliberate and should not be forgotten.

**Deferred — the host-side console workflow.** `host/b8008net` still exposes the
retired command surface. Replacing it with a console-driven workflow that speaks the
monitor's `L`/`D`/`W`/`G` protocol is user-facing work that deserves its own spec
section and plan. Task 7 marks the surface rather than deleting it, so nothing new
depends on it in the meantime.

**Deferred — everything requiring hardware.** The 13 hardware-only rows are the
bring-up checklist in `VPLAN.md` §6 and stay `BLOCKED`.
