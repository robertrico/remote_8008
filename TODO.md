# TODO — Verification to core-repo rigor (SBY / EQY / cocotb)

> **Untracked working document.** Not committed by design. When a phase lands,
> its rows flip in `docs/VPLAN.md` (the tracked source of truth) and get
> deleted here.

## Goal

Every one of the 133 VPLAN rows ends in exactly one of two states:
`PASS` (a machine-checked artifact that fails when the behavior is wrong) or
`WAIVED` (a written justification in VPLAN §rules-3 form). Zero
`UNIMPLEMENTED` rows. Current: 44 PASS / 77 UNIMPLEMENTED / 1 SUPERSEDED /
11 IMPORTED.

## Scope charter (rulings that gate every row below)

1. **The b8008 core is fully verified upstream** (`intel-8008-vhdl`: 28 unit
   TBs, exhaustive ALU, cycle-exact ISA, formal SBY+miters+EQY, differential
   fuzzer, silicon-validated). Any row whose failure could only be caused by
   a core defect is **re-verification of an import** → WAIVE citing the A-row.
2. **Firmware (DHCP / udp fork / software Etherbone) is done**: 100%-line
   host-tested, mutation-validated, CI-gated. No further firmware rows.
3. **The product is the harness**: deliver bytes between a host socket and
   the core's UART pins, faithfully, over the wire. Rows that prove *that*
   are the point — the console bridge, the CDC/reset/clock wrapper, the wire
   transport, and end-to-end program delivery.

---

## Phase 0 — Infrastructure (blocks everything below)

- [ ] **0.1 cocotb runner.** `sim/cocotb/` + Makefile pattern target
      (`cocotb-<name>`), mirroring core repo's `sim/cocotb/test_b8008_top.py`
      harness. DUT = the *converted netlist* (`build/b8008_net_core.v` +
      `ghdl_gates.v`) wrapped with the Migen-emitted ConsoleBridge verilog, so
      both clock domains are real. Verilator backend (icarus too slow at
      2048-deep FIFOs). Deliverable: one smoke test (reset, banner byte
      appears in rx_fifo) green in CI.
