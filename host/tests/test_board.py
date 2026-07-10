# Task 10 -- Board connection class: discovery hookup, single-instance lock,
# litex_server spawn/skip, identifier sanity check, read/write passthrough.
# No real network I/O: RemoteClient is injected via client_factory, and the
# litex_server liveness/spawn steps are monkeypatched.
import os

import pytest

from b8008net import board as board_mod
from b8008net.board import Board, BoardBusy, acquire_lock

_FIXTURE_CSR = os.path.join(os.path.dirname(__file__), "fixtures", "csr.csv")


class FakeRemoteClient:
    """Stands in for litex.tools.litex_client.RemoteClient."""

    instances = []

    def __init__(self, host, port, csr_csv, identifier="b8008_net 2026-07-09 16:13:33"):
        self.host = host
        self.port = port
        self.csr_csv = csr_csv
        self.opened = False
        self.closed = False
        self._identifier = identifier
        self._mem = {}
        self.bases = type("Bases", (), {"identifier_mem": 0x1000})()
        self.mems = type("Mems", (), {"b8008_ram": type("Ram", (), {"base": 0x90000000})()})()
        self.regs = object()
        self.reads = []
        self.writes = []
        FakeRemoteClient.instances.append(self)
        self._write_identifier()

    def _write_identifier(self):
        base = self.bases.identifier_mem
        for i, ch in enumerate(self._identifier.encode("ascii") + b"\x00"):
            self._mem[base + 4 * i] = ch

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    def read(self, addr, n=1, burst="incr"):
        self.reads.append((addr, n, burst))
        if n == 1:
            return self._mem.get(addr, 0)
        return [self._mem.get(addr + 4 * i, 0) for i in range(n)]

    def write(self, addr, values):
        self.writes.append((addr, values, "burst" if isinstance(values, list) else "single"))
        if isinstance(values, list):
            for i, v in enumerate(values):
                self._mem[addr + 4 * i] = v
        else:
            self._mem[addr] = values


def _factory(identifier="b8008_net 2026-07-09 16:13:33"):
    def make(host, port, csr_csv):
        return FakeRemoteClient(host, port, csr_csv, identifier=identifier)
    return make


@pytest.fixture(autouse=True)
def _reset_fake_client_registry():
    FakeRemoteClient.instances.clear()
    yield
    FakeRemoteClient.instances.clear()


def test_connect_skips_spawn_when_server_already_listening(tmp_path, monkeypatch):
    monkeypatch.setattr(board_mod, "_port_listening", lambda host, port, timeout=0.2: True)
    spawned = []
    monkeypatch.setattr(board_mod, "_spawn_litex_server", lambda host: spawned.append(host))

    b = Board.connect(
        _FIXTURE_CSR, host="192.168.7.50",
        client_factory=_factory(),
        lock_path=str(tmp_path / "b8008net.lock"),
    )
    try:
        assert spawned == []
        assert b.host == "192.168.7.50"
        assert b.client.opened is True
    finally:
        b.close()


def test_connect_spawns_server_when_not_listening(tmp_path, monkeypatch):
    calls = {"listens": 0}

    def fake_listening(host, port, timeout=0.2):
        calls["listens"] += 1
        # Not listening the first check (pre-spawn), listening thereafter.
        return calls["listens"] > 1

    spawned = []

    def fake_spawn(host):
        spawned.append(host)
        return RecordingProc()

    monkeypatch.setattr(board_mod, "_port_listening", fake_listening)
    monkeypatch.setattr(board_mod, "_spawn_litex_server", fake_spawn)

    b = Board.connect(
        _FIXTURE_CSR, host="192.168.7.51",
        client_factory=_factory(),
        lock_path=str(tmp_path / "b8008net.lock"),
    )
    try:
        assert spawned == ["192.168.7.51"]
        assert b._server_proc is not None
    finally:
        b.close()


