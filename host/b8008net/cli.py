# cli.py -- b8008net command-line entry point.
#
# Thin by design (per Task 10 brief): argument parsing and print formatting
# only, all real work lives in discovery.py/board.py/commands.py/hexfile.py.
# First live run against hardware happens in Task 13; the load/peek/poke/
# run/reset/stop/step subcommands added in Task 12 are exercised here only
# via `b8008net --help` (install sanity) and the commands.py/hexfile.py unit
# tests (FakeBoard) -- no live board in this task.
import argparse
import sys

from . import commands, hexfile
from .board import Board
from .console import console_loop

# b8008_status bits (see b8008_net_core.v / versa_soc.py CSR bank):
#   bit 0: is_running -- b8008 core executing
#   bit 1: triggered   -- start/trigger strobe latched
#   bit 2: tx_busy     -- console TX in flight
STATUS_RUNNING_BIT   = 0
STATUS_TRIGGERED_BIT = 1
STATUS_TX_BUSY_BIT   = 2


def _bit(value, n):
    return bool((value >> n) & 1)


def _connect(args):
    """Shared Board.connect() call for every subcommand: --csr/--host plus
    --no-cache (skip the discovery cache read; see discovery.py/board.py's
    stale-cache handling -- --no-cache forces a fresh DNS/sweep instead of
    trusting a possibly-dead cached IP)."""
    return Board.connect(args.csr, host=args.host, use_cache=not args.no_cache)


def cmd_status(args):
    board = _connect(args)
    try:
        identifier = board.identifier()
        status = board.regs.b8008_status.read()

        print(f"identifier:  {identifier}")
        print(f"board host:  {board.host}")
        print(f"is_running:  {_bit(status, STATUS_RUNNING_BIT)}")
        print(f"triggered:   {_bit(status, STATUS_TRIGGERED_BIT)}")
        print(f"tx_busy:     {_bit(status, STATUS_TX_BUSY_BIT)}")

        if args.rtt:
            rtt = commands.measure_rtt(board)
            print(f"avg CSR read RTT: {rtt * 1000:.2f} ms "
                  f"({commands.RTT_DEFAULT_READS} reads)")
    finally:
        board.close()
    return 0


def cmd_console(args):
    board = _connect(args)
    try:
        console_loop(board)
    finally:
        board.close()
    return 0


# Errors that mean "the operation was refused/failed cleanly" -- caught at
# the CLI boundary and reported as a one-line message + nonzero exit, not a
# traceback. (AddressRangeError subclasses ValueError, which argparse-style
# CLIs traditionally reserve for usage errors, but here it's a runtime
# range check against the connected board's RAM window, not a parse error.)
_COMMAND_ERRORS = (
    commands.AddressRangeError,
    commands.VerifyError,
    commands.NotStoppedError,
    commands.RunStateError,
    hexfile.HexFileError,
)


def _parse_addr(text):
    return int(text, 16)


