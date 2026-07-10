# commands.py -- load/peek/poke/run/reset/stop/step against a Board (Task 12).
#
# RAM window (binding facts, Task 4/9 findings): word_addr = ram_base +
# 4*a8008 -- the wishbone word index IS the absolute 14-bit 8008 address, no
# offset subtraction. load/peek/poke are restricted to 0x1000-0x3FFF, the
# monitor's RAM region at the default map generics (see b8008_monitor.asm:
# "Loaded programs use 0x2000-0x3EFF; the monitor owns page 0x3F00-0x3FFF" --
# 0x1000-0x1FFF is also RAM per the b8008_top DEFAULT map, just unused by the
# monitor's own conventions).
#
# ctl fields (b8008_integration.py's CSRStorage): run_stop=bit0,
# step_cycle=bit1, step_sync=bit2, int_req=bit3, int_vector=bits4-6, each a
# pulse=True field -- writing a 1 bit drives it high for exactly one b8008
# cycle then it self-clears. status fields: is_running=bit0.
#
# `run` sends "G ADDR\r" through the console byte path (console.py) so the
# *monitor firmware* executes the jump, same as typing it at the prompt or
# send_hex.py's --go. If the core is stopped it is first restarted (one
# ctl.run_stop pulse -- which re-bootstraps the monitor) and the boot
# banner awaited, because a stopped core's USART never consumes console
# bytes. `stop` / `reset` / `step` pulse ctl bits directly -- they control
# whether the b8008 core's clock runs at all, independent of what firmware
# (if any) is resident.
import sys
import time

from . import console

RAM_MIN = 0x1000
RAM_MAX = 0x3FFF
CPU_ADDR_MAX = 0x3FFF  # full 14-bit 8008 address space, for `run`

STATUS_RUNNING_BIT = 0

CTL_RUN_STOP_BIT = 0
CTL_STEP_CYCLE_BIT = 1
CTL_STEP_SYNC_BIT = 2
CTL_INT_REQ_BIT = 3
CTL_INT_VECTOR_SHIFT = 4

RUN_STATE_MAX_ATTEMPTS = 3

# run()-from-stopped: how long to poll the console for the monitor's boot
# banner/prompt after restarting the core, and the poll cadence. The banner
# lands ~400 ms after run on hardware; 3 s is deliberately generous and gets
# calibrated at the hardware stage.
BANNER_TIMEOUT_S = 3.0
BANNER_POLL_S = 0.05
MONITOR_PROMPT = b">"

# run()-from-stopped: how long to spend draining any stale bytes out of the
# RX FIFO before pulsing run_stop. See _drain_stale()'s docstring for why
# this has to happen -- it's a correctness fix, not a nicety.
DRAIN_STALE_TIMEOUT_S = 1.0


class AddressRangeError(ValueError):
    """addr (or addr+length-1) falls outside the valid RAM command window."""


class VerifyError(Exception):
    """A post-write readback didn't match what was written."""

    def __init__(self, offset, expected, got):
        self.offset = offset
        self.expected = expected
        self.got = got
        super().__init__(
            f"verify failed at 0x{offset:04X}: expected 0x{expected:02X}, "
            f"got 0x{got:02X}")


class NotStoppedError(Exception):
    """load() refused: the core is running and force=True wasn't given."""


class RunStateError(Exception):
    """set_run_state() couldn't reach the target running state within
    RUN_STATE_MAX_ATTEMPTS ctl.run_stop pulses."""


def _bit(value, n):
    return bool((value >> n) & 1)


def _check_range(addr, length, low=RAM_MIN, high=RAM_MAX):
    end = addr + length - 1
    if addr < low or end > high:
        raise AddressRangeError(
            f"address range 0x{addr:04X}-0x{end:04X} outside valid window "
            f"0x{low:04X}-0x{high:04X}")


def _read_words(board, word_addr, n):
    """Board.read (like the underlying litex RemoteClient) returns a bare
    int for n==1 and a list for n>1 -- normalize to a list either way."""
    result = board.read(word_addr, n)
    return [result] if n == 1 else list(result)


def is_running(board):
    return _bit(board.regs.b8008_status.read(), STATUS_RUNNING_BIT)


def _pulse_ctl(board, bit):
    board.regs.b8008_ctl.write(1 << bit)


