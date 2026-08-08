"""Console RX path behavior. DUT is ConsoleBridge, pure cd_sys."""
from migen import run_simulation

from console_bridge import ConsoleBridge


# clk/baud = 10 -> 10 sys cycles per bit, 100 per 10-bit frame. Keeps sims fast.
FAST = dict(sys_clk_freq=1_000_000, baudrate=100_000)
BIT = 10
FRAME = 10 * BIT


def serial_send(dut, byte):
    """Drive one 8N1 frame onto dut.pads.rx at the FAST bit period."""
    def bit(v):
        yield dut.pads.rx.eq(v)
        for _ in range(BIT):
            yield

    yield from bit(0)                       # start
    for i in range(8):
        yield from bit((byte >> i) & 1)     # LSB first
    yield from bit(1)                       # stop


def test_bridge_receives_a_byte():
    """A byte clocked into pads.rx lands in rx_fifo.

    VPLAN: RX-1
    """
    dut = ConsoleBridge(**FAST)
    seen = []

    def gen():
        yield dut.pads.rx.eq(1)
        for _ in range(BIT):
            yield
        yield from serial_send(dut, 0xA5)
        for _ in range(FRAME):
            yield
        seen.append((yield dut.rx_fifo.level))
        seen.append((yield dut.rx_fifo.source.data))

    run_simulation(dut, gen())
    assert seen == [1, 0xA5]


def test_rx_fifo_depth_is_4096():
    """rx_fifo depth is exactly 4096.

    VPLAN: RX-1
    """
    assert ConsoleBridge(sys_clk_freq=75e6).rx_fifo.fifo.depth == 4096
