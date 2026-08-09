# discovery.py -- zero-config board discovery for b8008net.
#
# Three strategies, tried in order by discover():
#   1. Cache: last-known host, saved at ~/.b8008net_host.
#   2. DNS: plain hostname "b8008", then the search-domain form "b8008.lan"
#      (the board runs no mDNS responder, so "b8008.local" is not tried).
#   3. Probe sweep: send an Etherbone probe packet (magic + probe flag, no
#      records) to every host on the local /24 and take the first responder.
#      The board's gateware answers Etherbone probes without any CPU/firmware
#      involvement.
import os
import select
import socket
import struct
import time

from litex.tools.remote.etherbone import EtherbonePacket, etherbone_magic

DEFAULT_CACHE_PATH = os.path.expanduser("~/.b8008net_host")
ETHERBONE_PORT = 1234
DNS_NAMES = ("b8008", "b8008.lan")
SWEEP_TIMEOUT_S = 0.5


def probe_packet():
    """Build a raw Etherbone probe packet: magic 0x4e6f, probe flag set,
    no records. The board's gateware answers this without CPU help."""
    pkt = EtherbonePacket()
    pkt.pf = 1
    pkt.encode()
    return bytes(pkt.bytes)


def _ip_to_int(ip):
    return struct.unpack("!I", socket.inet_aton(ip))[0]


def _int_to_ip(n):
    return socket.inet_ntoa(struct.pack("!I", n & 0xFFFFFFFF))


def subnet_candidates(ip, netmask):
    """All host addresses on ip's /netmask subnet, excluding ip itself, the
    network address, and the broadcast address."""
    ip_i = _ip_to_int(ip)
    mask_i = _ip_to_int(netmask)
    network = ip_i & mask_i
    broadcast = network | (~mask_i & 0xFFFFFFFF)
    return [
        _int_to_ip(host)
        for host in range(network + 1, broadcast)
        if host != ip_i
    ]


def save_cache(path, host):
    """Persist the last-known board host/IP. Overwrites, does not append."""
    with open(path, "w") as f:
        f.write(host.strip() + "\n")


def load_cache(path):
    """Return the cached host, or None if there is no cache yet."""
    try:
        with open(path) as f:
            host = f.read().strip()
    except FileNotFoundError:
        return None
    return host or None


def clear_cache(path=DEFAULT_CACHE_PATH):
    """Drop the cached host, if any. Called when a cache-sourced host fails
    to connect -- the board most likely moved (DHCP re-acquisition) and the
    cached IP is now stale/dead; the next discover() call should not hand
    the same bad address straight back out."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def resolve_dns(names=DNS_NAMES):
    """Try each hostname in order; return the first that resolves."""
    for name in names:
        try:
            return socket.gethostbyname(name)
        except OSError:
            continue
    return None


def _is_probe_reply(data):
    """True if `data` parses as an Etherbone packet with the probe-reply
    flag set (magic 0x4e6f, pr=1) -- the same check CommUDP.probe() makes
    of its own response in comm_udp.py. Any UDP socket on the sweep port
    would otherwise be accepted as "the board" (a stray broadcast reply, a
    completely unrelated service, garbage) -- this validates the datagram
    actually came from Etherbone-speaking gateware before trusting it."""
    try:
        pkt = EtherbonePacket(init=data)
        pkt.decode()
    except Exception:
        return False
    return pkt.magic == etherbone_magic and pkt.pr == 1


def probe_broadcast(port=ETHERBONE_PORT, timeout=SWEEP_TIMEOUT_S, sock=None):
    """Single broadcast Etherbone probe: one datagram to 255.255.255.255,
    first valid probe-reply wins. Both faster than the unicast sweep and the
    only reliable client->board direction on mesh WiFi that drops or NATs
    client->wired unicast (the board replies unicast, which is reliable).
    Returns the responder's address, or None."""
    packet = probe_packet()
    owns_sock = sock is None
    if sock is None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        # Subnet-directed broadcast reaches through mesh WiFi bridges that
        # drop the limited (255.255.255.255) form; send both regardless.
        targets = ["255.255.255.255"]
        try:
            ip, netmask = local_ipv4_and_netmask()
            subnet_bcast = _int_to_ip(_ip_to_int(ip) | (~_ip_to_int(netmask) & 0xFFFFFFFF))
            targets.insert(0, subnet_bcast)
        except OSError:
            pass
        sent = 0
        for target in targets:
            try:
                sock.sendto(packet, (target, port))
                sent += 1
            except OSError:
                continue
        if not sent:
            return None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _, _ = select.select([sock], [], [], remaining)
            if not readable:
                return None
            data, addr = sock.recvfrom(2048)
            if _is_probe_reply(data):
                return addr[0]
    finally:
        if owns_sock:
            sock.close()


def probe_sweep(candidates, port=ETHERBONE_PORT, timeout=SWEEP_TIMEOUT_S, sock=None):
    """Send an Etherbone probe to every candidate, then wait up to `timeout`
    seconds total for a datagram that actually parses as an Etherbone
    probe-reply (_is_probe_reply). Non-parsing datagrams are ignored and the
    remainder of the window keeps listening -- accepting the first UDP
    packet to arrive, sight unseen, would let any noise on the port
    masquerade as the board. Returns the responder's address, or None if the
    window closes without a valid reply.

    `sock` is injectable (a bound UDP socket) so tests can exercise the
    select loop against a localhost echo responder without touching the LAN.
    """
    packet = probe_packet()
    owns_sock = sock is None
    if sock is None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for candidate in candidates:
            try:
                sock.sendto(packet, (candidate, port))
            except OSError:
                continue

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _, _ = select.select([sock], [], [], remaining)
            if not readable:
                return None
            data, addr = sock.recvfrom(2048)
            if _is_probe_reply(data):
                return addr[0]
            # Doesn't parse as an Etherbone probe reply -- ignore and keep
            # listening for the rest of the window.
    finally:
        if owns_sock:
            sock.close()


def local_ipv4_and_netmask():
    """Best-effort local IPv4 address + netmask. Assumes a /24, which is
    the common case on the small LANs this board targets; no packets are
    sent (UDP connect() only resolves a route, it doesn't transmit)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ip, "255.255.255.0"


def discover(cache_path=DEFAULT_CACHE_PATH, use_cache=True):
    """Zero-config board discovery: cache, then DNS, then a subnet probe
    sweep. Successful DNS/sweep results are written back to the cache.

    `use_cache=False` skips the cache *read* only (a DNS/sweep result is
    still written back to the cache on success, same as always) -- used by
    the CLI's --no-cache flag and by Board.connect's cache-invalidation
    retry (board.py), which has already established the cache is stale and
    doesn't want it consulted again on the retry."""
    if use_cache:
        cached = load_cache(cache_path)
        if cached:
            return cached

    # Broadcast probe first: one packet, and the only reliable
    # client->board direction on mesh WiFi (see probe_broadcast).
    bcast_host = probe_broadcast()
    if bcast_host:
        save_cache(cache_path, bcast_host)
        return bcast_host

    dns_host = resolve_dns()
    if dns_host:
        save_cache(cache_path, dns_host)
        return dns_host

    try:
        ip, netmask = local_ipv4_and_netmask()
        candidates = subnet_candidates(ip, netmask)
    except OSError:
        return None

    found = probe_sweep(candidates)
    if found:
        save_cache(cache_path, found)
    return found
