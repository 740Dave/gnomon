"""Test isolation for the public Gnomon test subset.

Pins the package under test to this checkout and keeps every test away from the real machine:
a throwaway HOME for the whole session, throwaway run / metrics directories, and no inherited
GNOMON_* session variables. Standard library plus pytest only; no web framework is imported here.
"""

import atexit
import ipaddress
import os
import shutil
import socket
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_WEB = os.path.join(_ROOT, "web")
if os.path.isdir(_WEB) and _WEB not in sys.path:
    sys.path.insert(0, _WEB)

# Pin the package under test to this checkout (an editable install of another tree must not win).
os.environ["PYTHONPATH"] = os.pathsep.join(
    [_ROOT] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and p != _ROOT])
import gnomon as _gm_pkg  # noqa: E402

_PKG_DIR = os.path.join(_ROOT, "gnomon")
if os.path.realpath(os.path.dirname(_gm_pkg.__file__)) != os.path.realpath(_PKG_DIR):
    pytest.exit("WRONG TREE UNDER TEST: package resolved to %s, not %s" % (_gm_pkg.__file__, _PKG_DIR), returncode=4)

# One throwaway root for the whole session: HOME and the run directory live under it.
_ISO_ROOT = os.path.realpath(tempfile.mkdtemp(prefix="gmtestiso."))
_SESSION_HOME = os.path.join(_ISO_ROOT, "home")
_SESSION_RUN_DIR = os.path.join(_ISO_ROOT, "run")
os.makedirs(_SESSION_HOME, mode=0o700)
os.makedirs(_SESSION_RUN_DIR, mode=0o700)
_ORIG_ENV = {k: os.environ.get(k) for k in ("HOME", "GNOMON_RUN_DIR", "GNOMON_FEATURE_EVENTS_DIR", "GNOMON_USAGE_DB",
                                            "GNOMON_OPERATOR_MESSAGES_DIR")}
os.environ["HOME"] = _SESSION_HOME
os.environ["GNOMON_RUN_DIR"] = _SESSION_RUN_DIR
os.environ["GNOMON_FEATURE_EVENTS_DIR"] = os.path.join(_ISO_ROOT, "feature-events")
os.environ["GNOMON_USAGE_DB"] = os.path.join(_ISO_ROOT, "usage.sqlite")
os.environ["GNOMON_OPERATOR_MESSAGES_DIR"] = os.path.join(_ISO_ROOT, "operator-messages")
_OWNER_PID = os.getpid()


def _cleanup():
    if os.getpid() == _OWNER_PID:
        shutil.rmtree(_ISO_ROOT, ignore_errors=True)


atexit.register(_cleanup)


@pytest.fixture(scope="session")
def gm_session_home():
    return _SESSION_HOME


_LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1", "")


def _is_loopback(host):
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _gm_no_network(monkeypatch):
    """The public subset needs no network: name lookups and connections to anything but loopback fail loudly."""
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def guarded_getaddrinfo(host, *args, **kwargs):
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if host is not None and not _is_loopback(host):
            try:
                ipaddress.ip_address(host)       # a numeric address needs no lookup
            except ValueError:
                raise OSError("network disabled in the public test subset: lookup of %r" % (host,))
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(self, address):
        if isinstance(address, tuple) and address and isinstance(address[0], str) and not _is_loopback(address[0]):
            raise OSError("network disabled in the public test subset: connect to %r" % (address[0],))
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield


# ---- Fixtures below are copied from the internal suite's conftest (isolation of the harness, flags, presence proof and the
# ---- deployment-data root), so more of its tests can run here. Edits: none except the two constants right below.
def _ancestor_pids():
    pids = set()
    try:
        from gnomon.hub import procinfo
    except Exception:  # pragma: no cover
        return pids
    pid = os.getppid()
    for _ in range(64):
        if pid is None or pid <= 1 or pid in pids:
            break
        pids.add(pid)
        try:
            pid = procinfo.probe(pid).ppid
        except Exception:
            break
    return pids



_LIVE_ANCESTORS = _ancestor_pids()
_LIVE_ROOTS = tuple(os.path.realpath(os.path.join(_ORIG_ENV["HOME"] or "/nonexistent", d))
                    for d in ("gnomon-data",))

