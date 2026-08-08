# remote_8008 — System Specification

**Status:** Phase 0 output. Authoritative.
**Date:** 2026-08-08
**Applies to:** the LiteX SoC wrapping the b8008 core on the Lattice ECP5-5G Versa.

---

## 0. How to read this document

Every normative statement carries an ID of the form `S-<AREA>-<n>`. `docs/VPLAN.md`
cites these IDs. A statement using **shall** is a requirement the RTL must satisfy.
A statement using **is** or **does** states an imported fact about the b8008 core or
the LiteX libraries — not a requirement on new RTL, but a constraint new RTL must
work within.

Items recorded as **UNSPECIFIED** are deliberate. They are decisions taken to leave
behavior undefined, not gaps awaiting an answer. Section 15 is the complete register
of them. No other part of this document may leave behavior undefined.

**This spec is authoritative over the existing RTL.** `soc/b8008_integration.py`,
`soc/versa_soc.py`, and `src/b8008_net_core.vhdl` have never been synthesized to a
running board and their tests have never been executed. Where they disagree with this
document, they are wrong. Where this document cites their current behavior, it is to
mark a known divergence, not to ratify it.

---

## 1. Product contract

### 1.1 What the product is

`S-PROD-1` remote_8008 is a **network-attached dumb terminal to an Intel 8008**.
The 8008 is a b8008 soft core in ECP5 fabric, executing its ROM-resident monitor.
A host on the same LAN connects over UDP and exchanges bytes with that monitor's
serial console.

`S-PROD-2` The product's complete host-visible operation set is **two operations**:
read a byte from the console, and write a byte to the console. There are no other
host-commanded operations.

### 1.2 The guarantee

`S-PROD-3` **Faithful remote execution.** A byte handed to the product is delivered
to its destination, exactly once, in order, or the product reports that it was not.
The product never silently loses, reorders, or duplicates a byte within its boundary.

`S-PROD-4` The product makes **no real-time guarantee.** The 8008 may be stalled for
unbounded periods by a slow or absent host (see §10). Execution is faithful, not
timely. No statement in this document may be read as a promise about wall-clock
execution rate.

### 1.3 The boundary of the guarantee

`S-PROD-5` The guarantee of `S-PROD-3` holds between **the host's UDP socket and the
b8008 core's `uart_tx`/`uart_rx` pins**, inclusive of every element in between.

`S-PROD-6` The guarantee does **not** extend past the core's `uart_rx` pin. Bytes
delivered to that pin faster than the executing 8008 program can consume them are
lost inside the 8008, and the product neither prevents nor detects this. Pacing the
transmit stream is the **host's** responsibility (see §9.4).

`S-PROD-7` The guarantee does not extend to the Ethernet, IP, UDP, or DHCP layers.
Those are provided by LiteEth, the Etherbone core, and the VexRiscv firmware, and
their behavior is imported, not specified here.

### 1.4 What the product is not

`S-PROD-8` The product does **not** provide host-side program load, memory read,
memory write, run, stop, step, reset, or interrupt injection. Every one of those is
reachable in-band through the monitor's own command set (§3.3) and is therefore
8008 software, not product surface.

`S-PROD-9` The product does **not** provide a debug or observability subsystem. Raw
core signals are routed to physical pins (§14) for an external observer to consume.
The observer is out of scope; the pin list is not.

---

## 2. Scope

### 2.1 In scope

The console byte path in both directions; the Etherbone/UDP register contract; the
backpressure mechanism; the clock-domain map; the reset architecture and its
sequencing; the CSR map and its read/write semantics in every reachable state; the
debug pin list.

### 2.2 Out of scope

The b8008 core's internal correctness (§3). The 8008 monitor ROM's behavior. The
VexRiscv firmware and DHCP. LiteEth and Etherbone internals. Any external observer
attached to the debug pins. Physical-8008 lockstep comparison (§15, U-5).

---

## 3. Imported assumptions — the b8008 core

`S-CORE-1` The b8008 core is treated as a **verified black box**. This document
specifies nothing about its internal behavior and the verification plan re-tests
none of it.

`S-CORE-2` Evidence discharging that trust lives in the `intel-8008-vhdl` repository
and is enumerated in `docs/VPLAN.md` §A. It comprises 28 module testbenches, an ALU
exhaustive check over 656,384 cases against a reference model, 31 program-level
verification scripts, a cycle-exactness check for all 27 instruction classes against
`docs/isa.json`, a 46-test ISA self-test that has passed **on silicon**, and period
software (SCELBAL BASIC, HEXPAWN) running on real hardware.

