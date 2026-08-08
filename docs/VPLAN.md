# remote_8008 — Verification Plan

**Status:** Phases 1–4 complete. All 119 verification rows `UNIMPLEMENTED`; the 11
imported assumptions are `IMPORTED` and are never run here.
**Date:** 2026-08-08
**Contract for:** RTL that does not yet exist.
**Spec:** [`SPEC.md`](../SPEC.md) — every row cites a `S-<AREA>-<n>` ID.

---

## 0. Rules

1. Every row states an **observable pass/fail condition**. The words *correctly*,
   *properly*, *as expected*, *works*, and *reasonable* are banned from the Assertion
   and Conditions columns.
2. A row whose pass condition cannot be evaluated by a machine is not a row.
3. `Status` is one of: `UNIMPLEMENTED`, `IN-PROGRESS`, `PASS`, `FAIL`, `BLOCKED`,
   `IMPORTED`, `WAIVED`. A `WAIVED` row carries a written justification.
4. Rows marked **HW** can only be evaluated on real silicon. They are collected into
   the bring-up checklist (§6) and are `BLOCKED` until the board runs.

### Check-type legend

| Code | Means | Repo idiom it follows |
|---|---|---|
| `SBY` | SymbiYosys formal property (PSL/SVA), bounded or unbounded | new; the tooling is planned |
| `EQY` | Yosys equivalence check between two representations | new; the tooling is planned |
| `COCOTB-D` | directed cocotb test | new; replaces ad-hoc GHDL TBs for wrapper logic |
| `COCOTB-R` | constrained-random cocotb test | new |
| `GHDL-TB` | GHDL VHDL testbench | `make test-*` in the core repo |
| `VBENCH` | Verilator C++ bench driving buses directly | `sim/bench_tb.cpp`, `soc/bench_core.py` |
| `PYTEST` | host-side Python test | `host/tests/` |
| `SCRIPT` | program-level run + output check | `test_programs/verification_scripts/` + `checkpoint_lib.sh` |
| `HW` | requires the physical board | `soc/host_selftest.py` |

---

## 1. Assumptions imported from `intel-8008-vhdl` (§A)

`S-CORE-1` and `S-CORE-2` make the b8008 core a verified black box. These rows are
**never executed by this repository.** They record what is being trusted, where the
evidence lives, and what would have to be re-examined if a core-level defect ever
surfaces. Status `IMPORTED` is terminal.

| ID | Spec cite | Assumption | Discharged by | Status |
|---|---|---|---|---|
| A-1 | S-CORE-2 | Every b8008 submodule behaves per its testbench | 28 `make test-*` targets in `intel-8008-vhdl` | IMPORTED |
| A-2 | S-CORE-2 | ALU arithmetic matches a reference model over the full input space | `make test-alu-exhaustive`, 656,384 cases | IMPORTED |
| A-3 | S-CORE-2 | All 27 instruction classes are cycle-exact vs. the datasheet | `check_cycle_count_test.sh` vs. `docs/isa.json` | IMPORTED |
| A-4 | S-CORE-2 | The ISA executes on real ECP5 silicon | 46-test hardware ISA self-test, passed | IMPORTED |
| A-5 | S-CORE-2 | The core runs non-trivial period software | SCELBAL BASIC, HEXPAWN on hardware | IMPORTED |
| A-6 | S-CORE-4 | φ-cycle is 55 clocks = 2.2 µs at 25 MHz | `phase_clocks.vhdl:67-69`; `make test-phase-clocks` | IMPORTED |
| A-7 | S-CORE-5 | A T-state is two φ-cycles = 4.4 µs | `state_timing_generator.vhdl:7`; `make test-state-timing` | IMPORTED |
| A-8 | S-CORE-9 | `run_enable=0` freezes φ in place and emits no edge pulses | `phase_clocks.vhdl:89-109`; `make test-phase-clocks` | IMPORTED |
| A-9 | S-CORE-14 | The monitor implements `H`/`D`/`W`/`L`/`G` over the console | `b8008_monitor.asm`; hardware-validated | IMPORTED |
| A-10 | S-CDC-1 X1 | `RS232PHYRX` synchronizes `pads.rx` with a 2-FF `MultiReg` | `litex/soc/cores/uart.py:120` | IMPORTED |
| A-11 | S-CDC-1 X2 | The core's USART synchronizes `uart_rx` with a 2-FF synchronizer | `usart.vhdl:63-78` | IMPORTED |

**If an assumption is ever suspected**, the row names exactly which core-repo test to
re-run. No re-derivation from first principles, and no re-testing here.

---

## 2. Phase 1 — Spec walk

### 2.1 Clocks (`CLK`)

| ID | Spec cite | Assertion | Conditions | Check | Status |
|---|---|---|---|---|---|
| CLK-1 | S-CLK-1 | Every clock domain in the built design derives from `clk100`; the set of domains is exactly {`por`,`sys2x_i`,`sys2x`,`sys`,`b8008`,`eth_rx`,`eth_tx`} | post-elaboration netlist | `PYTEST` on elaborated SoC | UNIMPLEMENTED |
| CLK-2 | S-CLK-1 | `cd_sys` period is 13.333 ns ± 0; `cd_b8008` period is 40.000 ns ± 0 | post-PnR timing report | `PYTEST` parsing `.pnr` report | UNIMPLEMENTED |
| CLK-3 | S-CLK-3 | The generated constraint file declares `cd_sys` and `cd_b8008` in separate clock groups | build output inspected | `PYTEST` on generated `.lpf` | UNIMPLEMENTED |
| CLK-4 | S-CLK-3 | Zero timing paths exist between `cd_sys` and `cd_b8008` other than the three of `S-CDC-1` | post-PnR path report | `PYTEST` parsing timing report | UNIMPLEMENTED |
| CLK-5 | S-CLK-5 | `RS232PHY` emits a start bit every 8.6805 µs ± 1 `cd_sys` period when streaming | continuous TX of `0x55` | `COCOTB-D` | UNIMPLEMENTED |
| CLK-6 | S-CLK-6 | The baud divisor equals `round(75e6/115200)=651`; it is not derived from a platform attribute | elaboration | `PYTEST` on `B8008Core` params | UNIMPLEMENTED |
| CLK-7 | S-CORE-4 (A-6) | `dbg.phi1` rising edges are 55 `cd_b8008` periods apart while the core runs | core running, no stall | `COCOTB-D` | UNIMPLEMENTED |
| CLK-8 | S-CORE-6 | `dbg.sync` toggles exactly once per 55 `cd_b8008` periods | core running | `COCOTB-D` | UNIMPLEMENTED |

