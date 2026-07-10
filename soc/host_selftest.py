#!/usr/bin/env python3
#
# host_selftest.py -- scripted RemoteClient self-test for the b8008_net SoC.
# ----------------------------------------------------------------------------
# The hardware counterpart of the Verilator bench (bench_core.py / bench_tb.cpp,
# Task 9): the bench proved the custom logic pre-silicon over a raw CSR/wishbone
# bus; this drives the SAME surfaces on the REAL board through Etherbone, via a
# litex_server bridge. It is written and import/syntax-checked in Task 9; its
# pure helpers get FakeBoard unit tests in Task 10, and it is run live against
# hardware in Tasks 13/14.
#
#   litex_server --udp --udp-ip <board-ip>        # in one shell
#   python host_selftest.py --csr build/versa/csr.csv --host <board-ip>
#
# Four checks (exit 0 iff all pass):
#   1. Connect, print the SoC identifier.
#   2. RAM window: burst write 0..255 to the b8008_ram window, read back, assert.
#   3. Console RX: poll b8008_rxlevel, drain b8008_rxtx, assert the "8008 "
#      banner prefix; print the decoded banner.
#   4. Console TX->RX: send a monitor command ('H' + CR, the help command from
#      b8008_monitor.asm) via b8008_rxtx, assert the "Help" response echoes back.
#
# CSR/memory names carry the SoC's "b8008_" bank prefix (self.b8008 = B8008Core
# in versa_soc.py); the RAM window is the b8008_ram memory region. The wishbone
# window is word-per-32-bit: word index i (== absolute 14-bit 8008 address) is
# at byte address base + 4*i.
# ----------------------------------------------------------------------------
import argparse
import os
import sys
import time

# Banner prefix emitted by b8008_monitor.asm send_banner ("8008 Monitor\r\n").
BANNER_PREFIX = b"8008 "
# 'H' help command + carriage return; monitor replies with the "Help" menu.
HELP_COMMAND  = b"H\r"
HELP_MARKER   = b"Help"


# ── pure helpers (no board dependency; FakeBoard-unit-tested in Task 10) ──────
def compare_bytes(expected, actual):
    """Return (ok, first_mismatch_index). index is -1 when equal."""
    if len(expected) != len(actual):
        n = min(len(expected), len(actual))
        for i in range(n):
            if expected[i] != actual[i]:
                return False, i
        return False, n
    for i in range(len(expected)):
        if expected[i] != actual[i]:
            return False, i
    return True, -1


def batches(seq, size):
    """Yield successive `size`-length slices of `seq` (drain/burst batching)."""
    if size <= 0:
        raise ValueError("batch size must be positive")
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def find_prefix(buf, prefix):
    """True iff `buf` starts with `prefix`."""
    return bytes(buf[:len(prefix)]) == bytes(prefix)


def decode_printable(buf):
    """Render bytes as a console string, escaping non-printables."""
    out = []
    for b in bytes(buf):
        if b in (0x0d, 0x0a):
            out.append("\\r" if b == 0x0d else "\\n")
        elif 0x20 <= b < 0x7f:
            out.append(chr(b))
        else:
            out.append(f"\\x{b:02x}")
    return "".join(out)


# ── board-interaction helpers (take a RemoteClient-like `client`) ────────────
def _import_read_identifier():
    """Import the single shared identifier reader (b8008net.board, Task 10).

    Lazy + fallback so this script stays runnable standalone with only litex
    installed: when the b8008net package isn't pip-installed, its sources
    still live next to this file (host/b8008net/), so put host/ on sys.path
    and import from there. Kept out of module top-level so the pure helpers
    above remain importable with zero third-party deps."""
    try:
        from b8008net.board import read_identifier
    except ImportError:
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "host"))
        from b8008net.board import read_identifier
    return read_identifier


def get_identifier(client):
    """Best-effort read of the SoC identifier string."""
    ident = _import_read_identifier()(client)
    return "<no identifier_mem>" if ident is None else ident