`S-CORE-3` The core's port list is **frozen** at what `src/b8008_net_core.vhdl`
exposes today. No change to `intel-8008-vhdl` is required or permitted by this
specification.

### 3.1 Imported timing facts

These are measured from the core's source and are binding constraints on the wrapper.

`S-CORE-4` `phase_clocks` at `CLK_FREQ_HZ = 25e6` produces: φ1 high for 20 clocks
(0.8 µs), dead time 10 clocks (0.4 µs), φ2 high for 15 clocks (0.6 µs), dead time
10 clocks (0.4 µs). **One φ-cycle = 55 clocks = 2.2 µs = 454.5454 kHz.** This is
within the Intel 8008's 3 µs maximum cycle time.

`S-CORE-5` One 8008 T-state is **two complete φ-cycles**
(`state_timing_generator.vhdl:7`) **= 110 clocks = 4.4 µs = 227.2727 kHz**.

`S-CORE-6` `phase_clocks`' `sync` output toggles once per φ-cycle. Therefore one full
`sync` period equals exactly one T-state, and a `sync` rising edge marks a T-state
boundary.

`S-CORE-7` The core encodes its T-state on `S0/S1/S2` as: T1 = 010, T2 = 100,
T3 = 001, T4 = 111, T5 = 101, T1I = 110, STOPPED = 011. The state machine also has a
`WAIT` state.

`S-CORE-8` The shortest 8008 instruction is 5 T-states = **22 µs**.

### 3.2 Imported control facts

`S-CORE-9` `phase_clocks` has a `run_enable` input. When low, the phase state machine
freezes in place: φ1 and φ2 hold their current levels and no edge pulses are emitted.
This is a hold, not a gated clock — every CPU flop stays on the raw 25 MHz clock tree.

`S-CORE-10` `debug_clock_control`'s `run_stop` is a **toggle**, and a stop→run
transition additionally asserts a 500-clock (20 µs) reset. Run-from-stopped is
therefore a **restart**, not a resume.

`S-CORE-11` `debug_clock_control`'s `triggered` output is hardcoded to `'0'`
(`debug_clock_control.vhdl:193`) and can never carry information.

`S-CORE-12` `src/b8008_net_core.vhdl` asserts an internal 2 ms auto-start pulse
(50,000 clocks at 25 MHz) into `btn_run_stop`, causing the core to begin executing
without any external command. `bootstrap_done` is tied to `'0'`, disabling the
post-bootstrap hardware break.

`S-CORE-13` `S-CORE-9` through `S-CORE-12` mean the core **starts itself and runs
forever** absent intervention. The product relies on this: it issues no run command
(`S-PROD-2`).

### 3.3 The in-band command surface

`S-CORE-14` The monitor ROM (`projects/b8008_monitor/b8008_monitor.asm`) provides,
over the serial console: `H` (help), `D` (dump memory), `W` (write a byte), `L`
(load Intel HEX into RAM), `G` (go to address). All host-side memory and execution
control is performed through these commands, over the byte path this document
specifies.

`S-CORE-15` The monitor's receive path is a **polled single-byte loop** with no
buffer (`IN 1`, bit 7 = ready flag). Combined with `S-CORE-8`, the monitor can
execute at most 3 instructions per byte-time at 115200 baud (§5.4). Its hex-load
inner loop is longer than that. Unpaced transmission **will** lose bytes inside the
8008. This is the concrete reason for `S-PROD-6`.

---

## 4. Architecture

```
  host (LAN)                      ECP5-5G Versa
 ┌──────────┐         ┌──────────────────────────────────────────────┐
 │ b8008net │         │  cd_sys @ 75 MHz          │ cd_b8008 @ 25MHz │
 │  CLI     │         │                           │                  │
 │          │  UDP    │ ┌──────┐  ┌────────────┐  │  ┌────────────┐  │
 │ socket   ├────────►│ │ Ether│  │ console    │  │  │ b8008_net_ │  │
 │          │◄────────┤ │ bone │◄►│ CSRs+FIFOs │◄─┼─►│ core       │  │
 └──────────┘         │ └──────┘  └─────┬──────┘  │  │  ┌──────┐  │  │
                      │                 │         │  │  │b8008 │  │  │
                      │ ┌──────────┐    │RS232PHY │  │  │ +ROM │  │  │
                      │ │ VexRiscv │    └─────────┼──┤  │ +RAM │  │  │
                      │ │ DHCP fw  │   serial     │  │  └──────┘  │  │
                      │ └──────────┘   115200     │  └─────┬──────┘  │
                      └───────────────────────────┴────────┼─────────┘
                                                           │ debug pins
                                                           ▼  (X3 header)
                                                      external observer
                                                        (out of scope)
```

