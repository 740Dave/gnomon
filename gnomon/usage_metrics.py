"""harness usage metrics DB + unused-capability report.

The report answers: which kinds of injection reach the harness, how often,
and from whom. If an obviously useful category is unused, or used by
only one agent lineage (one role but not another), that is a possible knowledge gap.
Silence is the failure it hides.

Two pieces, both stdlib-only:

* **Indexer** — discovers every ``<root>/*/.claude/injection_log.jsonl`` (roots
  default to ``~/gnomon-data/sessions`` plus any configured legacy roots) and loads it
  INCREMENTALLY into sqlite. Per file it keeps ``(inode, byte offset, head
  hash)``: a re-run reads only the appended complete lines (idempotent); a
  truncated, rotated or rewritten-in-place file is detected, its rows dropped,
  and it is re-read from byte 0. Logs are opened read-only; nothing is ever
  written under a session tree.

  **Categories only, no content.** Each record keeps only short category tokens
  (``kind``/``source``/``trigger``/``decision``/``route``/``subtype``), a derived
  self-``verb``, the operator ``\\command`` WORD (never its arguments; registered
  words only) and the slash-command word (harness-generated only). A field value that is not a short single token (``^[A-Za-z0-9]
  [A-Za-z0-9_.:-]{0,47}$``) is replaced by ``<non-category>``. Residual, stated
  honestly: a *single-token* string in a categorical field is kept as a category
  by contract.

* **Report** — ``python -m gnomon.usage_metrics report [--since DATE]
  [--json]``: (a) a category x lineage count matrix for the window; (b) zero-
  where-peers-nonzero; (c) categories KNOWN to the harness (derived from the code:
  ``hooks.SOURCES``, the self_mcp tool names, the operator-tap registrations and a
  static scan of the provenance-record write sites) that were never seen; (d)
  lineages below a declared expected rate (a small JSON rules file). Every flag
  carries :data:`SIGNAL_LABEL` — a signal to investigate, never a defect — and the
  denominator (sessions + records per lineage) travels with it; a lineage with
  fewer than ``min_sessions`` sessions is marked low-denominator so a zero there
  cannot read as confident.

Feature flag ``usage_metrics`` (registry default ON — read-only and
display-only; architect flip 2026-09-29) gates the web panel (``/api/usage``) and
future automatic indexing. This CLI works regardless: it only reads logs.

X26 (schema v5): the same DB also holds the ``feature_events`` stream
(one categories-only JSON line per Gnomon feature use, written by :mod:`gnomon.feature_events`), indexed by
:func:`index_feature_events` with the same (inode, offset, head-hash) guarantees through ONE shared streaming loop
(:class:`_LineStream`). ``usage_index_job`` runs it every few minutes in the background; ``report --features``
(:mod:`gnomon.feature_report`) joins it to the feature registry. Without ``--features`` every X7 output is
unchanged apart from ``schema_version``.

Two words that could carry TYPED text are allowlisted (X7 audit F2): the
operator ``\\command`` word is kept only if it is a REGISTERED operator command
(``known_categories()["cmd"]``), else ``<unregistered>``; the slash-command word
is kept only when the harness itself generated the content, else ``<typed>``.
"""

from __future__ import annotations

import argparse
import ast
import datetime as _dt
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

FLAG_NAME = "usage_metrics"
SCHEMA_VERSION = 5  # v2: records.pollution; v3: registered-only cmd / harness-only slash (F2); v4: credential-shaped tokens redacted (X7 fixpass2); v5: feature_events stream (X26)
SIGNAL_LABEL = "signal — investigate"

# Import-time path defaults are a hazard (g31, 2026-09-30): a module imported under one
# HOME and called under another would keep the FIRST HOME forever. They resolve at CALL
# time; the old public names stay working through the module ``__getattr__`` below.
def default_db_path() -> str:
    from .config import runtime_root_default

    return os.path.join(runtime_root_default(), "metrics", "harness_usage.sqlite")


#: X26 (g31): the BACKGROUND job and the web backend resolve their DB through this, so a test can pin
#: ``GNOMON_USAGE_DB`` to a temp file and no test run ever indexes into the real usage DB. (default_db_path
#: itself is unchanged: the X7 contract pins it to the machine path.)
ENV_USAGE_DB = "GNOMON_USAGE_DB"


def job_db_path() -> str:
    return os.environ.get(ENV_USAGE_DB) or default_db_path()


def default_roots() -> Tuple[str, ...]:
    """``<data_root>/sessions`` plus the deployment's LEGACY session sources (X13 S5-4; see ``deploy_data.legacy_session_roots``)."""
    from .config import sessions_root_default
    from .deploy_data import legacy_session_roots

    return (sessions_root_default(),) + legacy_session_roots()


def __getattr__(name: str):  # PEP 562: UM.DEFAULT_DB_PATH / UM.DEFAULT_ROOTS resolve NOW, not at import
    if name == "DEFAULT_DB_PATH":
        return default_db_path()
    if name == "DEFAULT_ROOTS":
        return default_roots()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


LOG_REL = os.path.join(".claude", "injection_log.jsonl")
DEFAULT_MIN_SESSIONS = 3

_HEAD_BYTES = 256
#: Streaming bounds (X7 audit F5): the unread tail is read in chunks of this size,
#: never whole; a single line longer than _MAX_LINE_BYTES is skipped (counted bad)
#: rather than buffered; the offset is committed every _COMMIT_EVERY rows.
_CHUNK_BYTES = 1 << 20
_MAX_LINE_BYTES = 8 << 20
_COMMIT_EVERY = 5000
UNREGISTERED = "<unregistered>"
TYPED = "<typed>"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,47}$")
NON_CATEGORY = "<non-category>"
REDACTED = "<redacted>"

#: Credential-shaped single tokens (X7 fixpass audit N1). A token that matches is
#: replaced by the fixed REDACTED marker BEFORE anything is written, in every
#: column that carries a token (kind/source/trigger/decision/route/subtype/verb/
#: command/slash). Vendor prefixes first, then a generic high-entropy test.
_CRED_PREFIX_RE = re.compile(
    r"^(?:"
    r"gh[pousr]_[A-Za-z0-9]{8,}"            # GitHub classic / OAuth / app tokens
    r"|github_pat_[A-Za-z0-9_]{8,}"          # GitHub fine-grained PAT
    r"|(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{12,}"  # AWS key ids
    r"|sk[-_][A-Za-z0-9_\-]{6,}"             # OpenAI / Anthropic / Stripe style
    r"|(?:rk|pk)_(?:live|test)_[A-Za-z0-9]{6,}"
    r"|xox[abposr]-[A-Za-z0-9-]{4,}"         # Slack
    r"|xapp-[A-Za-z0-9-]{4,}"
    r"|glpat-[A-Za-z0-9_\-]{8,}"             # GitLab
    r"|AIza[A-Za-z0-9_\-]{16,}"              # Google API key
    r"|hf_[A-Za-z0-9]{16,}"                  # Hugging Face
    r"|npm_[A-Za-z0-9]{16,}"
    r"|pypi-[A-Za-z0-9_\-]{16,}"
    r"|shp(?:at|ss|ca|pa)_[A-Za-z0-9]{16,}"  # Shopify
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_.\-]+"  # JWT
    r")$", re.IGNORECASE)


#: Plain lowercase words joined by - _ : (X7b N1): the harness's own triggers such as
#: ``folder-trust-accept:enter`` are exempt from the entropy rule; digits are not.
_PLAIN_WORD_RE = re.compile(r"^[a-z]+(?:[-_:][a-z]+)*$")


