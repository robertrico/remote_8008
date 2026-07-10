# CLI-level tests for the review-fix flags: `status --rtt` (measures and
# prints average CSR read round-trip time) and `--no-cache` (skips the
# discovery cache read; see board.py/discovery.py's stale-cache handling).
#
# Board.connect is monkeypatched here (like commands.py/console.py tests
# fake the Board surface) so these stay unit tests -- no real network, no
# real litex_server.
import argparse

import pytest

from b8008net import cli, commands


class _Reg:
    def __init__(self, read=None):
        self._read = read

    def read(self):
        return self._read()


class FakeBoard:
    """Just enough of Board's surface for cmd_status: .identifier(),
    .host, .regs.b8008_status, .close(). measure_rtt() only touches
    .regs.b8008_status.read(), already covered directly in
    test_commands.py -- this is about the CLI wiring, not measure_rtt
    itself."""

    def __init__(self):
        self.host = "192.168.7.42"
        self.closed = False
        self.regs = type("Regs", (), {})()
        self.regs.b8008_status = _Reg(read=lambda: 0b101)

    def identifier(self):
        return "b8008_net test"

    def close(self):
        self.closed = True


def _status_args(**overrides):
    defaults = dict(csr="fake.csv", host=None, no_cache=False, rtt=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_status_without_rtt_flag_does_not_measure_rtt(monkeypatch, capsys):
    board = FakeBoard()
    monkeypatch.setattr(cli.Board, "connect", lambda *a, **kw: board)
    calls = []
    monkeypatch.setattr(commands, "measure_rtt", lambda b, reads=commands.RTT_DEFAULT_READS: calls.append(1))

    rc = cli.cmd_status(_status_args(rtt=False))

    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "avg CSR read RTT" not in out


def test_status_rtt_flag_measures_and_prints_average(monkeypatch, capsys):
    board = FakeBoard()
    monkeypatch.setattr(cli.Board, "connect", lambda *a, **kw: board)
    monkeypatch.setattr(commands, "measure_rtt", lambda b, reads=commands.RTT_DEFAULT_READS: 0.0123)

    rc = cli.cmd_status(_status_args(rtt=True))

    assert rc == 0
    assert board.closed is True
    out = capsys.readouterr().out
    assert "avg CSR read RTT" in out
    assert "12.3" in out or "12.30" in out  # 0.0123 s -> ~12.3 ms


def test_no_cache_flag_is_plumbed_through_to_board_connect(monkeypatch):
    board = FakeBoard()
    seen = {}

    def fake_connect(csr, host=None, use_cache=True):
        seen["use_cache"] = use_cache
        return board

    monkeypatch.setattr(cli.Board, "connect", fake_connect)

    cli.cmd_status(_status_args(no_cache=True))
    assert seen["use_cache"] is False

    cli.cmd_status(_status_args(no_cache=False))
    assert seen["use_cache"] is True


def test_status_parser_accepts_rtt_and_no_cache_flags():
    parser = cli.build_parser()
    args = parser.parse_args(["status", "--csr", "x.csv", "--rtt", "--no-cache"])
    assert args.rtt is True
    assert args.no_cache is True
    assert args.func is cli.cmd_status


def test_other_subcommands_accept_no_cache_flag():
    parser = cli.build_parser()
    args = parser.parse_args(["stop", "--csr", "x.csv", "--no-cache"])
    assert args.no_cache is True