`S-ARCH-1` The console byte path crosses the sys↔b8008 clock boundary **as an
asynchronous serial line**, not as parallel data. The RS232 PHY, both FIFOs, and all
CSRs live entirely in `cd_sys`. The core's USART lives entirely in `cd_b8008`.

`S-ARCH-2` `S-ARCH-1` is a deliberate structural choice: it reduces the parallel-data
clock-domain crossing surface of the byte path to **zero**. The only crossings in the
product are the two serial lines and one backpressure level (§7).

---

## 5. Clocks and timing

### 5.1 Domains

`S-CLK-1` All domains derive from the Versa's on-board `clk100` (100 MHz).

| Domain | Frequency | Source | Purpose |
|---|---|---|---|
| `cd_por` | 100 MHz | `clk100` direct | power-on reset counter |
| `cd_sys2x_i` | 150 MHz | `ECP5PLL` output | intermediate for `cd_sys` |
| `cd_sys2x` | 150 MHz | `ECLKSYNCB` from `cd_sys2x_i` | edge clock |
| `cd_sys` | 75 MHz | `CLKDIVF` ÷2 from `cd_sys2x` | SoC, Etherbone, console logic |
| `cd_b8008` | 25 MHz | `ECP5PLL` output | b8008 core |
| `cd_eth_rx`, `cd_eth_tx` | PHY-sourced | Ethernet PHY | LiteEth |

`S-CLK-2` `cd_sys` and `cd_b8008` are **related** — both descend from one `ECP5PLL` —
but no fixed phase relationship between them is declared or relied upon. All logic
crossing between them shall be designed as if they were asynchronous.

`S-CLK-3` The build shall declare `cd_sys` and `cd_b8008` as separate clock groups so
the timing tool does not attempt to close paths between them. Any crossing not covered
by §7 is a design error, not a timing exception to be waived.

`S-CLK-4` `cd_eth_rx`/`cd_eth_tx` are false-pathed against `cd_sys`, as in stock
LiteEth. Imported, not specified here.

### 5.2 Derived 8008 rates

Restating §3.1 for use by later sections: φ-cycle **2.2 µs** (454.5454 kHz), T-state
**4.4 µs** (227.2727 kHz), shortest instruction **22 µs**.

### 5.3 Console line rate

`S-CLK-5` The console serial line runs at **115200 baud, 8N1**. One byte occupies
10 bit-times = **86.805 µs**.

`S-CLK-6` The `RS232PHY` baud divisor is computed from an explicitly passed
`sys_clk_freq`. It shall not be inferred from the platform, which carries no
frequency, because a silent fallback would mis-clock the divisor.

### 5.4 The pacing arithmetic

`S-CLK-7` From `S-CLK-5` and `S-CORE-8`: the 8008 can execute at most
⌊86.805 / 22⌋ = **3 instructions** between consecutive received bytes at line rate.
This is the quantitative basis for `S-CORE-15` and `S-PROD-6`.

---

## 6. Reset architecture

### 6.1 Reset sources

`S-RST-1` The product has exactly **two** reset sources:

| Name | Trigger | Scope |
|---|---|---|
| `POR` | power application | everything |
| `EXT` | `rst_n` pad asserted | everything except the POR counter |

`S-RST-2` There is **no host-commanded reset.** This follows from `S-PROD-2`.
Recovery from a wedged monitor is a power cycle or `rst_n`. This is recorded as an
accepted product limitation, not an oversight (§15, U-1).

### 6.2 Known defect in the current RTL

`S-RST-3` `cd_b8008` **does** have an `AsyncResetSynchronizer`, but it is gated on
the wrong condition. `ECP5PLL.create_clkout()` defaults `with_reset=True`
(`litex/soc/cores/clock/lattice_ecp5.py:48`), and `connect_clkout()`
(`litex/soc/cores/clock/common.py:129-131`) therefore auto-attaches
`AsyncResetSynchronizer(cd_b8008, ~pll.locked)`. `ResetSignal("b8008")` is driven.

The defect is the gate, not the absence. `~pll.locked` omits `cd_sys`'s `self.reset`
term (POR and `rst_n`) and carries no dependency on `cd_sys`'s reset state, so
`cd_b8008` can release **before** `cd_sys` — violating the ordering `S-RST-6`
requires.

A correction that adds a second synchronizer without disabling the automatic one
produces **two drivers** on `b8008_rst`: the ECP5 lowering
(`litex/build/lattice/common.py`) instantiates a fresh `FD1S3BX` pair per
`AsyncResetSynchronizer` special. The fix must pass `with_reset=False` to
`create_clkout` and supply a single correctly-gated synchronizer.