### 2.2 Reset (`RST`)

| ID | Spec cite | Assertion | Conditions | Check | Status |
|---|---|---|---|---|---|
| RST-1 | S-RST-1, S-RST-2 | Exactly two reset sources reach the design: POR and `rst_n`. No CSR write asserts any reset | elaborated netlist | `PYTEST` fan-in trace of every reset net | UNIMPLEMENTED |
| RST-2 | S-RST-3, S-RST-4 | `cd_b8008.rst` is driven by an `AsyncResetSynchronizer` whose input is `~pll.locked \| reset` | elaboration | `PYTEST` | UNIMPLEMENTED |
| RST-3 | S-RST-4 | `cd_b8008` reset **deasserts** synchronously to `cd_b8008`: the deassert edge occurs within 1 ns of a `cd_b8008` rising edge | 100 randomized PLL-lock arrival phases | `COCOTB-R` | UNIMPLEMENTED |
| RST-4 | S-RST-4 | `cd_b8008` reset **asserts** asynchronously: assertion is observed at the core's `i_rst` within 1 ns of the source condition, with `cd_b8008` stopped | clock stopped, then reset asserted | `COCOTB-D` | UNIMPLEMENTED |
| RST-5 | S-RST-5 R2 | POR holds for exactly 65,536 `clk100` cycles (655.36 µs) | cold start | `COCOTB-D` | UNIMPLEMENTED |
| RST-6 | S-RST-5 R3 | PLL reset does not deassert while `por_done=0` or `rst_n=0` | both conditions swept | `SBY` (safety property) | UNIMPLEMENTED |
| RST-7 | S-RST-6 | Console logic reset deasserts **strictly before** `cd_b8008` reset deasserts, by ≥1 `cd_sys` period | every reset event | `SBY` (ordering property, unbounded) | UNIMPLEMENTED |
| RST-8 | S-RST-6 | The first byte the core emits after reset is captured: `rx_fifo.level` reaches ≥1 and the first popped byte is `0x38` (`'8'`) | full cold boot in sim | `VBENCH` | UNIMPLEMENTED |
| RST-9 | S-RST-5 R8, S-CORE-12 | Auto-start fires 50,000 `cd_b8008` cycles (2.000 ms) after core reset deassert, ±1 cycle | cold boot | `COCOTB-D` | UNIMPLEMENTED |
| RST-10 | S-RST-7 | After boot completes, the first 5 bytes popped from `rx_fifo` are `"8008 "` | full boot | `VBENCH`, then `HW` | UNIMPLEMENTED |
| RST-11 | S-RST-8 | Immediately after reset deassert: `console_rx=0x00000000` and `console_tx=0x00000000` | read before the core emits its first byte | `COCOTB-D` | UNIMPLEMENTED |
| RST-11a | S-RST-8 | On a **cold** power-on with no prior state, `console_err=0x00000000` | cold start only | `COCOTB-D` | UNIMPLEMENTED |
| RST-12 | S-CSR-9 | A reset asserted while any `console_err` bit is set leaves that bit set after reset completes | each of the 3 bits, each reset source | `COCOTB-D` | UNIMPLEMENTED |
| RST-13 | S-RST-5 | Reset asserted during any single step R2–R9 returns the design to R1 and completes the full sequence, ending at `S-RST-7`'s condition | reset injected at each of the 8 steps | `COCOTB-D` × 8 | UNIMPLEMENTED |

### 2.3 Clock-domain crossings (`CDC`)

| ID | Spec cite | Assertion | Conditions | Check | Status |
|---|---|---|---|---|---|
| CDC-1 | S-CDC-1 | The design contains exactly three signals crossing between `cd_sys` and `cd_b8008`, and they are X1, X2, X3 | elaborated netlist | `PYTEST` structural scan | UNIMPLEMENTED |
| CDC-2 | S-CDC-1 X3 | X3 passes through ≥2 flip-flops clocked by `cd_b8008` before any fan-out | netlist | `PYTEST` structural scan | UNIMPLEMENTED |
| CDC-3 | S-CDC-3, S-CLK-2 | X3 is a level: it holds its value for ≥2 `cd_b8008` periods on every transition | 1000 randomized transition phases | `COCOTB-R` | UNIMPLEMENTED |
| CDC-4 | S-CDC-3, S-BP-3 | The stall reaches the core's `run_enable` within 3 `cd_b8008` periods of the `cd_sys`-side assertion | swept over all 3 sys/b8008 phase alignments | `COCOTB-D` | UNIMPLEMENTED |
| CDC-5 | S-CDC-4 | The design contains zero `PulseSynchronizer` instances | elaborated netlist | `PYTEST` | UNIMPLEMENTED |
| CDC-6 | S-CDC-2 | With a randomized metastability model injected on X3's first flop, `run_enable` never glitches for <1 `cd_b8008` period | 10,000 injected events | `COCOTB-R` | UNIMPLEMENTED |

### 2.4 RX path (`RX`)

