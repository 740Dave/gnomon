"""Enforcement of g52(b): a test that touches the feature-event emitter or day-bucketed paths must derive the
day at CALL time (or freeze the emitter's clock), never from a module-level constant fixed at COLLECTION. A full run that spans
local midnight otherwise stalls/reads yesterday's directory while the emitter writes today's (flaky-test-2026-10-01).

Narrow by design: a MODULE-LEVEL assignment (AST, top level only) whose target name is day-like (DAY, TODAY, DATE, YMD, ...) and
whose value calls a wall-clock function (time.strftime/time/localtime/gmtime, datetime.now/today/utcnow, date.today), in a test
file that mentions feature_events / GNOMON_FEATURE_EVENTS_DIR / day-bucketed events. Function-local day computation is fine."""

import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = re.compile(r"(^|_)(day|today|date|ymd|daystamp)(_|$)", re.I)
CLOCK_ATTRS = {"strftime", "time", "localtime", "gmtime", "now", "today", "utcnow", "ctime"}
TOUCHES = re.compile(r"feature_events|GNOMON_FEATURE_EVENTS_DIR|day-bucket|feature_event\(")
SELF = os.path.basename(__file__)


def _calls_clock(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr in CLOCK_ATTRS:
                return True
            if isinstance(f, ast.Name) and f.id in {"strftime", "localtime", "gmtime"}:
                return True
    return False


def scan(tests_dir=HERE, skip=(SELF,)):
    """[(file, line, name)] of import-time day constants in feature-event-touching tests."""
    found = []
    for fn in sorted(os.listdir(tests_dir)):
        if not fn.endswith(".py") or fn in skip:
            continue
        src = open(os.path.join(tests_dir, fn), encoding="utf-8", errors="replace").read()
        if not TOUCHES.search(src):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for st in tree.body:                                   # top level only: module-level == fixed at collection
            targets = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []
            val = getattr(st, "value", None)
            if val is None or not _calls_clock(val):
                continue
            for t in targets:
                if isinstance(t, ast.Name) and NAME.search(t.id):
                    found.append((fn, st.lineno, t.id))
    return found


def test_no_test_derives_the_day_from_the_clock_at_import_time():
    assert scan() == [], ("g52(b): derive the day at call time (or freeze the emitter's clock) instead of a module-level constant: "
                          + repr(scan()))


# ---- the scan itself must be able to FAIL (a guard that cannot fail is decoration) ---------------------------------------

def _write(tmp_path, name, body):
    (tmp_path / name).write_text(body)


def test_the_scan_flags_the_known_bad_shapes(tmp_path):
    _write(tmp_path, "test_a.py", 'import time\nfrom gnomon import feature_events\nDAY = time.strftime("%Y%m%d", time.localtime())\n')
    _write(tmp_path, "test_b.py", 'import datetime\nGNOMON_FEATURE_EVENTS_DIR = 1\nTODAY: str = datetime.date.today().isoformat()\n')
    assert [(f, n) for f, _, n in scan(str(tmp_path), skip=())] == [("test_a.py", "DAY"), ("test_b.py", "TODAY")]


def test_the_scan_stays_quiet_on_the_good_shapes(tmp_path):
    _write(tmp_path, "test_c.py", 'import time\nfrom gnomon import feature_events\n'
                                   'def _day(now=None):\n    return time.strftime("%Y%m%d", time.localtime(now))\n'
                                   'def test_x():\n    DAY = time.strftime("%Y%m%d")\n')           # call-time and function-local
    _write(tmp_path, "test_d.py", 'import time\nDAY = time.strftime("%Y%m%d")\n')                  # no feature-event contact
    _write(tmp_path, "test_e.py", 'from gnomon import feature_events\nTIMEOUT = 5\nDAY = "20260101"\n')   # constant, not the clock
    assert scan(str(tmp_path), skip=()) == []