*(Corrected 2026-08-08. This clause previously asserted that `cd_b8008` had no
synchronizer at all and that `i_rst` was undriven. That was wrong — it was written
from reading `_CRG`'s explicit `specials` list without accounting for what
`create_clkout` attaches implicitly. Recorded rather than silently edited, because
the spec being wrong about the design is exactly the failure this document exists
to prevent.)*

`S-RST-4` This shall be corrected. `cd_b8008` shall have an `AsyncResetSynchronizer`
driven from the same condition as `cd_sys` (`~pll.locked | reset`), so that the core's
reset is **asynchronously asserted and synchronously released** in its own domain.

### 6.3 Reset sequencing

`S-RST-5` The following order shall hold on every `POR` and every `EXT`. Each step has
an observable precondition; no step may begin before its predecessor's precondition
is true.

| # | Step | Observable precondition |
|---|---|---|
| R1 | All domains held in reset; PLL held in reset | power applied |
| R2 | POR counter runs: 65,536 cycles of `clk100` = **655.36 µs** | `cd_por` clocking |
| R3 | PLL reset released | `por_done = 1` **and** `rst_n = 1` |
| R4 | PLL achieves lock | `pll.locked = 1` |
| R5 | `cd_sys` reset released, synchronously to `cd_sys` | R4 true |
| R6 | Console logic — `RS232PHY`, both FIFOs, all CSRs — out of reset and able to accept a byte | R5 true |
| R7 | `cd_b8008` reset released, synchronously to `cd_b8008` | **R6 true** |
| R8 | Core internal POR completes; 2 ms auto-start pulse fires | R7 true |
| R9 | Monitor executes; banner `"8008 Monitor\r\n"` enters `rx_fifo` | R8 true |

`S-RST-6` **R6 strictly precedes R7.** The console path shall be capable of accepting
a byte before the core is capable of emitting one. Violating this order loses the
boot banner, and the banner is the product's only power-on liveness evidence.

`S-RST-7` After R9 completes, the product is **up**. "Up" means: `console_rx.level`
is non-zero and the bytes readable there begin with `"8008 "`.

### 6.4 Reset end state

`S-RST-8` After any reset completes, and before any host access:

| Element | State |
|---|---|
| `rx_fifo` | empty, then filling with the boot banner |
| `tx_fifo` | empty |
| `console_err` | **unchanged** — sticky bits survive every reset (`S-CSR-9`). All bits read 0 only on a cold power-on, where there was no prior state to preserve. |
| backpressure stall | deasserted |
| 8008 | executing the monitor from ROM |
| 8008 RAM | **UNSPECIFIED** — see §15, U-2 |

---

## 7. Clock-domain crossing inventory

`S-CDC-1` The product contains exactly **four** crossings. Any fifth is a design
error.

| # | Signal | From | To | Mechanism | Status |
|---|---|---|---|---|---|
| X1 | core `uart_tx` serial line | `cd_b8008` | `cd_sys` | 2-FF `MultiReg` inside `RS232PHYRX` (`litex/soc/cores/uart.py:120`) | **already correct** |
| X2 | PHY `tx` serial line | `cd_sys` | `cd_b8008` | 2-FF synchronizer inside `usart.vhdl:63-78` | **already correct** |
| X3 | backpressure stall level | `cd_sys` | `cd_b8008` | 2-FF `MultiReg` | **to be built** |
| X4 | `ResetSignal("sys")` (reset-ordering term) | `cd_sys` | `cd_b8008` | `AsyncResetSynchronizer` on `cd_b8008`, gated via `b8008_rst_gate` (`soc/versa_soc.py:463-467`) | **already correct** |

*(Corrected 2026-08-08. This clause previously said "exactly three" and omitted X4. Task
8 legitimately added a fourth crossing: `cd_b8008`'s `AsyncResetSynchronizer` is gated on
`b8008_rst_gate`, which includes `ResetSignal("sys")` so that `cd_b8008`'s reset cannot
release before `cd_sys`'s does (`S-RST-6`). It is correctly synchronized and introduces
no hazard, but its omission left the inventory undercounting the design by one signal
while `docs/VPLAN.md` row CDC-1 asserted the old three-crossing inventory and read
`PASS`. Recorded rather than silently edited, for the same reason `S-RST-3`'s correction
above is.)*

`S-CDC-2` X1 and X2 are single-bit asynchronous serial lines. Metastability
resolution is the synchronizer's job; framing recovery is the receiver's. Neither
carries multi-bit data across the boundary, so no bus-coherency problem exists.

`S-CDC-3` X3 carries a **level**, not a pulse, and is inherently tolerant of the
synchronizer's 2-cycle latency (80 ns at 25 MHz), which is negligible against the
86.805 µs byte time. It shall not be implemented as a pulse.