- [ ] **0.2 ConsoleBridge SBY flow.** Emit standalone Verilog for the
      ConsoleBridge + CDC + backpressure logic via `migen.fhdl.verilog.convert`
      (idiom already in `soc/test_integration.py::test_elaborates`), then
      `formal/console_bridge/` with `.sby` (mode bmc + prove, k-induction like
      core repo's `state_timing_generator.sby`). Properties in SVA on a
      `bind`-style wrapper. Deliverable: one trivial invariant proven.
- [ ] **0.3 EQY flow.** Port core repo's `formal/eqy/` recipe:
      `eqy -f <module>.eqy` comparing two netlists. Deliverable: EQ-1 running
      (even if partitioned/assisted).
- [ ] **0.4 Makefile + CI wiring.** `make formal`, `make cocotb`, `make eqy`
      umbrella targets; CI matrix jobs per module like core repo's
      verification.yml `sby ${{ matrix.module }}`.
- [ ] **0.5 Console driver for E2E.** Python console client class over
      `Board` (pop/push via console CSRs with S-TX-9 pacing) — extracted from
      `b8008net.console`'s session code so scripts can drive the monitor
      programmatically. Needed by Phase 4 and 0.6.
- [ ] **0.6 `b8008net load` — bulk send over the wire.** The monitor has `L`
      (Intel-HEX load) but no host tool speaks it effectively yet. CLI
      subcommand (`make load FILE=prog.hex [HOST=…]` / `b8008net.cli load`):
      * open the console via 0.5, enter `L`, stream the HEX record-by-record;
      * **pacing is the whole problem** (S-PROD-6: the byte-loss guarantee
        ends at the core's `uart_rx` pin; S-TX-9: never write while
        `console_tx.full`; SPEC §5.4 pacing arithmetic gives the sustainable
        byte rate — the 8008 polls its USART between monitor loop
        iterations, so the ceiling is monitor-side, not FIFO-side);
      * strategy: gate every byte on `console_tx.full == 0` **and** throttle
        to the §5.4 rate; drain/parse the monitor's echo + per-record
        acknowledgment (checksum error responses) as flow control — the echo
        stream is the only ground truth that a record actually landed;
      * verify mode: `D`-dump the written range afterward and diff against
        the image (this is E2E-4's check — the tool and the row share code);
      * reuse the core repo's `send_hex.py` as the reference for record
        framing + what the monitor's `L` expects on the wire;
      * failure reporting: first mismatching record/byte, not just pass/fail.
      Deliverable feeds E2E-4/E2E-5 directly (E2E-5's runner = 0.6 load +
      `G` + checkpoint assertions).

## Phase 1 — SBY formal rows (ConsoleBridge + wrapper; 15 rows)

All on the 0.2 flow. Group by property class; one `.sby` per module, tasks
per row like the core repo.

- [ ] RX-8  `console_rx.valid == (level != 0)` — invariant
- [ ] RX-9  `console_rx.data` is the byte the next pop consumes — invariant
- [ ] RX-10 read concurrent with push yields a consistent {data,valid,level}
      snapshot — atomicity
- [ ] TX-7  `console_tx.full == (level == 256)` — invariant
- [ ] TX-9  wishbone ack for `console_tx_data` within 2 `cd_sys` cycles in
      every state — bounded liveness
- [ ] TX-10 no wishbone cycle held >2 `cd_sys` cycles by console logic
- [ ] BP-3  stall state constant while 3968 < level < 4032 — hysteresis
- [ ] BP-4  **headline**: `rx_fifo` write-while-full never occurs — unbounded
      safety (k-induction)
- [ ] BP-5  corollary: `console_err[0]` never set in any reachable state
- [ ] CSR-3 every register read returns within 2 `cd_sys` cycles incl. reset
- [ ] CSR-6 pop landing same-cycle as push: level unchanged, correct byte
- [ ] CSR-11 set-wins arbitration on sticky-bit clear vs set
- [ ] RST-6 PLL reset holds while `por_done=0 || rst_n=0` — needs the CRG in
      the formal netlist; if the PLL primitive blocks it, restate on the
      generated reset-controller logic and note the boundary
- [ ] RST-7 console reset deasserts strictly before `cd_b8008` reset — 2-clock
      formal model; core repo's `mcc_stg_cluster` shows the multi-domain idiom

## Phase 2 — cocotb rows (netlist + both domains; 20 rows)

On the 0.1 runner. Order: cheap directed first, randomized last.

- [ ] RX-6  `console_rx` reads 0 during reset — directed
- [ ] RST-11 both console CSRs read 0 immediately after reset deassert
- [ ] TX-3  256-byte known sequence lands at core `uart_rx` in order — directed
- [ ] TX-8  `console_tx` reads 0 during reset
- [ ] CSR-1 reserved bits read 0 — directed sweep over every register
- [ ] CSR-2 writing 1s to reserved bits changes nothing observable
- [ ] CLK-5 RS232PHY start-bit cadence 8.6805 µs ± 1 sys period. The PHY is
      vendored LiteX, but the *divisor wiring* is ours and the cadence is the
      product's pacing contract → keep.
- [ ] RST-5 POR width exactly 65,536 `clk100` cycles
- [ ] RST-9 auto-start at 50,000 `cd_b8008` cycles ± 1
- [ ] RST-13 reset during each sequence step R2–R9 returns to R1 and completes
      — 8 directed cases
- [ ] CDC-3 X3 stall is a level ≥2 `cd_b8008` periods — randomized
- [ ] CDC-4 stall reaches `run_enable` within 3 `cd_b8008` periods
- [ ] CDC-6 metastability-model injection on X3 first flop: no sub-period
      `run_enable` glitch — randomized
- [ ] BP-6  guard forcibly disabled → err[0] sets (fault-injection build)
- [ ] BP-7  ≤3 bytes enter rx_fifo after stall assertion — randomized
- [ ] BP-10 stall ≥1M cycles, drain below LWM, next byte is the successor —
      long-sim; budget wall-clock before CI inclusion
- [ ] BP-11 zero byte loss across stall/resume — randomized
- [ ] RX-13 100k-byte pseudorandom stream: popped == emitted — randomized,
      long
- [ ] TX-11 100k-byte stream gated on full=0 arrives in order — randomized,
      long
- [ ] X-3..X-6 boundary cross-products (reset-during-stall, pop/write pairs
      straddling empty/full, reset at HWM) — 4 directed cases
- [ ] CSR-4 reads leave levels + err unchanged — randomized read storm
- [ ] CSR-12 back-to-back register pairs on consecutive cycles — randomized
- [ ] STR-5 re-run the RX/TX/BP/CSR cocotb set with X3 unconnected

## Phase 3 — build-artifact PYTEST rows (10 rows; no new infra)

- [ ] CLK-1 clock-domain set from elaborated design == spec set
- [ ] CLK-2 `.pnr`/report periods: sys 16.667 ns (60 MHz — **update the row**:
      spec said 13.333 ns @ 75 MHz, superseded by the 60 MHz amendment),
      b8008 40 ns
- [ ] CLK-3 `.lpf` declares sys/b8008 clock groups (open D-12 — test will
      FAIL first; fix the constraint or record the divergence disposition)
- [ ] CLK-4 zero cross-domain paths beyond the four of S-CDC-1 — timing-report
      parse
- [ ] CLK-6 baud divisor == round(60e6/115200) — **update row for 60 MHz**
- [ ] RST-1 reset-net fan-in trace: exactly POR + rst_n, no CSR-driven reset
- [ ] BP-9  no combinationally-driven clock net — netlist scan
- [ ] STR-4 all 15 X3 debug pins constrained to the `_b8008_dbg_io` sites
- [ ] STR-6 no multi-bit bus crosses sys↔b8008 — netlist scan
- [ ] CSR-14 no literal CSR address in any test/host source — grep test
- [ ] EQ-3  ROM bytes in synthesized netlist == `src/rom_baked.mem` (guards
      the yosys 0xff init bug, L-5)

## Phase 4 — E2E over the wire (the product proof; 7 rows)

VBENCH is retired: re-home every "VBENCH, then HW" row to **HW** (live board
via `make selftest`/scripts) with the cocotb tier from Phase 2 covering the
pre-hardware side.

- [ ] E2E-1 scripted `H\r` → `Help` (selftest check [3] already does this —
      formalize the row flip, cite the artifact)
- [ ] E2E-2 scripted `D 0000` → `^[0-9A-F]{4} - [0-9A-F]{2}` — add to selftest
- [ ] E2E-3 scripted `W 0100 5A` → `D 0100` == `0100 - 5A` — add to selftest
- [ ] E2E-4 Intel-HEX image via `L` at paced rate, `D`-readback byte-identical
      — implemented by the 0.6 load tool's verify mode; run per test program
- [ ] E2E-5 **the crown row**: all 31 core-repo verification programs loaded
      via `L`, run via `G`, console output asserted by the same
      `checkpoint_lib.sh` expectations — the core repo's regression executed
      *through the product*. Runner script + CI-optional (board required);
      `make e2e HOST=…`
- [ ] E2E-6 full 16 KB `D` dump, zero loss/dup, backpressure engaged ≥once —
      instrument via `console_err` + level watermarks during the dump
- [ ] E2E-7 negative test: unpaced HEX blast produces a mismatch — proves the
      S-PROD-6 boundary is real; run on hardware, expect failure signature

## Phase 5 — wire-contract rows (6 rows)

- [ ] WIRE-2 255-word burst write on hardware — needs a writable 255-word
      target; scratch is 1 word and D-10 removed the RAM window. Options:
      (a) restate as burst-of-255-single-writes to scratch + readback of the
      last, (b) WAIVE citing D-10/S-PROD-8 (no host-writable region that
      large exists by design, the software server's 255-word path is already
      CTEST-proven). Decide and write it down.
- [ ] WIRE-3 256-word burst atomicity — same decision as WIRE-2
- [ ] WIRE-4 n-word read == n UDP round trips — mock-transport pytest over
      `CommUDPBroadcast` counting datagrams
- [ ] WIRE-5 dropped-reply retry returns same value, advances no FIFO — lossy
      FakeSocket + a console-CSR-shaped register model
- [ ] WIRE-6 {data,valid,level} in one round trip; no split read in host code
      — packet count + source scan
- [ ] WIRE-7 100k bytes, 5% loss both directions, stream integrity — lossy
      mock transport end-to-end through `b8008net.console` logic

## Phase 6 — EQY equivalence (2 rows)

- [ ] EQ-1 `build/b8008_net_core.v` ≡ `src/b8008_net_core.vhdl` — GHDL
      elaborated vs converted; partition per module like core repo's eqy
      configs; the ROM memory will need `blackbox`/`memory` handling
- [ ] EQ-2 post-`synth_ecp5` netlist ≡ `build/b8008_net_core.v` — core repo
      precedent exists; expect DP16KD mapping to need `-map` collateral

## Waive candidates (write the justification, flip to WAIVED)

- [ ] CLK-7, CLK-8 — phi/SYNC cadence of the *core* (imported A-6/A-7; a
      failure here is a core defect or a clock-wiring defect, and the
      clock-wiring side is covered by CLK-1/2 + RST rows)
- [ ] X-1, X-2 — STOPPED/WAIT unreachability are core state-machine facts
      (imported A-1/A-3); the wrapper cannot cause them without also failing
      CDC/BP rows
- [ ] BP-8, BP-12 — stall freeze semantics *inside the core* are imported
      (A-8); the wrapper's half — stall delivery and level discipline — is
      CDC-3/4/6 + BP-7. Waive the core-observing halves, keep the wrapper
      halves (already listed in Phase 2)
- [ ] RST-8, RST-10 — first-byte/banner capture: discharged on silicon every
      `make selftest` run (check [2]); re-home from VBENCH to HW and flip on
      that evidence rather than waive
- [ ] SWEB-12 — ARP keepalive cadence is firmware policy (scope ruling 2);
      its delivery mechanics are CTEST-proven. Waive as firmware-internal, or
      fold into a selftest uptime check if trivial
- [ ] WIRE-2/3 — see Phase 5 decision
- [ ] §6 HW-1..11 audit — walk the bring-up checklist against what
      selftest/login now demonstrably do; flip the earned ones with evidence,
      re-scope the rest

## Bookkeeping (each phase)

- [ ] Flip VPLAN rows with artifact citations; keep `test_vplan_coverage.py`
      green
- [ ] Mutation-validate each new tier the way the C tier was (plant, kill,
      revert)
- [ ] CI: new jobs per tier; long randomized/E2E jobs behind
      `workflow_dispatch` or nightly, not per-push
- [ ] Update the 60 MHz-stale row texts (CLK-2, CLK-6) as encountered