def _entropy(s: str) -> float:
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def is_credential_shaped(v: str) -> bool:
    """True for a vendor-prefixed secret or any long high-entropy single token.
    Category words in this code base are short lowercase/hyphen words; a token of
    20+ chars that mixes letters and digits, or 24+ chars with high character
    entropy, or a 32+ char hex run, is not one of them."""
    if _CRED_PREFIX_RE.match(v):
        return True
    n = len(v)
    if n >= 20 and re.search(r"[A-Za-z]", v) and re.search(r"\d", v):
        return True
    if n >= 32 and re.fullmatch(r"[0-9a-fA-F]+", v):
        return True
    if n >= 24 and _entropy(v) >= 3.6 and not _PLAIN_WORD_RE.match(v):
        return True
    return False
_DATE_SUFFIX_RE = re.compile(r"-\d{8}(?:-\d{6})?$")

#: How each self-verb shows up in the provenance log. ``None`` = the verb writes
#: NO record (read-only), so "never seen" means UNOBSERVABLE, not unused.
#: Guarded by the test suite: the key set must equal the verbs derived from
#: ``harness/self_mcp.py``, so a new verb cannot be silently misreported.
VERB_LOGGED_AS: Dict[str, Optional[str]] = {
    "request_compact": "kind=injection source=self-verb trigger=compact",
    "inject": "kind=injection source=self-verb trigger!=compact",
    "memory": "kind=memory-remember source=self-verb",
    "search_transcript": "kind=transcript-search source=self-verb",
    "quiesce": "kind=quiescent decision in (enter, refused)",
    "idle_snooze": "kind=idle-snooze decision in (snooze, question, dismiss, refused, disabled, already-dismissed)",
    "alert": "kind=alert source=self-verb decision in (raised, deduped, rate-limited, refused)",
    "sync_my_skills": "kind=skill-sync source=self-verb decision in (dry-run, applied, applied-partial, refused, disabled, failed)",
    "restart_service": "kind=restart-service source=harness trigger=restart-service decision in (refused, confirm-requested, restarted, failed)",
    "launch_agent": "kind=agent-launch launcher_kind=agent (ledger, has instance_id) decision in (reserve, deny)",
    # pre-review B1: the four DSD self-verbs write no provenance row of their own; the DSD record itself is the audit trail.
    "send_dsd": None, "my_dsds": None, "withdraw_dsd": None, "replace_dsd": None,
    # one call = one ``work-verb`` record (the verb name is in ``verb``); a status declaration = one ``operator-status`` record.
    "work_add": "kind=work-verb verb=work_add", "work_update": "kind=work-verb verb=work_update",
    "work_done": "kind=work-verb verb=work_done", "work_drop": "kind=work-verb verb=work_drop",
    "work_list": "kind=work-verb verb=work_list",
    "operator_status_set": "kind=operator-status setter!=operator (a session's call, set or refused)",
    "mail_list": "kind=mail source=self-verb op=mail_list",
    "mail_read": "kind=mail source=self-verb op=mail_read",
    "mail_draft": "kind=mail source=self-verb op=mail_draft",
    "mail_to_operator": "kind=mail source=self-verb op=mail_to_operator",
    "context": None,  # Actuator.context() is a pure read — no record is written
    "plan_usage": None,  # Actuator.plan_usage() is a pure read — no record is written
}

FAMILIES = ("source", "trigger", "slash", "verb", "cmd", "kind", "feature")

#: Test pollution (coordinator ruling 2026-09-29, measured at the object): a
#: launch DENIAL whose ``target_profile`` is the literal test profile ``target`` or
#: a temp path is a test/audit run that reached a live harness through an
#: inherited ``GNOMON_SESSION_DIR`` (74 denials from one role: 71 from
#: ``test_adoption_hardening`` before the conftest fix + auditor probes), not the
#: agent using the verb. Excluded from reports by default, counted per
#: lineage, restorable with ``--include-test-pollution``.
POLLUTION_LAUNCH_DENY = "launch-deny-test-target"
_TEST_TARGET_NAMES = frozenset({"target"})
_TEST_TARGET_PREFIXES = ("/var/folders/", "/tmp/", "/private/var/folders/", "/private/tmp/")


def classify_pollution(rec: Dict[str, Any]) -> Optional[str]:
    """A test launch ATTEMPT: a ``deny`` or a ``reserve`` (X7 audit F7) whose target
    is the test profile name or a temp path."""
    if rec.get("kind") != "agent-launch" or rec.get("decision") not in ("deny", "reserve"):
        return None
    tp = rec.get("target_profile")
    if not isinstance(tp, str):
        return None
    if tp in _TEST_TARGET_NAMES or tp.startswith(_TEST_TARGET_PREFIXES):
        return POLLUTION_LAUNCH_DENY
    return None


# --- normalization -------------------------------------------------------------


def lineage_of(instance: str) -> str:
    """The instance id without its ``-YYYYMMDD[-HHMMSS]`` date suffix."""
    return _DATE_SUFFIX_RE.sub("", instance or "") or (instance or "")


