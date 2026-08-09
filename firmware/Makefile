# remote_8008 firmware/Makefile
#
# Builds firmware.bin: a from-scratch replacement for the LiteX BIOS in
# integrated ROM (see linker.ld). Reuses the LiteX-generated build glue
# (variables.mak/common.mak) and the libbase/libliteeth/libcompiler_rt/libc
# archives the Task 7 SoC build already compiled for this exact SoC
# (build/versa/software/{libbase,libliteeth,libcompiler_rt,libc}/*.a) instead
# of recompiling them -- they don't change just because the ROM's contents
# do.
#
# BUILD_PATH points at that existing LiteX software build tree; override it
# if the SoC is rebuilt to a different --output-dir.
BUILD_PATH ?= $(CURDIR)/../build/versa

# Fresh-checkout guard: on a clean clone the LiteX software tree doesn't
# exist yet (it's a product of a versa_soc.py build), and a raw `include` of
# a missing variables.mak dies with an opaque "No such file or directory".
# Fail with directions instead. `make firmware` from the repo root runs
# the bootstrap automatically (its prerequisite is this very file); the
# explicit `make bootstrap-headers` target does only the bootstrap.
VARIABLES_MAK := $(BUILD_PATH)/software/include/generated/variables.mak
ifeq ($(wildcard $(VARIABLES_MAK)),)
$(error missing $(VARIABLES_MAK) -- the LiteX SoC software tree has not been generated yet; run `make firmware` (or `make bootstrap-headers`) from the repo root to bootstrap it)
endif

include $(VARIABLES_MAK)
include $(SOC_DIRECTORY)/software/common.mak

# The prebuilt libliteeth.a (Task 7 SoC build) was compiled without
# ETH_UDP_BROADCAST, so it has no udp_set_broadcast() -- and this firmware
# needs it to send DHCP's broadcast DISCOVER/REQUEST (see the RX-filter
# comment in main.c's dhcp_run() for why the *receive* side still needs its
# own workaround regardless). Rather than patch/recompile the whole SoC
# build, compile libliteeth's udp.c straight from the vendored LiteX source
# with that one flag turned on, and skip linking the prebuilt libliteeth.a
# entirely (nothing else in it -- mdio, tftp -- is used here).
CFLAGS += -DETH_UDP_BROADCAST
# (ETH_UDP_RX_DEBUG hex-dumps every RX frame -- add back if needed. The
# local udp.c fork carries targeted ARP diagnostics instead; it shadows the
# vendored copy because make searches the current directory before VPATH.)
VPATH  += $(LIBLITEETH_DIRECTORY)

OBJECTS = crt0.o main.o dhcp8008.o eb8008.o udp.o mdio.o

# Stripped down from the BIOS's full PACKAGES list: just what main.c and
# dhcp8008.c actually pull in (libbase's uart/timer/crc32, picolibc,
# compiler-rt), each already built by the Task 7 SoC build.
FW_LIBS    = libbase libcompiler_rt libc
FW_LIBDIRS = $(BUILD_PATH)/software/libbase \
             $(BUILD_PATH)/software/libcompiler_rt \
             $(BUILD_PATH)/software/libc

all: build/firmware.bin

build:
	mkdir -p build

build/firmware.elf: linker.ld $(OBJECTS) | build
	$(CC) $(LDFLAGS) -T linker.ld -N -o $@ \
		$(OBJECTS) \
		$(FW_LIBDIRS:%=-L%) \
		-Wl,--start-group \
		$(FW_LIBS:lib%=-l%) \
		-Wl,--end-group \
		-Wl,--gc-sections \
		-Wl,-Map,$@.map
ifneq ($(OS),Windows_NT)
	chmod -x $@
endif

build/firmware.bin: build/firmware.elf
	$(OBJCOPY) -O binary $< $@
ifneq ($(OS),Windows_NT)
	chmod -x $@
endif

# pull in dependency info for *existing* .o files
-include $(OBJECTS:.o=.d)

%.o: %.c
	$(compile)

%.o: %.S
	$(assemble)

clean:
	$(RM) $(OBJECTS) $(OBJECTS:.o=.d) build/firmware.elf build/firmware.elf.map build/firmware.bin

.PHONY: all clean