def test_connect_raises_boardbusy_when_lock_already_held(tmp_path, monkeypatch):
    monkeypatch.setattr(board_mod, "_port_listening", lambda host, port, timeout=0.2: True)
    lock_path = str(tmp_path / "b8008net.lock")
    held = acquire_lock(lock_path)
    try:
        with pytest.raises(BoardBusy):
            Board.connect(_FIXTURE_CSR, host="192.168.7.52",
                           client_factory=_factory(), lock_path=lock_path)
    finally:
        held.release()


def test_connect_discovers_host_when_none_given(tmp_path, monkeypatch):
    # No cache on disk (hermetic cache_path under tmp_path) -- connect()
    # must fall through to discovery.discover() for the initial resolve.
    monkeypatch.setattr(board_mod, "_port_listening", lambda host, port, timeout=0.2: True)
    monkeypatch.setattr(board_mod.discovery, "discover",
                         lambda cache_path=None, use_cache=True: "b8008.lan")

    b = Board.connect(_FIXTURE_CSR, host=None, client_factory=_factory(),
                       lock_path=str(tmp_path / "b8008net.lock"),
                       cache_path=str(tmp_path / "b8008net_host"))
    try:
        assert b.host == "b8008.lan"
    finally:
        b.close()


def test_connect_raises_when_discovery_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(board_mod, "_port_listening", lambda host, port, timeout=0.2: True)
    monkeypatch.setattr(board_mod.discovery, "discover",
                         lambda cache_path=None, use_cache=True: None)

    with pytest.raises(RuntimeError):
        Board.connect(_FIXTURE_CSR, host=None, client_factory=_factory(),
                       lock_path=str(tmp_path / "b8008net.lock"),
                       cache_path=str(tmp_path / "b8008net_host"))


# ── stale-cache invalidation + one retry (review finding) ──────────────────
def test_connect_retries_discovery_once_when_cache_sourced_host_fails(tmp_path, monkeypatch):
    # The board moved (DHCP re-acquisition): the cached IP no longer
    # answers. connect() must drop the stale cache entry and retry
    # discovery once (DNS here) before giving up -- and it must succeed
    # against the freshly discovered host.
    monkeypatch.setattr(board_mod, "_port_listening", lambda host, port, timeout=0.2: True)

    cache_path = tmp_path / "b8008net_host"
    board_mod.discovery.save_cache(str(cache_path), "192.168.7.200")  # stale

    clear_calls = []
    real_clear_cache = board_mod.discovery.clear_cache

    def spying_clear_cache(path=board_mod.discovery.DEFAULT_CACHE_PATH):
        clear_calls.append(path)
        return real_clear_cache(path)

    monkeypatch.setattr(board_mod.discovery, "clear_cache", spying_clear_cache)
    monkeypatch.setattr(board_mod.discovery, "resolve_dns",
                         lambda names=board_mod.discovery.DNS_NAMES: "192.168.7.201")

    attempts = {"n": 0}

    class FlakyThenGoodClient(FakeRemoteClient):
        def open(self):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionRefusedError("stale board is gone")
            super().open()

    b = Board.connect(
        _FIXTURE_CSR, host=None,
        client_factory=lambda host, port, csr_csv: FlakyThenGoodClient(host, port, csr_csv),
        lock_path=str(tmp_path / "b8008net.lock"),
        cache_path=str(cache_path),
    )
    try:
        assert attempts["n"] == 2  # one failed attempt, one that succeeded
        assert b.host == "192.168.7.201"
        assert clear_calls == [str(cache_path)]
        # Cache now holds the freshly discovered host, not the stale one.
        assert board_mod.discovery.load_cache(str(cache_path)) == "192.168.7.201"
    finally:
        b.close()