def _tok(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if not isinstance(v, str):
        return NON_CATEGORY
    v = v.strip()
    if not v:
        return None
    if is_credential_shaped(v):
        return REDACTED
    return v if _TOKEN_RE.match(v) else NON_CATEGORY


def _word(v: Any, strip: str) -> Optional[str]:
    """The FIRST whitespace token of a command string, sigil stripped — never args."""
    if not isinstance(v, str) or not v.strip():
        return None
    head = v.strip().split()[0].lstrip(strip)
    return _tok(head) if head else None


def _ts(rec: Dict[str, Any]) -> Optional[float]:
    ts = rec.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    iso = rec.get("ts_iso")
    if isinstance(iso, str):
        try:
            return _dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def derive_verb(rec: Dict[str, Any]) -> Optional[str]:
    kind, source = rec.get("kind"), rec.get("source")
    if kind == "injection" and source == "self-verb":
        return "request_compact" if rec.get("trigger") == "compact" else "inject"
    if kind == "memory-remember" and source == "self-verb":
        return "memory"
    if kind == "transcript-search" and source == "self-verb":
        return "search_transcript"
    if kind == "work-verb" and rec.get("verb") in ("work_add", "work_update", "work_done", "work_drop", "work_list"):
        return str(rec["verb"])
    if kind == "operator-status" and rec.get("setter") not in (None, "operator"):
        return "operator_status_set"
    # one mail verb call = one record; the verb is the record's ``op``.
    if kind == "mail" and source == "self-verb" and rec.get("op") in ("mail_list", "mail_read", "mail_draft", "mail_to_operator"):
        return str(rec["op"])
    if kind == "quiescent" and rec.get("decision") in ("enter", "refused"):
        return "quiesce"
    # one call = one ``skill-sync`` record (dry run, apply or refusal).
    if kind == "skill-sync" and source == "self-verb":
        return "sync_my_skills"
    # one call = one record (tap 1 / tap 2 / confirm / refusal); ``clear`` and ``snooze-expired`` are not calls.
    if kind == "idle-snooze" and rec.get("decision") in (
            "snooze", "question", "dismiss", "refused", "disabled", "already-dismissed"):
        return "idle_snooze"
    # one call = one ``alert`` record (raised / deduped / rate-limited / refused); a ``disabled`` call
    # writes none (the handler is not bound), so it is not counted.
    if kind == "alert" and source == "self-verb" and rec.get("decision") in (
            "raised", "deduped", "rate-limited", "refused"):
        return "alert"
    # ONE attempt = the terminal record (a confirm-twice first call is one); ``started`` and ``config-invalid`` are not.
    if (kind == "restart-service" and source == "harness"
            and rec.get("decision") in ("refused", "confirm-requested", "restarted", "failed")):
        return "restart_service"
    # One ATTEMPT = the parent's launch-ledger record (gate-written, so it carries
    # ``instance_id``) with decision ``reserve`` (granted) or ``deny`` (refused).
    # ``launched``/``released`` are follow-ups, and the client launcher's own
    # records (no ``instance_id``) mirror the same attempt — counting them would
    # double-count.
    if (kind == "agent-launch" and rec.get("launcher_kind") == "agent" and "instance_id" in rec
            and rec.get("decision") in ("reserve", "deny")):
        return "launch_agent"
    return None


_REGISTERED_CMDS: Optional[Set[str]] = None


def registered_commands() -> Set[str]:
    """The registered operator \\commands, derived from the code once per process."""
    global _REGISTERED_CMDS
    if _REGISTERED_CMDS is None:
        _REGISTERED_CMDS = set(known_categories()["cmd"])
    return _REGISTERED_CMDS


def _harness_generated(rec: Dict[str, Any]) -> bool:
    """Content the HARNESS composed (so its slash word is code, not typed text).
    Operator / bus / broadcast / dsd-answer text and the ``inject`` self-verb's
    text are authored by a person or an agent."""
    src = rec.get("source")
    if src in ("monitor-guidance", "bootstrap", "resume", "harness"):
        return True
    # The self-verb branch must be exactly the harness's own /compact: a raw frame
    # on the self socket can set trigger='compact' with any parsed_command (X7
    # fixpass audit N2), so the slash word is trusted only when it IS /compact.
    return (src == "self-verb" and rec.get("trigger") == "compact"
            and isinstance(rec.get("parsed_command"), str)
            and rec.get("parsed_command").strip() == "/compact")


def normalize(rec: Dict[str, Any], registered: Optional[Set[str]] = None) -> Optional[Dict[str, Any]]:
    """Category-only projection of one provenance record (no content, ever)."""
    if not isinstance(rec, dict):
        return None
    registered = registered_commands() if registered is None else registered
    kind = _tok(rec.get("kind")) or "<no-kind>"
    # agent-def-render's ``source`` is a filesystem PATH, not an injection source
    source = None if rec.get("kind") == "agent-def-render" else _tok(rec.get("source"))
    command = None
    if rec.get("kind") == "operator-command":
        w = _word(rec.get("command"), "\\")
        command = w if w is None or w in registered else UNREGISTERED
    slash = _word(rec.get("parsed_command"), "/")
    if slash is not None and not _harness_generated(rec):
        slash = TYPED
    subtype = None
    for key in ("point", "event", "control", "launcher_kind", "memory_type", "surface"):
        if rec.get(key) is not None:
            subtype = _tok(rec.get(key))
            break
    return {
        "ts": _ts(rec),
        "kind": kind,
        "source": source,
        "trigger": _tok(rec.get("trigger")),
        "decision": _tok(rec.get("decision")),
        "route": _tok(rec.get("route")),
        "subtype": subtype,
        "verb": derive_verb(rec),
        "command": command,
        "slash": slash,
        "pollution": classify_pollution(rec),
    }


# --- schema ----------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    session TEXT PRIMARY KEY, session_dir TEXT NOT NULL, root TEXT NOT NULL,
    instance TEXT NOT NULL, lineage TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, session TEXT NOT NULL,
    dev INTEGER, inode INTEGER, offset INTEGER NOT NULL DEFAULT 0,
    head_len INTEGER NOT NULL DEFAULT 0, head_sha TEXT,
    bad_lines INTEGER NOT NULL DEFAULT 0, resets INTEGER NOT NULL DEFAULT 0,
    indexed_at REAL,
    stream TEXT NOT NULL DEFAULT 'injection_log',
    retired INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id),
    byte_offset INTEGER NOT NULL, ts REAL, session TEXT NOT NULL,
    instance TEXT NOT NULL, lineage TEXT NOT NULL, kind TEXT NOT NULL,
    source TEXT, trigger TEXT, decision TEXT, route TEXT, subtype TEXT,
    verb TEXT, command TEXT, slash TEXT, pollution TEXT,
    UNIQUE (file_id, byte_offset));
CREATE INDEX IF NOT EXISTS ix_records_lineage_ts ON records(lineage, ts);
CREATE INDEX IF NOT EXISTS ix_records_kind ON records(kind);
CREATE INDEX IF NOT EXISTS ix_records_verb ON records(verb);
CREATE INDEX IF NOT EXISTS ix_records_session ON records(session);
CREATE INDEX IF NOT EXISTS ix_records_ts ON records(ts);
CREATE TABLE IF NOT EXISTS feature_events (
    id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id),
    byte_offset INTEGER NOT NULL, ts REAL, feature_id TEXT NOT NULL, actor TEXT,
    surface TEXT, outcome TEXT, session_run TEXT, workspace TEXT, project TEXT,
    pollution TEXT, count INTEGER,
    UNIQUE (file_id, byte_offset));