`S-CDC-4` No `PulseSynchronizer` is required by this specification. The existing
`PulseSynchronizer` instances in `b8008_integration.py` serve retired controls
(`S-PROD-8`) and shall be removed.

---

## 8. Console RX path (8008 → host)

### 8.1 Structure

`S-RX-1` Bytes emitted from the core's `uart_tx` pin are received by `RS232PHYRX`
in `cd_sys` and pushed into `rx_fifo`, a `stream.SyncFIFO` of depth **4096**,
`buffered=True`, entirely in `cd_sys`.

`S-RX-2` The host reads bytes from `rx_fifo` via the `console_rx` register (§11.1)
and advances the FIFO via the `console_rx_pop` register (§11.2).

### 8.2 Exactly-once delivery

`S-RX-3` Reading `console_rx` **shall not** consume a byte. The read is
non-destructive and idempotent: two consecutive reads with no intervening pop return
identical values.

`S-RX-4` A byte is consumed **only** by a write to `console_rx_pop`.

`S-RX-5` `S-RX-3` and `S-RX-4` exist because the transport retries reads. LiteX's
`CommUDP` retries a timed-out read (`host/b8008net/console.py:11`). If reads were
destructive pops, a lost reply packet followed by a retry would consume and discard a
byte that the host never saw — a loss inside the product boundary, violating
`S-PROD-3`. Separating read from pop makes read retries harmless, and puts the
consuming action on the write path, which is the reliable direction.

`S-RX-6` A duplicated `console_rx_pop` write, arising from a retried write, pops one
extra byte. The host shall not retry pops. UDP writes are not acknowledged and are
not retried by `CommUDP`, so this hazard does not arise in the specified transport;
it is stated so that any future transport change is evaluated against it.

### 8.3 Read behavior in every state

`S-RX-7` `console_rx` is readable at all times, in every state, including during
reset and while the core is stalled. It never blocks and never returns an error.

| State | `console_rx.data` | `console_rx.valid` | `console_rx.level` |
|---|---|---|---|
| reset asserted | `0x00` | `0` | `0` |
| FIFO empty | `0x00` | `0` | `0` |
| FIFO non-empty | head byte | `1` | 1..4096 |
| FIFO full, stall engaged | head byte | `1` | 4096 |

`S-RX-8` The three fields of `console_rx` are captured in the **same** register read
and are mutually consistent — `valid` is true if and only if `level > 0`, and `data`
is the byte that the next `console_rx_pop` will consume. A host never needs two reads
to learn both the level and the byte, and therefore can never observe a torn pair.

`S-RX-9` A write to `console_rx_pop` while the FIFO is empty consumes nothing, has no
other effect, and sets sticky bit `console_err.rx_pop_when_empty` (§11.5).

---

## 9. Console TX path (host → 8008)

### 9.1 Structure

`S-TX-1` Bytes written by the host to `console_tx_data` are pushed into `tx_fifo`,
a `stream.SyncFIFO` of depth **256**, `buffered=True`, entirely in `cd_sys`, and
drained by `RS232PHYTX` onto the core's `uart_rx` pin at 115200 baud.

### 9.2 Write behavior in every state

`S-TX-2` A write to `console_tx_data` while `console_tx.full = 0` **shall** push the
byte. The byte is then guaranteed by `S-PROD-3` to reach the core's `uart_rx` pin.

`S-TX-3` A write to `console_tx_data` while `console_tx.full = 1` **shall** be
rejected: the byte is not pushed, no byte already in the FIFO is displaced, and
sticky bit `console_err.tx_write_when_full` is set (§11.5).

`S-TX-4` `S-TX-3` makes an overrun **detectable rather than silent**. The host learns
that its write did not land, and can retry. A silently discarded write would be a
loss inside the product boundary, violating `S-PROD-3`.

`S-TX-5` `console_tx` is readable at all times and never blocks.

| State | `console_tx.level` | `console_tx.full` |
|---|---|---|
| reset asserted | `0` | `0` |
| FIFO empty | `0` | `0` |
| FIFO partially filled | 1..255 | `0` |
| FIFO full | `256` | `1` |

### 9.3 No blocking writes

`S-TX-6` The Etherbone write to `console_tx_data` shall acknowledge immediately,
regardless of FIFO state. It shall **not** stall the wishbone bus awaiting space.
Stalling a 75 MHz system bus on a 115200-baud serial line would block the VexRiscv
firmware, and a hung DHCP client takes the whole appliance off the network.

### 9.4 Pacing is the host's responsibility