| ID | Spec cite | Assertion | Conditions | Check | Status |
|---|---|---|---|---|---|
| RX-1 | S-RX-1 | `rx_fifo` depth is exactly 4096 | elaboration | `PYTEST` | UNIMPLEMENTED |
| RX-2 | S-RX-3 | Two consecutive reads of `console_rx` with no intervening pop return bit-identical 32-bit values | at levels 1, 2, 2048, 4095, 4096 | `COCOTB-D` × 5 | UNIMPLEMENTED |
| RX-3 | S-RX-3 | 1000 consecutive reads of `console_rx` with no pop leave `level` unchanged | level = 1 | `COCOTB-D` | UNIMPLEMENTED |
| RX-4 | S-RX-4 | One write to `console_rx_pop` decreases `level` by exactly 1 | at levels 1, 2, 4096 | `COCOTB-D` × 3 | UNIMPLEMENTED |
| RX-5 | S-RX-4 | *n* pops from a FIFO preloaded with a known *n*-byte sequence yield that sequence, in order, with no gap or repeat | *n* ∈ {1, 2, 255, 4096} | `COCOTB-D` × 4 | UNIMPLEMENTED |
| RX-6 | S-RX-7 | `console_rx` returns `0x00000000` while reset is asserted | reset held | `COCOTB-D` | UNIMPLEMENTED |
| RX-7 | S-RX-7 | `console_rx.data=0x00` and `valid=0` when `level=0` | FIFO drained | `COCOTB-D` | UNIMPLEMENTED |
| RX-8 | S-RX-8 | `console_rx.valid == (console_rx.level != 0)` on every read | unbounded | `SBY` (invariant) | UNIMPLEMENTED |
| RX-9 | S-RX-8 | The byte returned in `console_rx.data` is the byte the next `console_rx_pop` consumes | unbounded | `SBY` (invariant) | UNIMPLEMENTED |
| RX-10 | S-RX-8 | A read of `console_rx` concurrent with a FIFO push returns a `{data, valid, level}` triple consistent with a single point in time — never a level from after the push with data from before | push aligned to the read on every `cd_sys` phase | `SBY` (atomicity) | UNIMPLEMENTED |
| RX-11 | S-RX-9, S-CSR-5 | A pop at `level=0` leaves `level=0` and sets `console_err[2]` | FIFO empty | `COCOTB-D` | UNIMPLEMENTED |
| RX-12 | S-RX-9 | A pop at `level=0` does not corrupt the next byte to arrive: the byte pushed after the illegal pop is the byte the next legal pop returns | empty, illegal pop, push, pop | `COCOTB-D` | UNIMPLEMENTED |
| RX-13 | S-PROD-3 | Over a 100,000-byte pseudorandom stream, the sequence popped equals the sequence emitted from the core's `uart_tx` pin, byte for byte | randomized host drain latency 0–10 ms | `COCOTB-R` | UNIMPLEMENTED |
| RX-14 | S-RX-5 | A read of `console_rx` repeated *k* times (simulating transport retry) followed by one pop advances the stream by exactly one byte, for *k* ∈ 1..10 | at levels 1 and 4096 | `COCOTB-D` | UNIMPLEMENTED |

### 2.5 TX path (`TX`)

| ID | Spec cite | Assertion | Conditions | Check | Status |
|---|---|---|---|---|---|
| TX-1 | S-TX-1 | `tx_fifo` depth is exactly 256 | elaboration | `PYTEST` | UNIMPLEMENTED |
| TX-2 | S-TX-2 | A write while `full=0` increases `level` by exactly 1 | at levels 0, 1, 255 | `COCOTB-D` × 3 | UNIMPLEMENTED |
| TX-3 | S-TX-2 | A 256-byte known sequence written while never full is received at the core's `uart_rx` pin in that order, byte for byte | FIFO drained at line rate | `COCOTB-D` | UNIMPLEMENTED |
| TX-4 | S-TX-3, S-CSR-8 | A write while `full=1` leaves `level=256` and leaves every byte already in the FIFO unchanged | FIFO full | `COCOTB-D` | UNIMPLEMENTED |
| TX-5 | S-TX-3, S-TX-4 | A write while `full=1` sets `console_err[1]` | FIFO full | `COCOTB-D` | UNIMPLEMENTED |
| TX-6 | S-TX-3 | The byte rejected in TX-4 never appears at the core's `uart_rx` pin | FIFO full, then drained | `COCOTB-D` | UNIMPLEMENTED |
| TX-7 | S-TX-5 | `console_tx.full == (console_tx.level == 256)` on every read | unbounded | `SBY` (invariant) | UNIMPLEMENTED |
| TX-8 | S-TX-5 | `console_tx` returns `0x00000000` while reset is asserted | reset held | `COCOTB-D` | UNIMPLEMENTED |
| TX-9 | S-TX-6 | The wishbone `ack` for a `console_tx_data` write asserts within 2 `cd_sys` cycles regardless of `tx_fifo` state | at levels 0, 255, 256 | `SBY` (bounded liveness) | UNIMPLEMENTED |
| TX-10 | S-TX-6 | No wishbone cycle anywhere in the design is held for >2 `cd_sys` cycles by console logic | unbounded | `SBY` | UNIMPLEMENTED |
| TX-11 | S-PROD-3 | Over a 100,000-byte pseudorandom stream with writes gated on `full=0`, the sequence at the core's `uart_rx` pin equals the sequence written | randomized host write latency 0–10 ms | `COCOTB-R` | UNIMPLEMENTED |

### 2.6 Backpressure (`BP`)

| ID | Spec cite | Assertion | Conditions | Check | Status |
|---|---|---|---|---|---|
| BP-1 | S-BP-5 | Stall asserts on the `cd_sys` cycle after `rx_fifo.level` first reaches 4032 | level driven 4031→4032 | `COCOTB-D` | UNIMPLEMENTED |
| BP-2 | S-BP-5 | Stall deasserts on the `cd_sys` cycle after `level` first reaches 3968, and not before | level driven 4032→3969→3968 | `COCOTB-D` | UNIMPLEMENTED |
| BP-3 | S-BP-5 | Stall does not change state while 3968 < `level` < 4032 | level swept across the band in both directions | `SBY` (hysteresis invariant) | UNIMPLEMENTED |
| BP-4 | S-BP-8 | `rx_fifo` write-while-full **never** occurs | unbounded, all reachable states | `SBY` (unbounded safety — the headline property) | UNIMPLEMENTED |
| BP-5 | S-BP-9 | `console_err[0]` is `0` in every reachable state | unbounded | `SBY` (corollary of BP-4) | UNIMPLEMENTED |
| BP-6 | S-BP-9 | With BP-4's guard forcibly disabled, presenting a byte to a full `rx_fifo` sets `console_err[0]` | fault injection | `COCOTB-D` | UNIMPLEMENTED |
| BP-7 | S-BP-6 | At most 3 bytes enter `rx_fifo` after stall assertion | stall asserted mid-byte, at 20 randomized bit positions | `COCOTB-R` | UNIMPLEMENTED |
| BP-8 | S-BP-4 | While stalled, `dbg.phi1` and `dbg.phi2` hold constant and `dbg.sync` does not toggle, for ≥1,000,000 `cd_b8008` cycles | host never drains | `COCOTB-D` | UNIMPLEMENTED |
| BP-9 | S-BP-4 | No clock net in the design is driven by combinational logic | post-synthesis netlist | `PYTEST` structural scan | UNIMPLEMENTED |
| BP-10 | S-BP-7 | After a stall of ≥1,000,000 cycles, draining below LWM resumes execution, and the next byte emitted is the byte the monitor would have emitted with no stall | compare against an unstalled reference run | `COCOTB-D` | UNIMPLEMENTED |
| BP-11 | S-BP-7, S-BP-1 | Across a stall/resume cycle, zero bytes are lost: the popped stream equals the reference stream | 100 stall/resume cycles at random points | `COCOTB-R` | UNIMPLEMENTED |
| BP-12 | S-BP-4 | A stall asserted mid-T-state leaves `S0/S1/S2` at a value in the legal set {010,100,001,111,101,110,011} | 110 stall phases, one per `cd_b8008` cycle of a T-state | `COCOTB-R` | UNIMPLEMENTED |