def set_run_state(board, running, max_attempts=RUN_STATE_MAX_ATTEMPTS):
    """Toggle-and-verify: already-there is a no-op; otherwise pulse
    ctl.run_stop and re-read status, up to max_attempts times. Raises
    RunStateError if the target state is never reached.

    No explicit settle delay between the pulse and the status re-read: the
    CDC path (PulseSynchronizer sys->b8008 for the pulse, MultiReg
    b8008->sys for status) settles in a handful of clock cycles, which the
    UDP round-trip latency between the two CSR accesses covers many times
    over."""
    for _ in range(max_attempts):
        if is_running(board) == running:
            return
        _pulse_ctl(board, CTL_RUN_STOP_BIT)

    if is_running(board) != running:
        raise RunStateError(
            f"could not reach running={running} after {max_attempts} "
            f"run_stop pulses (status stuck at running={is_running(board)})")


def stop(board):
    """Halt the b8008 core's clock (ctl.run_stop toggle-and-verify)."""
    set_run_state(board, running=False)


def reset(board):
    """There is no reset CSR field (b8008_integration.py's ctl fields are
    run_stop/step_cycle/step_sync/int_req/int_vector only). A stop-then-run
    cycle re-bootstraps the monitor instead -- console.py documents the
    live symptom: the "8008 Monitor" banner reappears ~400 ms after run.
    That's what this does: force the core to the stopped (reset-held)
    state, then back to running."""
    set_run_state(board, running=False)
    set_run_state(board, running=True)


def step(board, sync=False):
    """Pulse step_cycle (or step_sync). Only meaningful while the core is
    stopped -- warns (does not refuse) if it's currently running."""
    if is_running(board):
        print(
            "warning: step while the core is running has no defined "
            "effect -- stop it first",
            file=sys.stderr)
    _pulse_ctl(board, CTL_STEP_SYNC_BIT if sync else CTL_STEP_CYCLE_BIT)


