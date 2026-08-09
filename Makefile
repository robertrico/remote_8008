# remote_8008 Makefile
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

OSS_CAD_SUITE ?= $(HOME)/oss-cad-suite/bin
GHDL ?= $(OSS_CAD_SUITE)/ghdl

LITEX_TAG ?= 2026.04
VENV := .venv
PY := $(VENV)/bin/python

# .venv/bin first (meson/ninja for the LiteX BIOS build), then oss-cad-suite
# (yosys/nextpnr-ecp5/ecppack/ghdl).
export PATH := $(CURDIR)/$(VENV)/bin:$(OSS_CAD_SUITE):$(PATH)

.PHONY: litex-env
litex-env:
	test -d $(VENV) || python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip meson ninja
	test -f litex_setup.py || curl -fsSL -o litex_setup.py \
	  https://raw.githubusercontent.com/enjoy-digital/litex/master/litex_setup.py
	cd $(VENV) && ../$(PY) ../litex_setup.py --init --install --tag=$(LITEX_TAG) --config=standard
	$(PY) -c "import litex, liteeth, litex_boards; print('litex OK')"

# Toolchain sanity: build the stock Versa ECP5 target to a bitstream.
# NOTE: must run from build/ — litex_setup.py clones the litex/ repo into this
# directory, and running `python -m` from here shadows the installed package.
.PHONY: stock-sanity
stock-sanity:
	mkdir -p build
	cd build && $(CURDIR)/$(PY) -m litex_boards.targets.lattice_versa_ecp5 \
	  --toolchain=trellis --device=LFE5UM5G --build --output-dir stock_sanity
	test -f build/stock_sanity/gateware/lattice_versa_ecp5.bit
	@echo "stock sanity OK"

# ============================================================================
# sim-core: GHDL boot testbench for the pure-logic monitor core
# ============================================================================
# Proves b8008_net_core boots the monitor firmware headless (auto-start, no
# button) and emits its UART banner. Flags/compile-order shape copied from
# projects/b8008_monitor: analyze the full ordered b8008 source list, the
# monitor peripherals used by the core, the baked ROM model, then the core and
# testbench. 460 ms of sim time = minutes of wall-clock (banner arrives
# ~400 ms: POR + firmware delay_short). No waveform: the GHW writer chokes past
# 2 GiB on sims this long.
# ============================================================================
GHDL_FLAGS ?= --std=08 --work=work
CORE_WORKDIR := build/core

# Core repo location — the single knob (see README).
CORE_DIR ?= $(HOME)/Development/intel-8008-vhdl

SRC_DIR  := $(CORE_DIR)/src/b8008
COMP_DIR := $(CORE_DIR)/src/components
# rom_4kx8_bram.vhdl copied in at extraction
MON_DIR  := src

# Ordered b8008 core sources (mirrors B8008_SRCS in projects/project.mk).
B8008_SRCS := \
	$(SRC_DIR)/b8008_types.vhdl \
	$(SRC_DIR)/stack_pointer.vhdl \
	$(SRC_DIR)/stack_memory.vhdl \
	$(SRC_DIR)/instruction_register.vhdl \
	$(SRC_DIR)/instruction_decoder.vhdl \
	$(SRC_DIR)/condition_flags.vhdl \
	$(SRC_DIR)/register_file.vhdl \
	$(SRC_DIR)/scratchpad_decoder.vhdl \
	$(SRC_DIR)/ahl_pointer.vhdl \
	$(SRC_DIR)/temp_registers.vhdl \
	$(SRC_DIR)/alu.vhdl \
	$(SRC_DIR)/carry_lookahead.vhdl \
	$(SRC_DIR)/io_buffer.vhdl \
	$(SRC_DIR)/mem_mux_refresh.vhdl \
	$(COMP_DIR)/phase_clocks.vhdl \
	$(SRC_DIR)/state_timing_generator.vhdl \
	$(SRC_DIR)/machine_cycle_control.vhdl \
	$(SRC_DIR)/memory_io_control.vhdl \
	$(SRC_DIR)/register_alu_control.vhdl \
	$(SRC_DIR)/interrupt_ready_ff.vhdl \
	$(SRC_DIR)/b8008.vhdl \
	$(SRC_DIR)/ram_sync.vhdl \
	$(SRC_DIR)/address_decoder.vhdl \
	$(SRC_DIR)/b8008_top.vhdl

# Monitor peripherals the core wires up, the baked ROM model, then the core.
CORE_SRCS := \
	$(SRC_DIR)/debug_clock_control.vhdl \
	$(COMP_DIR)/usart.vhdl \
	$(COMP_DIR)/b8008_usart.vhdl \
	$(MON_DIR)/rom_4kx8_bram.vhdl \
	src/b8008_net_core.vhdl