`S-TX-7` The product delivers bytes to the core's `uart_rx` pin at up to full line
rate. Per `S-CORE-15` and `S-CLK-7`, the executing 8008 program may be unable to
consume at that rate.

`S-TX-8` The host **shall** pace its transmissions to a rate the executing 8008
program can absorb. The product provides no pacing mechanism, no rate limit, and no
indication that the 8008 has fallen behind.

`S-TX-9` `S-TX-8` is an accepted product limitation. Loading a program via the
monitor's `L` command is expected to be slow — bounded by host round-trip time, not
by line rate. Slowness is acceptable; silent corruption is not, and the host's
obligation here is what prevents it.

---

## 10. Backpressure

### 10.1 Requirement

`S-BP-1` The product **shall never drop a byte** on the RX path. When the host is
slow or absent, the **8008 is stalled** rather than its output discarded.

`S-BP-2` `S-BP-1` is legal only because of `S-PROD-4`. A product that promised
real-time execution could not stall the CPU. This product does not make that promise.

### 10.2 Mechanism

`S-BP-3` A stall signal shall be derived in `cd_sys` from `rx_fifo.level`, crossed to
`cd_b8008` via X3 (§7), and gate the core's `run_enable` inside
`src/b8008_net_core.vhdl` — which is this repository's own VHDL, so no change to the
frozen core is required.

`S-BP-4` The stall shall use `phase_clocks`' `run_enable` hold (`S-CORE-9`), freezing
the phase state machine. It shall **not** gate any clock.

### 10.3 Thresholds

`S-BP-5` Stall asserts when `rx_fifo.level ≥ HWM` and deasserts when
`rx_fifo.level ≤ LWM`, with `HWM = 4032` and `LWM = 3968`. The 64-entry hysteresis
band prevents oscillation at the threshold.

`S-BP-6` The headroom `4096 − HWM = 64` **shall** exceed the maximum number of bytes
that can still arrive after the stall asserts. That maximum is at most 3: one byte in
the core's USART transmit shift register, one in the PHY's receive shift register,
and one accounted for by the X3 synchronizer's 2-cycle latency. The specified
headroom carries roughly 20× margin.

### 10.4 The consequence, stated plainly

`S-BP-7` If the host stops reading, the 8008 stops executing, indefinitely. This is
correct behavior, not a fault. There is no timeout, no watchdog, and no automatic
release. Execution resumes when, and only when, the host drains below `LWM`.

While the 8008 is stalled it cannot poll its own UART receive register either, so any
byte the host transmits during that stall is overwritten inside the core's USART
regardless of how well the host paces its writes; this route is outside the guarantee
boundary (`S-PROD-6`) and is not a violation, but it is caused by the product's own
RX-side stall rather than by host pacing as `U-6` frames it, and a host CLI should stop
transmitting whenever `console_rx.level` is backing up toward `HWM`.

### 10.5 Overflow is unreachable

`S-BP-8` Given `S-BP-5` and `S-BP-6`, `rx_fifo` **shall never** overflow.

`S-BP-9` Sticky bit `console_err.rx_overflow` shall be implemented anyway, as a
**canary**: it asserts if a byte is ever presented to a full `rx_fifo`. Under a
correct implementation it can never assert.

`S-BP-10` `S-BP-8` is stated as a property to be **proven exhaustively**, not sampled
by test. See `docs/VPLAN.md` row `BP-4`.

---

## 11. Register map

`S-CSR-1` All registers live in the `console` CSR bank. All are 32 bits wide on the
wishbone/Etherbone side. All reserved bits read `0` and ignore writes.

`S-CSR-2` All registers are accessible at all times, in every state, including during
reset. No register access blocks, stalls, or errors.

`S-CSR-3` Reads have no side effects, without exception. The only state-changing
accesses in the entire map are writes to `console_rx_pop`, `console_tx_data`, and
`console_err_clear`.

`S-CSR-1a` Register **addresses are assigned by the LiteX CSR allocator** and are not
fixed by this document. The generated `csr.csv` is the single authoritative source of
addresses, and host code and tests shall read them from it rather than restating them.
What this document fixes is the register **set**, their **names**, their **access
type**, and their **bit semantics**.

`S-CSR-1b` The `console` bank shall contain **exactly** the six registers below and
no others.

| Name | Access | Purpose |
|---|---|---|
| `console_rx` | RO | atomic {data, valid, level} |
| `console_rx_pop` | WO | consume one RX byte |
| `console_tx` | RO | {level, full} |
| `console_tx_data` | WO | push one TX byte |
| `console_err` | RO | sticky error bits |
| `console_err_clear` | WO | write-1-to-clear |

### 11.1 `console_rx` (RO)