### 2.7 Registers (`CSR`)

| ID | Spec cite | Assertion | Conditions | Check | Status |
|---|---|---|---|---|---|
| CSR-1 | S-CSR-1 | Reserved bits of every register read `0` | all 6 registers | `COCOTB-D` | UNIMPLEMENTED |
| CSR-2 | S-CSR-1 | Writing `0xFFFFFFFF` to reserved bits changes no observable state | all writable registers | `COCOTB-D` | UNIMPLEMENTED |
| CSR-3 | S-CSR-2 | Every register returns a value within 2 `cd_sys` cycles in every state including reset | all 6, reset asserted and deasserted | `SBY` (bounded liveness) | UNIMPLEMENTED |
| CSR-4 | S-CSR-3 | Reading any register leaves `rx_fifo.level`, `tx_fifo.level`, and `console_err` unchanged | 10,000 random reads at random FIFO levels | `COCOTB-R` | UNIMPLEMENTED |
| CSR-5 | S-CSR-4 | A `console_rx_pop` write of `0x00000000` pops exactly one byte, identically to a write of `0xFFFFFFFF` | level ≥ 2 | `COCOTB-D` | UNIMPLEMENTED |
| CSR-6 | S-CSR-6 | A pop landing on the same `cd_sys` cycle as a FIFO push yields `level` unchanged and the correct next byte | push/pop aligned on the same cycle | `SBY` | UNIMPLEMENTED |
| CSR-7 | S-CSR-7 | Bits 31:8 of a `console_tx_data` write do not reach `tx_fifo` | write `0xFFFFFF41`, expect `0x41` at the pin | `COCOTB-D` | UNIMPLEMENTED |
| CSR-8 | S-CSR-9 | A read of `console_err` leaves all its bits unchanged | each bit set, then read 100× | `COCOTB-D` | UNIMPLEMENTED |
| CSR-9 | S-CSR-11 | Writing `1` to a `console_err_clear` bit clears exactly that `console_err` bit and no other | all 3 bits, all 8 combinations | `COCOTB-D` × 8 | UNIMPLEMENTED |
| CSR-10 | S-CSR-10 | Writing `0` to a `console_err_clear` bit leaves the corresponding bit unchanged | bit set, write `0` | `COCOTB-D` | UNIMPLEMENTED |
| CSR-11 | S-CSR-12 | A clear coinciding on the same `cd_sys` cycle with the set condition leaves the bit **set** | all 3 bits, exact-cycle alignment | `SBY` (set-wins arbitration) | UNIMPLEMENTED |
| CSR-12 | S-CSR-2 | Back-to-back accesses on consecutive `cd_sys` cycles to any ordered pair of registers each produce the value they would in isolation | all 36 ordered pairs | `COCOTB-R` | UNIMPLEMENTED |
| CSR-13 | S-CSR-1b | The `console` bank in the generated `csr.csv` contains exactly the six named registers and no others | generated `csr.csv` | `PYTEST` | UNIMPLEMENTED |
| CSR-14 | S-CSR-1a | No test or host source file contains a literal CSR address; every address is read from `csr.csv` | source scan of `host/` and the test suite | `PYTEST` | UNIMPLEMENTED |

### 2.8 Wire contract (`WIRE`)

| ID | Spec cite | Assertion | Conditions | Check | Status |
|---|---|---|---|---|---|
| WIRE-1 | S-WIRE-2, S-WIRE-1 | Etherbone is elaborated with `buffer_depth=255` | elaboration | `PYTEST` | UNIMPLEMENTED |
| WIRE-2 | S-WIRE-2, S-WIRE-5 | A 255-word burst write completes with all 255 words landing, none dropped | full-depth burst | `HW` | UNIMPLEMENTED |
| WIRE-3 | S-WIRE-2 | A 256-word burst write is either fully accepted or rejected as a unit — never partially applied | over-depth burst | `HW` | UNIMPLEMENTED |
| WIRE-4 | S-WIRE-3 | A host read of *n* words issues *n* UDP round trips | measured packet count for *n* ∈ {1,16,255} | `PYTEST` with a mock transport | UNIMPLEMENTED |
| WIRE-5 | S-WIRE-6, S-RX-6 | A read whose reply is dropped, then retried, yields the same value and advances no FIFO | reply-drop injected on 10% of reads | `PYTEST` with a lossy mock transport | UNIMPLEMENTED |
| WIRE-6 | S-RX-8, S-WIRE-4 | The host obtains `{data, valid, level}` in one round trip; no host code path reads level and data separately | source inspection + packet count | `PYTEST` | UNIMPLEMENTED |
| WIRE-7 | S-PROD-3 | Over 100,000 bytes with 5% packet loss injected in both directions, the host-assembled stream equals the core's emitted stream | lossy mock transport | `PYTEST` | UNIMPLEMENTED |

### 2.9 End-to-end (`E2E`)

These reuse the core repo's existing answer keys (`S-CORE-2`). They author no new
expected values.