def _drain_stale(board, timeout=DRAIN_STALE_TIMEOUT_S):
    """Empty the RX FIFO before restarting a stopped core.

    The RX FIFO is 4096 deep and persists across a stop -- it is NOT reset
    by ctl.run_stop. If the console was used before the core was stopped
    (or a previous run() left its own trailing '>' behind), a stale '>'
    can already be sitting in the FIFO. _await_monitor_prompt() only checks
    for MONITOR_PROMPT in whatever drain() returns -- it can't tell a
    leftover byte from a fresh one -- so without this, that stale '>'
    would satisfy the very first poll, before the just-issued run_stop
    pulse has even reset the monitor. The 'G ADDR\\r' bytes then get sent
    into the monitor's ~400 ms boot window, where nothing is listening on
    the USART yet: the bytes are silently lost, and the CLI reports
    success because -- as far as it can tell -- the prompt showed up and
    the write to rxtx didn't error.

    So: drain to empty first, BEFORE the run_stop pulse, unconditionally.
    Only once the FIFO is provably empty does a prompt that shows up next
    mean anything. Bounded by `timeout` so a pathological FIFO (or a board
    stuck continuously emitting) can't hang this forever."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not console.drain(board):
            return


def _await_monitor_prompt(board, timeout=BANNER_TIMEOUT_S):
    """Poll console.drain() until the monitor's prompt ('>', the tail of
    its boot banner -- same readiness check send_hex.py uses) shows up, or
    the timeout expires. Returns True if the prompt was seen."""
    seen = bytearray()
    deadline = time.monotonic() + timeout
    while True:
        seen += console.drain(board)
        if MONITOR_PROMPT in seen:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(BANNER_POLL_S)


def run(board, addr, banner_timeout=BANNER_TIMEOUT_S):
    """Send 'G ADDR\\r' through the console byte path so the monitor stays
    in charge. ADDR is zero-padded to 4 uppercase hex digits -- matches
    b8008_monitor.asm's parse_dump_args (variable-length hex accumulator,
    terminated by space/comma/CR/invalid digit; leading zeros are harmless).

    If the core is stopped, its USART never consumes console bytes and the
    G command would be a silent no-op -- so first drain any stale bytes out
    of the RX FIFO (_drain_stale -- a leftover '>' from before the stop
    must not be mistaken for a fresh one), then restart the core
    (set_run_state; per debug_clock_control this re-bootstraps the
    monitor), then wait for the boot banner/prompt on the console before
    sending G. If no banner shows up within banner_timeout, proceed anyway
    with a warning (the hardware stage calibrates the real boot delay)."""
    _check_range(addr, 1, low=0, high=CPU_ADDR_MAX)

    if not is_running(board):
        _drain_stale(board)
        set_run_state(board, running=True)
        if not _await_monitor_prompt(board, banner_timeout):
            print(
                f"warning: core restarted but no monitor prompt appeared "
                f"within {banner_timeout:.1f} s -- sending G anyway",
                file=sys.stderr)

    console.send(board, f"G {addr:04X}\r".encode("ascii"))


def load(board, segments, force=False):
    """Burst-write each (addr, bytes) segment at ram_base + 4*addr, then
    burst-read it back and verify byte-for-byte. Refuses when the core is
    running unless force=True (a running core can be actively using the RAM
    it would clobber).

    Throughput model (real hardware, accepted for now -- see console.py's
    drain() docstring for the full explanation): litex_server's RemoteServer
    hard-codes read_max_length=1 for CommUDP (litex_server.py) and
    comm_udp.py's CommUDP.read() asserts burst == "incr" -- so no matter
    what burst mode this call requests, the *server* answers with exactly
    one 32-bit word per UDP round trip. The write side (board.write, this
    call) still goes out as one real Etherbone burst -- writes aren't
    clamped, only reads are -- but this readback verify is therefore ~1 RTT
    per byte on hardware, same as peek()/poke(). Measured with
    measure_rtt(); a patched litex_server (or a from-scratch CommUDP-alike
    client bypassing it) would be the actual fix, not attempted here."""
    if is_running(board) and not force:
        raise NotStoppedError(
            "core is running -- refusing to load over it "
            "(pass force=True / --force to override)")

    for addr, data in segments:
        _check_range(addr, len(data))
        word_addr = board.ram_base + 4 * addr
        board.write(word_addr, list(data))

        got = _read_words(board, word_addr, len(data))
        for i, (expected, actual) in enumerate(zip(data, got)):
            actual &= 0xFF
            if actual != expected:
                raise VerifyError(offset=addr + i, expected=expected, got=actual)


def peek(board, addr, length=16):
    """Burst-read `length` bytes starting at `addr`. Returns bytes; pair
    with format_hexdump() for display."""
    _check_range(addr, length)
    word_addr = board.ram_base + 4 * addr
    return bytes(b & 0xFF for b in _read_words(board, word_addr, length))


def format_hexdump(addr, data):
    """Canonical hexdump: 4-hex-digit address column, 16 bytes/row (space
    separated), ascii gutter (printable bytes verbatim, '.' otherwise)."""
    lines = []
    for row_start in range(0, len(data), 16):
        row = data[row_start:row_start + 16]
        hex_cols = " ".join(f"{b:02X}" for b in row)
        ascii_col = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in row)
        lines.append(f"{addr + row_start:04X}  {hex_cols:<47}  |{ascii_col}|")
    return "\n".join(lines)


def poke(board, addr, values):
    """Single-word writes (one CSR round trip per byte, no burst), each
    read back and verified before moving to the next byte.

    Throughput model: same ~1 RTT/word ceiling as load()'s verify readback
    -- see that docstring and console.py's drain() docstring. poke() was
    already one word per CSR round trip by design (no burst requested), so
    this doesn't change its shape; it's called out here for completeness."""
    _check_range(addr, len(values))
    for i, value in enumerate(values):
        word_addr = board.ram_base + 4 * (addr + i)
        board.write(word_addr, value)
        got = board.read(word_addr) & 0xFF
        if got != value:
            raise VerifyError(offset=addr + i, expected=value, got=got)


RTT_DEFAULT_READS = 50


def measure_rtt(board, reads=RTT_DEFAULT_READS):
    """Average CSR read round-trip time over `reads` back-to-back reads of
    b8008_status, in seconds. Backs `b8008net status --rtt`.

    Why this matters (see console.py's drain() docstring and load()'s
    verify-path docstring for the full story): litex_server clamps every
    UDP-transport CSR read to exactly one 32-bit word per round trip
    regardless of the burst mode requested, so console drain() and the
    load/poke verify reads are inherently ~1 RTT per byte/word on real
    hardware. This function measures that RTT directly against a live
    board so the hardware stage (Task/stage 1) has a real number instead
    of a guess -- accept-and-document for now, no code workaround exists
    at this layer (see the docstrings above for what an actual fix would
    require)."""
    start = time.monotonic()
    for _ in range(reads):
        board.regs.b8008_status.read()
    elapsed = time.monotonic() - start
    return elapsed / reads