def test_connect_cache_retry_failure_still_raises_and_cache_stays_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(board_mod, "_port_listening", lambda host, port, timeout=0.2: True)

    cache_path = tmp_path / "b8008net_host"
    board_mod.discovery.save_cache(str(cache_path), "192.168.7.200")

    # Rediscovery finds nothing either (DNS and sweep both empty).
    monkeypatch.setattr(board_mod.discovery, "resolve_dns",
                         lambda names=board_mod.discovery.DNS_NAMES: None)
    monkeypatch.setattr(board_mod.discovery, "local_ipv4_and_netmask",
                         lambda: ("10.0.0.5", "255.255.255.0"))
    monkeypatch.setattr(board_mod.discovery, "probe_sweep", lambda candidates, **kw: None)

    class AlwaysFailsClient(FakeRemoteClient):
        def open(self):
            raise ConnectionRefusedError("board unreachable")

    lock_path = str(tmp_path / "b8008net.lock")
    with pytest.raises(ConnectionRefusedError):
        Board.connect(
            _FIXTURE_CSR, host=None,
            client_factory=lambda host, port, csr_csv: AlwaysFailsClient(host, port, csr_csv),
            lock_path=lock_path,
            cache_path=str(cache_path),
        )

    # Stale entry was dropped and nothing valid was found to replace it --
    # the cache stays empty rather than reverting to the known-dead host.
    assert board_mod.discovery.load_cache(str(cache_path)) is None

    # Lock was released despite the failure.
    reacquired = acquire_lock(lock_path)
    reacquired.release()


def test_connect_with_explicit_host_never_retries_discovery_on_failure(tmp_path, monkeypatch):
    # An explicit --host is never second-guessed: a connect failure there
    # is just a failure, not a cue to go rediscover.
    monkeypatch.setattr(board_mod, "_port_listening", lambda host, port, timeout=0.2: True)

    discover_calls = []
    monkeypatch.setattr(
        board_mod.discovery, "discover",
        lambda cache_path=None, use_cache=True: discover_calls.append(1))

    class AlwaysFailsClient(FakeRemoteClient):
        def open(self):
            raise ConnectionRefusedError("board unreachable")

    lock_path = str(tmp_path / "b8008net.lock")
    with pytest.raises(ConnectionRefusedError):
        Board.connect(
            _FIXTURE_CSR, host="192.168.7.222",
            client_factory=lambda host, port, csr_csv: AlwaysFailsClient(host, port, csr_csv),
            lock_path=lock_path,
        )

    assert discover_calls == []  # discovery never consulted for an explicit host

    reacquired = acquire_lock(lock_path)
    reacquired.release()


def test_connect_boardbusy_is_never_retried_even_when_cache_sourced(tmp_path, monkeypatch):
    # Lock contention isn't a stale-cache symptom -- retrying discovery
    # wouldn't help and would just mask the real error.
    monkeypatch.setattr(board_mod, "_port_listening", lambda host, port, timeout=0.2: True)

    cache_path = tmp_path / "b8008net_host"
    board_mod.discovery.save_cache(str(cache_path), "192.168.7.200")

    discover_calls = []
    monkeypatch.setattr(
        board_mod.discovery, "discover",
        lambda cache_path=None, use_cache=True: discover_calls.append(1))

    lock_path = str(tmp_path / "b8008net.lock")
    held = acquire_lock(lock_path)
    try:
        with pytest.raises(BoardBusy):
            Board.connect(_FIXTURE_CSR, host=None, client_factory=_factory(),
                           lock_path=lock_path, cache_path=str(cache_path))
    finally:
        held.release()

    assert discover_calls == []
    # Cache is untouched -- BoardBusy isn't a "the board moved" signal.
    assert board_mod.discovery.load_cache(str(cache_path)) == "192.168.7.200"