| Bits | Field | Meaning |
|---|---|---|
| 7:0 | `data` | head byte of `rx_fifo`; `0x00` when `valid = 0` |
| 8 | `valid` | `1` iff `level > 0` |
| 21:9 | `level` | occupancy, 0..4096 |
| 31:22 | — | reserved, reads `0` |

Behavior in every state: §8.3. Non-destructive: `S-RX-3`.

### 11.2 `console_rx_pop` (WO)

`S-CSR-4` A write of **any** value consumes exactly one byte from `rx_fifo`. The
written data is ignored. Two writes consume two bytes.

`S-CSR-5` A write while empty consumes nothing and sets
`console_err.rx_pop_when_empty` (`S-RX-9`).

`S-CSR-6` The effect of a pop is visible in the **next** read of `console_rx`. There
is no ordering hazard between a pop and a concurrent FIFO push: the FIFO's own write
and read pointers are both in `cd_sys` and the FIFO resolves it.

### 11.3 `console_tx` (RO)

| Bits | Field | Meaning |
|---|---|---|
| 8:0 | `level` | occupancy, 0..256 |
| 9 | `full` | `1` iff `level = 256` |
| 31:10 | — | reserved, reads `0` |

Behavior in every state: §9.2.

### 11.4 `console_tx_data` (WO)

`S-CSR-7` Bits 7:0 are the byte to push. Bits 31:8 are ignored.

`S-CSR-8` Accept/reject behavior: `S-TX-2`, `S-TX-3`.

### 11.5 `console_err` (RO)

| Bit | Field | Sets when |
|---|---|---|
| 0 | `rx_overflow` | a byte was presented to a full `rx_fifo` (`S-BP-9`; unreachable per `S-BP-8`) |
| 1 | `tx_write_when_full` | `console_tx_data` written while `full = 1` (`S-TX-3`) |
| 2 | `rx_pop_when_empty` | `console_rx_pop` written while `level = 0` (`S-RX-9`) |
| 31:3 | — | reserved, reads `0` |

`S-CSR-9` All bits are **sticky**: once set, a bit remains set until explicitly
cleared via `console_err_clear`. No reset of any kind clears them, and reading
`console_err` does not clear them.

`S-CSR-10` `S-CSR-9` means the evidence of a fault survives a reset. A fault that
provokes a power cycle must still be visible afterward, or the product deletes the
record of its own misbehavior.

### 11.6 `console_err_clear` (WO)

`S-CSR-11` Write-1-to-clear, bit-for-bit against `console_err`. Writing `1` to a bit
position clears the corresponding sticky bit. Writing `0` leaves it unchanged.

`S-CSR-12` A clear that coincides with the setting condition **shall resolve in favor
of setting**: the bit remains set. Losing a fresh fault indication to a concurrent
clear would make the sticky bits unreliable in exactly the situation they exist for.

---

## 12. Host wire contract

`S-WIRE-1` The transport is **Etherbone over UDP**, served by the hardware Etherbone
endpoint. The host uses LiteX's `RemoteClient` via the `b8008net` package.

`S-WIRE-2` Etherbone's `buffer_depth` shall be **255**, not the LiteEth default of
16. The default silently overflows on the 255-word bursts `RemoteClient` uses for
writes.

`S-WIRE-3` UDP **reads** are one 32-bit word per round trip. `RemoteServer` codes
`read_max_length = {"CommUDP": 1}`, and `CommUDP.read()` asserts `burst == "incr"`,
so even a `burst="fixed"` request is downgraded by the read-merger into *n* separate
single-word round trips. Console drain therefore costs approximately **one round trip
per byte**.

`S-WIRE-4` `S-WIRE-3` is **accepted**, not worked around. The product is slow by
design (`S-PROD-4`, `S-TX-9`). `console_rx`'s atomic {data, valid, level} packing
(`S-RX-8`) is the one concession: it makes each of those round trips carry everything
the host needs, rather than requiring two.

`S-WIRE-5` UDP **writes** burst up to 255 words per packet. This is the fast
direction and is where the host's pop and transmit traffic lives.

`S-WIRE-6` `CommUDP` retries a timed-out read. `S-RX-3` makes this safe. No other
element of the product relies on read idempotency, because `S-CSR-3` makes all reads
side-effect-free.

`S-WIRE-7` Concurrent access by two hosts is **UNSPECIFIED** (§15, U-3).

---

## 13. Identity and network bring-up

`S-NET-1` The VexRiscv firmware performs DHCP with hostname option 12 = `b8008`,
uses the Etherbone MAC as `chaddr`, re-acquires the lease periodically, and writes
the leased address into Etherbone's IP CSR.

