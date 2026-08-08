# console.py -- interactive console session with the b8008 monitor over the
# board's console CSR bank (SPEC.md S8/S9/S11), rewritten (Task 1,
# make-login-console-client) against the registers that replaced the old
# destructive-read rxtx/rxlevel/txfull surface.
#
# Register contract this module speaks (SPEC.md S11, addresses from
# csr.csv -- never hardcoded here):
#   console_rx        RO  {data[7:0], valid[8], level[21:9]}, one atomic
#                          read (S-RX-8): never read level and data
#                          separately, never torn.
#   console_rx_pop     WO  write-any consumes exactly one byte (S-CSR-4).
#                          THE ONLY thing that consumes a byte (S-RX-4).
#   console_tx         RO  {level[8:0], full[9]}.
#   console_tx_data     WO  push one byte; REJECTED (not queued) while full
#                          (S-TX-3).
#   console_err        RO  sticky rx_overflow/tx_write_when_full/
#                          rx_pop_when_empty bits (S-CSR-9): survive reset,
#                          cleared only by console_err_clear.
#   console_err_clear   WO  write-1-to-clear, bit-for-bit (S-CSR-11).
#
# Why read and pop are split (S-RX-5): LiteX's CommUDP retries a timed-out
# read. If reading console_rx consumed a byte (as the old rxtx register
# did), a dropped reply packet followed by a retry would silently discard
# a byte the host never saw -- a loss *inside* the product's own guarantee
# boundary. Making the read non-destructive and idempotent (S-RX-3) means
# a retried read is harmless; only the explicit console_rx_pop write
# consumes anything, and UDP writes in this transport are not retried
# (S-RX-6), so a pop is never duplicated. This costs ~2 UDP round trips
# per RX byte -- one read, one pop (S-WIRE-3, S-WIRE-4) -- and that cost
# is accepted, not optimized away: collapsing it back into a single
# destructive read/pop operation would reintroduce the exact bug this
# register rewrite exists to fix. Do not "simplify" it.
#
# Monitor behavior note: a remote stop->run cycle (e.g. via another
# b8008net session or a future `b8008net run`) resets and re-bootstraps
# the monitor -- the "8008 Monitor" banner reappears on the console
# roughly 400 ms after run, same as first power-on. That's expected, not
# a bug: a console user who sees the banner twice just watched the core
# get reset out from under them.
import sys
import time

# console_rx bit layout (SPEC.md S11.1)
_RX_DATA_MASK = 0xFF
_RX_VALID_BIT = 1 << 8

# console_tx bit layout (SPEC.md S11.3)
_TX_FULL_BIT = 1 << 9

# console_err bit layout (SPEC.md S11.5)
ERR_RX_OVERFLOW = 1 << 0
ERR_TX_WRITE_WHEN_FULL = 1 << 1
ERR_RX_POP_WHEN_EMPTY = 1 << 2

_ERROR_NAMES = (
    (ERR_RX_OVERFLOW, "rx_overflow"),
    (ERR_TX_WRITE_WHEN_FULL, "tx_write_when_full"),
    (ERR_RX_POP_WHEN_EMPTY, "rx_pop_when_empty"),
)

DRAIN_MAX_BYTES = 4096  # rx_fifo depth (S-RX-1): bounds drain()'s loop so an
                        # infinitely chatty board can't starve the pump's
                        # stdin side.

# TX_GAP_S is a correctness parameter, not a tuning knob. The guarantee
# this product makes (S-PROD-3) ends at the core's uart_rx pin (S-PROD-6):
# past that pin, the 8008 monitor's receive path is a polled single-byte
# loop with no buffer (S-CORE-15), and at 115200 baud/8N1 one byte-time is
# 86.805 us (S-CLK-5) versus a 22 us shortest 8008 instruction (S-CORE-8)
# -- the monitor gets at most floor(86.805/22) = 3 instructions between
# consecutive bytes at line rate (S-CLK-7), and its hex-load inner loop is
# longer than that (S-CORE-15). Transmitting unpaced therefore *will* lose
# bytes inside the 8008 core itself, past every FIFO tx_fifo/rx_fifo in
# this design -- a loss no CSR, sticky bit, or retry logic on this side of
# uart_rx can ever detect. TX_GAP_S = 3 ms gives ~34 byte-times of margin
# between transmitted bytes, comfortably covering the monitor's slowest
# inner loop. Do not shrink this to "speed up" sends.
TX_GAP_S = 0.003

