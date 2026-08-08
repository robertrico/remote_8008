"""Meta-tests: the suite must stay tied to VPLAN.md.

VPLAN: (meta)
"""
import importlib
import inspect
import os
import pkgutil
import re
import sys

from conftest import VPLAN_MD, VPLAN_RE

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.abspath(os.path.join(_HERE, ".."))
_ROOT = os.path.abspath(os.path.join(_SOC, ".."))
MAKEFILE = os.path.join(_ROOT, "Makefile")

ROW_ID = re.compile(
    r"^(CLK|RST|CDC|RX|TX|BP|CSR|WIRE|E2E|EQ|STR|X|A)-\d+[a-z]?$"
)

# The `vplan` target's pytest invocation, e.g. "soc/tests soc/test_integration.py".
VPLAN_RECIPE_RE = re.compile(r"^vplan:\n\t\$\(PY\) -m pytest (.+?) -v", re.M)

# soc/test_integration.py predates this VPLAN conformance plan. Its two tests
# exercise a build-output import path, not a spec-conformance claim, and are
# deliberately out of scope for the "every test declares a VPLAN row" rule
# below. Named here so the exemption is a visible, explicit decision rather
# than an accident of the scanner only ever looking inside soc/tests/.
EXEMPT_FILES = {os.path.join(_ROOT, "soc", "test_integration.py")}


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


def _vplan_targets():
    """Paths (relative to repo root) that `make vplan` hands to pytest.

    Parsed straight out of the Makefile's `vplan` recipe rather than
    hardcoded, so this stays in sync automatically if that target's
    pytest invocation ever grows another directory or file -- no second,
    forgettable edit required here.
    """
    with open(MAKEFILE) as f:
        text = f.read()
    m = VPLAN_RECIPE_RE.search(text)
    assert m, "could not find `vplan` target's pytest invocation in Makefile"
    return m.group(1).split()


def _modules_in(path):
    """Yield (module_name, abs_file_path) for every test_*.py at `path`.

    `path` may be a directory (scanned non-recursively, like the vplan
    target's own pytest invocation) or a single .py file.
    """
    if os.path.isdir(path):
        if path not in sys.path:
            sys.path.insert(0, path)
        for mod in pkgutil.iter_modules([path]):
            if mod.name.startswith("test_"):
                yield mod.name, os.path.join(path, mod.name + ".py")
    elif os.path.isfile(path):
        mod_dir, base = os.path.split(path)
        mod_name, ext = os.path.splitext(base)
        if ext == ".py" and mod_name.startswith("test_"):
            if mod_dir not in sys.path:
                sys.path.insert(0, mod_dir)
            yield mod_name, path


def _suite_tests():
    """(module, function, docstring, exempt) for every test `make vplan` runs."""
    out = []
    for target in _vplan_targets():
        abs_target = os.path.normpath(os.path.join(_ROOT, target))
        for mod_name, mod_path in _modules_in(abs_target):
            m = importlib.import_module(mod_name)
            exempt = mod_path in EXEMPT_FILES
            for name, fn in inspect.getmembers(m, inspect.isfunction):
                if name.startswith("test_") and inspect.getmodule(fn) is m:
                    out.append((mod_name, name, fn.__doc__ or "", exempt))
    return out


def test_vplan_md_parses():
    """VPLAN.md yields a non-trivial set of row IDs.

    VPLAN: (meta)
    """
    assert len(_vplan_row_ids()) > 100


def test_every_test_declares_a_row():
    """Every non-exempt test function's docstring names the VPLAN row it
    discharges, across every file `make vplan` collects -- not just this
    directory.

    VPLAN: (meta)
    """
    missing = [
        f"{mod}::{name}"
        for mod, name, doc, exempt in _suite_tests()
        if not exempt and not VPLAN_RE.search(doc)
    ]
    assert not missing, f"tests without a `VPLAN:` declaration: {missing}"


def test_declared_rows_exist_in_vplan():
    """Every declared row ID is a real row in VPLAN.md.

    VPLAN: (meta)
    """
    known = _vplan_row_ids()
    bad = []
    for mod, name, doc, _exempt in _suite_tests():
        m = VPLAN_RE.search(doc)
        if not m:
            continue
        for rid in (x.strip() for x in m.group(1).split(",")):
            if rid and rid != "(meta)" and rid not in known:
                bad.append(f"{mod}::{name} -> {rid}")
    assert not bad, f"tests citing unknown VPLAN rows: {bad}"
