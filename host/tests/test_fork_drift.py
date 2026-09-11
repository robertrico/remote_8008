# test_fork_drift.py -- guards the firmware/udp.c fork against silent drift.
#
# firmware/udp.c is a fork of the vendored libliteeth udp.c (see SPEC.md
# S-NET-4 for why the fork exists). Two failure modes this pins:
#
# 1. Upstream moves: if the vendored file changes, the fork was taken from a
#    stale base and every upstream fix is silently missing. The sha256 pin
#    below fails, telling a human to re-audit the fork against the new base.
# 2. Build-system inversion: the fork only shadows the vendored copy because
#    make searches the current directory before VPATH. If firmware/udp.c
#    stopped being picked up, every S-NET-4 behavior would silently revert.
#    The marker checks assert the fork's load-bearing changes are present in
#    the file the firmware build will actually compile.
import hashlib
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

# litex_setup.py clones next to its cwd, which the Makefile makes .venv;
# older environments have the trees at the repo root. Accept either.
_LITEX = next((d for d in (REPO / ".venv/litex", REPO / "litex") if (d / ".git").exists()),
              REPO / "litex")
VENDORED = _LITEX / "litex/soc/software/libliteeth/udp.c"
FORK     = REPO / "firmware/udp.c"

# sha256 of the vendored base the fork was audited against (2026-08-09).
# If this fails: diff the vendored file against this baseline, port anything
# relevant into firmware/udp.c, then update the pin.
VENDORED_BASE_SHA = "4d21ba37a0b3d193ddaa7c9359a337dda52613a78353df7a9da23569ee32416a"

# Load-bearing fork changes (SPEC.md S-NET-4 / S-WIRE-2b). Each marker is a
# code fragment that exists only in the fork.
FORK_MARKERS = [
    "udp_set_peer",             # ARP-free reply addressing
    "udp_last_src_mac",         # requester MAC capture
    "udp_announce_arp",         # gratuitous ARP announce
    "udp_arp_refresh",          # forced gateway ARP round-trip
    "my_ip | 0xff",             # subnet-broadcast acceptance (filter fix)
    "txlen < 100",              # min-frame TX padding
]


def test_vendored_base_unchanged():
    sha = hashlib.sha256(VENDORED.read_bytes()).hexdigest()
    assert sha == VENDORED_BASE_SHA, (
        "vendored libliteeth udp.c changed since the fork was audited -- "
        "re-audit firmware/udp.c against the new base and update the pin"
    )


def test_fork_carries_all_load_bearing_changes():
    text = FORK.read_text()
    missing = [m for m in FORK_MARKERS if m not in text]
    assert not missing, f"firmware/udp.c lost fork changes: {missing}"


def test_firmware_makefile_compiles_the_fork():
    # The fork shadows the vendored file via cwd-before-VPATH resolution;
    # udp.o must still be in OBJECTS and the fork file must exist.
    mk = (REPO / "firmware/Makefile").read_text()
    assert "udp.o" in mk
    assert FORK.exists()
