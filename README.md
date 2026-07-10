# remote_8008

## What this is

A LiteX System-on-Chip for the Lattice ECP5-5G Versa board that wraps the
silicon-validated **b8008** core (`greygiant:retro:b8008`, v3.0 — proven on
real ECP5-5G hardware) and exposes it as a network appliance. A VexRiscv
soft-core handles DHCP/identity; a hardware Etherbone endpoint gives a host
tool remote program load, a console bridge to the 8008 monitor, and
run/stop/step control — no serial cable required.

## Provenance

Extracted 2026-07-10 from `intel-8008-vhdl` `projects/b8008_net/` @
`311df3f` as a fresh repo (history not carried). Development history lives
in the source repo's log for that path.

## Condensed work log

The effort completed Tasks 1-12 of the [implementation
plan](docs/superpowers/plans/2026-07-09-litex-ethernet-monitor.md), in
order:

- **LiteX environment**: pinned toolchain, stock Versa target built to a
  bitstream as an end-to-end sanity check on yosys/nextpnr/ecppack.
- **Dynamic-IP Etherbone spike**: proved a `CSRStorage`-driven IP address
  elaborates against `add_etherbone(..., with_ethmac=True)`, the one custom
  mechanism with no stock LiteX precedent.
- **`b8008_net_core` wrapper**: a pure-logic port of the monitor top (no
  PLL, no pads, no debouncers) that boots the 8008 monitor headless
  (auto-start, no button press) in GHDL sim.
- **GHDL→Verilog netlist flow**: `b8008_net_core.vhdl` converts via
  `ghdl --synth --out=verilog`; the converted gate-level netlist reboots the
  monitor correctly under a Verilator gate-level sim (an iverilog run of the
  same ~11M-cycle boot would crawl).
- **`B8008Core` Migen integration**: wishbone shim onto an absolute-address
  8008 RAM window, CSR control/status with clock-domain-crossed pulses, and
  a UART-CSR console bridge, instantiating the converted netlist.
- **Full SoC** (`versa_soc.py`): VexRiscv + hardware Etherbone + `B8008Core`
  build to a bitstream with timing met. Etherbone's `buffer_depth` is set to
  **255**, not the LiteEth default of 16 — the default silently overflows on
  the 255-word bursts `RemoteClient` uses for writes.
- **DHCP/identity firmware**: VexRiscv firmware that DHCPs with hostname
  option 12 = `b8008`, uses the Etherbone MAC as `chaddr`, and re-acquires
  the lease periodically; writes the leased address into Etherbone's IP CSR.
- **Three simulation tiers**: `sim-core` (GHDL, behavioral boot),
  `sim-netlist` (Verilator, gate-level boot), and `sim-bench` (Verilator C++
  driving `B8008Core`'s CSR and wishbone buses directly) — the last is the
  pre-hardware gate, since litex_sim's TAP-based Ethernet model doesn't run
  on this macOS host.
- **Host package `b8008net`**: discovery (DNS + probe sweep + cache),
  lockfile-guarded board connection, interactive console with batched FIFO
  drains, and `load`/`peek`/`poke`/`run`/`reset`/`step` with read-back
  verify. 113/113 tests passing. One deliberate performance note: UDP CSR
  **reads** are one 32-bit word per round-trip by design (the server clamps
  UDP reads to length 1) — fine for an 8008-paced console/peek, not a
  throughput path. **Writes** burst up to 255 words per packet; that's where
  wire-speed program loading lives.

**Status: pre-hardware.** The Verilator bench is the validation gate to
this point; nothing here has run on real silicon yet. Tasks 13-15 of the
plan — hardware bring-up, live DHCP/Etherbone verification, and workflow
parity against the serial-era monitor — remain.

## Architecture

| Path | Contents |
|---|---|
| `soc/` | LiteX SoC target (`versa_soc.py`) and the `B8008Core` Migen integration module (`b8008_integration.py`), plus the Verilator bench harness and elaboration tests |
| `src/` | The `b8008_net_core` VHDL wrapper and its ROM behavioral model |
| `sim/` | The three sim tiers: GHDL boot testbench, Verilator gate-level netlist testbench, Verilator C++ CSR/wishbone bench driver |
| `firmware/` | VexRiscv firmware (DHCP client, writes the leased IP into Etherbone's IP CSR) — distinct from the 8008 monitor ROM baked into the b8008 core itself |
| `host/` | The `b8008net` Python package: discovery, board connection, console, load/peek/poke/run commands, and its pytest suite |

## Setup

1. **fusesoc**, pinned to **2.4.6**: `pipx install fusesoc==2.4.6`. This
   repo consumes the b8008 core as the FuseSoC core `greygiant:retro:b8008`
   via its `ghdl_synth_verilog` generator, which produces
   `build/b8008_net_core.v` (top module `b8008_net_core`; the VHDL side of
   that wrapper lives in `src/`). For the full generator contract —
   required `depend:` wiring, output path layout, GHDL/PyYAML prerequisites
   — see `intel-8008-vhdl/docs/fusesoc.md` in the core repo.
2. **oss-cad-suite** (GHDL, yosys, nextpnr-ecp5, ecppack) on `PATH` or
   pointed at via the Makefile's `OSS_CAD_SUITE`/`GHDL` variables.
3. **`CORE_DIR`**: a make variable naming the core repo checkout that
   provides `greygiant:retro:b8008` — default `~/Development/intel-8008-vhdl`.
   Override with `make CORE_DIR=/path/to/intel-8008-vhdl ...` if your
   checkout lives elsewhere.
4. `make litex-env` — sets up the pinned LiteX toolchain (LiteX `2026.04`)
   in a local `.venv`.

## Build/test commands

```bash
make litex-env          # one-time: LiteX 2026.04 + deps into .venv

make sim-core           # GHDL behavioral boot sim
make sim-netlist        # Verilator gate-level netlist boot sim
make sim-bench          # Verilator CSR/wishbone bench (pre-hardware gate)

make convert             # FuseSoC-generated VHDL -> Verilog netlist (build/b8008_net_core.v)
make bootstrap-headers   # fresh checkout only: SoC software-only build -> generated
                          # headers/archives (csr.h, regions.ld, libbase/...) that
                          # `make firmware` links against. Run this BEFORE `make firmware`
                          # on a clean checkout, or `firmware`'s $(SW_VARIABLES) prereq
                          # is circular (it needs a SoC build; `build` needs firmware).
make firmware            # build the VexRiscv DHCP/identity firmware
make build               # full bitstream -> build/versa/gateware/versa_soc.bit
                          # (runs `convert` + `firmware` as prerequisites)

# host test suite needs pytest + the b8008net package installed into .venv
# (litex-env only installs the LiteX toolchain itself):
.venv/bin/python -m pip install pytest
.venv/bin/python -m pip install -e host
.venv/bin/python -m pytest host/tests   # b8008net host package test suite
```

Proven ordering on a fresh checkout: `make litex-env` → `make bootstrap-headers` →
`make firmware` → `make build` (or just `make build`, since it pulls in `convert`
and `firmware`, but `firmware` itself needs `bootstrap-headers` run first on a
clean tree — see the Makefile's `bootstrap-headers` comment block for why the
dependency can't be made automatic without a circular `build`/`firmware` edge).

## Continuing this work

The plan this repo implements —
`docs/superpowers/plans/2026-07-09-litex-ethernet-monitor.md` — continues
here. Tasks 1-12 are done (see the work log above); Tasks 13-15 (hardware
bring-up, live network verification, workflow parity) are next.