CREATE INDEX IF NOT EXISTS ix_fe_feature_ts ON feature_events(feature_id, ts);
CREATE INDEX IF NOT EXISTS ix_fe_actor_ts ON feature_events(actor, ts);
"""

_REC_COLS = ("ts", "kind", "source", "trigger", "decision", "route", "subtype", "verb",
             "command", "slash", "pollution")


def connect(db_path: str) -> sqlite3.Connection:
    d = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is not None and row[0] != str(SCHEMA_VERSION):
        # The DB is a DERIVED cache of the (read-only) session logs, so an older
        # schema is dropped and rebuilt from byte 0 — never mixed with new rows.
        try:
            older = int(row[0]) < SCHEMA_VERSION
        except ValueError:
            older = False
        if not older:
            con.close()
            raise RuntimeError(f"usage DB {db_path} has schema_version {row[0]}, this code is "
                               f"{SCHEMA_VERSION} — refusing to mix (move the DB aside)")
        con.executescript("DROP TABLE IF EXISTS feature_events; DROP TABLE IF EXISTS records; "
                          "DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS sessions; DELETE FROM meta;")
        con.commit()
        # DROP leaves the old rows' bytes in free pages; an older schema may hold
        # a typed word (X7 audit F2), so the file is rewritten.
        con.execute("VACUUM")
        row = None
    con.executescript(_SCHEMA)
    if row is None:
        con.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        con.commit()
    return con


# --- indexer -----------------------------------------------------------------------


def _instance_for(session_dir: str) -> str:
    lp = os.path.join(session_dir, ".claude", "launch.json")
    try:
        with open(lp, "r", encoding="utf-8") as fh:
            iid = json.load(fh).get("instance_id")
        if isinstance(iid, str) and iid.strip():
            return iid.strip()
    except (OSError, ValueError, AttributeError):
        pass
    return os.path.basename(os.path.normpath(session_dir))


def discover(roots: Sequence[str]) -> Tuple[List[Tuple[str, str]], List[Dict[str, Any]]]:
    """``[(root, log_path)]`` plus a per-root census (exists / session dirs / logs)."""
    found: List[Tuple[str, str]] = []
    census: List[Dict[str, Any]] = []
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        entry = {"root": root, "exists": os.path.isdir(root), "session_dirs": 0, "logs": 0}
        if entry["exists"]:
            for name in sorted(os.listdir(root)):
                sd = os.path.join(root, name)
                if not os.path.isdir(sd):
                    continue
                entry["session_dirs"] += 1
                lp = os.path.join(sd, LOG_REL)
                if os.path.isfile(lp):
                    entry["logs"] += 1
                    found.append((root, lp))
        census.append(entry)
    return found, census


def _head_sha(fh, n: int) -> str:
    fh.seek(0)
    h = hashlib.sha256()
    left = n
    while left > 0:
        b = fh.read(min(_CHUNK_BYTES, left))
        if not b:
            break
        h.update(b)
        left -= len(b)
    return h.hexdigest()


class _LineStream:
    """Bounded streaming of the COMPLETE lines of a file's unread tail, shared by both indexers
 (X26 one streaming loop with a per-stream normaliser, not a copy).

    Iterating yields ``(line_offset, offset_after_line, raw)``; ``raw`` is None for an OVERSIZED
    line (skipped, never buffered: the caller counts it bad). A partial trailing line (no newline)
    is never yielded: :attr:`consumed` stops before it. Memory is at most one chunk plus one
    bounded partial line (X7 audit F5)."""

    def __init__(self, fh, offset: int) -> None:
        self._fh = fh
        self.consumed = offset  # file offset just past the last COMPLETE line

    def __iter__(self):
        fh = self._fh
        fh.seek(self.consumed)
        buf = b""
        base = self.consumed   # file offset of buf[0]
        skipping = False       # inside an oversized line: drop bytes until its newline
        while True:
            chunk = fh.read(_CHUNK_BYTES)
            if not chunk:
                break
            data = buf + chunk
            pos = 0
            while True:
                nl = data.find(b"\n", pos)
                if nl < 0:
                    break
                raw, line_off = data[pos:nl], base + pos
                pos = nl + 1
                self.consumed = base + pos
                if skipping or nl - (line_off - base) > _MAX_LINE_BYTES:
                    skipping = False
                    yield line_off, self.consumed, None
                    continue
                yield line_off, self.consumed, raw
            buf = data[pos:]
            base += pos
            if len(buf) > _MAX_LINE_BYTES:
                # an oversized partial line: never buffer it; skip to its newline
                skipping = True
                base += len(buf)
                buf = b""


def _file_row(con: sqlite3.Connection, fh, path: str, session: str, st: os.stat_result, stream: str,
              stats: Dict[str, int], new_key: str, reset_key: str) -> Tuple[int, int]:
    """The ``files`` row for ``path`` -> ``(file_id, offset)``, creating it, or RESETTING it (rows
    dropped, offset 0) when the path now holds a different inode, a shorter file or rewritten head
    bytes. Shared by both streams (their stats keys differ)."""
    row = con.execute("SELECT id, dev, inode, offset, head_len, head_sha FROM files WHERE path=?",
                      (path,)).fetchone()
    if row is None:
        cur = con.execute("INSERT INTO files(path, session, dev, inode, stream) VALUES (?, ?, ?, ?, ?)",
                          (path, session, st.st_dev, st.st_ino, stream))
        stats[new_key] += 1
        return cur.lastrowid, 0
    file_id, dev, inode, offset, head_len, head_sha = row
    rotated = (dev, inode) != (st.st_dev, st.st_ino)
    truncated = st.st_size < offset or st.st_size < head_len
    rewritten = (not rotated and not truncated and head_len
                 and _head_sha(fh, head_len) != head_sha)
    if rotated or truncated or rewritten:
        con.execute("DELETE FROM records WHERE file_id=?", (file_id,))
        con.execute("DELETE FROM feature_events WHERE file_id=?", (file_id,))
        con.execute("UPDATE files SET offset=0, head_len=0, head_sha=NULL, bad_lines=0, "
                    "resets=resets+1, dev=?, inode=? WHERE id=?",
                    (st.st_dev, st.st_ino, file_id))
        stats[reset_key] += 1
        return file_id, 0
    return file_id, offset


def _index_file(con: sqlite3.Connection, root: str, path: str, stats: Dict[str, int],
                registered: Set[str]) -> None:
    session_dir = os.path.dirname(os.path.dirname(path))
    session = os.path.basename(session_dir)
    instance = _instance_for(session_dir)
    lineage = lineage_of(instance)
    con.execute("INSERT OR REPLACE INTO sessions(session, session_dir, root, instance, lineage) "
                "VALUES (?, ?, ?, ?, ?)", (session, session_dir, root, instance, lineage))
    st = os.stat(path)
    insert_sql = ("INSERT OR IGNORE INTO records(file_id, byte_offset, session, instance, lineage, "
                  + ", ".join(_REC_COLS) + ") VALUES (?, ?, ?, ?, ?, "
                  + ", ".join("?" * len(_REC_COLS)) + ")")
    added = bad = 0
    with open(path, "rb") as fh:  # READ-ONLY, always
        file_id, offset = _file_row(con, fh, path, session, st, "injection_log", stats,
                                    "files_new", "files_reset")

        def _checkpoint(consumed: int, n_bad: int) -> None:
            con.execute("UPDATE files SET offset=?, bad_lines=bad_lines+? WHERE id=?",
                        (consumed, n_bad, file_id))
            con.commit()

        stream = _LineStream(fh, offset)
        pending = 0
        bad_since_commit = 0
        for line_off, consumed, raw in stream:
            if raw is None:      # oversized line
                bad += 1
                bad_since_commit += 1
                continue
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                bad += 1
                bad_since_commit += 1
                continue
            n = normalize(rec, registered)
            if n is None:
                bad += 1
                bad_since_commit += 1
                continue
            cur = con.execute(insert_sql, (file_id, line_off, session, instance, lineage,
                                           *[n[c] for c in _REC_COLS]))
            added += cur.rowcount
            pending += 1
            if pending >= _COMMIT_EVERY:
                _checkpoint(consumed, bad_since_commit)
                pending = bad_since_commit = 0
        consumed = stream.consumed
        new_head_len = min(_HEAD_BYTES, consumed)
        new_head_sha = _head_sha(fh, new_head_len) if new_head_len else None
    con.execute("UPDATE files SET offset=?, head_len=?, head_sha=?, bad_lines=bad_lines+?, "
                "session=?, dev=?, inode=?, indexed_at=strftime('%s','now') WHERE id=?",
                (consumed, new_head_len, new_head_sha, bad_since_commit, session, st.st_dev,
                 st.st_ino, file_id))
    con.commit()
    stats["records_added"] += added
    stats["bad_lines"] += bad


def index(db_path: Optional[str] = None, roots: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Incrementally index every session log under ``roots`` into ``db_path``."""
    db_path = db_path or default_db_path()
    roots = list(roots) if roots else list(default_roots())
    logs, census = discover(roots)
    if not any(c["exists"] for c in census):
        raise FileNotFoundError(f"no session root exists: {[c['root'] for c in census]}")
    stats: Dict[str, Any] = {"files_seen": len(logs), "files_new": 0, "files_reset": 0,
                             "records_added": 0, "bad_lines": 0, "roots": census}
    registered = registered_commands()
    con = connect(db_path)
    try:
        for root, path in logs:
            _index_file(con, root, path, stats, registered)
    finally:
        con.close()
    return stats


# --- X26: the feature_events stream --------------------------------------------------------

#: the sampled emitter-flag history and the index-run clock live in ``meta`` (
#: requirement-denominator: the report states whether the emitter was ON for the whole window)
META_FLAG_LOG = "fe_emitter_flag_log"
META_RUN_AT = "fe_index_run_at"
EVENT_STREAM = "feature_events"
_EVENT_COLS = ("ts", "feature_id", "actor", "surface", "outcome", "session_run", "workspace", "project",
               "pollution", "count")


def default_events_root() -> str:
    from . import feature_events as _fe

    return _fe.event_root()


def _cred(v: str) -> bool:
    return bool(_CRED_PREFIX_RE.match(v))


def _shaped(v: Any, rx: "re.Pattern[str]") -> Optional[str]:
    """None for an absent value; the value itself iff it is a string that is not credential-shaped and
    matches ``rx``; REDACTED for a vendor-prefixed credential shape; NON_CATEGORY otherwise. Never the raw
    offending value."""
    if v is None:
        return None
    if not isinstance(v, str):
        return NON_CATEGORY
    if _cred(v):
        return REDACTED
    return v if rx.match(v) else NON_CATEGORY