CORE_TB      := sim/b8008_net_core_tb.vhdl
CORE_TB_UNIT := b8008_net_core_tb

.PHONY: sim-core
sim-core:
	@mkdir -p $(CORE_WORKDIR)
	$(GHDL) -a $(GHDL_FLAGS) --workdir=$(CORE_WORKDIR) \
	    $(B8008_SRCS) $(CORE_SRCS) $(CORE_TB)
	$(GHDL) -e $(GHDL_FLAGS) --workdir=$(CORE_WORKDIR) $(CORE_TB_UNIT)
	$(GHDL) -r $(GHDL_FLAGS) --workdir=$(CORE_WORKDIR) $(CORE_TB_UNIT) \
	    --stop-time=460ms \
	    --assert-level=error \
	    --ieee-asserts=disable-at-0

# ============================================================================
# convert: FuseSoC-generated VHDL -> Verilog netlist for b8008_net_core
# ============================================================================
# The b8008 core VHDL now lives in the intel-8008-vhdl repo (CORE_DIR) and is
# consumed via FuseSoC: remote_8008.core depends on greygiant:retro:b8008 and
# invokes its ghdl_synth_verilog generator (extra_files: this repo's
# rom_4kx8_bram.vhdl + b8008_net_core.vhdl wrapper), which GHDL --synths the
# core's own rtl+debug_io filesets plus our extra_files into a single Verilog
# netlist. See intel-8008-vhdl/docs/fusesoc.md for the full generator
# contract this rule follows (cross-repo --cores-root form, copy-out path
# pattern, the depend: requirement in remote_8008.core).
#
# rm -rf build/fusesoc before each run: --cores-root . scans recursively, so
# a stale generated .core left under build/fusesoc/ from a prior run would be
# rediscovered on the next one (duplicate-VLNV confusion / stale find hits).
#
# GHDL_GATES (build/ghdl_gates.v) is the generator's own copy of the Verilog
# primitive library (gate_mdff/gate_midff) - not read by GHDL itself, but
# needed by whatever reads this netlist next (yosys / verilator).
# ============================================================================
GHDL_GATES  := build/ghdl_gates.v
NETLIST_TOP := b8008_net_core
NETLIST_V   := build/b8008_net_core.v
# Absolute venv path, not bare `fusesoc`: make 3.81 execs metachar-free
# recipe lines via execvp, which searches make's inherited PATH and ignores
# the exported venv PATH above.
FUSESOC     ?= $(VENV)/bin/fusesoc

# Prereqs keep the OLD rule's core-VHDL sensitivity: a core repo edit must
# regenerate the netlist (core and consumer co-evolve during this phase).
$(NETLIST_V): remote_8008.core $(B8008_SRCS) $(CORE_SRCS)
	@mkdir -p build
	rm -rf build/fusesoc
	$(FUSESOC) --cores-root $(CORE_DIR) --cores-root . \
	    run --setup --tool icarus --build-root build/fusesoc greygiant:retro:remote-8008
	cp "$$(find build/fusesoc -path '*/src/*' -name b8008_net_core.v | head -1)" $(NETLIST_V)
	cp "$$(find build/fusesoc -path '*/src/*' -name ghdl_gates.v | head -1)" $(GHDL_GATES)
	@head -3 $(NETLIST_V)

.PHONY: convert
convert: $(NETLIST_V)

# ============================================================================
# sim-netlist: Verilator gate-level boot sim of the converted netlist
# ============================================================================
# Proves the GHDL->Verilog netlist still boots the monitor to its UART
# banner, driven by Verilog memory models (sim/models.v) standing in for the
# external ROM/RAM buses, and a UART RX decoder testbench (sim/netlist_tb.v).
# 450 ms of sim time (~11M cycles at 25 MHz) over a gate-level netlist would
# crawl under iverilog - use verilator (compiled native code). In practice
# verilator's --binary run finishes this whole 450 ms budget in a few
# seconds of wall clock; give it minutes of headroom anyway.
# ============================================================================
# sim/netlist_tb.v hardcodes the ROM path relative to this directory (repo
# root), which is the cwd when ./obj_dir/netlist_tb runs.
NETLIST_TB := sim/netlist_tb.v
MODELS_V   := sim/models.v
VERILATOR  := $(OSS_CAD_SUITE)/verilator

.PHONY: sim-netlist
sim-netlist: $(NETLIST_V)
	$(VERILATOR) --binary --timing -Wno-fatal \
	    --top-module netlist_tb \
	    -o netlist_tb \
	    $(NETLIST_V) $(GHDL_GATES) $(MODELS_V) $(NETLIST_TB)
	./obj_dir/netlist_tb

