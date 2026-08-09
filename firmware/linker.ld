/* linker.ld -- cribbed verbatim from litex/litex/soc/software/bios/linker.ld.
 *
 * This firmware REPLACES the BIOS in integrated ROM (--integrated-rom-init):
 * it executes in place from the rom region's origin, same as the BIOS does,
 * so it needs the same BIOS-style link: .text/.rodata in rom, .data loaded
 * from rom but relocated to sram at startup (crt0.S's data_init loop),
 * .bss zeroed in sram. generated/regions.ld (written by the LiteX builder
 * for this SoC) supplies the actual rom/sram origins and sizes.
 */

INCLUDE generated/output_format.ld
ENTRY(_start)

/* NOT generated/regions.ld: the LiteX builder rewrites that file on every
 * --integrated-rom-init gateware build with the ROM shrunk to fit the
 * *previous* firmware image, so including it makes each firmware build fail
 * as soon as the binary grows ("region rom overflowed" chicken-and-egg).
 * Fixed map instead: origins are stable for this SoC and the lengths are the
 * BaseSoC ceilings (integrated_rom_size=0x10000, integrated_sram_size=0x2000
 * in soc/versa_soc.py). The SoC build still sizes the real ROM to the actual
 * binary -- this ceiling only governs the link. */
MEMORY {
	rom : ORIGIN = 0x00000000, LENGTH = 0x00010000
	sram : ORIGIN = 0x10000000, LENGTH = 0x00002000
}

SECTIONS
{
	.text :
	{
		_ftext = .;
		/* Make sure crt0 comes first, and doesn't get discarded by
		   greedy optimisation. */
		*crt0*(.text)
		KEEP(*crt0*(.text))
		KEEP(*(.text.isr))

		*(.text .stub .text.* .gnu.linkonce.t.*)
		_etext = .;
	} > rom

	.rodata :
	{
		. = ALIGN(8);
		_frodata = .;
		*(.rodata .rodata.* .gnu.linkonce.r.*)
		*(.rodata1)
		*(.got .got.*)
		*(.toc .toc.*)

		/* Make sure the file is aligned on disk as well
		   as in memory; CRC calculation requires that. */
		FILL(0);
		. = ALIGN(8);
		_erodata = .;
	} > rom

	.data :
	{
		. = ALIGN(8);
		_fdata = .;
		*(.data .data.* .gnu.linkonce.d.*)
		*(.data1)
		*(.sdata .sdata.* .gnu.linkonce.s.*)

		FILL(0);
		. = ALIGN(8);
		_edata = .;
	} > sram AT > rom

	.bss :
	{
		. = ALIGN(8);
		_fbss = .;
		*(.dynsbss)
		*(.sbss .sbss.* .gnu.linkonce.sb.*)
		*(.scommon)
		*(.dynbss)
		*(.bss .bss.* .gnu.linkonce.b.*)
		*(COMMON)
		. = ALIGN(8);
		_ebss = .;
		_end = .;
	} > sram

	/DISCARD/ :
	{
		*(.eh_frame)
		*(.comment)
	}
}

PROVIDE(_fstack = ORIGIN(sram) + LENGTH(sram));

PROVIDE(_fdata_rom = LOADADDR(.data));
PROVIDE(_edata_rom = LOADADDR(.data) + SIZEOF(.data));