# send()'s bound on how long it will wait for console_tx.full to clear
# before a single byte before giving up and reporting a short write. Real
# stalls this long mean something downstream (a wedged monitor, a host
# that stopped draining) is wrong; spinning forever would hang the pump.
TX_FULL_TIMEOUT_S = 1.0
TX_FULL_POLL_S = 0.001  # spin interval while waiting for console_tx.full to clear

POLL_INTERVAL_S = 0.01  # console_loop's select() cadence
READ_CHUNK = 4096       # max bytes pulled from stdin per pump iteration
EXIT_BYTE = 0x1D        # Ctrl-] -- console_loop's escape sequence


def drain(board):
    """Pop whatever's currently waiting in the console RX FIFO, one byte at
    a time: read console_rx (a single register read yields the atomic
    {data, valid, level} triple -- S-RX-8, never read level and data
    separately), stop as soon as valid == 0, otherwise record the byte and
    issue exactly one console_rx_pop write to consume it before reading
    again. Bounded at DRAIN_MAX_BYTES so a board that keeps producing bytes
    faster than the host drains can't starve the rest of the pump loop.

    This costs ~2 UDP round trips per byte returned -- one console_rx read,
    one console_rx_pop write -- versus one round trip for the old
    destructive-read register. That cost is the accepted consequence of
    S-RX-3's non-destructive read (S-WIRE-4 already accepts the product
    being slow by design): it's what makes a CommUDP read retry safe
    (S-RX-5) instead of silently eating a byte the host never saw. Never
    pop without having first seen valid == 1 for that byte (S-RX-9: a pop
    against an empty FIFO is a no-op that sets a sticky error bit), and
    never retry a pop (S-RX-6) -- pops are the one consuming action on this
    path and must fire exactly once per byte."""
    rx = board.regs.b8008_console_console_rx
    pop = board.regs.b8008_console_console_rx_pop
    out = bytearray()
    for _ in range(DRAIN_MAX_BYTES):
        word = rx.read()
        if not (word & _RX_VALID_BIT):
            break
        out.append(word & _RX_DATA_MASK)
        pop.write(1)  # value is ignored (S-CSR-4); this is the only consuming action
    return bytes(out)


def send(board, data, gap_s=None):
    """Push each byte of `data` to the board's console TX (monitor RX),
    checking console_tx.full before every write (S-TX-2/S-TX-3: a write
    while full is rejected outright, not queued, so we must never issue
    one) and pacing TX_GAP_S (default) between bytes -- see the module
    comment above for why that pacing is a correctness requirement, not an
    optimization.

    Returns the count of bytes actually accepted. If the FIFO stays full
    past TX_FULL_TIMEOUT_S for a given byte, send() gives up on that byte
    and returns short rather than spinning forever or writing into a full
    FIFO and losing the byte silently -- a caller MUST check the return
    value against len(data) to know whether everything landed."""
    if gap_s is None:
        gap_s = TX_GAP_S
    tx = board.regs.b8008_console_console_tx
    tx_data = board.regs.b8008_console_console_tx_data

    accepted = 0
    for byte in bytes(data):
        waited = 0.0
        while tx.read() & _TX_FULL_BIT:
            if waited >= TX_FULL_TIMEOUT_S:
                return accepted  # bounded wait exceeded -- report short, don't spin
            time.sleep(TX_FULL_POLL_S)
            waited += TX_FULL_POLL_S
        tx_data.write(byte)
        accepted += 1
        if gap_s:
            time.sleep(gap_s)
    return accepted