# ── post-open failure (identifier read) must hit the same cleanup path ──────
def test_connect_identifier_read_failure_releases_lock_and_reaps_server(tmp_path, monkeypatch):
    """Review finding: the post-connect identifier sanity check used to sit
    OUTSIDE the try whose except runs _terminate_server + lock.release. In
    the litex_server bind-before-probe race (RemoteServer.open() binds its
    TCP listen port before probing the board), client.open() can succeed
    against a dying server and the failure then surfaces on the very next
    read -- the identifier read. That must not leak the flock or orphan the
    spawned server."""
    listen_state = {"up": False}
    monkeypatch.setattr(board_mod, "_port_listening",
                         lambda host, port, timeout=0.2: listen_state["up"])

    proc = RecordingProc()

    def fake_spawn(host):
        listen_state["up"] = True
        return proc

    monkeypatch.setattr(board_mod, "_spawn_litex_server", fake_spawn)

    class OpensThenDiesClient(FakeRemoteClient):
        """open() succeeds, but every subsequent board read raises -- the
        dying-litex_server shape."""

        def read(self, addr, n=1, burst="incr"):
            raise ConnectionResetError(
                "litex_server died after accept (bind-before-probe race)")

    lock_path = str(tmp_path / "b8008net.lock")
    with pytest.raises(ConnectionResetError):
        Board.connect(
            _FIXTURE_CSR, host="192.168.7.70",
            client_factory=lambda host, port, csr_csv:
                OpensThenDiesClient(host, port, csr_csv),
            lock_path=lock_path,
        )

    # The spawned server was terminated AND reaped, not orphaned.
    assert "terminate" in proc.calls
    assert any(isinstance(c, tuple) and c[0] == "wait" for c in proc.calls)

    # And the lock was released -- a fresh acquire must succeed, NOT BoardBusy.
    reacquired = acquire_lock(lock_path)
    reacquired.release()


def test_connect_cache_retry_not_blocked_by_own_lock_after_identifier_failure(tmp_path, monkeypatch):
    """The compounding failure the leak caused: a cache-sourced host whose
    identifier read fails leaks the lock, and the stale-cache rediscovery
    retry then trips over OUR OWN leaked flock -- reporting a spurious
    BoardBusy instead of retrying. With the fix, the first attempt cleans
    up after itself and the retry proceeds normally."""
    monkeypatch.setattr(board_mod, "_port_listening",
                         lambda host, port, timeout=0.2: True)

    cache_path = tmp_path / "b8008net_host"
    board_mod.discovery.save_cache(str(cache_path), "192.168.7.200")  # stale

    monkeypatch.setattr(board_mod.discovery, "resolve_dns",
                         lambda names=board_mod.discovery.DNS_NAMES: "192.168.7.201")

    created = {"n": 0}

    def factory(host, port, csr_csv):
        created["n"] += 1
        client = FakeRemoteClient(host, port, csr_csv)
        if created["n"] == 1:
            # First attempt (cache-sourced host): open() succeeds, but the
            # identifier read blows up.
            def dead_read(addr, n=1, burst="incr"):
                raise ConnectionResetError("stale board is gone")
            client.read = dead_read
        return client

    b = Board.connect(
        _FIXTURE_CSR, host=None, client_factory=factory,
        lock_path=str(tmp_path / "b8008net.lock"),
        cache_path=str(cache_path),
    )
    try:
        assert created["n"] == 2  # failed attempt + successful retry
        assert b.host == "192.168.7.201"
        assert board_mod.discovery.load_cache(str(cache_path)) == "192.168.7.201"
    finally:
        b.close()


