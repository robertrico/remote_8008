# hexfile.py -- Intel-HEX parser for `b8008net load` (Task 12).
#
# Mirrors send_hex.py's format assumptions and the monitor's own loader
# (b8008_monitor.asm: get_hex_byte / cmd_load): standard Intel-HEX records,
# ":LLAAAATT<data>CC". Type 00 stores at AAAA, type 01 is EOF, other types
# are consumed and ignored (matches the monitor, which just skips them).
# Checksum: the sum of every byte in the record -- LL, AH, AL, TT, each data
# byte, and CC itself -- must be 0 mod 256 (the monitor's HEX_SUM check).
#
# parse() returns a list of (addr, bytes) segments with adjacent data
# records coalesced into one contiguous run, ready for commands.load()'s
# burst writes.


class HexFileError(Exception):
    """Malformed Intel-HEX input: bad ':' framing, odd/invalid hex digits,
    or a record whose declared length doesn't match its payload."""


class ChecksumError(HexFileError):
    """A record's checksum byte doesn't zero out the running sum."""


def parse(text):
    segments = []
    cur_addr = None
    cur_data = bytearray()

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise HexFileError(f"line {lineno}: record must start with ':'")

        try:
            raw = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise HexFileError(f"line {lineno}: invalid hex digit ({exc})") from exc

        if len(raw) < 5:
            raise HexFileError(f"line {lineno}: record too short")

        count = raw[0]
        addr = (raw[1] << 8) | raw[2]
        rtype = raw[3]

        if len(raw) != 5 + count:
            raise HexFileError(
                f"line {lineno}: declared length {count} doesn't match "
                f"record ({len(raw)} bytes)")

        if (sum(raw) & 0xFF) != 0:
            raise ChecksumError(f"line {lineno}: checksum error (addr=0x{addr:04X})")

        if rtype == 0x01:  # EOF
            break
        if rtype != 0x00:  # not data -- consumed, not stored
            continue

        data = raw[4:4 + count]
        if cur_addr is not None and addr == cur_addr + len(cur_data):
            cur_data.extend(data)
        else:
            if cur_addr is not None:
                segments.append((cur_addr, bytes(cur_data)))
            cur_addr = addr
            cur_data = bytearray(data)

    if cur_addr is not None:
        segments.append((cur_addr, bytes(cur_data)))

    return segments