def check_errors(board, clear=True):
    """Read console_err's sticky fault bits (S-CSR-9: they survive reset
    and persist until explicitly cleared) and, when `clear` is true, W1C
    exactly the bits observed -- never a blanket write, so a fault that
    sets a bit in the instant between our read and our clear (S-CSR-12)
    isn't lost. Returns the observed bitmask (0 if nothing is set)."""
    err_reg = board.regs.b8008_console_console_err
    err = err_reg.read()
    if err and clear:
        board.regs.b8008_console_console_err_clear.write(err)
    return err


def _describe_errors(err):
    return [name for bit, name in _ERROR_NAMES if err & bit]


def _report_errors(err):
    """Announce a fault on stderr -- never into the byte stream, so it can
    never be mistaken for monitor output. This is the entire reason the
    sticky bits exist: a transcript with a hole must say so."""
    names = ", ".join(_describe_errors(err)) or f"0x{err:x}"
    print(f"-- console: error condition detected ({names}) -- bytes may be "
          f"missing from this transcript --", file=sys.stderr)


def console_pump(board, stdin, stdout, stdin_ready):
    """One iteration of the interactive loop body: check for sticky error
    conditions and announce them, drain the board to stdout, and -- if
    stdin_ready -- forward buffered stdin to the board, stopping at (and
    not sending) the Ctrl-] escape byte. Returns False to request the loop
    exit, True to keep going.

    Deliberately split out from console_loop's termios/select plumbing so
    this body is testable with plain file-like fakes (see test_console.py):
    no real tty or select() needed here."""
    err = check_errors(board)
    if err:
        _report_errors(err)

    out = drain(board)
    if out:
        stdout.write(out)
        flush = getattr(stdout, "flush", None)
        if flush is not None:
            flush()

    if stdin_ready:
        data = stdin.read(READ_CHUNK)
        if not data:
            return False  # EOF on stdin
        exit_idx = data.find(bytes([EXIT_BYTE]))
        if exit_idx != -1:
            if exit_idx > 0:
                _send_and_report(board, data[:exit_idx])
            return False
        _send_and_report(board, data)

    return True


def _send_and_report(board, data):
    """Wrap send() so a short write is never silently swallowed. send()
    itself already refuses to spin forever or write into a full FIFO --
    but if console_pump ignores the count it returns, those bytes vanish
    with nothing said, and neither software nor hardware indicator can
    ever catch it after the fact: a client that (correctly) never writes
    while console_tx.full is set never trips tx_write_when_full either.
    This is the one loss path in this module that the sticky bits cannot
    see, so it has to be reported here, at the only point that knows how
    many bytes were asked for versus accepted."""
    accepted = send(board, data)
    if accepted < len(data):
        _report_dropped(len(data) - accepted, len(data))


def _report_dropped(dropped, total):
    """Announce a short TX write on stderr -- never into the byte stream,
    for the same reason _report_errors keeps its message out of stdout:
    it must not be mistaken for monitor output."""
    print(f"-- console: {dropped} of {total} typed byte(s) dropped -- TX "
          f"FIFO stayed full --", file=sys.stderr)


def console_loop(board, stdin=None, stdout=None):
    """Interactive console session: puts stdin in raw tty mode, then polls
    the board and stdin every POLL_INTERVAL_S via select(), pumping bytes
    both ways (console_pump) until Ctrl-] or EOF. Terminal settings are
    always restored, on every exit path (normal, Ctrl-], EOF, or
    KeyboardInterrupt).

    termios/tty/select/os are imported here rather than at module level so
    the rest of this module (drain/send/console_pump, all unit-tested
    against FakeBoard) stays importable and testable on hosts/CI without a
    real controlling tty."""
    import os
    import select
    import termios
    import tty

    real_stdin = stdin if stdin is not None else sys.stdin
    real_stdout = stdout if stdout is not None else sys.stdout

    fd = real_stdin.fileno()
    out = getattr(real_stdout, "buffer", real_stdout)

    class _FdReader:
        @staticmethod
        def read(n):
            return os.read(fd, n)

    reader = _FdReader()

    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        print("-- b8008net console: Ctrl-] to exit --\r", file=sys.stderr)
        while True:
            ready, _, _ = select.select([fd], [], [], POLL_INTERVAL_S)
            if not console_pump(board, reader, out, stdin_ready=bool(ready)):
                break
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
