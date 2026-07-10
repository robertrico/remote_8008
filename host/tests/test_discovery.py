# Task 10 -- zero-config board discovery: probe packet, subnet sweep
# candidates, last-known-host cache, single-instance lockfile.
import socket
import threading

import pytest

from litex.tools.remote.etherbone import EtherbonePacket

from b8008net import discovery
from b8008net.discovery import (
    probe_packet,
    subnet_candidates,
    save_cache,
    load_cache,
    clear_cache,
    discover,
    probe_sweep,
)
from b8008net.board import acquire_lock, BoardBusy


def test_sweep_builds_probe():
    raw = probe_packet()
    assert isinstance(raw, (bytes, bytearray))
    # Etherbone magic 0x4e6f, big-endian, is the first two bytes of every
    # packet (including bare probes with no records).
    assert raw[0:2] == b"\x4e\x6f"

    # Round-trip through the library's own parser and confirm the probe
    # flag made it through.
    pkt = EtherbonePacket(init=raw)
    pkt.decode()
    assert pkt.magic == 0x4E6F
    assert pkt.pf == 1


def test_sweep_candidates():
    candidates = subnet_candidates("192.168.7.23", "255.255.255.0")
    assert len(candidates) == 253
    assert "192.168.7.23" not in candidates   # self
    assert "192.168.7.0" not in candidates    # network address
    assert "192.168.7.255" not in candidates  # broadcast address
    assert "192.168.7.1" in candidates
    assert "192.168.7.254" in candidates


def test_cache_roundtrip(tmp_path):
    cache_path = tmp_path / "b8008net_host"
    assert load_cache(str(cache_path)) is None

    save_cache(str(cache_path), "192.168.7.42")
    assert load_cache(str(cache_path)) == "192.168.7.42"

    # Overwriting the cache replaces, not appends.
    save_cache(str(cache_path), "b8008.lan")
    assert load_cache(str(cache_path)) == "b8008.lan"


def _encoded_probe_reply():
    """A raw Etherbone probe-reply packet: magic 0x4e6f, pr=1, no records --
    what real Etherbone-speaking gateware sends back for a probe."""
    pkt = EtherbonePacket()
    pkt.pr = 1
    pkt.encode()
    return bytes(pkt.bytes)


def test_probe_sweep_first_responder_wins():
    # A real (127.0.0.1-only) UDP responder exercises the actual select()
    # loop and packet round-trip, not just the packet bytes -- the brief
    # explicitly allows this as long as it never leaves localhost.
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0))
    port = responder.getsockname()[1]

    def respond_once():
        responder.settimeout(2.0)
        try:
            data, addr = responder.recvfrom(2048)
            assert data == probe_packet()
            responder.sendto(_encoded_probe_reply(), addr)
        except socket.timeout:
            pass

    t = threading.Thread(target=respond_once, daemon=True)
    t.start()
    try:
        found = probe_sweep(["127.0.0.1"], port=port, timeout=1.0)
        assert found == "127.0.0.1"
    finally:
        t.join(timeout=2.0)
        responder.close()


def test_probe_sweep_no_responder_returns_none():
    silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    silent.bind(("127.0.0.1", 0))
    port = silent.getsockname()[1]
    try:
        found = probe_sweep(["127.0.0.1"], port=port, timeout=0.1)
        assert found is None
    finally:
        silent.close()


# ── probe_sweep() validates the response is really an Etherbone probe
# reply (review finding: any UDP datagram used to be accepted as "the
# board") ───────────────────────────────────────────────────────────────
def test_probe_sweep_ignores_garbage_then_accepts_valid_probe_reply():
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0))
    port = responder.getsockname()[1]

    def respond():
        responder.settimeout(2.0)
        try:
            data, addr = responder.recvfrom(2048)
            assert data == probe_packet()
            # Garbage first -- must be ignored, not mistaken for the board.
            responder.sendto(b"not-an-etherbone-packet-at-all", addr)
            # Then a real probe reply -- this one must win.
            responder.sendto(_encoded_probe_reply(), addr)
        except socket.timeout:
            pass

    t = threading.Thread(target=respond, daemon=True)
    t.start()
    try:
        found = probe_sweep(["127.0.0.1"], port=port, timeout=1.0)
        assert found == "127.0.0.1"
    finally:
        t.join(timeout=2.0)
        responder.close()


def test_probe_sweep_garbage_only_responder_is_ignored_returns_none():
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0))
    port = responder.getsockname()[1]

    def respond():
        responder.settimeout(2.0)
        try:
            data, addr = responder.recvfrom(2048)
            responder.sendto(b"still-not-etherbone", addr)
        except socket.timeout:
            pass

    t = threading.Thread(target=respond, daemon=True)
    t.start()
    try:
        found = probe_sweep(["127.0.0.1"], port=port, timeout=0.3)
        assert found is None
    finally:
        t.join(timeout=2.0)
        responder.close()


# ── clear_cache() / discover(use_cache=False) ───────────────────────────
def test_clear_cache_removes_cached_file(tmp_path):
    cache_path = tmp_path / "b8008net_host"
    save_cache(str(cache_path), "192.168.7.42")
    assert load_cache(str(cache_path)) == "192.168.7.42"

    clear_cache(str(cache_path))
    assert load_cache(str(cache_path)) is None


def test_clear_cache_is_a_noop_when_no_cache_exists(tmp_path):
    cache_path = tmp_path / "b8008net_host"
    clear_cache(str(cache_path))  # must not raise
    assert load_cache(str(cache_path)) is None


def test_discover_use_cache_false_skips_cached_host(tmp_path, monkeypatch):
    cache_path = tmp_path / "b8008net_host"
    save_cache(str(cache_path), "192.168.7.99")  # stale cache present

    # With use_cache=False, discover() must not return the cached host --
    # it should fall straight through to DNS (mocked here to a known value).
    monkeypatch.setattr(discovery, "resolve_dns", lambda names=discovery.DNS_NAMES: "192.168.7.7")

    found = discover(cache_path=str(cache_path), use_cache=False)
    assert found == "192.168.7.7"

    # The cache is still updated afterward with the freshly discovered host
    # (use_cache only skips the *read*, not the write-back).
    assert load_cache(str(cache_path)) == "192.168.7.7"


def test_discover_use_cache_true_returns_cached_host_without_dns(tmp_path, monkeypatch):
    cache_path = tmp_path / "b8008net_host"
    save_cache(str(cache_path), "192.168.7.99")

    def _boom(names=discovery.DNS_NAMES):
        raise AssertionError("resolve_dns should not be called when the cache hits")

    monkeypatch.setattr(discovery, "resolve_dns", _boom)

    found = discover(cache_path=str(cache_path), use_cache=True)
    assert found == "192.168.7.99"


def test_lock_excludes(tmp_path):
    lock_path = tmp_path / "b8008net.lock"

    held = acquire_lock(str(lock_path))
    try:
        with pytest.raises(BoardBusy):
            acquire_lock(str(lock_path))
    finally:
        held.release()

    # Once released, a fresh acquire succeeds again.
    reacquired = acquire_lock(str(lock_path))
    reacquired.release()