# ============================================================================
# sim-bench: pre-hardware gate -- Verilator C++ bench around B8008Core
# ============================================================================
# The macOS-runnable pre-hardware gate (no TAP -> no simulated Ethernet).
# bench_core.py emits build/bench_core.v: B8008Core with its CSR bus and
# wishbone bus (and both clock domains + resets) exposed as ports, materialised
# via csr_bus.CSRBankArray + Interconnect. sim/bench_tb.cpp drives those buses
# directly to prove the wishbone RAM window, the console-bridge CSR FIFOs, the
# control-CSR CDC, and stop->restart semantics against the real converted
# netlist -- exactly the custom logic; LiteEth/Etherbone transport is stock
# upstream, first exercised on hardware in Task 13.
#
# bench_core.py runs from build/ so bench_core.v and its $readmemh rom.init land
# together; the Verilator binary is therefore run from build/ too. --cc --exe
# --build compiles the C++ driver (which supplies main()) into obj_bench/.
# ============================================================================
BENCH_V      := build/bench_core.v
BENCH_TB     := sim/bench_tb.cpp
BENCH_MDIR   := build/obj_bench

.PHONY: sim-bench
sim-bench: $(NETLIST_V)
	$(PY) soc/bench_core.py
	$(VERILATOR) --cc --exe --build -Wno-fatal \
	    --top-module bench_core \
	    --Mdir $(BENCH_MDIR) \
	    -o bench_tb \
	    $(CURDIR)/$(BENCH_V) $(GHDL_GATES) $(NETLIST_V) \
	    $(CURDIR)/$(BENCH_TB)
	cd build && ./obj_bench/bench_tb

VERSA_DIR    := build/versa
VERSA_BIT    := $(VERSA_DIR)/gateware/versa_soc.bit
FIRMWARE_BIN := firmware/build/firmware.bin

# 60 MHz, not the stock 75: at 75 MHz nextpnr closes the sys/etherbone domain
# at only ~66 MHz (timing FAIL, bitstream still emitted) -- Etherbone and the
# CPU are then unreliable on silicon. 60 MHz closes with margin (~77 MHz).
SYS_CLK_FREQ ?= 60e6

# --ethmac-only: plain CPU ethmac, no liteeth hybrid interface. The hybrid
# hardware Etherbone path is dead on silicon at gigabit (see tag
# hybrid-debug-2026-08-08); Etherbone is served by the firmware instead
# (firmware/eb8008.c), protocol-compatible with litex_server/RemoteClient.
# --debug-uart stays on while the network stack is under bring-up: without it
# the SoC has no console at all (uart stub) and a working-vs-broken build is
# indistinguishable from the outside. Drop it deliberately when done.
SOC_FLAGS ?= --sys-clk-freq $(SYS_CLK_FREQ) --ethmac-only --debug-uart

# ============================================================================
# bootstrap-headers: software-only SoC build -> generated headers + libraries
# ============================================================================
# The firmware links against the LiteX-generated headers (csr.h, regions.ld,
# variables.mak) and the libbase/libcompiler_rt/libc archives compiled for
# this exact SoC -- all products of a versa_soc.py build. On a fresh checkout
# none of them exist yet, which would make `build`'s `firmware` prerequisite
# circular (firmware needs the SoC build's software tree; build needs the
# firmware). Break the cycle with a software-only bootstrap: elaborate the
# SoC and compile its software packages, but skip the 10-30 min gateware
# compile (--no-compile-gateware is a stock Builder flag; software
# generation/compilation still runs). The BIOS this bootstrap compiles into
# build/versa/software/bios/ is a throwaway -- only the headers and library
# archives matter.
#
# The rule target is variables.mak (the file firmware/Makefile includes), so
# an existing SoC build -- full or bootstrap -- satisfies it and this rule
# never re-runs. Order-only dep on $(NETLIST_V): elaboration instantiates
# B8008Core, so the converted netlist must exist, but a *newer* netlist must
# not force a pointless software re-bootstrap.
# ============================================================================
SW_VARIABLES := $(VERSA_DIR)/software/include/generated/variables.mak

$(SW_VARIABLES): | $(NETLIST_V)
	@mkdir -p $(VERSA_DIR)
	$(PY) soc/versa_soc.py --build --output-dir $(VERSA_DIR) --csr-csv $(VERSA_DIR)/csr.csv \
	    $(SOC_FLAGS) --no-compile-gateware
	test -f $(SW_VARIABLES)
	@echo "bootstrap: $(SW_VARIABLES)"

