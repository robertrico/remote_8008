# test_eb_server.py -- CommUDPBroadcast: the broadcast-request transport.
#
# VPLAN: SWEB-13. The bridge's socket broadcasts to the same port it is bound
# to, so it receives an echo of every datagram it sends; probe() and read()
# must skip echoes and stale replies rather than asserting on the first
# datagram like stock CommUDP. All transport here is faked -- no sockets.
import socket

import pytest

from b8008net.eb_server import CommUDPBroadcast, broadcast_addr

from litex.tools.remote.etherbone import (EtherbonePacket, EtherboneRecord,
                                          EtherboneReads, EtherboneWrites)


class FakeSocket:
    """Scripted datagram source: recvfrom() pops the queue, sendto() records.
    An empty queue raises socket.timeout like a real non-blocking socket."""

    def __init__(self, replies=()):
        self.queue = list(replies)
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((bytes(data), addr))

    def recvfrom(self, _size):
        if not self.queue:
            raise socket.timeout()
        return self.queue.pop(0)

    def settimeout(self, _t):
        pass


def _probe_request_bytes():
    pkt = EtherbonePacket()
    pkt.pf = 1
    pkt.encode()
    return bytes(pkt.bytes) + bytes(4)


def _probe_reply_bytes():
    pkt = EtherbonePacket()
    pkt.pr = 1
    pkt.encode()
    return bytes(pkt.bytes)


def _read_reply_bytes(correlation_id, values):
    record = EtherboneRecord(addr_size=4)
    record.writes = EtherboneWrites(addr_size=4, base_addr=correlation_id,
                                    datas=iter(values))
    record.wcount = len(values)
    pkt = EtherbonePacket()
    pkt.records = [record]
    pkt.encode()
    return bytes(pkt.bytes)


def _comm(fake):
    comm = CommUDPBroadcast(server="10.0.0.255", port=1234)
    comm.socket = fake
    return comm


def test_probe_skips_own_echo_then_accepts_reply():
    fake = FakeSocket([
        (_probe_request_bytes(), ("10.0.0.7", 1234)),  # own broadcast echo
        (_probe_reply_bytes(),   ("10.0.0.45", 1234)),  # the board
    ])
    assert _comm(fake).probe("10.0.0.255", 1234) == 1
    # the request went to the broadcast address
    assert fake.sent[0][1] == ("10.0.0.255", 1234)


def test_probe_ignores_garbage_and_times_out_loose():
    fake = FakeSocket([(b"\x00" * 12, ("10.0.0.9", 1234))])
    assert _comm(fake).probe("10.0.0.255", 1234, loose=True) == 0


def test_probe_raises_when_no_board_answers():
    with pytest.raises(ConnectionRefusedError):
        _comm(FakeSocket()).probe("10.0.0.255", 1234)


def test_read_skips_echo_and_stale_reply_correlates_by_counter():
    comm = CommUDPBroadcast(server="10.0.0.255", port=1234)
    # read_counter increments to 1 for the first read; a stale reply carries
    # an old id and must be skipped, not returned.
    fake = FakeSocket([
        (b"\x4e\x6f\x10\x44" + bytes(8), ("10.0.0.7", 1234)),      # own echo (reads, no writes)
        (_read_reply_bytes(0xdead, [0x11111111]), ("10.0.0.45", 1234)),  # stale id
        (_read_reply_bytes(1, [0xCAFEBABE]), ("10.0.0.45", 1234)),       # the answer
    ])
    comm.socket = fake
    assert comm.read(0xF0000000) == 0xCAFEBABE


def test_read_returns_list_for_burst():
    comm = CommUDPBroadcast(server="10.0.0.255", port=1234)
    fake = FakeSocket([
        (_read_reply_bytes(1, [1, 2, 3]), ("10.0.0.45", 1234)),
    ])
    comm.socket = fake
    assert comm.read(0xF0000000, length=3) == [1, 2, 3]


def test_read_times_out_after_retries():
    comm = CommUDPBroadcast(server="10.0.0.255", port=1234)
    comm.socket = FakeSocket()
    with pytest.raises(socket.timeout):
        comm.read(0xF0000000)


def test_broadcast_addr_is_subnet_directed(monkeypatch):
    import b8008net.eb_server as ebs
    monkeypatch.setattr(ebs, "local_ipv4_and_netmask",
                        lambda: ("192.168.4.17", "255.255.255.0"))
    assert broadcast_addr() == "192.168.4.255"


def test_broadcast_addr_falls_back_to_limited(monkeypatch):
    import b8008net.eb_server as ebs

    def _boom():
        raise OSError("no route")

    monkeypatch.setattr(ebs, "local_ipv4_and_netmask", _boom)
    assert broadcast_addr() == "255.255.255.255"
