# eb_server.py -- litex_server with a broadcast-request CommUDP.
#
# Mesh WiFi systems (the bench MR60 among them) can drop or NAT-mangle
# WiFi-client -> wired-LAN *unicast* while delivering broadcast reliably.
# The firmware's software Etherbone server (firmware/eb8008.c) therefore
# accepts broadcast requests and unicasts its replies straight back to the
# requester's MAC (board -> client unicast is the reliable direction).
#
# This module is b8008net's replacement for the stock `litex_server --udp`:
# identical TCP front (RemoteClient connects to localhost as usual), but the
# UDP back end broadcasts its requests so they actually reach the board.
import argparse
import socket
import time

from litex.tools.remote.comm_udp import CommUDP
from litex.tools.litex_server import RemoteServer

from b8008net.discovery import local_ipv4_and_netmask, _ip_to_int, _int_to_ip


def broadcast_addr():
    """Subnet-directed broadcast for the local /24 -- forwarded by mesh
    bridges that drop the limited (255.255.255.255) form."""
    try:
        ip, netmask = local_ipv4_and_netmask()
        return _int_to_ip(_ip_to_int(ip) | (~_ip_to_int(netmask) & 0xFFFFFFFF))
    except OSError:
        return "255.255.255.255"


class CommUDPBroadcast(CommUDP):
    """CommUDP that sends every request to the LAN broadcast address (with
    SO_BROADCAST) and accepts the board's unicast replies from any source.

    A socket that broadcasts to the port it is itself bound to receives an
    echo of its own datagrams, so every receive path here skips packets that
    do not parse as the expected reply instead of asserting on the first
    datagram like the stock class."""

    def open(self, probe=True):
        if hasattr(self, "socket"):
            return
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.socket.bind(("", self.port))
        self.socket.settimeout(self.timeout)
        if probe:
            self.probe(self.server, self.port)

    def probe(self, ip, port, loose=False):
        from litex.tools.remote.etherbone import EtherbonePacket
        request = EtherbonePacket(self.addr_width)
        request.pf = 1
        request.encode()
        request.bytes += bytes(4)
        # 3 retries max: the TCP front opens only after this probe, and
        # Board.connect gives the whole bridge 5 s to come up.
        for _ in range(3):
            try:
                self.socket.sendto(request.bytes, (ip, port))
                while True:
                    datas, addr = self.socket.recvfrom(8192)
                    try:
                        reply = EtherbonePacket(self.addr_width, datas)
                        reply.decode()
                    except Exception:
                        continue  # noise
                    if getattr(reply, "pr", 0) == 1:
                        return 1
                    # own echo (pf set) or unrelated -- keep listening
            except socket.timeout:
                continue
        if loose:
            return 0
        raise ConnectionRefusedError("no Etherbone probe reply via broadcast")

    def read(self, addr, length=None, burst="incr"):
        from litex.tools.remote.etherbone import (EtherbonePacket,
            EtherboneRecord, EtherboneReads)
        assert burst == "incr"
        length_int = 1 if length is None else length
        for _ in range(10):
            self.read_counter += 1
            record = EtherboneRecord(addr_size=self.addr_width//8)
            record.reads = EtherboneReads(addr_size=self.addr_width//8,
                addrs=[addr+4*j for j in range(length_int)])
            record.rcount = len(record.reads)
            record.reads.base_ret_addr = self.read_counter
            packet = EtherbonePacket(addr_width=self.addr_width)
            packet.records = [record]
            packet.encode()
            self.socket.sendto(packet.bytes, (self.server, self.port))
            timed_out = False
            while True:
                try:
                    datas, addr_from = self.socket.recvfrom(8192)
                except socket.timeout:
                    timed_out = True
                    break
                try:
                    reply = EtherbonePacket(self.addr_width, datas)
                    reply.decode()
                    rec = reply.records.pop()
                    values = rec.writes.get_datas()
                    if rec.writes.base_addr != self.read_counter:
                        continue  # stale reply
                except Exception:
                    continue  # own echo / noise
                break
            if not timed_out:
                break
        else:
            raise socket.timeout
        return values[0] if length is None else values


def main():
    parser = argparse.ArgumentParser(
        description="b8008net Etherbone bridge (broadcast UDP transport).")
    parser.add_argument("--bind-ip",   default="localhost")
    parser.add_argument("--bind-port", default=1234, type=int)
    parser.add_argument("--udp-port",  default=1234, type=int)
    args = parser.parse_args()

    comm = CommUDPBroadcast(server=broadcast_addr(), port=args.udp_port)
    server = RemoteServer(comm, args.bind_ip, args.bind_port)
    server.open()
    server.start(4)
    try:
        while True:
            time.sleep(100)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
