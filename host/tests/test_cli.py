# CLI-level tests: `status` (identifier/host, optional --rtt), `login`
# (zero-config discovery or an explicit --host, then the console loop --
# `console` is a plain alias of it), and proof the retired command surface
# (SPEC.md S-PROD-8: load/peek/poke/run/reset/stop/step) is gone.
#
# Board.connect/discovery.discover/console_loop are monkeypatched here (like
# console.py's own tests fake the register surface) so these stay unit
# tests -- no real network, no real litex_server.
import argparse

import pytest

from b8008net import cli


class _Reg:
    def __init__(self, read=None):
        self._read = read

    def read(self):
        return self._read()


class FakeBoard:
    """Just enough of Board's surface for cmd_status: .identifier(),
    .host, .regs.b8008_console_console_tx (backs --rtt's _measure_rtt,
    which polls the same read-only register the interactive console does
    -- b8008_status doesn't exist any more, SPEC.md S-PROD-8/D-8/D-9),
    .close()."""

    def __init__(self):
        self.host = "192.168.7.42"
        self.closed = False
        self.regs = type("Regs", (), {})()
        self.regs.b8008_console_console_tx = _Reg(read=lambda: 0)

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
    monkeypatch.setattr(cli, "_measure_rtt", lambda b, reads=cli.RTT_DEFAULT_READS: calls.append(1))

    rc = cli.cmd_status(_status_args(rtt=False))

    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "avg CSR read RTT" not in out


def test_status_rtt_flag_measures_and_prints_average(monkeypatch, capsys):
    board = FakeBoard()
    monkeypatch.setattr(cli.Board, "connect", lambda *a, **kw: board)
    monkeypatch.setattr(cli, "_measure_rtt", lambda b, reads=cli.RTT_DEFAULT_READS: 0.0123)

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


def test_login_and_console_accept_no_cache_flag_and_default_csr():
    parser = cli.build_parser()
    for name in ("login", "console"):
        args = parser.parse_args([name, "--no-cache"])
        assert args.no_cache is True
        assert args.csr == cli.DEFAULT_CSR_CSV  # zero-config: no --csr required
        assert args.func is cli.cmd_login


# ── login ────────────────────────────────────────────────────────────────
class FakeLoginBoard:
    def __init__(self, host, identifier="b8008_net test"):
        self.host = host
        self._identifier = identifier
        self.closed = False

    def identifier(self):
        return self._identifier

    def close(self):
        self.closed = True


def _login_args(**overrides):
    defaults = dict(csr="fake.csv", host=None, no_cache=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_login_discovers_then_opens_console(monkeypatch, capsys):
    """`login` with no --host calls discovery, connects to what it finds,
    and enters the console loop with that board."""
    board = FakeLoginBoard(host="192.168.1.50")
    discover_calls = []
    connect_calls = []
    loop_calls = []

    def fake_discover(use_cache=True):
        discover_calls.append(use_cache)
        return "192.168.1.50"

    def fake_connect(csr, host=None, use_cache=True):
        connect_calls.append((csr, host, use_cache))
        return board

    monkeypatch.setattr(cli.discovery, "discover", fake_discover)
    monkeypatch.setattr(cli.Board, "connect", fake_connect)
    monkeypatch.setattr(cli, "console_loop", lambda b: loop_calls.append(b))

    rc = cli.cmd_login(_login_args())

    assert rc == 0
    assert discover_calls == [True]
    assert connect_calls == [("fake.csv", "192.168.1.50", True)]
    assert loop_calls == [board]
    assert board.closed is True
    err = capsys.readouterr().err
    assert "192.168.1.50" in err
    assert "b8008_net test" in err


def test_login_honours_explicit_host(monkeypatch):
    """`login --host 10.0.0.5` skips discovery entirely."""
    board = FakeLoginBoard(host="10.0.0.5")
    discover_calls = []
    connect_calls = []

    monkeypatch.setattr(cli.discovery, "discover",
                         lambda use_cache=True: discover_calls.append(1))
    monkeypatch.setattr(
        cli.Board, "connect",
        lambda csr, host=None, use_cache=True: connect_calls.append((csr, host, use_cache)) or board)
    monkeypatch.setattr(cli, "console_loop", lambda b: None)

    rc = cli.cmd_login(_login_args(host="10.0.0.5"))

    assert rc == 0
    assert discover_calls == []  # discovery never called
    assert connect_calls == [("fake.csv", "10.0.0.5", True)]
    assert board.closed is True


def test_login_reports_discovery_failure(monkeypatch, capsys):
    """When discovery finds nothing, login exits non-zero with a message
    naming what it tried -- DNS names and the subnet swept -- not a bare
    traceback."""
    monkeypatch.setattr(cli.discovery, "discover", lambda use_cache=True: None)
    monkeypatch.setattr(cli.discovery, "local_ipv4_and_netmask",
                         lambda: ("192.168.1.42", "255.255.255.0"))
    connect_calls = []
    monkeypatch.setattr(cli.Board, "connect",
                         lambda *a, **kw: connect_calls.append(1))

    rc = cli.cmd_login(_login_args())

    assert rc != 0
    assert connect_calls == []  # never even attempted a connection
    err = capsys.readouterr().err
    for name in cli.discovery.DNS_NAMES:
        assert name in err
    assert "192.168.1.42" in err
    assert "255.255.255.0" in err


def test_login_reports_discovery_failure_when_no_local_route(monkeypatch, capsys):
    """Even if this machine has no usable IPv4 route at all (so the subnet
    sweep couldn't even be built), the failure message still says so
    instead of raising."""
    monkeypatch.setattr(cli.discovery, "discover", lambda use_cache=True: None)

    def raise_oserror():
        raise OSError("no route")

    monkeypatch.setattr(cli.discovery, "local_ipv4_and_netmask", raise_oserror)

    rc = cli.cmd_login(_login_args())

    assert rc != 0
    err = capsys.readouterr().err
    for name in cli.discovery.DNS_NAMES:
        assert name in err


def test_retired_commands_are_gone():
    """load/peek/poke/run/reset/stop/step are no longer registered
    subcommands."""
    parser = cli.build_parser()
    for command in ("load", "peek", "poke", "run", "reset", "stop", "step"):
        with pytest.raises(SystemExit):
            parser.parse_args([command, "--csr", "x.csv"])
