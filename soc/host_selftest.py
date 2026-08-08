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
# Three checks (exit 0 iff all pass):
#   1. Connect, print the SoC identifier.
#   2. Console RX: poll the console_rx/console_rx_pop FIFO (non-destructive
#      read, explicit pop -- SPEC.md S-RX-3/S-RX-4), assert the "8008 "
#      banner prefix; print the decoded banner.
#   3. Console TX->RX: send a monitor command ('H' + CR, the help command from
#      b8008_monitor.asm) via console_tx_data, assert the "Help" response
#      echoes back.
#
# RETIRED (make-login-console-client Task 2): this used to run a check
# between what are now [1] and [2] -- a wishbone RAM-window burst
# write/readback against client.mems.b8008_ram. SPEC.md S-PROD-8 retired the
# host-facing RAM window entirely (D-10, csr.csv confirms b8008_ram no
# longer appears in the memory map): the 8008's 16KB RAM is wired to the
# core's own b8008-domain port only now (soc/b8008_integration.py), with no
# host-reachable CSR/wishbone path to it at all. There is nothing left for a
# host-side check to exercise, so the check is gone rather than left calling
# a memory region that no longer exists -- recorded here instead of leaving
# a silent gap in the numbering.
#
# CSR/memory names carry the SoC's "b8008_" bank prefix (self.b8008 = B8008Core
# in versa_soc.py); the console registers carry the console sub-bank's own
# prefix too (b8008_console_console_*, see console_bridge.py). Never hardcode
# an address -- everything here goes through litex's regs/bases lookup, which
# is generated from the live csr.csv.
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
def _import_from_b8008net(module_name):
    """Import `module_name` from the sibling b8008net package (host/b8008net/).

    Lazy + fallback so this script stays runnable standalone with only litex
    installed: when the b8008net package isn't pip-installed, its sources
    still live next to this file (host/b8008net/), so put host/ on sys.path
    and import from there. Kept out of module top-level so the pure helpers
    above remain importable with zero third-party deps. Shared by
    get_identifier() (board.read_identifier) and the console checks below
    (console.drain/console.send), so there is exactly one import path for
    "the b8008net package isn't installed yet" instead of one per caller."""
    import importlib
    try:
        return importlib.import_module(f"b8008net.{module_name}")
    except ImportError:
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "host"))
        return importlib.import_module(f"b8008net.{module_name}")


def get_identifier(client):
    """Best-effort read of the SoC identifier string."""
    read_identifier = _import_from_b8008net("board").read_identifier
    ident = read_identifier(client)
    return "<no identifier_mem>" if ident is None else ident


def drain_rx(client):
    """Drain the console RX FIFO via the non-destructive console_rx /
    console_rx_pop protocol (SPEC.md S-RX-3/S-RX-4): delegate to
    b8008net.console.drain(), the single implementation of that bit layout
    and protocol (see its docstring for why read and pop are split, and why
    that split must not be "simplified" back into one destructive read),
    rather than re-deriving it here. Bounded at console.DRAIN_MAX_BYTES
    (4096, the RX FIFO depth), same as every other caller of that
    function."""
    return _import_from_b8008net("console").drain(client)


def send_command(client, data):
    """Push `data` into the console TX (monitor RX) via
    b8008net.console.send() -- checks console_tx.full before every byte and
    paces TX_GAP_S between them (SPEC.md S-PROD-6/S-TX-2/S-TX-3), the same
    protocol the interactive console uses. A short write here (FIFO stuck
    full) simply shows up as poll_marker() below never seeing HELP_MARKER
    arrive -- check [3]'s FAIL output names that explicitly."""
    _import_from_b8008net("console").send(client, data)


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

    # 2: console banner.
    banner = poll_banner(client)
    if find_prefix(banner, BANNER_PREFIX):
        print(f"[2] console banner: {decode_printable(banner)!r}  PASS")
    else:
        print(f"[2] console banner: got {decode_printable(banner)!r}  FAIL")
        failures += 1

    # 3: command round-trip.
    send_command(client, HELP_COMMAND)
    resp = poll_marker(client, HELP_MARKER)
    if HELP_MARKER in resp:
        print(f"[3] command 'H' -> {decode_printable(resp)!r}  PASS")
    else:
        print(f"[3] command 'H' -> got {decode_printable(resp)!r}  FAIL")
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