def test_connect_warns_on_identifier_mismatch(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(board_mod, "_port_listening", lambda host, port, timeout=0.2: True)

    b = Board.connect(
        _FIXTURE_CSR, host="192.168.7.53",
        client_factory=_factory(identifier="b8008_net 2020-01-01 00:00:00"),
        lock_path=str(tmp_path / "b8008net.lock"),
    )
    try:
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "identifier" in err.lower()
    finally:
        b.close()


def test_connect_no_warning_when_identifier_matches(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(board_mod, "_port_listening", lambda host, port, timeout=0.2: True)

    b = Board.connect(
        _FIXTURE_CSR, host="192.168.7.54",
        client_factory=_factory(identifier="b8008_net 2026-07-09 16:13:33"),
        lock_path=str(tmp_path / "b8008net.lock"),
    )
    try:
        err = capsys.readouterr().err
        assert "WARNING" not in err
    finally:
        b.close()


def test_read_write_and_ram_base_delegate_to_client(tmp_path, monkeypatch):
    monkeypatch.setattr(board_mod, "_port_listening", lambda host, port, timeout=0.2: True)

    b = Board.connect(_FIXTURE_CSR, host="192.168.7.55", client_factory=_factory(),
                       lock_path=str(tmp_path / "b8008net.lock"))
    try:
        assert b.ram_base == 0x90000000

        b.read(0x90000000, n=4, burst="fixed")
        assert b.client.reads[-1] == (0x90000000, 4, "fixed")

        b.write(0x90000000, [1, 2, 3])
        assert b.client.writes[-1][0] == 0x90000000
        assert b.client.writes[-1][1] == [1, 2, 3]

        assert b.regs is b.client.regs
    finally:
        b.close()


class RecordingProc:
    """Fake subprocess.Popen recording the terminate/wait/kill sequence."""

    def __init__(self, wait_times_out=False):
        self.calls = []
        self.wait_times_out = wait_times_out

    def terminate(self):
        self.calls.append("terminate")

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if self.wait_times_out and timeout is not None:
            import subprocess
            self.wait_times_out = False  # second wait (post-kill) succeeds
            raise subprocess.TimeoutExpired(cmd="litex_server", timeout=timeout)
        return 0

    def kill(self):
        self.calls.append("kill")


def test_close_releases_lock_and_terminates_server(tmp_path, monkeypatch):
    listen_state = {"up": False}
    monkeypatch.setattr(board_mod, "_port_listening",
                         lambda host, port, timeout=0.2: listen_state["up"])

    proc = RecordingProc()

    def fake_spawn(host):
        listen_state["up"] = True
        return proc

    monkeypatch.setattr(board_mod, "_spawn_litex_server", fake_spawn)

    lock_path = str(tmp_path / "b8008net.lock")
    b = Board.connect(_FIXTURE_CSR, host="192.168.7.56",
                       client_factory=_factory(), lock_path=lock_path)
    assert b.client.closed is False

    b.close()
    assert b.client.closed is True
    # close() must terminate AND reap (wait) the child, with a bounded wait.
    assert proc.calls[0] == "terminate"
    assert any(c[0] == "wait" and c[1] is not None
               for c in proc.calls if isinstance(c, tuple))
    assert "kill" not in proc.calls  # wait succeeded, no escalation

    # Lock was released by close(); a fresh acquire must now succeed.
    reacquired = acquire_lock(lock_path)
    reacquired.release()


def test_close_kills_server_when_wait_times_out(tmp_path, monkeypatch):
    listen_state = {"up": False}
    monkeypatch.setattr(board_mod, "_port_listening",
                         lambda host, port, timeout=0.2: listen_state["up"])

    proc = RecordingProc(wait_times_out=True)

    def fake_spawn(host):
        listen_state["up"] = True
        return proc

    monkeypatch.setattr(board_mod, "_spawn_litex_server", fake_spawn)

    b = Board.connect(_FIXTURE_CSR, host="192.168.7.57",
                       client_factory=_factory(),
                       lock_path=str(tmp_path / "b8008net.lock"))
    b.close()

    # terminate -> wait(timeout) raised TimeoutExpired -> kill -> wait (reap).
    assert proc.calls[0] == "terminate"
    assert "kill" in proc.calls
    assert proc.calls.index("kill") > 0
    # After kill, the child is still reaped with a final wait.
    kill_idx = proc.calls.index("kill")
    assert any(isinstance(c, tuple) and c[0] == "wait"
               for c in proc.calls[kill_idx + 1:])


def test_connect_terminates_spawned_server_when_client_open_fails(tmp_path, monkeypatch):
    """IMPORTANT (review): if connect spawned litex_server and a later step
    (client_factory / client.open) raises, the spawned server must be
    terminated AND reaped before re-raising -- not leaked as an orphan."""
    listen_state = {"up": False}
    monkeypatch.setattr(board_mod, "_port_listening",
                         lambda host, port, timeout=0.2: listen_state["up"])

    proc = RecordingProc()

    def fake_spawn(host):
        listen_state["up"] = True
        return proc

    monkeypatch.setattr(board_mod, "_spawn_litex_server", fake_spawn)

    class ExplodingClient(FakeRemoteClient):
        def open(self):
            raise ConnectionRefusedError("simulated open failure")

    lock_path = str(tmp_path / "b8008net.lock")
    with pytest.raises(ConnectionRefusedError):
        Board.connect(
            _FIXTURE_CSR, host="192.168.7.58",
            client_factory=lambda host, port, csr_csv:
                ExplodingClient(host, port, csr_csv),
            lock_path=lock_path,
        )

    # The spawned server was cleaned up: terminated and reaped.
    assert "terminate" in proc.calls
    assert any(isinstance(c, tuple) and c[0] == "wait" for c in proc.calls)

    # And the lock was released despite the failure.
    reacquired = acquire_lock(lock_path)
    reacquired.release()


def test_connect_terminates_spawned_server_when_factory_raises(tmp_path, monkeypatch):
    """Same leak, earlier failure point: client_factory itself raises
    (e.g. a bad csr.csv path blows up in CSRBuilder)."""
    listen_state = {"up": False}
    monkeypatch.setattr(board_mod, "_port_listening",
                         lambda host, port, timeout=0.2: listen_state["up"])

    proc = RecordingProc()

    def fake_spawn(host):
        listen_state["up"] = True
        return proc

    monkeypatch.setattr(board_mod, "_spawn_litex_server", fake_spawn)

    def exploding_factory(host, port, csr_csv):
        raise FileNotFoundError("simulated bad csr.csv")

    lock_path = str(tmp_path / "b8008net.lock")
    with pytest.raises(FileNotFoundError):
        Board.connect(_FIXTURE_CSR, host="192.168.7.59",
                       client_factory=exploding_factory, lock_path=lock_path)

    assert "terminate" in proc.calls
    assert any(isinstance(c, tuple) and c[0] == "wait" for c in proc.calls)

    reacquired = acquire_lock(lock_path)
    reacquired.release()


def test_read_identifier_none_without_identifier_mem():
    class NoIdentClient:
        bases = type("Bases", (), {})()

    assert board_mod.read_identifier(NoIdentClient()) is None


def test_board_identifier_empty_string_without_identifier_mem(tmp_path, monkeypatch):
    monkeypatch.setattr(board_mod, "_port_listening", lambda host, port, timeout=0.2: True)

    def factory(host, port, csr_csv):
        c = FakeRemoteClient(host, port, csr_csv)
        c.bases = type("Bases", (), {})()  # no identifier_mem attribute
        return c

    b = Board.connect(_FIXTURE_CSR, host="192.168.7.61", client_factory=factory,
                       lock_path=str(tmp_path / "b8008net.lock"))
    try:
        assert b.identifier() == ""
    finally:
        b.close()


def test_connect_startup_timeout_cleans_up_once(tmp_path, monkeypatch):
    """Server never comes up: RuntimeError, and the spawned proc is
    terminated exactly once (no double-cleanup from nested handlers)."""
    monkeypatch.setattr(board_mod, "_port_listening",
                         lambda host, port, timeout=0.2: False)
    monkeypatch.setattr(board_mod, "SERVER_STARTUP_TIMEOUT_S", 0.05)

    proc = RecordingProc()
    monkeypatch.setattr(board_mod, "_spawn_litex_server", lambda host: proc)

    lock_path = str(tmp_path / "b8008net.lock")
    with pytest.raises(RuntimeError):
        Board.connect(_FIXTURE_CSR, host="192.168.7.60",
                       client_factory=_factory(), lock_path=lock_path)

    assert proc.calls.count("terminate") == 1
    assert any(isinstance(c, tuple) and c[0] == "wait" for c in proc.calls)

    reacquired = acquire_lock(lock_path)
    reacquired.release()