@pytest.fixture(autouse=True)
def _gm_isolation(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GNOMON_RUN_DIR", str(tmp_path_factory.mktemp("gmrun")))
    for var in ("GNOMON_AGENT_DEPTH", "GNOMON_INSTANCE_ID", "GNOMON_SESSION_DIR", "GNOMON_PROBE"):
        monkeypatch.delenv(var, raising=False)
    # X26 (g31): a fresh private event dir and usage DB per test, and a fresh emitter (kind, flag cache, drop count).
    monkeypatch.setenv("GNOMON_FEATURE_EVENTS_DIR", str(tmp_path_factory.mktemp("fu-events")))
    monkeypatch.setenv("GNOMON_USAGE_DB", str(tmp_path_factory.mktemp("fu-db") / "usage.sqlite"))
    monkeypatch.setenv("GNOMON_OPERATOR_MESSAGES_DIR", str(tmp_path_factory.mktemp("opmsg")))  # GM-RM-X43
    from gnomon import feature_events as _fe

    _fe._reset_for_tests()
    from gnomon import feature_sites as _fs

    _fs._deploy_cache.update(key=None, rows={}, checked=None)    # F-C: the routes mtime check is cached per window

    # X58: the deny guard's operator-signal files (alerts, rate-limit state, the persistent class (b) warning) live under
    # the guard's ``deny-guard`` runtime directory. An in-process test of main() must never write the operator's real one: pin it per test.
    from gnomon.harness import deny_guard as _dg

    _dg_dir = str(tmp_path_factory.mktemp("dg-signal"))
    monkeypatch.setattr(_dg, "guard_dir", lambda: _dg_dir)

    from gnomon.harness import agent_launch as al

    real_find = getattr(al, "_real_find_gm_ancestor", al.find_gm_ancestor)
    monkeypatch.setattr(al, "_real_find_gm_ancestor", real_find, raising=False)

    def isolated_find(pid=None, *args, **kwargs):
        if args or kwargs:          # injected probe/argv_of: a unit test of the walker
            return real_find(pid, *args, **kwargs)
        return None

    monkeypatch.setattr(al, "find_gm_ancestor", isolated_find)

    real_query = al._socket_query

    def guarded_query(socket_path, command, expect_server_pid, *args, **kwargs):
        real = os.path.realpath(str(socket_path))
        if expect_server_pid in _LIVE_ANCESTORS or any(real.startswith(r + os.sep) for r in _LIVE_ROOTS):
            pytest.fail(f"TEST ISOLATION BREACH: a test queried a LIVE harness control socket "
                        f"({socket_path!r}, server pid {expect_server_pid}) — it would write into "
                        f"the operator's live injection_log")
        return real_query(socket_path, command, expect_server_pid, *args, **kwargs)

    monkeypatch.setattr(al, "_socket_query", guarded_query)

    # X13 S2 Diamond F9: a test must not read the LIVE feature flags. flags.flag() with no injected config reads the machine
    # config store (~/zerosession/config.json); relaunch.main() does so for workspace_scope_core, so the day that flag is ON
    # live, every relaunch test would acquire a bundle rail. Pin the default store to an ABSENT file (every flag at its registry
    # default). A test that wants a flag passes ``config=`` or monkeypatches ``flags._config_store`` itself (applied after this).
    from gnomon import flags as _flags
    from gnomon.config import ConfigStore as _ConfigStore

    _absent = str(tmp_path_factory.getbasetemp() / "pinned-absent-machine-config.json")
    monkeypatch.setattr(_flags, "_config_store", lambda: _ConfigStore(_absent))
    yield


@pytest.fixture(autouse=True)
def _gm_presence_stub():
    """confirm-proof (g76): NO test may pop a real macOS password dialog. Every test runs with a stub presence
    runner that APPROVES (so the pre-existing Apps web tests keep exercising the confirm path); a test of the
    proof itself installs its own stub via ``human_confirm.set_runner``. The one opt-in manual test of the real
    helper (``GNOMON_MANUAL_PRESENCE=1``) restores the real runner itself."""
    from gnomon import human_confirm as hc

    def _approve(reason, timeout_s):
        return hc.ProofResult(True, "authenticated", hc.METHOD, 1.0, "test stub")

    hc.set_runner(_approve)
    yield
    hc.set_runner(None)


# dsd-1a2b3c4d5e2 D2: the DSD submit-proof flag is registered default ON (the operator chose it). The ~30 pre-existing
# tests that POST /api/dsd/submit never sent in-tab activity, so for THEM the flag is OFF (= the previous
# behaviour, which is exactly what OFF promises). tests/test_confirm_proof_dsd_submit.py turns it ON explicitly
# (fixture ``submit_proof_on``) and asserts the registry default through ``real_flag_defaults``.
def _real_flag_defaults():
    from gnomon import flags as _f

    return {n: _f.KNOWN_FLAGS[n].default for n in ("confirm_proof", "dsd_submit_proof")}


_REAL_FLAG_DEFAULTS = _real_flag_defaults()


@pytest.fixture
def real_flag_defaults():
    return dict(_REAL_FLAG_DEFAULTS)


@pytest.fixture(autouse=True)
def _gm_submit_proof_off_by_default(monkeypatch):
    from gnomon import flags as _f

    spec = _f.KNOWN_FLAGS["dsd_submit_proof"]
    monkeypatch.setitem(_f.KNOWN_FLAGS, "dsd_submit_proof", _f.FlagSpec(spec.description, False))


@pytest.fixture
def submit_proof_on(monkeypatch):
    from gnomon import flags as _f

    spec = _f.KNOWN_FLAGS["dsd_submit_proof"]
    monkeypatch.setitem(_f.KNOWN_FLAGS, "dsd_submit_proof", _f.FlagSpec(spec.description, True))


@pytest.fixture(autouse=True)
def gn_deploy_root(tmp_path_factory, monkeypatch):
    """X13 S5-3: every test sees an EMPTY deployment-data root (``<root>/defaults/`` absent), so what a test observes
    is the NEUTRAL built-in default and never what the machine running it has deployed. A test that wants a
    deployment table writes it with the ``put_deploy_data`` fixture (it lands under this per-test root)."""
    from gnomon import deploy_data

    root = tmp_path_factory.mktemp("gn-deploy")
    monkeypatch.setattr(deploy_data, "deploy_root", lambda: str(root))
    deploy_data.reset_for_tests()
    yield root
    deploy_data.reset_for_tests()


@pytest.fixture
def put_deploy_data(gn_deploy_root):
    """``put_deploy_data("role-labels", {...})`` writes ``<deploy_root>/defaults/role-labels.json``;
    ``put_deploy_data("verb-policy.toml", "text")`` writes a raw file (any name with an extension)."""
    import json as _json

    def put(name, value):
        d = gn_deploy_root / "defaults"
        d.mkdir(parents=True, exist_ok=True)
        if "." in name:
            (d / name).write_text(value, encoding="utf-8")
        else:
            (d / (name + ".json")).write_text(_json.dumps(value), encoding="utf-8")
        from gnomon import deploy_data

        deploy_data.reset_for_tests()
        return str(d / (name if "." in name else name + ".json"))

    return put


@pytest.fixture(autouse=True)
def _gm_poll_caches_off_by_default():
    """Poll-pileup fix: the web server's polled-GET caches (``backend.server.set_poll_cache_ttl``) reuse a result for
    2 s in production. Tests that mutate disk state between two reads must see the change at once, so the TTL is 0
    (single-flight only: still one computation per concurrent burst, never a reused value). A test of the cache
    itself sets its own TTL. Restored afterwards."""
    srv = sys.modules.get("backend.server")
    if srv is None or not hasattr(srv, "set_poll_cache_ttl"):
        yield
        return
    prev = srv.POLL_CACHE_TTL_S
    srv.set_poll_cache_ttl(0)
    try:
        yield
    finally:
        srv.set_poll_cache_ttl(prev)




def _local_text(path, seen=None):
    """The text of a test file plus, recursively, the text of the helper modules in the same directory that it imports."""
    import re

    seen = set() if seen is None else seen
    if path in seen or not os.path.isfile(path):
        return ""
    seen.add(path)
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return ""
    here = os.path.dirname(path)
    for m in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.M):
        text += "\n" + _local_text(os.path.join(here, m.group(1) + ".py"), seen)
    return text