| ID | Spec cite | Assertion | Conditions | Check | Status |
|---|---|---|---|---|---|
| E2E-1 | S-CORE-14 | Sending `"H\r"` yields a response containing `"Help"` | booted, idle | `VBENCH`, then `HW` | UNIMPLEMENTED |
| E2E-2 | S-CORE-14 | `"D 0000\r"` yields a line matching `^[0-9A-F]{4} - [0-9A-F]{2}` | booted, idle | `VBENCH`, then `HW` | UNIMPLEMENTED |
| E2E-3 | S-CORE-14 | `"W 0100 5A\r"` followed by `"D 0100\r"` yields `0100 - 5A` | booted, idle | `VBENCH`, then `HW` | UNIMPLEMENTED |
| E2E-4 | S-CORE-14, S-TX-8 | An Intel HEX image sent via `L` at the host's paced rate reads back identical via `D` for every byte | each of the 31 core-repo test programs | `SCRIPT` over the console, then `HW` | UNIMPLEMENTED |
| E2E-5 | S-CORE-2 | Each core-repo verification program, loaded via `L` and started via `G`, produces the console output its `check_*.sh` script already asserts | all 31 programs | `SCRIPT` reusing `checkpoint_lib.sh` | UNIMPLEMENTED |
| E2E-6 | S-PROD-3 | A `D` dump of the full 16 KB space transfers with zero byte loss and zero duplication while backpressure engages at least once | drain deliberately throttled to force stalls | `VBENCH`, then `HW` | UNIMPLEMENTED |
| E2E-7 | S-CORE-8, S-CORE-15, S-TX-7, S-PROD-6 | Sending an Intel HEX image **unpaced** at line rate produces a readback mismatch | negative test — documents the boundary of `S-PROD-6` | `VBENCH` | UNIMPLEMENTED |

**E2E-7 is a test that must fail to load.** It exists to prove the guarantee boundary
of `S-PROD-6` is real and that the host's pacing obligation is not theoretical. A
build in which E2E-7 *passes* means either the pacing hazard has been silently fixed
somewhere — in which case `S-PROD-6` and `S-TX-8` are wrong and must be revised — or
the test is not actually sending unpaced.

### 2.10 Toolchain equivalence (`EQ`)

| ID | Spec cite | Assertion | Conditions | Check | Status |
|---|---|---|---|---|---|
| EQ-1 | S-CORE-3 | `build/b8008_net_core.v` is logically equivalent to `src/b8008_net_core.vhdl` | full module, all ports | `EQY` | UNIMPLEMENTED |
| EQ-2 | S-CORE-3 | The synthesized ECP5 netlist is equivalent to `build/b8008_net_core.v` | post-`synth_ecp5` | `EQY` | UNIMPLEMENTED |
| EQ-3 | scar tissue L-5 | Every byte of the ROM image in the synthesized netlist equals the corresponding byte of `src/rom_baked.mem` | all 4096 bytes | `PYTEST` on the netlist's init strings | UNIMPLEMENTED |

**EQ-1 closes a gap that exists today.** The GHDL→Verilog conversion is currently
trusted on faith: the gate-level sim tier proves the netlist *boots*, which is not the
same claim as *is equivalent*. A conversion bug that only manifests on an untaken
branch would pass the boot sim and reach silicon.

### 2.11 Structural (`STR`)

| ID | Spec cite | Assertion | Conditions | Check | Status |
|---|---|---|---|---|---|
| STR-1 | S-PROD-8, D-8 | No CSR named `ctl` exists; no register exposes `run_stop`, `step_cycle`, `step_sync`, `int_req`, or `int_vector` | generated `csr.csv` | `PYTEST` | UNIMPLEMENTED |
| STR-2 | D-9, S-CORE-11 | No register exposes a `triggered` field | `csr.csv` | `PYTEST` | UNIMPLEMENTED |
| STR-3 | S-PROD-8, D-10 | No wishbone memory region maps the 8008's RAM into the host address space | generated `csr.csv` / region map | `PYTEST` | UNIMPLEMENTED |
| STR-4 | S-PIN-1, S-PIN-2 | All 15 debug signals are constrained to the X3 sites named in `_b8008_dbg_io` | generated `.lpf` | `PYTEST` | UNIMPLEMENTED |
| STR-5 | S-PIN-3, S-PROD-9 | With every X3 pin left unconnected, all `RX`, `TX`, `BP`, and `CSR` rows still pass | full regression, pins floating | `COCOTB-D` re-run | UNIMPLEMENTED |
| STR-6 | S-ARCH-1, S-ARCH-2 | No multi-bit bus crosses between `cd_sys` and `cd_b8008` | elaborated netlist | `PYTEST` structural scan | UNIMPLEMENTED |

---

## 3. Phase 2 — Cross products

### 3.1 Axes

The originally proposed "divergence present/absent" axis is dropped: `S-PROD-8` and
§15 U-5 remove lockstep from the product, so it has no values. A proposed separate
"backpressure engaged/not" axis is **folded into axis A**, because `STALLED` *is*
backpressure engaged — keeping both would have double-counted one axis and generated
contradictory cells.

| Axis | Values | *n* |
|---|---|---|
| **A. CPU state** | RUNNING, STALLED, RESET, WAIT, STOPPED | 5 |
| **B. Host access timing** | idle, single, back-to-back, coincident-with-internal-update, retried | 5 |
| **C. FIFO occupancy** | empty, 1, mid, LWM, HWM−1, HWM, full | 7 |
| **D. `cd_sys`/`cd_b8008` phase alignment** | 3 (75/25 = 3:1) | 3 |

Full product: 5 × 5 × 7 × 3 = **525 cells.**

### 3.2 Pruning, applied in sequence

Each row prunes from what the previous row left, so the counts are disjoint and the
remainder column is exact.

| # | Pruned group | Removed | Left | Justification |
|---|---|---|---|---|
| 1 | A=STOPPED | 105 | 420 | `S-CORE-13`: the core auto-starts and the product issues no run command (`S-PROD-2`). STOPPED is unreachable after R8. Replaced by one reachability proof — **X-1**. |
| 2 | A=WAIT | 105 | 315 | WAIT is entered only when `ready=0`. The core's RAM and ROM are single-cycle BRAM that never deassert ready. Unreachable. Replaced by one reachability proof — **X-2**. |
| 3 | A=RESET × C≠empty | 90 | 225 | `S-RST-8`: both FIFOs are empty during reset. Six of the seven C values are unreachable while A=RESET. |
| 4 | A=STALLED × C ∈ {empty, 1, mid} | 45 | 180 | `S-BP-5`: the stall deasserts at LWM=3968 and cannot be asserted below it. While A=STALLED, C ∈ {LWM, HWM−1, HWM, full} only. |
| 5 | A=RUNNING × C=full | 15 | 165 | `S-BP-8`: `level=4096` while not stalled is precisely the state BP-4 proves unreachable. Discharged by proof, not by enumeration. |

