# remote_8008

**A network-attached Intel 8008 you can talk to from anywhere on your LAN.**

An ECP5-5G Versa runs the silicon-validated [b8008](https://github.com/robertrico/intel-8008-vhdl)
core in fabric, executing its ROM-resident monitor. A host connects over UDP and
exchanges bytes with that monitor's console. No serial cable, no terminal program
pinned to a physical desk — the 8008 lives on the network.

It is not an emulator. The 8008's instruction decode, timing states, and two-phase
clock are real logic running at real 8008 rates: a 2.2 µs φ-cycle, a 4.4 µs T-state.
The board is doing what a 1972 chip did, at the speed it did it.

## What it guarantees

> No byte is lost, reordered, or duplicated between your socket and the core's UART
> pins.

Deliberately **not** real-time. If your host stops reading, the 8008 **stops
executing** until you catch up — it never discards output to keep pace. Faithful,
not fast. See [`SPEC.md`](SPEC.md) §1.

The guarantee ends at the core's UART receive pin. Past that, the 8008's own polling
loop can only absorb about three instructions' worth of work per received byte, so
pacing a bulk transfer is the host's job. Loading a program is slow. That is the
name of the game.

## What it is not

No host-side program load, memory peek/poke, run/stop/step, or interrupt injection.
Every one of those already exists **inside** the 8008, in the monitor's own command
set — `H` help, `D` dump, `W` write, `L` load Intel HEX, `G` go. You reach them the
same way you would over a serial cable: by typing at them.

That is the whole design. The product is a wire, not a debugger. Recovery from a
wedged monitor is the reset button on the board, and that is a deliberate choice
(`SPEC.md` §15, U-1).

Raw core signals — `D[7:0]`, `S0`–`S2`, `SYNC`, `φ1`, `φ2`, `INT` — are routed to the
X3 header so an external observer can be built later without touching any of this.
The pin list is specified; whatever you hang off it is not.

## Status

**Spec-first, pre-hardware.** The bitstream builds and the simulation tiers run, but
nothing here has executed on real silicon, and the test suites have not been run
against a board.

[`SPEC.md`](SPEC.md) and [`docs/VPLAN.md`](docs/VPLAN.md) were written **before** the
RTL was corrected, and they are authoritative over it. The verification plan records
**12 divergences** where the existing gateware disagrees with the spec — including a
`cd_b8008` reset that never reaches the core, a console register whose destructive
read loses a byte whenever a UDP reply is dropped, and an RX FIFO that drops bytes
silently when full.

None of those are surprises. They are the output of specifying the product properly
before finishing it.

| | |
|---|---|
| Verification rows | 119, all `UNIMPLEMENTED` |
| Imported assumptions (core repo, never re-run here) | 11 |
| Divergences from current RTL | 12 |
| Rows passing | **0** |

The b8008 core itself is out of scope and treated as verified: 28 module testbenches,
an exhaustive ALU check over 656,384 cases, 31 program-level verification scripts,
cycle-exactness for all 27 instruction classes, a 46-test ISA self-test **passed on
silicon**, and SCELBAL BASIC running on hardware.

Usage documentation lands when the plan is green. Documenting a workflow that has
never run would be documenting a guess.

## Architecture

```
 your laptop                     ECP5-5G Versa
┌──────────┐        ┌─────────────────────────────────────────┐
│ b8008net │  UDP   │  cd_sys 75 MHz      │  cd_b8008 25 MHz   │
│   CLI    ├───────►│  Etherbone          │                    │
│          │◄───────┤  console FIFOs  ────┼──► b8008 core      │
└──────────┘        │  VexRiscv/DHCP    serial 115200 + ROM    │
                    └───────────────────────────┬─────────────┘
                                                ▼ X3 debug pins
                                          external observer
                                            (out of scope)
```

The console crosses the clock boundary **as a serial line**, not as parallel data.
That is deliberate: it reduces the byte path's clock-domain-crossing surface to
zero, and leaves the whole design with exactly three crossings.

| Path | Contents |
|---|---|
| `SPEC.md` | The product specification. Authoritative over all RTL. |
| `docs/VPLAN.md` | The verification plan — the contract the RTL must satisfy |
| `soc/` | LiteX target (`versa_soc.py`) and the `B8008Core` integration module |
| `src/` | The `b8008_net_core` VHDL wrapper and its ROM model |
| `sim/` | Three sim tiers: GHDL boot, Verilator gate-level, Verilator CSR/wishbone bench |
| `firmware/` | VexRiscv DHCP/identity firmware — distinct from the 8008 monitor ROM |
| `host/` | The `b8008net` Python package and its pytest suite |

Note that `host/`'s current commands cover a surface the spec has since retired
(`load`, `peek`, `poke`, `run`, `step`). They are not deleted yet; they are simply no
longer part of the product contract.

## Provenance

Extracted 2026-07-10 from `intel-8008-vhdl` `projects/b8008_net/` @ `311df3f` as a
fresh repo. Development history lives in the source repo's log for that path. This
repo consumes the core as the FuseSoC core `greygiant:retro:b8008` via its
`ghdl_synth_verilog` generator.

## Setup

1. **fusesoc**, pinned to **2.4.6**: `pipx install fusesoc==2.4.6`. For the generator
   contract — required `depend:` wiring, output layout, prerequisites — see
   `intel-8008-vhdl/docs/fusesoc.md`.
2. **oss-cad-suite** (GHDL, yosys, nextpnr-ecp5, ecppack) on `PATH`, or pointed at
   via the Makefile's `OSS_CAD_SUITE`/`GHDL` variables.
3. **`CORE_DIR`**: the core repo checkout providing `greygiant:retro:b8008` — default
   `~/Development/intel-8008-vhdl`.
4. `make litex-env` — pinned LiteX toolchain (LiteX `2026.04`) into a local `.venv`.
5. **RISC-V cross gcc** (`riscv64-unknown-elf-gcc`) on `PATH`. On macOS/Homebrew its
   `cc1` also needs `brew install isl mpfr` — without them, missing dylibs make `cc1`
   crash with a misleading "internal compiler error". Diagnose with `otool -L`.

## Build

```bash
make litex-env          # one-time: LiteX 2026.04 + deps into .venv

make sim-core           # GHDL behavioral boot sim
make sim-netlist        # Verilator gate-level netlist boot sim
make sim-bench          # Verilator CSR/wishbone bench

make convert            # FuseSoC-generated VHDL -> Verilog netlist
make bootstrap-headers  # fresh checkout only, BEFORE `make firmware`
make firmware           # VexRiscv DHCP/identity firmware
make build              # full bitstream -> build/versa/gateware/versa_soc.bit
```

Fresh-checkout order: `make litex-env` → `make bootstrap-headers` → `make firmware`
→ `make build`. `bootstrap-headers` cannot be made an automatic prerequisite without
a circular `build`/`firmware` edge — see the Makefile comment block.

Host package:

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pip install -e host
.venv/bin/python -m pytest host/tests
```

## Two known toolchain traps

Both cost real time once, and both are now spec'd rather than remembered:

- Etherbone's `buffer_depth` must be **255**, not LiteEth's default of 16. The
  default silently overflows on the 255-word bursts `RemoteClient` uses for writes.
  (`SPEC.md` `S-WIRE-2`)
- `litex_server` clamps `CommUDP` reads to one word and its read-merger downgrades
  `burst="fixed"` into *n* separate round trips. A requested burst silently becomes
  *n* packets. (`SPEC.md` `S-WIRE-3`)
