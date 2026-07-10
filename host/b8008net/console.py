# console.py -- interactive console session with the b8008 monitor over the
# board's rxtx/rxlevel/txfull CSR FIFOs (Task 11).
#
# Monitor behavior note: a remote stop->run cycle (e.g. via another
# b8008net session or a future `b8008net run`) resets and re-bootstraps
# the monitor -- the "8008 Monitor" banner reappears on the console
# roughly 400 ms after run, same as first power-on. That's expected, not
# a bug: a console user who sees the banner twice just watched the core
# get reset out from under them.
#
# rxtx reads are destructive pops; CommUDP retries a timed-out read and
# the retry pops the NEXT byte -- a lost response packet = one lost
# console byte. Inherent to CSR-FIFO-over-Etherbone (litex's uartbone
# shares it), rare on LAN, not worth engineering around.
import sys
import time

DRAIN_BATCH_MAX = 256   # max bytes per drain() burst read
TXFULL_POLL_S = 0.001   # spin interval while send() waits for txfull to clear
POLL_INTERVAL_S = 0.01  # console_loop's select() cadence
READ_CHUNK = 4096       # max bytes pulled from stdin per pump iteration
EXIT_BYTE = 0x1D        # Ctrl-] -- console_loop's escape sequence


def drain(board):
    """Pop whatever's currently waiting in the console RX FIFO as a single
    batched burst read (never a read-per-byte loop): consult rxlevel, then
    read at most DRAIN_BATCH_MAX bytes from rxtx at a fixed address. Callers
    that want everything call this repeatedly (console_loop does, once per
    poll tick).

    Real-hardware throughput model (accepted for now -- decision recorded
    here, no code workaround exists at this layer): the gateware itself
    supports genuine bursts (CommUART's read_max_length is 255 in
    litex_server.py), but this project talks UDP, and litex_server hard-
    codes RemoteServer.read_max_length={"CommUDP": 1} -- one 32-bit word
    per UDP round trip, no matter what burst mode the client asks for.
    comm_udp.py's CommUDP.read() goes further and asserts burst == "incr",
    so even the "fixed" burst this call requests (`board.read(addr, n,
    burst="fixed")`, correct for reading a FIFO tap without incrementing
    the address) is not honored server-side for UDP transport -- it's
    silently downgraded by the read-merger loop into `n` separate
    single-word round trips. So: on real hardware, this drain (and the
    load()/poke() verify-readback path in commands.py) costs ~1 RTT per
    byte/word, regardless of DRAIN_BATCH_MAX or burst="fixed" -- those
    still matter for correctness (address handling) and for the sim/local
    path, just not for wall-clock throughput over UDP.

    Decision: ACCEPT for now; measure the real RTT at the hardware stage
    with `b8008net status --rtt` (commands.measure_rtt) rather than guess.
    An actual fix would be patching litex_server to raise CommUDP's
    read_max_length (the gateware already supports the bigger bursts), or
    writing a from-scratch CommUDP-alike client that talks Etherbone
    directly and skips litex_server's read-merger entirely -- both out of
    scope here."""
    level = board.regs.b8008_rxlevel.read()
    if level == 0:
        return b""
    n = min(level, DRAIN_BATCH_MAX)
    addr = board.regs.b8008_rxtx.addr
    data = board.read(addr, n, burst="fixed")
    return bytes(byte & 0xFF for byte in data)


def send(board, data):
    """Push each byte of `data` to the board's console TX (monitor RX),
    spinning on txfull before every byte so we never overrun the FIFO."""
    for byte in bytes(data):
        while board.regs.b8008_txfull.read():
            time.sleep(TXFULL_POLL_S)
        board.regs.b8008_rxtx.write(byte)


def console_pump(board, stdin, stdout, stdin_ready):
    """One iteration of the interactive loop body: drain the board to
    stdout, and -- if stdin_ready -- forward buffered stdin to the board,
    stopping at (and not sending) the Ctrl-] escape byte. Returns False to
    request the loop exit, True to keep going.

    Deliberately split out from console_loop's termios/select plumbing so
    this body is testable with plain file-like fakes (see test_console.py):
    no real tty or select() needed here."""
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
                send(board, data[:exit_idx])
            return False
        send(board, data)

    return True


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