`S-NET-2` The behavior of `S-NET-1` is **imported**, not specified here. It is
verified only at the level of "the board is reachable at the expected name"
(`docs/VPLAN.md` §HW).

---

## 14. Debug pin tap

`S-PIN-1` The following core signals shall be routed to the X3 expansion header, at
the sites already defined by `_b8008_dbg_io` in `soc/versa_soc.py`:

`D[7:0]`, `S0`, `S1`, `S2`, `SYNC`, `φ1`, `φ2`, `INT`.

`S-PIN-2` The **signal list and their pin assignments are part of this
specification.** Their electrical characteristics, timing relationships, and any
framing an observer might impose are **not** (§15, U-4).

`S-PIN-3` No product behavior depends on these pins. Leaving the header unconnected
shall have no effect on anything in §8 through §12.

`S-PIN-4` `S-PIN-1` through `S-PIN-3` exist so that an external observer — an MCU, a
logic analyzer, a future shadow rig — can be built without any change to this
specification or to the product's verification.

---

## 15. UNSPECIFIED register

These are deliberate. Each was considered and left undefined.

| ID | Item | Rationale |
|---|---|---|
| **U-1** | Recovery from a wedged monitor other than power cycle or `rst_n`. | `S-PROD-2` admits no host control. **Confirmed accepted:** pressing the physical reset is the intended recovery. Not a limitation awaiting a fix, and no row shall be added for it. |
| **U-2** | Contents of the 8008's RAM after reset. | The core's RAM is BRAM with no specified init. The monitor initializes what it needs. The product makes no claim. |
| **U-3** | Behavior when two hosts access the board concurrently. | The `b8008net` lockfile is host-side only and does not coordinate across machines. Undefined interleaving. |
| **U-4** | Electrical, timing, and framing contract of the debug pins. | Observer's responsibility (`S-PIN-2`). |
| **U-5** | Physical-8008 lockstep comparison. | Deferred entirely. No comparator, no divergence definition, no capture path in this product. Would require its own spec. |
| **U-6** | Byte loss inside the 8008 caused by inadequate host pacing. | Outside the guarantee boundary (`S-PROD-6`). Neither prevented nor detected. |
| **U-7** | Behavior of Ethernet/IP/UDP/DHCP layers under adverse network conditions. | Imported from LiteEth and the firmware (`S-PROD-7`). |
| **U-8** | Duration of a stall induced by backpressure. | Unbounded by design (`S-BP-7`). |

---

## 16. Divergences from current RTL

Recorded so that implementation work has an explicit list. Each is a place where
this specification requires a change.

| # | Current RTL | Required by spec |
|---|---|---|
| D-1 | `cd_b8008`'s auto-attached `AsyncResetSynchronizer` is gated only on `~pll.locked`, omitting POR/`rst_n` and any ordering against `cd_sys` | `S-RST-3`, `S-RST-4` |
| D-2 | No reset ordering between console logic and core | `S-RST-6` |
| D-3 | `rxtx` read is a destructive pop | `S-RX-3`, `S-RX-4` |
| D-4 | `rxlevel` and `rxtx` are separate registers, read separately | `S-RX-8` |
| D-5 | RX FIFO full silently drops bytes; no flag | `S-BP-1`, `S-BP-9` |
| D-6 | No backpressure mechanism | §10 |
| D-7 | TX write while full silently discards | `S-TX-3` |
| D-8 | `ctl` CSR carries retired controls (`run_stop`, `step_cycle`, `step_sync`, `int_req`, `int_vector`) | `S-PROD-8`, `S-CDC-4` |
| D-9 | `status.triggered` exposed but hardcoded `0` in the core | `S-CORE-11`; remove |
| D-10 | Wishbone RAM window exposed to host | `S-PROD-8`; remove |
| D-11 | No sticky error register | §11.5 |
| D-12 | No declared clock groups between `cd_sys` and `cd_b8008` | `S-CLK-3` |

---

## 17. Glossary

**b8008** — the block-based VHDL Intel 8008 implementation in `intel-8008-vhdl`,
current and silicon-validated. Distinct from the deprecated `s8008` and `v8008`.

**φ-cycle** — one complete φ1+dead+φ2+dead sequence. 2.2 µs here.

**T-state** — one 8008 timing state. Two φ-cycles = 4.4 µs here.

**Sticky bit** — a status bit that latches on its setting condition and holds until
explicitly cleared, surviving all resets.

**Canary** — a detector for a condition that a correct implementation makes
unreachable. Its purpose is to catch a regression in the mechanism that makes it
unreachable, not to handle the condition.

**Product boundary** — host UDP socket to core `uart_tx`/`uart_rx` pins. The region
within which `S-PROD-3` holds.