def ram_window_base(client):
    return client.mems.b8008_ram.base


def ram_write_readback(client, count=256):
    """Burst-write 0..count-1 to the RAM window, read back, compare."""
    base = ram_window_base(client)
    expected = [i & 0xff for i in range(count)]
    for i, v in enumerate(expected):
        client.write(base + 4 * i, v)
    actual = [client.read(base + 4 * i) & 0xff for i in range(count)]
    return compare_bytes(expected, actual)


def drain_rx(client, max_bytes=4096):
    """Drain the console RX FIFO via b8008_rxlevel/b8008_rxtx CSRs."""
    out = bytearray()
    while len(out) < max_bytes:
        if client.regs.b8008_rxlevel.read() == 0:
            break
        out.append(client.regs.b8008_rxtx.read() & 0xff)
    return bytes(out)


def poll_banner(client, timeout_s=3.0, poll_s=0.02):
    """Accumulate console RX until the banner prefix appears or we time out."""
    buf = bytearray()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        buf += drain_rx(client)
        if find_prefix(buf, BANNER_PREFIX):
            return bytes(buf)
        time.sleep(poll_s)
    return bytes(buf)


def send_command(client, data):
    """Push each byte of `data` into the console TX (monitor RX) via b8008_rxtx."""
    for b in bytes(data):
        client.regs.b8008_rxtx.write(b)


def poll_marker(client, marker, timeout_s=3.0, poll_s=0.02):
    """Accumulate console RX until `marker` appears anywhere, or time out."""
    buf = bytearray()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        buf += drain_rx(client)
        if bytes(marker) in bytes(buf):
            return bytes(buf)
        time.sleep(poll_s)
    return bytes(buf)


# ── checks ───────────────────────────────────────────────────────────────────
def run_checks(client):
    failures = 0

    # 1: identity.
    ident = get_identifier(client)
    print(f"[1] connected; identifier: {ident!r}  PASS")

    # 2: RAM window burst write/readback.
    ok, idx = ram_write_readback(client, count=256)
    if ok:
        print("[2] RAM window: 256 bytes write/readback  PASS")
    else:
        print(f"[2] RAM window: mismatch at index {idx}  FAIL")
        failures += 1

    # 3: console banner.
    banner = poll_banner(client)
    if find_prefix(banner, BANNER_PREFIX):
        print(f"[3] console banner: {decode_printable(banner)!r}  PASS")
    else:
        print(f"[3] console banner: got {decode_printable(banner)!r}  FAIL")
        failures += 1

    # 4: command round-trip.
    send_command(client, HELP_COMMAND)
    resp = poll_marker(client, HELP_MARKER)
    if HELP_MARKER in resp:
        print(f"[4] command 'H' -> {decode_printable(resp)!r}  PASS")
    else:
        print(f"[4] command 'H' -> got {decode_printable(resp)!r}  FAIL")
        failures += 1

    return failures


def main():
    parser = argparse.ArgumentParser(
        description="RemoteClient self-test for the b8008_net SoC (Etherbone).")
    parser.add_argument("--csr",  required=True, help="Path to the SoC csr.csv.")
    parser.add_argument("--host", default="localhost",
                        help="litex_server host (default: localhost).")
    parser.add_argument("--port", default=1234, type=int,
                        help="litex_server port (default: 1234).")
    args = parser.parse_args()

    # Import lazily so the pure helpers above (and py_compile) do not require a
    # litex install; the FakeBoard unit tests in Task 10 exercise those directly.
    from litex.tools.litex_client import RemoteClient

    client = RemoteClient(host=args.host, port=args.port, csr_csv=args.csr)
    client.open()
    try:
        failures = run_checks(client)
    finally:
        client.close()

    if failures:
        print(f"host_selftest: {failures} check(s) FAILED")
        sys.exit(1)
    print("host_selftest: all checks PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
