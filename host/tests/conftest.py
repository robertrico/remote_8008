# conftest.py -- hermeticity guard for the host test suite.
#
# These tests once emitted real Etherbone probes onto the operator's LAN (a
# live board even answered mid-suite). The guard below makes that impossible:
# every UDP sendto to a non-loopback destination raises OSError. Loopback is
# allowed because the suite's own echo-responder fixtures live there.
#
# OSError (not AssertionError) is deliberate: production code paths treat a
# failed send as "that transport is unavailable" (probe_broadcast returns
# None, probe_sweep skips the candidate), so discovery-order tests exercise
# the same fallback logic a machine with no network would.
import socket

import pytest

_REAL_SENDTO = socket.socket.sendto


def _loopback_only_sendto(self, data, *args):
    # sendto(data, address) or sendto(data, flags, address)
    address = args[-1]
    host = address[0]
    if not (host.startswith("127.") or host == "localhost" or host == "::1"):
        raise OSError(
            f"host test suite is hermetic: refusing sendto({host!r}) -- "
            "mock the transport instead of touching the network"
        )
    return _REAL_SENDTO(self, data, *args)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "sendto", _loopback_only_sendto)