def cmd_load(args):
    try:
        with open(args.file, encoding="ascii") as f:
            segments = hexfile.parse(f.read())
    except (OSError, hexfile.HexFileError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    board = _connect(args)
    try:
        commands.load(board, segments, force=args.force)
    except _COMMAND_ERRORS as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        board.close()

    total = sum(len(data) for _, data in segments)
    print(f"loaded and verified {total} bytes across {len(segments)} segment(s)")
    return 0


def cmd_peek(args):
    addr = _parse_addr(args.addr)
    board = _connect(args)
    try:
        data = commands.peek(board, addr, args.length)
    except _COMMAND_ERRORS as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        board.close()

    print(commands.format_hexdump(addr, data))
    return 0


def cmd_poke(args):
    addr = _parse_addr(args.addr)
    try:
        values = [int(b, 16) for b in args.bytes]
    except ValueError as e:
        print(f"error: invalid byte value ({e})", file=sys.stderr)
        return 1
    if any(not (0 <= v <= 0xFF) for v in values):
        print("error: byte values must be 00-FF", file=sys.stderr)
        return 1

    board = _connect(args)
    try:
        commands.poke(board, addr, values)
    except _COMMAND_ERRORS as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        board.close()

    print(f"wrote and verified {len(values)} byte(s) at 0x{addr:04X}")
    return 0


def cmd_run(args):
    addr = _parse_addr(args.addr)
    board = _connect(args)
    try:
        commands.run(board, addr)
    except _COMMAND_ERRORS as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        board.close()

    print(f"sent: G {addr:04X}")
    return 0


def cmd_reset(args):
    board = _connect(args)
    try:
        commands.reset(board)
    except _COMMAND_ERRORS as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        board.close()

    print("core reset (stop-then-run cycle) -- monitor banner should "
          "reappear in ~400 ms")
    return 0


def cmd_stop(args):
    board = _connect(args)
    try:
        commands.stop(board)
    except _COMMAND_ERRORS as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        board.close()

    print("core stopped")
    return 0


def cmd_step(args):
    board = _connect(args)
    try:
        commands.step(board, sync=(args.mode == "sync"))
    except _COMMAND_ERRORS as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        board.close()

    print(f"stepped ({args.mode or 'cycle'})")
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
             f"({commands.RTT_DEFAULT_READS} reads) -- litex_server clamps "
             "UDP CSR reads to one 32-bit word per round trip, so this is "
             "the real per-byte/word throughput ceiling for console/load/"
             "poke on hardware.")
    status_parser.set_defaults(func=cmd_status)

    console_parser = subparsers.add_parser(
        "console", help="Interactive console session with the b8008 monitor (Ctrl-] to exit).")
    _add_connection_args(console_parser)
    console_parser.set_defaults(func=cmd_console)

    load_parser = subparsers.add_parser(
        "load", help="Load an Intel-HEX file into RAM (0x1000-0x3FFF), "
                      "with burst read-back verification.")
    _add_connection_args(load_parser)
    load_parser.add_argument("file", help="Intel-HEX file (as produced by p2hex).")
    load_parser.add_argument(
        "--force", action="store_true",
        help="Load even if the core is currently running (status.is_running).")
    load_parser.set_defaults(func=cmd_load)

    peek_parser = subparsers.add_parser(
        "peek", help="Hexdump RAM starting at ADDR (0x1000-0x3FFF).")
    _add_connection_args(peek_parser)
    peek_parser.add_argument("addr", help="Hex address, e.g. 2000 or 0x2000.")
    peek_parser.add_argument(
        "length", nargs="?", type=int, default=16,
        help="Number of bytes to read (default 16).")
    peek_parser.set_defaults(func=cmd_peek)

    poke_parser = subparsers.add_parser(
        "poke", help="Write bytes to RAM starting at ADDR (0x1000-0x3FFF), "
                      "each verified by readback.")
    _add_connection_args(poke_parser)
    poke_parser.add_argument("addr", help="Hex address, e.g. 2000 or 0x2000.")
    poke_parser.add_argument(
        "bytes", nargs="+", help="Hex byte value(s), e.g. AA BB 01.")
    poke_parser.set_defaults(func=cmd_poke)

    run_parser = subparsers.add_parser(
        "run",
        help="Jump the monitor to ADDR (sends 'G ADDR' via the console).",
        description="Send 'G ADDR' through the console so the monitor "
                    "firmware executes the jump itself. If the core is "
                    "stopped, it is restarted first (one ctl.run_stop "
                    "pulse) -- restarting re-bootstraps the monitor, so "
                    "the '8008 Monitor' banner reappears (~400 ms) and is "
                    "awaited (up to ~3 s) before the G command is sent; "
                    "if no banner appears, G is sent anyway with a warning.")
    _add_connection_args(run_parser)
    run_parser.add_argument("addr", help="Hex address, e.g. 2000 or 0x2000.")
    run_parser.set_defaults(func=cmd_run)

    reset_parser = subparsers.add_parser(
        "reset",
        help="Restart the monitor (stop-then-run cycle).",
        description="Restart the monitor. There is no reset CSR field -- "
                    "this does a stop-then-run cycle on ctl.run_stop, which "
                    "re-bootstraps the b8008 core: the '8008 Monitor' banner "
                    "reappears on the console ~400 ms later, same as "
                    "power-on. Anything the previous program was doing is "
                    "abandoned; RAM contents survive (only the core is "
                    "reset, not the RAM).")
    _add_connection_args(reset_parser)
    reset_parser.set_defaults(func=cmd_reset)

    stop_parser = subparsers.add_parser(
        "stop",
        help="Halt the b8008 core's clock (ctl.run_stop, toggle-and-verify).",
        description="Halt the b8008 core's clock via ctl.run_stop "
                    "(toggle-and-verify, max 3 attempts). NOTE: leaving the "
                    "stopped state is always a monitor RESTART, not a "
                    "resume -- 'reset' does it explicitly, and 'run ADDR' "
                    "on a stopped core restarts it automatically (then "
                    "waits for the banner) before sending G. The banner "
                    "reappearing ~400 ms after any of these is expected, "
                    "same as power-on.")
    _add_connection_args(stop_parser)
    stop_parser.set_defaults(func=cmd_stop)

    step_parser = subparsers.add_parser(
        "step", help="Single-step the core (only meaningful while stopped; "
                      "warns if the core is running).")
    _add_connection_args(step_parser)
    step_parser.add_argument(
        "mode", nargs="?", choices=["sync"], default=None,
        help="Omit for a plain cycle step, or 'sync' for a state-boundary step.")
    step_parser.set_defaults(func=cmd_step)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
