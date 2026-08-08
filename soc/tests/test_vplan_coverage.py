"""Meta-tests: the suite must stay tied to VPLAN.md.

VPLAN: (meta)
"""
import importlib
import inspect
import os
import pkgutil
import re

from conftest import VPLAN_MD, VPLAN_RE

ROW_ID = re.compile(
    r"^(CLK|RST|CDC|RX|TX|BP|CSR|WIRE|E2E|EQ|STR|X|A)-\d+[a-z]?$"
)


def _vplan_row_ids():
    ids = set()
    with open(VPLAN_MD) as f:
        for line in f:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            if cells and ROW_ID.match(cells[0]):
                ids.add(cells[0])
    return ids


def _suite_tests():
    """(module, function, docstring) for every test in this directory."""
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    for mod in pkgutil.iter_modules([here]):
        if not mod.name.startswith("test_"):
            continue
        m = importlib.import_module(mod.name)
        for name, fn in inspect.getmembers(m, inspect.isfunction):
            if name.startswith("test_") and inspect.getmodule(fn) is m:
                out.append((mod.name, name, fn.__doc__ or ""))
    return out


def test_vplan_md_parses():
    """VPLAN.md yields a non-trivial set of row IDs.

    VPLAN: (meta)
    """
    assert len(_vplan_row_ids()) > 100


def test_every_test_declares_a_row():
    """Every test function's docstring names the VPLAN row it discharges.

    VPLAN: (meta)
    """
    missing = [
        f"{mod}::{name}"
        for mod, name, doc in _suite_tests()
        if not VPLAN_RE.search(doc)
    ]
    assert not missing, f"tests without a `VPLAN:` declaration: {missing}"


def test_declared_rows_exist_in_vplan():
    """Every declared row ID is a real row in VPLAN.md.

    VPLAN: (meta)
    """
    known = _vplan_row_ids()
    bad = []
    for mod, name, doc in _suite_tests():
        m = VPLAN_RE.search(doc)
        if not m:
            continue
        for rid in (x.strip() for x in m.group(1).split(",")):
            if rid and rid != "(meta)" and rid not in known:
                bad.append(f"{mod}::{name} -> {rid}")
    assert not bad, f"tests citing unknown VPLAN rows: {bad}"