def normalize_feature_event(rec: Any) -> Optional[Dict[str, Any]]:
    """The categories-only projection of ONE event line (requirement-categories-only): an
    ALLOWLIST (every other key is dropped, never passed through), every field validated by shape, a
    vendor-prefixed credential redacted BEFORE anything is written. ``actor`` and ``session_run`` skip the
    generic entropy test on purpose (a dated instance id looks like a secret to it; measured 2026-09-30) and
    keep only the vendor-prefix test plus their shape. Returns None for a non-object line;
    ``_future_version`` marks an event with ``v`` > 1 (indexed under the v1 allowlist)."""
    if not isinstance(rec, dict):
        return None
    from . import feature_events as fe

    ver = rec.get("v")
    future = isinstance(ver, int) and not isinstance(ver, bool) and ver > fe.SCHEMA_VERSION
    ts = rec.get("ts")
    ts = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts) else None
    fid = _shaped(rec.get("feature_id"), fe.FEATURE_ID_RE) or NON_CATEGORY
    actor = _shaped(rec.get("actor"), fe.ACTOR_RE)
    session_run = _shaped(rec.get("session_run"), fe.ACTOR_RE)
    surface = rec.get("surface")
    reserved = fid in fe.RESERVED_FEATURE_IDS
    if isinstance(surface, str) and _cred(surface):
        surface = REDACTED
    elif surface in fe.SURFACES and not reserved:
        pass
    elif reserved and surface in fe.KINDS:
        pass            # the one exception (R1): a reserved summary event's surface is the process KIND
    else:
        surface = NON_CATEGORY
    outcome = rec.get("outcome")
    if outcome is None:
        pass
    elif isinstance(outcome, str) and _cred(outcome):
        outcome = REDACTED
    elif outcome in fe.OUTCOMES and not (outcome == "noop" and surface != "hook"):
        pass
    else:
        outcome = NON_CATEGORY
    workspace = _shaped(rec.get("workspace"), fe.SLUG_RE)
    project = rec.get("project")
    if project is not None:
        if not isinstance(project, str):
            project = NON_CATEGORY
        else:
            halves = project.split("/")
            if _cred(project) or any(_cred(h) for h in halves):
                project = REDACTED
            elif not (len(halves) == 2 and all(fe.SLUG_RE.match(h) for h in halves)):
                project = NON_CATEGORY
    count = None
    if reserved:
        c = rec.get("count")
        if isinstance(c, int) and not isinstance(c, bool) and c >= 0:
            count = c
        else:
            fid = NON_CATEGORY   # a summary event WITHOUT a valid integer count fails the schema
    return {"ts": ts, "feature_id": fid, "actor": actor, "surface": surface, "outcome": outcome,
            "session_run": session_run, "workspace": workspace, "project": project,
            "pollution": "probe" if rec.get("probe") is True else None, "count": count,
            "_future_version": future}


def discover_events(roots: Sequence[str]) -> Tuple[List[Tuple[str, str]], List[Dict[str, Any]]]:
    """``[(root, jsonl_path)]`` plus a per-root census. Find-style traversal that survives a symlinked
    root (a data root may be a symlink): ``os.walk`` on the path as given, regular ``*.jsonl`` files at
    any depth under it. A MISSING root is not an error (no events yet) and is reported ``exists: false``."""
    found: List[Tuple[str, str]] = []
    census: List[Dict[str, Any]] = []
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        entry = {"root": root, "exists": os.path.isdir(root), "files": 0}
        if entry["exists"]:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                for fn in sorted(filenames):
                    if fn.endswith(".jsonl"):
                        p = os.path.join(dirpath, fn)
                        if os.path.isfile(p):
                            entry["files"] += 1
                            found.append((root, p))
        census.append(entry)
    return found, census


def _index_event_file(con: sqlite3.Connection, path: str, stats: Dict[str, int]) -> None:
    st = os.stat(path)
    insert_sql = ("INSERT OR IGNORE INTO feature_events(file_id, byte_offset, " + ", ".join(_EVENT_COLS)
                  + ") VALUES (?, ?, " + ", ".join("?" * len(_EVENT_COLS)) + ")")
    added = bad = 0
    with open(path, "rb") as fh:  # READ-ONLY, always
        file_id, offset = _file_row(con, fh, path, "", st, EVENT_STREAM, stats,
                                    "events_files_new", "events_files_reset")
        con.execute("UPDATE files SET retired=0 WHERE id=?", (file_id,))

        def _checkpoint(consumed: int, n_bad: int) -> None:
            con.execute("UPDATE files SET offset=?, bad_lines=bad_lines+? WHERE id=?",
                        (consumed, n_bad, file_id))
            con.commit()

        stream = _LineStream(fh, offset)
        pending = 0
        bad_since_commit = 0
        for line_off, consumed, raw in stream:
            if raw is None:
                bad += 1
                bad_since_commit += 1
                continue
            if not raw.strip():
                continue
            text = raw.decode("utf-8", "replace")
            try:
                rec = json.loads(text)
            except ValueError:
                # a torn write (short write) leaves a newline-less fragment that the NEXT event is appended to
                # (Diamond F-D): count the fragment as bad, but resync at the last record start so the good event survives
                bad += 1
                bad_since_commit += 1
                i = text.rfind('{"v":')
                if i <= 0:
                    continue
                try:
                    rec = json.loads(text[i:])
                except ValueError:
                    continue
            n = normalize_feature_event(rec)
            if n is None:
                bad += 1
                bad_since_commit += 1
                continue
            if n.pop("_future_version"):
                stats["future_version_events"] += 1
            cur = con.execute(insert_sql, (file_id, line_off, *[n[c] for c in _EVENT_COLS]))
            added += cur.rowcount
            pending += 1
            if pending >= _COMMIT_EVERY:
                _checkpoint(consumed, bad_since_commit)
                pending = bad_since_commit = 0
        consumed = stream.consumed
        new_head_len = min(_HEAD_BYTES, consumed)
        new_head_sha = _head_sha(fh, new_head_len) if new_head_len else None
    con.execute("UPDATE files SET offset=?, head_len=?, head_sha=?, bad_lines=bad_lines+?, "
                "dev=?, inode=?, indexed_at=strftime('%s','now') WHERE id=?",
                (consumed, new_head_len, new_head_sha, bad_since_commit, st.st_dev, st.st_ino, file_id))
    con.commit()
    stats["events_added"] += added
    stats["events_bad_lines"] += bad


def _record_flag_sample(con: sqlite3.Connection, state: Optional[bool], now: float) -> None:
    """Append the emitter flag's CURRENT state to the history when it differs from the last sample."""
    if state is None:
        return
    row = con.execute("SELECT value FROM meta WHERE key=?", (META_FLAG_LOG,)).fetchone()
    try:
        log = json.loads(row[0]) if row else []
    except ValueError:
        log = []
    if not log or bool(log[-1][1]) != bool(state):
        log.append([now, bool(state)])
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (META_FLAG_LOG, json.dumps(log)))


def index_feature_events(db_path: Optional[str] = None, events_roots: Optional[Sequence[str]] = None, *,
                         emitter_flag: Optional[bool] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Incrementally load the feature-event JSONL under ``events_roots`` into ``feature_events`` with the
    same (inode, offset, head-hash) guarantees as the X7 stream (idempotent; a partial trailing line is left
    for the next run; truncate / rotate / rewrite-in-place resets that file). EXCEPTION: a file whose path no
    longer exists (the 90-day retention job deleted it) is RETIRED and its rows KEPT, never treated as a
    rotation. Files are opened read-only; nothing is written under the event root. ``emitter_flag`` is the
    emitter flag's current state, sampled into ``meta`` for the report."""
    import time as _time

    db_path = db_path or default_db_path()
    roots = list(events_roots) if events_roots else [default_events_root()]
    now = _time.time() if now is None else now
    found, census = discover_events(roots)
    stats: Dict[str, Any] = {"events_files_seen": len(found), "events_files_new": 0, "events_files_reset": 0,
                             "events_files_retired": 0, "events_added": 0, "events_bad_lines": 0,
                             "future_version_events": 0, "bad_files": 0, "events_roots": census}
    con = connect(db_path)
    try:
        for _root, path in found:
            try:
                _index_event_file(con, path, stats)
            except OSError:
                stats["bad_files"] += 1
                con.rollback()
        live = {p for _r, p in found}
        for fid, path in con.execute("SELECT id, path FROM files WHERE stream=? AND retired=0",
                                     (EVENT_STREAM,)).fetchall():
            if path not in live and not os.path.exists(path):
                con.execute("UPDATE files SET retired=1 WHERE id=?", (fid,))   # rows KEPT (vanished != rotated)
                stats["events_files_retired"] += 1
        _record_flag_sample(con, emitter_flag, now)
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (META_RUN_AT, repr(float(now))))
        con.commit()
    finally:
        con.close()
    return stats