def _needs(text):
    """Module names a test file needs beyond the standard library, judged from its text (and its local helpers')."""
    need = []
    if "fastapi" in text or "TestClient" in text or "backend.server" in text or "import backend" in text or "from backend" in text:
        need.append("fastapi")
    if "factlattice" in text:
        need.append("factlattice_engine")
    if "pydantic_evals" in text:
        need.append("pydantic_evals")
    return need


def pytest_ignore_collect(collection_path, config):
    """Clean skip: a test file that needs an optional package which is not installed is not collected (the header says so)."""
    import importlib.util

    p = str(collection_path)
    if not p.endswith(".py") or not os.path.basename(p).startswith("test_"):
        return None
    text = _local_text(p)
    if not text:
        return None
    for mod in _needs(text):
        if importlib.util.find_spec(mod) is None:
            _SKIPPED_FOR.setdefault(mod, []).append(os.path.basename(p))
            return True
    return None


_SKIPPED_FOR = {}


def pytest_report_header(config):
    return "optional packages: fastapi %s, factlattice_engine %s, pydantic_evals %s (test files that need a missing one are not collected)" % tuple(
        "present" if __import__("importlib.util").util.find_spec(m) else "ABSENT" for m in ("fastapi", "factlattice_engine", "pydantic_evals"))


def pytest_terminal_summary(terminalreporter):
    for mod, files in sorted(_SKIPPED_FOR.items()):
        terminalreporter.write_line("not collected (needs %s, not installed): %d file(s)" % (mod, len(files)))
