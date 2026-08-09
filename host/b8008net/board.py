# board.py -- single-instance lockfile, litex_server lifecycle, and a thin
# Board wrapper around litex's RemoteClient.
import csv
import fcntl
import os
import socket
import subprocess
import sys
import time

from . import discovery

DEFAULT_LOCK_PATH = os.path.expanduser("~/.b8008net.lock")
LITEX_SERVER_HOST = "localhost"
LITEX_SERVER_PORT = 1234
SERVER_STARTUP_TIMEOUT_S = 5.0
SERVER_SHUTDOWN_TIMEOUT_S = 3.0


class BoardBusy(Exception):
    """Raised when another b8008net instance already holds the lockfile."""


class Lock:
    """A held single-instance lock. Release explicitly or use as a context
    manager; acquire() is what actually takes the lock (see acquire_lock)."""

    def __init__(self, path):
        self.path = path
        self._fh = None

    def acquire(self):
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise BoardBusy(f"another b8008net instance holds {self.path}")
        return self

    def release(self):
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


def acquire_lock(path=DEFAULT_LOCK_PATH):
    """Take the single-instance lock at `path`. Raises BoardBusy if another
    process (or an earlier acquire() in this one) already holds it."""
    return Lock(path).acquire()


def _port_listening(host, port, timeout=0.2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_config_identifier(csr_csv):
    """Pull the `config_identifier` constant out of csr.csv -- the
    identifier string the SoC was built with, for comparison against what
    the live board reports over Etherbone."""
    with open(csr_csv, encoding="utf-8") as f:
        for row in csv.reader(filter(lambda r: r and r[0] != "#", f)):
            if len(row) >= 3 and row[0] == "constant" and row[1] == "config_identifier":
                return row[2]
    return None


def _spawn_litex_server(host):
    # b8008net's own bridge, not stock `litex_server --udp <host>`: requests
    # go out as LAN broadcast (see eb_server.py for why -- mesh WiFi drops
    # client->board unicast), so the board's address is not needed here.
    del host
    return subprocess.Popen(
        [sys.executable, "-m", "b8008net.eb_server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _terminate_server(proc):
    """Terminate a spawned litex_server and reap it: SIGTERM, bounded wait,
    SIGKILL escalation if it won't die, then a final wait so the child is
    never left as a zombie/orphan."""
    proc.terminate()
    try:
        proc.wait(timeout=SERVER_SHUTDOWN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def read_identifier(client):
    """Best-effort read of the SoC identifier string from identifier_mem
    (one ASCII char per 32-bit CSR word, NUL-terminated). Returns None when
    the CSR map has no identifier_mem. Shared by Board.identifier() and
    host_selftest.get_identifier()."""
    try:
        base = client.bases.identifier_mem
    except AttributeError:
        return None
    chars = []
    for i in range(256):
        c = client.read(base + 4 * i) & 0xFF
        if c == 0:
            break
        chars.append(chr(c))
    return "".join(chars)


class Board:
    """Thin wrapper around a litex RemoteClient: discovery + lockfile +
    litex_server lifecycle live here so callers just get a connected board."""

    def __init__(self, client, host, csr_csv, lock=None, server_proc=None):
        self.client = client
        self.host = host
        self.csr_csv = csr_csv
        self._lock = lock
        self._server_proc = server_proc

    @property
    def regs(self):
        return self.client.regs

    def read(self, addr, n=1, burst="incr"):
        return self.client.read(addr, n, burst=burst)

    def write(self, addr, values):
        return self.client.write(addr, values)

    def identifier(self):
        """Best-effort read of the SoC identifier string (identifier_mem)."""
        return read_identifier(self.client) or ""

    def close(self):
        self.client.close()
        if self._server_proc is not None:
            _terminate_server(self._server_proc)
            self._server_proc = None
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @classmethod
    def connect(cls, csr_csv, host=None, client_factory=None,
                lock_path=DEFAULT_LOCK_PATH, server_host=LITEX_SERVER_HOST,
                server_port=LITEX_SERVER_PORT, cache_path=discovery.DEFAULT_CACHE_PATH,
                use_cache=True):
        """Connect to the board: discover a host if none given, take the
        single-instance lock, spawn litex_server if nothing is already
        listening on server_port, open a RemoteClient, and sanity-check the
        SoC identifier against csr.csv (loud warning on mismatch, never
        fatal -- a stale csr.csv shouldn't block a status check).

        Stale-cache handling: if the host came from the discovery cache
        (not an explicit `host=` argument, and not freshly discovered via
        DNS/sweep) and the connect attempt below fails, the cached IP is
        assumed stale -- most likely the board moved (DHCP re-acquisition)
        and something else now answers, or nothing does. The cache entry is
        dropped and discovery is retried exactly once, bypassing the cache
        this time (DNS, then a subnet probe sweep), before giving up for
        good. An explicit `host=` is never second-guessed this way: if the
        caller said where the board is, a failure there is just a failure.
        BoardBusy (another instance already holds the lock) is not
        cache-related and is never retried."""
        if client_factory is None:
            from litex.tools.litex_client import RemoteClient
            client_factory = RemoteClient

        from_cache = False
        if host is None:
            cached = discovery.load_cache(cache_path) if use_cache else None
            if cached:
                host, from_cache = cached, True
            else:
                host = discovery.discover(cache_path=cache_path, use_cache=False)
            if host is None:
                raise RuntimeError(
                    "could not discover board (no cache, no DNS, no probe response)"
                )

        try:
            return cls._connect_to(csr_csv, host, client_factory, lock_path,
                                    server_host, server_port)
        except BoardBusy:
            raise
        except Exception:
            if not from_cache:
                raise
            discovery.clear_cache(cache_path)
            retried_host = discovery.discover(cache_path=cache_path, use_cache=False)
            if retried_host is None:
                raise
            return cls._connect_to(csr_csv, retried_host, client_factory,
                                    lock_path, server_host, server_port)

    @classmethod
    def _connect_to(cls, csr_csv, host, client_factory, lock_path, server_host, server_port):
        """The actual connect attempt against a specific `host`: lock, spawn
        litex_server if needed, open the client, identifier sanity-check.
        Split out from connect() so the stale-cache retry can call it a
        second time against a freshly discovered host without duplicating
        this logic."""
        lock = acquire_lock(lock_path)

        server_proc = None
        client = None
        try:
            if not _port_listening(server_host, server_port):
                server_proc = _spawn_litex_server(host)
                deadline = time.time() + SERVER_STARTUP_TIMEOUT_S
                while time.time() < deadline:
                    if _port_listening(server_host, server_port):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError(
                        f"litex_server did not come up on {server_host}:{server_port}"
                    )

            client = client_factory(host=server_host, port=server_port, csr_csv=csr_csv)
            client.open()

            board = cls(client, host, csr_csv, lock=lock, server_proc=server_proc)

            # The identifier sanity check talks to the board and so belongs
            # INSIDE this protected block: litex_server's RemoteServer.open()
            # binds its TCP listen port *before* probing the board, so
            # client.open() can succeed against a server that's about to die
            # from a failed board probe -- the failure then surfaces here, on
            # the first real read. If this sat after the except (as it once
            # did), that failure would leak the flock and orphan the spawned
            # server, and the stale-cache retry in connect() would then trip
            # over our own leaked lock and report a spurious BoardBusy.
            expected = _read_config_identifier(csr_csv)
            actual = board.identifier()
            if expected is not None and actual and actual != expected:
                print(
                    f"WARNING: board identifier {actual!r} does not match "
                    f"csr.csv identifier {expected!r} -- board may be running "
                    f"different firmware than this csr.csv describes.",
                    file=sys.stderr,
                )
        except Exception:
            # Single cleanup point for every failure past the lock: close the
            # client if it got opened, terminate and reap (not orphan) any
            # litex_server THIS connect spawned, and release the lock, before
            # re-raise.
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            if server_proc is not None:
                _terminate_server(server_proc)
            lock.release()
            raise

        return board