# --- known categories (derived from the code) -----------------------------------------


def _pkg_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _iter_sources() -> Iterable[Tuple[str, ast.AST]]:
    for dirpath, dirnames, filenames in os.walk(_pkg_dir()):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "tests")]
        for fn in filenames:
            if not fn.endswith(".py"):  # .py.bak-* snapshots are excluded by this
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    yield p, ast.parse(fh.read(), filename=p)
            except (OSError, SyntaxError, ValueError):
                continue


def _module_str_consts(tree: ast.AST) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            val = node.value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                for t in targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = val.value
    return out


def _str_of(node: ast.AST, consts: Dict[str, str]) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _scan_code(sources: Optional[Sequence[Tuple[str, ast.AST]]] = None) -> Tuple[Set[str], Set[str]]:
    """Static scan: provenance record KINDS (``X.record("kind", ...)`` and
    ``append_record(path, {"kind": ...})``) and operator \\commands
    (``tap.register("name", ...)``). ``sources`` = already-parsed ``(path, tree)`` pairs (X26: one parse is
    shared with the feature call-site scan); None parses the package."""
    kinds: Set[str] = set()
    cmds: Set[str] = set()
    for _path, tree in (sources if sources is not None else _iter_sources()):
        consts = _module_str_consts(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "record" and node.args:
                s = _str_of(node.args[0], consts)
                if s:
                    kinds.add(s)
            elif isinstance(f, ast.Attribute) and f.attr == "register" and node.args and \
                    isinstance(f.value, ast.Name) and f.value.id == "tap":
                s = _str_of(node.args[0], consts)
                if s:
                    cmds.add(s)
            elif isinstance(f, ast.Name) and f.id == "append_record" and len(node.args) >= 2 and \
                    isinstance(node.args[1], ast.Dict):
                for k, v in zip(node.args[1].keys, node.args[1].values):
                    if k is not None and _str_of(k, consts) == "kind":
                        s = _str_of(v, consts)
                        if s:
                            kinds.add(s)
    return kinds, cmds


def known_categories() -> Dict[str, Set[str]]:
    """What the harness CAN emit, derived from the code — never a hand list."""
    from .harness import hooks, optap, self_mcp

    verbs = set(self_mcp.TOOL_NAMES) | {self_mcp.LAUNCH_AGENT_TOOL["name"],
                                        self_mcp.QUIESCE_TOOL["name"],
                                        self_mcp.SEARCH_TRANSCRIPT_TOOL["name"],
                                        self_mcp.RESTART_SERVICE_TOOL["name"],
                                        self_mcp.IDLE_SNOOZE_TOOL["name"],
                                        self_mcp.PLAN_USAGE_TOOL["name"],
                                        self_mcp.ALERT_TOOL["name"], self_mcp.SYNC_MY_SKILLS_TOOL["name"]} | set(self_mcp.DSD_SELF_TOOL_NAMES) | set(self_mcp._OWL_NAMES) | {t["name"] for t in self_mcp.MAIL_TOOLS}
    parsed = list(_iter_sources())                      # parse the package ONCE for both scans
    kinds, cmds = _scan_code(parsed)
    from . import feature_scan

    features, _dynamic = feature_scan.scan_call_sites(parsed)   # X26: the ids found at feature_event call sites
    return {"source": set(hooks.SOURCES), "verb": verbs, "kind": kinds,
            "cmd": cmds | set(optap.RESERVED_COMMANDS), "feature": features}


# --- report -------------------------------------------------------------------------------


def _iso_date(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d")


def _parse_when(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"not an ISO date/datetime: {s!r}") from exc
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


def load_rules(path: str) -> Dict[str, Dict[str, float]]:
    """``{"<family>:<name>": {"min_per_session": <number>}}`` — fails LOUD on any typo."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("rules file must be a JSON object")
    out: Dict[str, Dict[str, float]] = {}
    for cat, rule in data.items():
        if not isinstance(cat, str) or ":" not in cat or cat.split(":", 1)[0] not in FAMILIES:
            raise ValueError(f"rule key {cat!r} must be '<family>:<name>' with family in {FAMILIES}")
        if not isinstance(rule, dict) or set(rule) != {"min_per_session"}:
            raise ValueError(f"rule {cat!r} must be exactly {{'min_per_session': <number>}}")
        v = rule["min_per_session"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"rule {cat!r} min_per_session must be a non-negative number")
        out[cat] = {"min_per_session": float(v)}
    return out


def _categories_of(row: Dict[str, Any]) -> List[str]:
    cats = [f"kind:{row['kind']}"]
    if row["kind"] == "injection":
        src = row["source"] or "<none>"
        cats.append(f"source:{src}")
        cats.append(f"trigger:{src}/{row['trigger'] or '<none>'}")
        if row["slash"]:
            cats.append(f"slash:{row['slash']}")
    if row["verb"]:
        cats.append(f"verb:{row['verb']}")
    if row["command"]:
        cats.append(f"cmd:{row['command']}")
    return cats


def _cat_sort_key(cat: str) -> Tuple[int, str]:
    fam = cat.split(":", 1)[0]
    return (FAMILIES.index(fam) if fam in FAMILIES else len(FAMILIES), cat)


def build_report(db_path: Optional[str] = None, since: Optional[str] = None,
                 until: Optional[str] = None, rules: Optional[Dict[str, Dict[str, float]]] = None,
                 min_sessions: int = DEFAULT_MIN_SESSIONS,
                 known: Optional[Dict[str, Set[str]]] = None,
                 include_lineages: Optional[Sequence[str]] = None,
                 exclude_lineages: Optional[Sequence[str]] = None,
                 include_test_pollution: bool = False) -> Dict[str, Any]:
    """Build the report. ``include_lineages`` / ``exclude_lineages`` are regexes
    (``re.search``) that scope WHICH lineages are compared — e.g. the agent
    lineages only, without probe/test sessions diluting the peer set."""
    db_path = db_path or default_db_path()
    lo, hi = _parse_when(since), _parse_when(until)
    inc = [re.compile(p) for p in (include_lineages or [])]
    exc = [re.compile(p) for p in (exclude_lineages or [])]

    def _keep(lin: str) -> bool:
        if inc and not any(r.search(lin) for r in inc):
            return False
        return not any(r.search(lin) for r in exc)

    known = known_categories() if known is None else known
    con = connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        where, params = [], []
        if lo is not None:
            where.append("ts >= ?")
            params.append(lo)
        if hi is not None:
            where.append("ts < ?")
            params.append(hi)
        sql = "SELECT lineage, session, " + ", ".join(_REC_COLS) + " FROM records"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = [dict(r) for r in con.execute(sql, params)]
        excluded = sorted({r["lineage"] for r in rows if not _keep(r["lineage"])})
        rows = [r for r in rows if _keep(r["lineage"])]
        pollution: Dict[str, int] = {}
        if not include_test_pollution:
            for r in rows:
                if r["pollution"]:
                    pollution[r["lineage"]] = pollution.get(r["lineage"], 0) + 1
            rows = [r for r in rows if not r["pollution"]]
        untimed = con.execute("SELECT COUNT(*) FROM records WHERE ts IS NULL").fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        # X26: the shared `files` table also holds the feature_events stream; the X7 report counts its own.
        bad_lines = con.execute("SELECT COALESCE(SUM(bad_lines), 0) FROM files "
                                "WHERE stream='injection_log'").fetchone()[0]
        n_files = con.execute("SELECT COUNT(*) FROM files WHERE stream='injection_log'").fetchone()[0]
    finally:
        con.close()

    sessions: Dict[str, Set[str]] = {}
    records: Dict[str, int] = {}
    counts: Dict[str, Dict[str, int]] = {}
    for r in rows:
        lin = r["lineage"]
        sessions.setdefault(lin, set()).add(r["session"])
        records[lin] = records.get(lin, 0) + 1
        for c in _categories_of(r):
            counts.setdefault(c, {})
            counts[c][lin] = counts[c].get(lin, 0) + 1
    lineages = sorted(sessions)
    denom = {lin: {"sessions": len(sessions[lin]), "records": records[lin],
                   "low_denominator": len(sessions[lin]) < min_sessions} for lin in lineages}
    cats = sorted(counts, key=_cat_sort_key)
    matrix = {c: {lin: counts[c].get(lin, 0) for lin in lineages} for c in cats}

    zero_flags: List[Dict[str, Any]] = []
    for c in cats:
        users = {lin: n for lin, n in matrix[c].items() if n > 0}
        if not users or len(users) == len(lineages):
            continue
        for lin in lineages:
            if matrix[c][lin] == 0:
                zero_flags.append({"type": "zero-where-peers-nonzero", "category": c, "lineage": lin,
                                   "peers_nonzero": users, "sessions": denom[lin]["sessions"],
                                   "low_denominator": denom[lin]["low_denominator"],
                                   "label": SIGNAL_LABEL})

    never: List[Dict[str, Any]] = []
    seen_all = set(counts)
    for fam in ("source", "verb", "cmd", "kind"):
        for name in sorted(known.get(fam, ())):
            cat = f"{fam}:{name}"
            if cat in seen_all:
                continue
            observable = not (fam == "verb" and VERB_LOGGED_AS.get(name, "") is None)
            never.append({"type": "never-seen", "category": cat, "observable": observable,
                          "note": ("usage is observable in the provenance log" if observable else
                                   "the harness writes NO record for this verb — unobservable, "
                                   "not necessarily unused"),
                          "label": SIGNAL_LABEL})

    below: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for cat, rule in sorted((rules or {}).items()):
        fam, name = cat.split(":", 1)
        if cat not in seen_all and name not in known.get(fam, set()):
            warnings.append(f"rule category {cat!r} is neither seen in the window nor known to the "
                            f"harness — check the rules file for a typo")
        if fam == "verb" and VERB_LOGGED_AS.get(name, "") is None:
            warnings.append(f"rule category {cat!r} is a verb the harness does not log — its rate "
                            f"is unobservable; the rule cannot be evaluated")
            continue
        for lin in lineages:
            n = matrix.get(cat, {}).get(lin, 0)
            rate = n / denom[lin]["sessions"]
            if rate < rule["min_per_session"]:
                below.append({"type": "below-expected-rate", "category": cat, "lineage": lin,
                              "count": n, "sessions": denom[lin]["sessions"],
                              "rate_per_session": round(rate, 3),
                              "min_per_session": rule["min_per_session"],
                              "low_denominator": denom[lin]["low_denominator"],
                              "label": SIGNAL_LABEL})

    return {
        "report": "harness usage metrics",
        "schema_version": SCHEMA_VERSION,
        "db": os.path.abspath(db_path),
        "window": {"since": since, "until": until},
        "lineage_filter": {"include": list(include_lineages or []),
                           "exclude": list(exclude_lineages or []), "excluded": excluded},
        "records_in_window": len(rows),
        "include_test_pollution": include_test_pollution,
        "test_pollution_excluded": pollution,
        "records_total": total,
        "untimed_records": untimed,
        "files_indexed": n_files,
        "bad_lines": bad_lines,
        "min_sessions": min_sessions,
        "denominator": denom,
        "lineages": lineages,
        "categories": cats,
        "matrix": matrix,
        "flags": {"zero_where_peers_nonzero": zero_flags, "never_seen": never,
                  "below_expected_rate": below},
        "rules": sorted(rules or {}),
        "rule_warnings": warnings,
        "label": SIGNAL_LABEL,
    }


def format_report(rep: Dict[str, Any], index_stats: Optional[Dict[str, Any]] = None) -> str:
    L: List[str] = []
    w = rep["window"]
    L.append(f"Harness usage report (every flag below: {SIGNAL_LABEL})")
    L.append(f"db: {rep['db']}")
    L.append(f"window: since={w['since'] or '-'} until={w['until'] or '-'}   records in window: "
             f"{rep['records_in_window']} / total {rep['records_total']}"
             f"   untimed: {rep['untimed_records']}   unparseable lines: {rep['bad_lines']}")
    lf = rep.get("lineage_filter") or {}
    if lf.get("include") or lf.get("exclude"):
        L.append(f"lineage filter: include={lf['include']} exclude={lf['exclude']}  "
                 f"-> excluded {len(lf['excluded'])}: {', '.join(lf['excluded']) or '-'}")
    if index_stats:
        L.append("index: files seen {files_seen}, new {files_new}, reset {files_reset}, "
                 "records added {records_added}, bad lines {bad_lines}".format(**index_stats))
        for c in index_stats.get("roots", []):
            L.append(f"  root {c['root']}: exists={c['exists']} session dirs={c['session_dirs']} "
                     f"with injection_log={c['logs']}")
    tp = rep.get("test_pollution_excluded") or {}
    if tp:
        L.append("test pollution excluded (launch denials targeting a test profile/tmp path; "
                 "--include-test-pollution to keep): "
                 + ", ".join(f"{k}={v}" for k, v in sorted(tp.items())))
    elif not rep.get("include_test_pollution"):
        L.append("test pollution excluded: none found")
    lins = rep["lineages"]
    codes = {lin: f"L{i + 1}" for i, lin in enumerate(lins)}
    L.append("")
    L.append(f"denominator (low-denominator = fewer than {rep['min_sessions']} sessions; a zero "
             "there is not a confident zero):")
    for lin in lins:
        d = rep["denominator"][lin]
        L.append(f"  {codes[lin]:>4}  {lin:<40} sessions {d['sessions']:>3}  records "
                 f"{d['records']:>5}{'  low-denominator' if d['low_denominator'] else ''}")
    L.append("")
    L.append("(a) category x lineage matrix   ('0?' = zero at a low-denominator lineage)")
    width = max([len(c) for c in rep["categories"]] + [8])
    L.append(" " * width + "  " + "".join(f"{codes[lin]:>6}" for lin in lins))
    fam = None
    for c in rep["categories"]:
        f = c.split(":", 1)[0]
        if f != fam and fam is not None:
            L.append("")
        fam = f
        cells = []
        for lin in lins:
            n = rep["matrix"][c][lin]
            cells.append(f"{('0?' if n == 0 and rep['denominator'][lin]['low_denominator'] else n):>6}")
        L.append(f"{c:<{width}}  " + "".join(cells))
    fl = rep["flags"]
    L.append("")
    z = fl["zero_where_peers_nonzero"]
    L.append(f"(b) zero where peers nonzero — {len(z)} [{SIGNAL_LABEL}]")
    for x in z:
        peers = ", ".join(f"{codes.get(k, k)}={v}" for k, v in x["peers_nonzero"].items())
        L.append(f"  {x['category']:<{width}}  {codes[x['lineage']]} {x['lineage']} = 0 "
                 f"(sessions {x['sessions']}{', low-denominator' if x['low_denominator'] else ''})"
                 f"   peers: {peers}")
    L.append("")
    n = fl["never_seen"]
    L.append(f"(c) known to the harness, never seen in the window — {len(n)} [{SIGNAL_LABEL}]")
    for x in n:
        L.append(f"  {x['category']:<{width}}  {'' if x['observable'] else 'UNOBSERVABLE: ' + x['note']}")
    L.append("")
    b = fl["below_expected_rate"]
    if not rep.get("rules"):
        L.append("(d) expected-rate rules — none configured (--rules FILE)")
    else:
        L.append(f"(d) below declared expected rate — {len(b)} [{SIGNAL_LABEL}]")
        for x in b:
            L.append(f"  {x['category']:<{width}}  {x['lineage']}: {x['count']} in "
                     f"{x['sessions']} session(s) = {x['rate_per_session']}/session < "
                     f"{x['min_per_session']}{'  (low-denominator)' if x['low_denominator'] else ''}")
        for wmsg in rep.get("rule_warnings", []):
            L.append(f"  ! {wmsg}")
    return "\n".join(L)


# --- the web panel's payload ((a)) -------------------------------------------

PANEL_SCOPES = ("intelligence", "all")


def intelligence_lineages(rep: Dict[str, Any]) -> List[str]:
    """Lineages launched from an agent DEFINITION (they carry ``agent-def-render``
    records) — i.e. the agents, as opposed to probes / pilots / demos.
    Derived from the data, not a hand list."""
    row = rep["matrix"].get("kind:agent-def-render", {})
    return sorted(lin for lin, n in row.items() if n > 0)


def panel_report(db_path: Optional[str] = None, roots: Optional[Sequence[str]] = None,
                 scope: str = "intelligence", since: Optional[str] = None,
                 min_sessions: int = DEFAULT_MIN_SESSIONS,
                 include_test_pollution: bool = False) -> Dict[str, Any]:
    """Index incrementally, then report — scoped to the agent lineages (the
    PEER SET for zero-where-peers-nonzero) unless ``scope='all'``."""
    db_path = db_path or default_db_path()
    if scope not in PANEL_SCOPES:
        raise ValueError(f"scope must be one of {PANEL_SCOPES}, got {scope!r}")
    stats = index(db_path, roots=roots)
    known = known_categories()  # one code scan per request, shared by both passes
    full = build_report(db_path, since=since, min_sessions=min_sessions, known=known,
                        include_test_pollution=include_test_pollution)
    intel = intelligence_lineages(full)
    if scope == "intelligence":
        pats = ["^(" + "|".join(re.escape(x) for x in intel) + ")$"] if intel else ["^$(?!)"]
        rep = build_report(db_path, since=since, min_sessions=min_sessions,
                           include_lineages=pats, include_test_pollution=include_test_pollution,
                           known=known)
    else:
        rep = full
    rep["scope"] = scope
    rep["intelligence_lineages"] = intel
    rep["all_lineages"] = full["lineages"]
    rep["index"] = stats
    return rep


# --- CLI ------------------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gnomon.usage_metrics",
                                 description="harness usage metrics (read-only on logs).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("index", "report"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=None, help="sqlite path (default <data_root>/metrics/harness_usage.sqlite)")
        p.add_argument("--root", action="append", default=None,
                       help="session root to scan (repeatable; default <data_root>/sessions + the "
                            "deployment's legacy session roots)")
        p.add_argument("--events-root", action="append", default=None, dest="events_root",
                       help="X26: feature-event root to index (repeatable; default the emitter's event root, "
                            "<data_root>/metrics/feature-events)")
        if name == "report":
            p.add_argument("--features", action="store_true",
                           help="X26: the built / valued / unused feature-usage report (joined to the feature "
                                "registry); without it the X7 report is unchanged")
            p.add_argument("--registry", default=None,
                           help="X26 --features: registry corpus directory (default: the machine's gnomon corpus)")
            p.add_argument("--include-probes", action="store_true", dest="include_probes",
                           help="X26 --features: keep probe events (GNOMON_PROBE=1), excluded by default")
            p.add_argument("--since", default=None, help="ISO date/datetime (UTC) lower bound")
            p.add_argument("--until", default=None, help="ISO date/datetime (UTC) upper bound")
            p.add_argument("--json", action="store_true")
            p.add_argument("--rules", default=None, help="expected-rate rules JSON file")
            p.add_argument("--min-sessions", type=int, default=DEFAULT_MIN_SESSIONS)
            p.add_argument("--no-index", action="store_true", help="report from the DB as-is")
            p.add_argument("--lineage", action="append", default=None,
                           help="regex; compare only matching lineages (repeatable)")
            p.add_argument("--include-test-pollution", action="store_true",
                           help="keep launch denials that target a test profile / tmp path")
            p.add_argument("--exclude-lineage", action="append", default=None,
                           help="regex; drop matching lineages from the comparison (repeatable)")
    args = ap.parse_args(argv)

    if args.root:
        missing = [r for r in args.root if not os.path.isdir(os.path.expanduser(r))]
        if missing:
            print(f"usage_metrics: session root(s) do not exist: {missing}", file=sys.stderr)
            raise SystemExit(2)
    if args.events_root:
        missing = [r for r in args.events_root if not os.path.isdir(os.path.expanduser(r))]
        if missing:
            print(f"usage_metrics: feature-event root(s) do not exist: {missing}", file=sys.stderr)
            raise SystemExit(2)
    features = bool(getattr(args, "features", False))
    if features and getattr(args, "no_index", False):
        # an OLD schema would be rebuilt by connect(): with --no-index that is refused, not done silently
        from .feature_report import peek_schema_version

        v = peek_schema_version(args.db or default_db_path())
        if v is not None and v != str(SCHEMA_VERSION):
            print(f"usage_metrics: the usage DB has schema_version {v}, this code is {SCHEMA_VERSION}; run "
                  "`index` (it rebuilds an older DB) or drop --no-index", file=sys.stderr)
            raise SystemExit(2)
    stats = None
    ev_stats = None
    if args.cmd == "index" or not getattr(args, "no_index", False):
        try:
            stats = index(args.db, roots=args.root)
        except FileNotFoundError as exc:
            print(f"usage_metrics: {exc}", file=sys.stderr)
            raise SystemExit(2)
        if args.cmd == "index" or features:
            ev_stats = _index_events_cli(args)
    if args.cmd == "index":
        stats = dict(stats)
        stats["feature_events"] = ev_stats
        print(json.dumps(stats, indent=2))
        return 0
    if features:
        return _features_cli(args, stats, ev_stats)
    try:
        rules = load_rules(args.rules) if args.rules else None
        rep = build_report(args.db, since=args.since, until=args.until, rules=rules,
                           min_sessions=args.min_sessions, include_lineages=args.lineage,
                           exclude_lineages=args.exclude_lineage,
                           include_test_pollution=args.include_test_pollution)
    except (ValueError, OSError, re.error) as exc:
        print(f"usage_metrics: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if stats is not None:
        rep["index"] = stats
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=False))
    else:
        print(format_report(rep, stats))
    return 0


def _emitter_flag_state() -> Optional[bool]:
    try:
        from . import feature_events as fe
        from . import flags

        return bool(flags.flag(fe.FLAG_EVENTS))
    except Exception:  # noqa: BLE001 - an unreadable flag is "no sample", never a guess
        return None


def _index_events_cli(args: Any) -> Dict[str, Any]:
    return index_feature_events(args.db, args.events_root, emitter_flag=_emitter_flag_state())


def _features_cli(args: Any, stats: Optional[Dict[str, Any]], ev_stats: Optional[Dict[str, Any]]) -> int:
    from . import feature_report as FR
    from .feature_registry import RegistryError

    try:
        rep = FR.build_feature_report(args.db, args.registry, since=args.since, until=args.until,
                                      min_sessions=args.min_sessions, include_probes=args.include_probes,
                                      index_stats=({"injection_log": stats, "feature_events": ev_stats}
                                                   if (stats is not None or ev_stats is not None) else None))
    except RegistryError as exc:
        print(f"usage_metrics: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"usage_metrics: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=False))
    else:
        print(FR.format_feature_report(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
