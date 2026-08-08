# cli.py -- b8008net command-line entry point.
#
# Thin by design: argument parsing and print formatting only. discovery.py
# finds a board, board.py owns the lockfile/litex_server/RemoteClient
# lifecycle, console.py owns the byte path. SPEC.md S-PROD-2 narrows the
# product's complete host-visible operation set to two operations -- read a
# byte, write a byte -- and S-PROD-8 retires every other host-side command
# this CLI used to expose (load/peek/poke/run/reset/stop/step/interrupt
# injection): each of those already exists *inside* the monitor's own
# command set (H/D/W/L/G), reachable over the console `login` opens. The
# subcommands that used to target those retired CSRs, and the commands.py
# module that implemented them, are gone (make-login-console-client Task 2)
# -- the CSRs they wrote (b8008_status/b8008_ctl/the wishbone RAM window)
# no longer exist in the gateware (D-8/D-9/D-10), so there was nothing left
# for them to correctly do.
import argparse
import sys
import time

from . import discovery
from .board import Board
from .console import console_loop

# `login`'s zero-config default -- the standard build output path (see the
# Makefile's VERSA_DIR). Every other subcommand still requires --csr
# explicitly; `login` is the one `make login` drives with no arguments.
DEFAULT_CSR_CSV = "build/versa/csr.csv"

RTT_DEFAULT_READS = 50


def _connect(args):
    """Shared Board.connect() call: --csr/--host plus --no-cache (skip the
    discovery cache read; see discovery.py/board.py's stale-cache handling
    -- --no-cache forces a fresh DNS/sweep instead of trusting a possibly-
    dead cached IP)."""
    return Board.connect(args.csr, host=args.host, use_cache=not args.no_cache)


def _measure_rtt(board, reads=RTT_DEFAULT_READS):
    """Average CSR read round-trip time over `reads` back-to-back reads of
    console_tx ({level, full} -- read-only, no side effects), in seconds.
    Backs `b8008net status --rtt`.

    This used to poll the retired b8008_status register; SPEC.md S-PROD-8
    removed it from the gateware (D-8/D-9) along with the rest of the
    ctl/status bank, so there is nothing there to read any more. console_tx
    is part of the permanent product surface (SPEC.md S11.3) and gives the
    same measurement: litex_server clamps every UDP-transport CSR read to
    one 32-bit word per round trip, so this is still the real per-byte
    throughput ceiling for the console."""
    start = time.monotonic()
    for _ in range(reads):
        board.regs.b8008_console_console_tx.read()
    elapsed = time.monotonic() - start
    return elapsed / reads


def cmd_status(args):
    board = _connect(args)
    try:
        print(f"identifier:  {board.identifier()}")
        print(f"board host:  {board.host}")

        if args.rtt:
            rtt = _measure_rtt(board)
            print(f"avg CSR read RTT: {rtt * 1000:.2f} ms "
                  f"({RTT_DEFAULT_READS} reads)")
    finally:
        board.close()
    return 0


def _discovery_failure_message():
    """The message `login` prints when discovery finds nothing.

    The one thing this MUST do (see the task brief this was built against):
    name where it looked, not just that it failed. Someone running this
    against fresh hardware with no other way to reach the board needs the
    DNS names tried and the subnet range swept so they have somewhere to
    start -- a bare "board not found" (or worse, a traceback) leaves them
    nowhere. See discovery.py's discover() for the actual cache -> DNS ->
    sweep order this describes."""
    lines = [
        "error: could not find the board on the network "
        "(no cached host, DNS lookup failed, and the probe sweep got no reply).",
        f"  DNS names tried: {', '.join(discovery.DNS_NAMES)}",
    ]
    try:
        ip, netmask = discovery.local_ipv4_and_netmask()
        candidates = discovery.subnet_candidates(ip, netmask)
        if candidates:
            lines.append(
                f"  subnet swept: {candidates[0]}-{candidates[-1]} "
                f"({len(candidates)} host(s), netmask {netmask}, from this "
                f"machine's address {ip})")
        else:
            lines.append(
                f"  subnet swept: no other hosts on {ip}/{netmask}")
    except OSError:
        lines.append(
            "  subnet swept: none -- could not determine this machine's "
            "own IPv4 address/route (no network connectivity?)")
    lines.append(
        "  is the board powered on, plugged into this LAN, and has it had "
        "time to acquire a DHCP lease? Once you know its address, "
        "`--host <ip>` (or `make login HOST=<ip>`) skips discovery entirely.")
    return "\n".join(lines)


def cmd_login(args):
    """Resolve a host (explicit --host, else discover()), connect, print a
    one-line banner naming what was reached, then hand off to the
    interactive console (Ctrl-] to exit). `console` is a plain alias for
    this -- see build_parser()."""
    host = args.host
    if host is None:
        host = discovery.discover(use_cache=not args.no_cache)
        if host is None:
            print(_discovery_failure_message(), file=sys.stderr)
            return 1

    board = Board.connect(args.csr, host=host, use_cache=not args.no_cache)
    try:
        print(f"-- connected: {board.host}  (identifier: {board.identifier()!r}) --",
              file=sys.stderr)
        console_loop(board)
    finally:
        board.close()
    return 0


def _add_connection_args(parser):
    """--csr/--host/--no-cache, identical across every subcommand that talks
    to a board."""
    parser.add_argument("--csr", required=True, help="Path to the SoC csr.csv.")
    parser.add_argument(
        "--host", default=None,
        help="Board host/IP. Omit for zero-config discovery (cache -> DNS -> probe sweep).")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Skip the discovery cache read; force fresh DNS/probe-sweep "
             "discovery. Ignored when --host is given explicitly. Note: "
             "even without this flag, a cache-sourced host that fails to "
             "connect is automatically dropped from the cache and "
             "rediscovery is retried once before giving up.")


def _add_login_args(parser):
    """Like _add_connection_args, but --csr defaults to the standard build
    output path instead of being required -- `login`/`console` are the
    zero-config entry points `make login` drives with no arguments at all."""
    parser.add_argument(
        "--csr", default=DEFAULT_CSR_CSV,
        help=f"Path to the SoC csr.csv (default: {DEFAULT_CSR_CSV}).")
    parser.add_argument(
        "--host", default=None,
        help="Board host/IP. Omit for zero-config discovery (cache -> DNS -> probe sweep).")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Skip the discovery cache read; force fresh DNS/probe-sweep discovery.")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="b8008net",
        description="Host CLI for the b8008_net Intel-8008-over-Ethernet FPGA monitor.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser(
        "status", help="Connect to the board and print identifier/status.")
    _add_connection_args(status_parser)
    status_parser.add_argument(
        "--rtt", action="store_true",
        help=f"Also measure and print average CSR read round-trip time "
             f"({RTT_DEFAULT_READS} reads) -- litex_server clamps UDP CSR "
             "reads to one 32-bit word per round trip, so this is the real "
             "per-byte throughput ceiling for the console.")
    status_parser.set_defaults(func=cmd_status)

    login_parser = subparsers.add_parser(
        "login", help="Discover the board (or connect to --host) and open "
                       "an interactive console session with the 8008 "
                       "monitor (Ctrl-] to exit).")
    _add_login_args(login_parser)
    login_parser.set_defaults(func=cmd_login)

    # `console` is a plain alias of `login` -- same implementation, same
    # arguments, just the name someone might reach for out of habit.
    console_parser = subparsers.add_parser(
        "console", help="Alias for `login`.")
    _add_login_args(console_parser)
    console_parser.set_defaults(func=cmd_login)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