**165 cells reach the per-row stage.** Axes D and B are then collapsed per row rather
than globally, because their relevance depends on what the row touches:

| Collapse | Applied to | Justification |
|---|---|---|
| **D → 1** (from 3) | every row not touching X1, X2, or X3 | Only three signals cross domains (`S-CDC-1`, enforced structurally by CDC-1 and STR-6). All CSR and FIFO logic is single-domain `cd_sys` and is phase-invariant by construction. D is swept only on CDC-3, CDC-4, CDC-6, RST-3, RST-4, BP-7, and BP-12. |
| **B: drop `coincident`** | `console_tx`, `console_tx_data`, `console_rx_pop`, `console_err_clear` | These have no autonomous update source that can coincide with a host access. Coincidence is meaningful only where hardware writes the same state the host reads: `console_rx` (FIFO push) and `console_err` (set condition). Covered by RX-10, CSR-6, and CSR-11. |
| **B: drop `retried`** | every write-only register | `CommUDP` retries reads, not writes (`S-RX-6`). Retry is meaningful only on `console_rx`, `console_tx`, and `console_err`, and is covered by RX-14 and WIRE-5. |

**The surviving cells are enumerated as the rows in §2 plus X-3 through X-6.** No cell
is covered by nothing, and §7 checks the inverse direction — that no spec statement is
covered by nothing.

### 3.3 New rows from the cross product

| ID | Spec cite | Assertion | Conditions | Check | Status |
|---|---|---|---|---|---|
| X-1 | S-CORE-13 | `S0/S1/S2 = 011` (STOPPED) never occurs after R8 completes | unbounded, product configuration | `SBY` (unreachability) | UNIMPLEMENTED |
| X-2 | S-CORE-7 | The core's WAIT state is never entered | unbounded, product configuration | `SBY` (unreachability) | UNIMPLEMENTED |
| X-3 | axes A×C | A reset asserted while A=STALLED deasserts the stall and completes the R1–R9 sequence | reset at 20 random points during a stall | `COCOTB-R` | UNIMPLEMENTED |
| X-4 | axes B×C | A back-to-back pop pair straddling the empty boundary (level 1→0) pops one byte and sets `console_err[2]` exactly once | level=1, two pops on consecutive cycles | `COCOTB-D` | UNIMPLEMENTED |
| X-5 | axes B×C | A back-to-back write pair straddling the full boundary (level 255→256) pushes one byte and sets `console_err[1]` exactly once | level=255, two writes on consecutive cycles | `COCOTB-D` | UNIMPLEMENTED |
| X-6 | axes A×C | A reset asserted with `rx_fifo` at HWM leaves `console_rx.level=0` and the stall deasserted after R9 | level=4032, reset | `COCOTB-D` | UNIMPLEMENTED |

**X-1 and X-2 are unreachability proofs, not tests.** They are how a pruning
justification gets checked rather than assumed. If either fails, 418 pruned cells come
back into scope.

---

## 4. Phase 3 — Check assignment summary

| Check type | Rows | Rationale |
|---|---|---|
| `COCOTB-D` | 42 | Directed cases with a named stimulus and a named expected value. The default for anything with a specific boundary. |
| `PYTEST` | 26 | Structural and elaboration-time claims, plus host-side transport behavior against a mock. No RTL simulation needed; these run in seconds and should gate every commit. |
| `SBY` | 16 | Invariants, orderings, arbitration, and unreachability — claims quantified over *all* reachable states. A sampling test cannot establish BP-4, X-1, or X-2 at all. |
| `COCOTB-R` | 11 | Where the interesting condition is a *phase* or an *interleaving* rather than a value — metastability injection, stall-during-byte, randomized host latency. |
| `VBENCH` | 7 | Full-boot and monitor-interaction claims needing the real converted netlist. The pre-hardware gate, per the existing `sim/bench_tb.cpp` idiom. |
| `HW` | 13 | Requires silicon: 2 in §2 (WIRE-2, WIRE-3) plus the 11 checklist rows of §6. |
| `SCRIPT` | 2 | Program-level reuse of the core repo's 31 existing answer keys, via `checkpoint_lib.sh`. |
| `EQY` | 2 | Representation equivalence. Not expressible as a test. |

These counts are **derived from this file, not maintained by hand** — regenerate with:

```bash
python3 - <<'EOF'
import re, collections
rows, ct = [], collections.Counter()
for line in open('docs/VPLAN.md'):
    if not line.startswith('|'): continue
    c = [x.strip() for x in re.split(r'(?<!\\)\|', line.strip().strip('|'))]
    if len(c) == 6 and re.match(r'^(CLK|RST|CDC|RX|TX|BP|CSR|WIRE|E2E|EQ|STR|X)-\d+[a-z]?$', c[0]):
        rows.append(c[0])
        for code in ['SBY','EQY','COCOTB-D','COCOTB-R','PYTEST','VBENCH','SCRIPT','HW']:
            if re.search(r'`'+re.escape(code)+r'`', c[4]):
                ct[code] += 1; break
hw = sum(1 for l in open('docs/VPLAN.md') if re.match(r'^\| \d+ \| HW-\d+', l))
print('rows', len(rows)+hw, dict(ct), 'checklist', hw)
EOF
```

Restating a count by hand is exactly the failure L-4 records. The plan holds itself to
the rule it imposes on the RTL.

### 4.1 Why `SBY` and not more tests

BP-4 — *`rx_fifo` never overflows* — is the product's central safety claim. A directed
test shows it did not overflow **in the cases run**. A random test shows it did not
overflow **in the cases sampled**. Neither is the claim. The claim is over all
reachable states, and only a proof delivers it. The same reasoning puts RST-7
(ordering), CSR-11 (arbitration), RX-8/RX-9/TX-7 (invariants), and X-1/X-2
(unreachability) in the formal column.

