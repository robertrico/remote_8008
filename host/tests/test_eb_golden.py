# test_eb_golden.py -- golden differential: the firmware's software Etherbone
# server against litex's own EtherbonePacket encoder/decoder.
#
# VPLAN: SWEB-9. The C unit tests (test_eb8008_host.c) check byte layouts the
# test author derived from etherbone.py; this test removes the author from the
# loop -- litex encodes every request and decodes every reply, so a mutual
# misreading of the dialect cannot pass. Randomized records are seeded for
# reproducibility.
import pathlib
import random
import shutil
import subprocess

import pytest

from litex.tools.remote.etherbone import (EtherbonePacket, EtherboneRecord,
                                          EtherboneReads, EtherboneWrites)

REPO = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def pipe(tmp_path_factory):
    if shutil.which("cc") is None:
        pytest.skip("no host C compiler")
    build = tmp_path_factory.mktemp("ebpipe")
    binary = build / "eb_pipe"
    subprocess.check_call([
        "cc", "-DEB8008_HOST_TEST", "-o", str(binary),
        str(REPO / "firmware/eb8008.c"),
        str(REPO / "firmware/test_eb8008_pipe.c"),
    ])
    proc = subprocess.Popen([str(binary)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    base = int(proc.stdout.readline())
    yield proc, base
    proc.stdin.close()
    proc.wait(timeout=5)


def xact(pipe_fixture, pkt_bytes):
    proc, _ = pipe_fixture
    proc.stdin.write(bytes(pkt_bytes).hex() + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline().strip()
    return bytes.fromhex(line) if line else b""


def test_probe_reply_decodes_as_litex_expects(pipe):
    req = EtherbonePacket()
    req.pf = 1
    req.encode()
    reply = xact(pipe, req.bytes)
    decoded = EtherbonePacket(init=reply)
    decoded.decode()
    assert decoded.magic == 0x4e6f
    assert decoded.pr == 1 and decoded.pf == 0
    assert decoded.addr_size == 4 and decoded.port_size == 4


def test_write_then_read_roundtrip(pipe):
    _, base = pipe
    values = [0x11111111, 0x22222222, 0x33333333]

    wr = EtherboneRecord(addr_size=4)
    wr.writes = EtherboneWrites(addr_size=4, base_addr=base, datas=iter(values))
    wr.wcount = len(values)
    pkt = EtherbonePacket()
    pkt.records = [wr]
    pkt.encode()
    assert xact(pipe, pkt.bytes) == b""  # writes are fire-and-forget

    rd = EtherboneRecord(addr_size=4)
    rd.reads = EtherboneReads(addr_size=4, addrs=[base + 4 * i for i in range(3)])
    rd.rcount = 3
    rd.reads.base_ret_addr = 0x1234
    pkt = EtherbonePacket()
    pkt.records = [rd]
    pkt.encode()
    reply = xact(pipe, pkt.bytes)

    decoded = EtherbonePacket(init=reply)
    decoded.decode()
    record = decoded.records.pop()
    assert record.writes.base_addr == 0x1234
    assert record.writes.get_datas() == values


def test_randomized_records_differential(pipe):
    _, base = pipe
    rng = random.Random(0x8008)
    model = {}

    # Zero the whole 256-word window first (through the wire, like everything
    # else here) -- the module-scoped pipe carries earlier tests' writes.
    for chunk in range(0, 256, 128):
        wr = EtherboneRecord(addr_size=4)
        wr.writes = EtherboneWrites(addr_size=4, base_addr=base + 4 * chunk,
                                    datas=iter([0] * 128))
        wr.wcount = 128
        pkt = EtherbonePacket()
        pkt.records = [wr]
        pkt.encode()
        assert xact(pipe, pkt.bytes) == b""

    for _ in range(50):
        n = rng.randint(1, 32)
        addrs = [base + 4 * rng.randrange(0, 256) for _ in range(n)]

        if rng.random() < 0.5:
            values = [rng.getrandbits(32) for _ in range(n)]
            wr = EtherboneRecord(addr_size=4)
            # litex writes land at base_addr + 4*i; mirror that in the model
            wr.writes = EtherboneWrites(addr_size=4, base_addr=addrs[0],
                                        datas=iter(values))
            wr.wcount = n
            pkt = EtherbonePacket()
            pkt.records = [wr]
            pkt.encode()
            assert xact(pipe, pkt.bytes) == b""
            for i, v in enumerate(values):
                model[addrs[0] + 4 * i] = v
        else:
            rd = EtherboneRecord(addr_size=4)
            rd.reads = EtherboneReads(addr_size=4, addrs=list(addrs))
            rd.rcount = n
            corr = rng.getrandbits(32)
            rd.reads.base_ret_addr = corr
            pkt = EtherbonePacket()
            pkt.records = [rd]
            pkt.encode()
            reply = xact(pipe, pkt.bytes)
            decoded = EtherbonePacket(init=reply)
            decoded.decode()
            record = decoded.records.pop()
            assert record.writes.base_addr == corr
            assert record.writes.get_datas() == [model.get(a, 0) for a in addrs]
