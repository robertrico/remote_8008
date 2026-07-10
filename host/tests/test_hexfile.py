# Task 12 -- Intel-HEX parsing for `b8008net load`. Mirrors the format
# send_hex.py already parses (see send_hex.py's docstring/record handling)
# and the monitor's own get_hex_byte/cmd_load in b8008_monitor.asm: standard
# Intel-HEX, type 00 = data, type 01 = EOF, checksum = running sum of every
# record byte (including the checksum byte itself) == 0 mod 256.
import pytest

from b8008net import hexfile

# Three records: two contiguous data records (0x1000-0x1005) that must
# coalesce into a single segment, then an EOF record.
THREE_RECORD_HEX = """
:04100000DEADBEEFB4
:021004000102E7
:00000001FF
"""

BAD_CHECKSUM_HEX = ":01200000AA00\n"


def test_parses_contiguous_records_into_one_merged_segment():
    segments = hexfile.parse(THREE_RECORD_HEX)
    assert segments == [(0x1000, bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x01, 0x02]))]


def test_noncontiguous_records_stay_separate_segments():
    text = ":02100000AABB89\n:021010001122AB\n:00000001FF\n"
    segments = hexfile.parse(text)
    assert segments == [
        (0x1000, bytes([0xAA, 0xBB])),
        (0x1010, bytes([0x11, 0x22])),
    ]


def test_eof_record_stops_parsing():
    text = THREE_RECORD_HEX + "\n:02200000AABB00\n"  # after EOF, must be ignored
    segments = hexfile.parse(text)
    assert segments == [(0x1000, bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x01, 0x02]))]


def test_checksum_error_raises():
    with pytest.raises(hexfile.ChecksumError):
        hexfile.parse(BAD_CHECKSUM_HEX)


def test_blank_lines_are_ignored():
    text = "\n\n" + THREE_RECORD_HEX + "\n\n"
    segments = hexfile.parse(text)
    assert segments == [(0x1000, bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x01, 0x02]))]


def test_missing_colon_raises():
    with pytest.raises(hexfile.HexFileError):
        hexfile.parse("04100000DEADBEEFB4\n")


def test_non_hex_digit_raises():
    with pytest.raises(hexfile.HexFileError):
        hexfile.parse(":0410000ZDEADBEEFB4\n")


def test_length_byte_payload_mismatch_raises():
    # Declares 5 data bytes but carries only 4 (record from THREE_RECORD_HEX
    # with LL bumped 04 -> 05; checksum untouched -- length check must fire
    # first, before any checksum verdict).
    with pytest.raises(hexfile.HexFileError):
        hexfile.parse(":05100000DEADBEEFB4\n")


def test_too_short_record_raises():
    # Fewer than the 5 mandatory bytes (LL AH AL TT CC) -- 4 bytes here.
    with pytest.raises(hexfile.HexFileError):
        hexfile.parse(":00000001\n")