### 4.2 Why `COCOTB` and not GHDL testbenches

The core repo's GHDL testbench idiom is right for VHDL modules with simple port
lists. The wrapper's device under test is a Migen-generated CSR bank talking to a
wishbone bus with a host model on the other side. cocotb lets the host model be
written in the same Python as `b8008net`, so the transport-retry scenarios (RX-14,
WIRE-5) exercise the *actual* host logic rather than a re-typed imitation of it —
which is scar-tissue lesson L-4 applied.

---

## 5. Phase 4 — Lessons carried forward

### 5.1 Imported from b8008 history

**L-1 — The decoder bug an independent model found.**
A defect survived the project's own tests because those tests encoded the same
misunderstanding as the design. It was found only when a model that had not seen the
design derived the expected behavior independently.
*Applied:* E2E-5 reuses the core repo's 31 answer keys rather than authoring new
expectations. EQ-1 checks the netlist against the VHDL rather than against a boot that
happens to work. Never grade the work with an answer key derived from the work.

**L-2 — The v8008 collapse.**
An attempted multi-cycle implementation grew until no one could reason about it, and
was abandoned. b8008 replaced it with small modules that each do one job.
*Applied:* `S-ARCH-1`/`S-ARCH-2` — the byte path crosses clock domains as a serial
line, giving exactly three crossings (CDC-1) instead of a parallel-data mesh. The
scope reductions of `S-PROD-8` are the same instinct: the product got smaller on
purpose. CDC-1 and STR-6 are the rows that keep it small.

**L-3 — s8008 timing.**
The single-cycle implementation could not meet real timing, and this was discovered
late.
*Applied:* CLK-2, CLK-3, CLK-4 are structural checks on the generated constraints,
not post-hoc report reading. BP-9 forbids combinational clock gating outright —
`S-BP-4` uses `run_enable`, which is precisely the fix already made once in
`debug_clock_control` when a gated `clk_out` caused LUT-on-clock skew.

**L-4 — The SA bit-reversal that survived a year.**
`test_microcode_gen` restated the bit packing instead of deriving it from
`microcode_gen.py`, so the test agreed with the bug.
*Applied:* CLK-6, RX-1, TX-1, WIRE-1, CSR-13 and STR-1..4 read their expected values
from generated artifacts (`csr.csv`, the `.lpf`, elaboration parameters), never from
constants retyped into the test. A test that restates a value cannot detect that the
value changed.

**L-5 — The yosys ROM 0xff bug.**
A toolchain defect corrupted ROM contents; it is written up in
`docs/bug-report-yosys-rom-0xff.md`.
*Applied:* EQ-3 checks the ROM image byte-for-byte in the synthesized netlist. The
toolchain is a component under test, not a trusted authority.

**L-6 — Etherbone `buffer_depth`.**
The LiteEth default of 16 silently overflows on the 255-word bursts `RemoteClient`
uses. A library default tuned for a different use case failed quietly.
*Applied:* WIRE-1 asserts the value structurally; WIRE-2 and WIRE-3 exercise the
boundary and the over-boundary case on hardware.

**L-7 — `litex_server`'s read clamp.**
`read_max_length={"CommUDP": 1}` plus a read-merger that downgrades `burst="fixed"`
means a requested burst silently becomes *n* round trips. The library did something
other than what was asked, without complaint.
*Applied:* WIRE-4 measures actual packet count rather than trusting the request. The
spec then *accepts* the behavior (`S-WIRE-4`) rather than pretending otherwise.

**L-8 — The `cc1` dylib crash reported as "internal compiler error".**
A missing dependency surfaced as a misleading message about the user's code.
*Applied:* a process note rather than a row — when a tool reports a fault in your
input, confirm the tool is intact before believing it.

### 5.2 Standard system-level classes

**L-9 — Reset during everything.** RST-13 injects a reset at each of the 8 sequence
steps; X-3 injects during a stall; X-6 injects at HWM; RST-12 checks sticky-bit
survival. Reset is the single most under-tested input in most designs because it is
usually applied only at time zero.

**L-10 — CDC metastability.** CDC-6 injects a metastability model on X3 over 10,000
events. CDC-2 checks the synchronizer exists structurally, because a synchronizer
accidentally optimized away still simulates without error and fails only on silicon,
intermittently, months later.

**L-11 — FIFO boundary conditions.** Every FIFO row is stated at a boundary
(0, 1, *n*−1, *n*), never at a comfortable middle: RX-2, RX-4, RX-5, TX-2, X-4, X-5.
BP-1/BP-2/BP-3 do the same for the hysteresis band.

**L-12 — Back-to-back CSR access.** CSR-12 covers all 36 ordered register pairs on
consecutive cycles. Registers verified only in isolation routinely fail when a second
access arrives before the first has settled.

**L-13 — Host reads during update.** RX-10 (read racing a FIFO push), CSR-6 (pop
racing a push), CSR-11 (clear racing a set). Each of these is a torn-read or
lost-update hazard, and each is assigned to `SBY` rather than to a test, because the
failing alignment is one cycle wide.

**L-14 — Silence is not success.** `S-BP-9`'s canary, `S-TX-3`'s rejection flag, and
`S-RX-9`'s empty-pop flag all exist so that a failure produces evidence. The original
design silently dropped bytes in three separate places (D-5, D-7, and the destructive
retry of D-3). Every one of them would have presented as "the monitor output looks a
bit odd sometimes."

---

## 6. Bring-up checklist — hardware-only rows

These cannot be evaluated in simulation. They are `BLOCKED` until the board runs, and
they are the content of Tasks 13–15. Order matters: each step's precondition is the
previous step's pass.