.PHONY: bootstrap-headers
bootstrap-headers: $(SW_VARIABLES)

# ============================================================================
# firmware: b8008_net DHCP/identity firmware -> firmware/build/firmware.bin
# ============================================================================
# Replaces the LiteX BIOS in integrated ROM (see firmware/linker.ld). Links
# against the headers/archives from the SoC software tree above --
# bootstrap-headers provides them on a fresh checkout, and any prior full
# `make build` also satisfies the dependency.
# ============================================================================
.PHONY: firmware
firmware: $(SW_VARIABLES)
	$(MAKE) -C firmware
	test -f firmware/build/firmware.bin
	@echo "firmware: firmware/build/firmware.bin"

# ============================================================================
# build: full b8008_net SoC -> Versa-ECP5 bitstream
# ============================================================================
# versa_soc.py wires the minimal VexRiscv + Etherbone/ethmac hybrid stack to
# B8008Core (the converted Intel-8008 monitor netlist). Depends on `convert`
# so build/b8008_net_core.v exists before elaboration, and on `firmware` so
# firmware/build/firmware.bin exists before --integrated-rom-init. The script
# re-homes its own directory to the end of sys.path (litex/liteeth/migen
# clones live here and would otherwise shadow the editable installs), so it
# is safe to run by path from this dir.
#
# --integrated-rom-init replaces the compiled BIOS with the firmware binary
# (stock LiteXArgumentParser flag, see versa_soc.py's Task 8 comment);
# --no-compile-software skips recompiling libbase/libliteeth/etc for a BIOS
# that is no longer used -- the firmware was already linked against the
# existing build/versa/software/* archives by the `firmware` target above.
# ============================================================================
.PHONY: build
build: convert firmware
	@mkdir -p $(VERSA_DIR)
	$(PY) soc/versa_soc.py --build --output-dir $(VERSA_DIR) --csr-csv $(VERSA_DIR)/csr.csv \
	    $(SOC_FLAGS) --integrated-rom-init $(FIRMWARE_BIN) --no-compile-software
	test -f $(VERSA_BIT)
	@echo "bitstream: $(VERSA_BIT)"

# openFPGALoader invocation copied from projects/project.mk (repo's proven
# Versa flashing recipe). prog loads volatile config RAM (lost on power
# cycle); prog-flash writes the SPI flash so the bitstream survives reboot.
.PHONY: prog
prog: $(VERSA_BIT)
	$(OSS_CAD_SUITE)/openFPGALoader -c ft2232 -m $(VERSA_BIT)

.PHONY: prog-flash
prog-flash: $(VERSA_BIT)
	$(OSS_CAD_SUITE)/openFPGALoader -c ft2232 -f $(VERSA_BIT)

$(VERSA_BIT):
	@echo "error: $(VERSA_BIT) not found -- run 'make build' first" >&2; exit 1

# ============================================================================
# check-synth: resource sanity on the built SoC
# ============================================================================
# DP16KD (ECP5 block RAM) count is the BRAM-inference check: 16KB b8008 RAM = 8
# DP16KD, 4KB ROM = 2, plus SoC integrated ROM/SRAM + ethmac buffers. If the
# 16384-word RAM inferred as flops instead, the total FF count explodes
# (~65k+) and DP16KD collapses -- both are flagged here.
# ============================================================================
.PHONY: check-synth
check-synth:
	@echo "=== DP16KD (block RAM) instances ==="; \
	 grep -iE "DP16KD" $(VERSA_DIR)/gateware/*.rpt $(VERSA_DIR)/gateware/*synth* 2>/dev/null | head; \
	 echo "expect >= 12 DP16KD (16KB RAM=8, ROM 4KB=2..4, + SoC)"; \
	 echo "=== Trellis packing / FF / fmax (see litex.log & *.rpt) ==="; \
	 grep -iE "TRELLIS_FF|Max frequency|DP16KD|LUT4" $(VERSA_DIR)/gateware/*.rpt 2>/dev/null | tail -40

.PHONY: vplan
vplan:
	$(PY) -m pytest soc/tests soc/test_integration.py -v

# ============================================================================
# login: zero-config console client
# ============================================================================
# `make login` discovers the board (cache -> DNS -> subnet probe sweep, see
# host/b8008net/discovery.py) and drops you into the 8008 monitor's console
# (Ctrl-] to exit). `make login HOST=10.0.0.5` skips discovery and connects
# directly. This is the deliverable make-login-console-client exists for.
# ============================================================================
.PHONY: login
login:
	@$(PY) -c 'import b8008net' 2>/dev/null || $(PY) -m pip install -e host
	$(PY) -m b8008net.cli login $(if $(HOST),--host $(HOST),)