| # | ID | Pass condition | Instrument |
|---|---|---|---|
| 1 | HW-1 | Bitstream loads; the ECP5 `DONE` pin asserts | board LED / programmer |
| 2 | HW-2 | PLL locks and `cd_sys` runs at 75.000 MHz ±100 ppm | scope on a routed test pin |
| 3 | HW-3 | `dbg.phi1` shows a 2.2 µs period, φ1 high 0.8 µs, φ2 high 0.6 µs, dead times 0.4 µs | scope on X3 — confirms A-6 on silicon |
| 4 | HW-4 | `dbg.sync` toggles once per φ-cycle | scope on X3 — confirms A-7 |
| 5 | HW-5 | The board acquires a DHCP lease with hostname `b8008` | DHCP server log |
| 6 | HW-6 | `RemoteClient` connects and the SoC identifier reads `b8008_net` | `host_selftest.py` check 1 |
| 7 | HW-7 | `console_rx.level` becomes non-zero within 3 s of power-on and the first 5 bytes are `"8008 "` | `host_selftest.py` — this is `S-RST-7`, "up" |
| 8 | HW-8 | `"H\r"` returns a response containing `"Help"` | `host_selftest.py` check 4 |
| 9 | WIRE-2 | A 255-word burst write lands in full | extended `host_selftest.py` |
| 10 | WIRE-3 | A 256-word burst write is all-or-nothing | extended `host_selftest.py` |
| 11 | HW-9 | `console_err` reads `0x00000000` after a 1-hour idle soak | polling script |
| 12 | HW-10 | A full 16 KB `D` dump transfers with zero loss while `console_rx.level` reaches HWM at least once | throttled drain; this is E2E-6 on silicon |
| 13 | E2E-4 | All 31 core-repo programs load via `L` and read back byte-identical | generalized `host_selftest.py` |
| 14 | E2E-5 | All 31 programs run via `G` and produce the output their `check_*.sh` asserts | generalized `host_selftest.py` |
| 15 | HW-11 | Workflow parity: every operation available over the serial-era monitor is available over the LAN, with identical monitor responses | side-by-side transcript diff |

**HW-3 and HW-4 are the only rows that re-verify an imported assumption.** That is
deliberate: A-6 and A-7 were discharged in simulation and on a *different* board
configuration, and every timing number in `SPEC.md` §5 is derived from them. Two
scope measurements are cheap insurance against a whole spec section resting on a
mis-parameterized generic.

---

## 7. Coverage of `SPEC.md`

`SPEC.md` defines **100** `S-*` IDs. **83 are cited in the Spec-cite column** of at
least one row.

The remaining **17 are uncited by design.** Each is a definition, a rationale, an
exclusion, or a pointer — none states a behavior a machine could check. They are
listed individually rather than waved past, because "the rest are non-normative" is
the sentence under which a real requirement goes missing.

| ID | Kind | Why no row |
|---|---|---|
| S-PROD-1 | definition | States what the product is. |
| S-PROD-2 | scope declaration | Its content is enforced negatively by STR-1 and STR-3. |
| S-PROD-4 | negative guarantee | Asserts the *absence* of a real-time promise. Nothing to observe; its consequences are BP-7, BP-8, BP-10. |
| S-PROD-5 | definition | Names the boundary `S-PROD-3` holds across. Tested through every row citing `S-PROD-3`. |
| S-PROD-7 | exclusion | Places Ethernet/IP/DHCP outside the guarantee. An exclusion has no pass condition. |
| S-CORE-1 | definition | Declares the core a black box. Its consequence is §1's `IMPORTED` rows. |
| S-CORE-10 | imported fact | Describes `run_stop` for a control the product does not expose. STR-1 asserts its absence. |
| S-CLK-4 | imported | LiteEth's stock false-path handling, explicitly not specified here. |
| S-CLK-7 | derivation | Arithmetic (⌊86.805 / 22⌋ = 3) supporting `S-CORE-15`. Its consequence is E2E-7. |
| S-RX-2 | structure | Names which registers serve the RX path. Exercised by every RX row. |
| S-TX-9 | rationale | Explains why slow loading is accepted. Design intent. |
| S-BP-2 | rationale | Explains why stalling is legal given `S-PROD-4`. |
| S-BP-10 | pointer | Directs `S-BP-8` to BP-4. The row it points at is the check. |
| S-WIRE-7 | UNSPECIFIED pointer | Forwards to U-3. |
| S-NET-1 | imported | DHCP behavior. Reachability is checked by HW-5 in §6, whose table carries no Spec-cite column. |
| S-NET-2 | exclusion | Declares `S-NET-1` out of scope. |
| S-PIN-4 | rationale | Explains why §14 exists. |

Regenerate this classification — note the split on `(?<!\\)\|`, because escaped pipes
inside cells will otherwise shift the column index and under-report citations:

```bash
python3 - <<'EOF'
import re
spec = open('SPEC.md').read(); vp = open('docs/VPLAN.md').read()
ids = sorted(set(re.findall(r'S-[A-Z]+-\d+[ab]?', spec)))
ROW = re.compile(r'^(A|CLK|RST|CDC|RX|TX|BP|CSR|WIRE|E2E|EQ|STR|X|HW)-\d+[a-z]?$')
cited = set()
for line in vp.split('\n'):
    if not line.startswith('|'): continue
    cells = [c.strip() for c in re.split(r'(?<!\\)\|', line.strip().strip('|'))]
    k = next((i for i, c in enumerate(cells) if ROW.match(c)), None)
    if k is not None and k + 1 < len(cells):
        cited |= set(re.findall(r'S-[A-Z]+-\d+[ab]?', cells[k + 1]))
unc = [i for i in ids if i not in cited]
print(f'{len(ids)} ids, {len(ids)-len(unc)} cited, {len(unc)} uncited:', unc)
EOF
```

**UNSPECIFIED items U-1..U-8 have no rows, by definition.** A row testing undefined
behavior would define it, which is the opposite of what the register records.

**Divergences D-1..D-12 each have a row**: D-1→RST-2, D-2→RST-7, D-3→RX-2/RX-3,
D-4→RX-8/RX-10, D-5→BP-4/BP-6, D-6→BP-1..BP-12, D-7→TX-4/TX-5, D-8→STR-1/CDC-5,
D-9→STR-2, D-10→STR-3, D-11→CSR-8..CSR-11, D-12→CLK-3/CLK-4.

---

## 8. Totals

Counts below are produced by the script in §4, not typed in.

| | Count |
|---|---|
| Imported assumptions (never run here) | 11 |
| Verification rows | 119 |
| — of which formal (`SBY` + `EQY`) | 18 |
| — of which hardware-only | 13 |
| Cross-product cells pruned before the per-row stage | 360 of 525 |
| Deliberate UNSPECIFIED items | 8 |
| Divergences from current RTL | 12 |
| Rows currently `PASS` | **0** |
