"""The central VERB POLICY: who may call which verb, and what happens to everyone else
(redirect 2: efficiency and fully consistent execution).

ONE module, ONE policy file, ONE entry point::

    verb_policy.check(verb, caller_id, target=None, ack=None, ctx=None) -> Decision

``Decision.outcome`` is ``allow`` | ``confirm`` | ``deny``. A verb's implementation asks
the policy; it never hard-codes a caller list. Stdlib only (``tomllib``; the harness is
stdlib-only, so TOML and not YAML).

Policy file (TOML), one ``[verbs.<name>]`` table per verb::

    schema = 1
    [verbs.restart_service]
    always  = ["alpha-architect"]     # ids always allowed
    others  = "confirm-twice"         # allow | warn | confirm-twice | deny   (everyone else)
    warning = "... {what} ..."        # the text of a warn / confirm-twice verb
    require_ack = true                # confirm-twice: the 2nd call needs a non-empty ack (default true)
    alert_to = ["alpha-architect"]    # warn only: also notify these sessions (hub notice path)
    confirm_window_seconds = 600      # optional; default 600, floor 10, ceiling 3600 (g27b)
    when    = ["context_below(85)"]   # optional; the restriction applies ONLY while all hold (closed set)
    [verbs.restart_service.targets.gnomon-web]   # optional per-target override; any subset of
    others  = "deny"                             # always / others / warning / require_ack / alert_to /
                                                 # when (the rest inherited from the verb)

``warning`` placeholders (the only ones allowed): ``{verb}``, ``{target}``, ``{what}``
(``{what}`` = ``verb(target)``, or just ``verb`` with no target).

WHERE THE POLICY LIVES (three layers, merged PER VERB, later wins): (1) the file shipped with the package
(``harness/verb_policy.toml``, ``Policy.default_path``) is the NEUTRAL default: it names no role, so everyone
confirms twice for ``restart_service``. (2) The DEPLOYMENT file ``<deploy_root>/defaults/verb-policy.toml``
(``Policy.deploy_path``; X13 S5-3) is where a deployment says WHO is allowlisted. (3) The MACHINE-CONFIG location
``<runtime_root>/verb-policy.toml`` (beside ``config.json``; ``Policy.override_path``) is the operator's last word.
A verb present in a later layer replaces that verb's WHOLE table; a verb it does not mention keeps the earlier
layer's. An override cannot remove a verb; use ``others = "deny"`` to restrict one. ``Policy.verb_sources`` records
which file each verb came from, and ``source_path`` is the last layer merged. ``REQUIRED_VERBS`` (every verb the
harness exposes) must all be present in the EFFECTIVE policy or it is INVALID. A file (any layer) that does not
parse or validate makes the policy INVALID: every ``check`` answers ``deny`` (reason ``policy-invalid``, naming the
file and the fault), the fault is reported loudly by the launcher, and a launch is NEVER broken (g27b f). There is
no silent fallback to an earlier layer when a later one is bad, and an ABSENT deployment file is simply the neutral
default (a fresh install).

MODES for everyone not in ``always``: ``allow`` (proceed); ``warn`` (PROCEED on the first call,
and the engine alerts: the warning is returned in the verb's result and logged as a
provenance record, and ``alert_to`` sessions, if named, get a notice); ``confirm-twice``
(below); ``deny`` (hidden and refused).

``when`` scopes the RESTRICTION: while every named predicate holds (an unevaluable predicate
counts as holding, the safe direction) ``others`` applies; while one does not, ``others`` is
``allow``. ``always`` is never affected. So D50's compact gate is later expressible as
``request_compact: others = "confirm-twice", when = ["context_below(85)"]``; it is NOT migrated
in this build, and ``context_below`` is only a RESERVED slot (the loader rejects any file that
references it until it is implemented).

CONFIRM-TWICE: the first call by a non-``always`` caller returns the warning and does
NOTHING (state: an in-memory pending entry, keyed by caller + verb + target, in THIS
process). A second call with the same key inside the window (default 10 min) carrying a
non-empty ``ack`` reason is ``allow`` (reason ``confirmed``); the caller logs caller, verb,
target and the ack. A second call without an ack is refused and does NOT extend the
window (unless the entry sets ``require_ack = false``: then the second call proceeds, logged as
confirmed with an empty ack); an expired window restarts at a first call. A successful confirmation is consumed.

Extension points (checklist g32/g33): every verb the harness exposes (self-verbs,
hub MCP tools, operator commands) MUST have an entry in the default file; the guard test
enumerates them from the code. A new predicate is added to PREDICATES in code, never named
in the file first. Nothing here reads the environment; ``ctx`` carries the clock, the
pending store and the predicate facts so tests are deterministic.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

POLICY_FILENAME = "verb-policy.toml"
DEFAULT_POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verb_policy.toml")
SCHEMA = 1

ALLOW, CONFIRM, DENY = "allow", "confirm", "deny"
OTHERS_VALUES = ("allow", "warn", "confirm-twice", "deny")

DEFAULT_WINDOW_SECONDS = 600.0
WINDOW_FLOOR_SECONDS = 10.0
WINDOW_CEILING_SECONDS = 3600.0
MAX_WARNING_CHARS = 1000
MAX_ACK_CHARS = 200
MAX_PENDING = 256
MAX_FILE_BYTES = 256 * 1024

VERB_RE = re.compile(r"\A[a-z][a-z0-9_-]{0,63}\Z")
CALLER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
TARGET_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PLACEHOLDERS = frozenset({"verb", "target", "what"})
_FIELD_RE = re.compile(r"\{([^{}]*)\}")

VERB_KEYS = frozenset({"always", "others", "warning", "confirm_window_seconds", "when", "targets",
                       "require_ack", "alert_to"})
TARGET_KEYS = frozenset({"always", "others", "warning", "when", "require_ack", "alert_to"})
TOP_KEYS = frozenset({"schema", "verbs"})

def _context_below(facts: Mapping[str, Any], args: Tuple[float, ...]) -> bool:
    """RESERVED slot for the later D50 migration (``context_below(85)``): NOT implemented."""
    raise NotImplementedError("the context_below predicate is reserved but not implemented yet")


#: The CLOSED set of named ``when`` predicates: ``name -> fn(facts, args) -> bool``, written in
#: the file as ``"name"`` or ``"name(arg, ...)"``. Implemented in code only: a name that is not
#: here makes the policy invalid. restart_service needs none, so nothing is implemented; the
#: ``context_below`` slot exists (see UNIMPLEMENTED) for the D50 migration of request_compact.
PREDICATES: Dict[str, Callable[[Mapping[str, Any], Tuple[float, ...]], bool]] = {
    "context_below": _context_below,
}
#: EVERY verb the harness exposes (self-verbs, hub MCP tools, operator commands; names shared
#: across surfaces are one entry). The effective policy must contain all of them; the guard test
#: checks this set against verbs enumerated from the code, so a new verb must be added HERE, to the
#: default file, and only then ships.
REQUIRED_VERBS = frozenset({
    "restart_service", "request_compact", "inject", "context", "plan_usage", "memory", "launch_agent", "quiesce",
    "idle_snooze", "alert", "sync_my_skills", "search_transcript", "mail_list", "mail_read", "mail_draft", "mail_to_operator", "send_dsd_for_human_review", "launch_app", "list_running_apps",
    "terminate_app", "send_dsd", "my_dsds", "withdraw_dsd", "replace_dsd", "all", "approve", "help", "hooks", "pause", "reject", "resume", "status",
    # (g32): the open-work verbs and the declared-status setter (+ its operator command)
    "work_add", "work_update", "work_done", "work_drop", "work_list", "operator_status_set", "presence",
    # (the Apps host's SEVEN hub verbs; g32 / g73-amendment item 1; app_open restored by g73-amendment-4b:
    # app id only)
    "app_list", "app_status", "app_start", "app_stop", "app_open", "app_propose_install", "app_propose_uninstall",
    # (stage 1a): the Scheduler's SIX hub verbs (g32). There is deliberately NO entry for confirming a job.
    "sched_list", "sched_status", "sched_propose", "sched_propose_remove", "sched_run_now", "sched_log",
})
#: Slots that exist in PREDICATES but must not be referenced by a policy file yet.
UNIMPLEMENTED = frozenset({"context_below"})
_WHEN_RE = re.compile(r"\A([a-z][a-z0-9_]*)(?:\(\s*(-?[0-9.]+(?:\s*,\s*-?[0-9.]+)*)?\s*\))?\Z")


# --- model ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    always: Tuple[str, ...] = ()
    others: str = DENY
    warning: str = ""
    when: Tuple[str, ...] = ()
    require_ack: bool = True
    alert_to: Tuple[str, ...] = ()


@dataclass(frozen=True)
class VerbPolicy:
    rule: Rule
    targets: Dict[str, Rule] = field(default_factory=dict)
    window_seconds: float = DEFAULT_WINDOW_SECONDS


@dataclass
class Policy:
    verbs: Dict[str, VerbPolicy] = field(default_factory=dict)
    #: the file in force: the override when one is merged in, else the default
    source_path: str = DEFAULT_POLICY_PATH
    default_path: str = DEFAULT_POLICY_PATH
    #: verb -> the file its table came from (default or override)
    verb_sources: Dict[str, str] = field(default_factory=dict)
    #: True when the machine-config override file is merged in
    overridden: bool = False
    #: where an override would live (stated in messages so the operator knows where to edit)
    override_path: str = ""
    #: where the DEPLOYMENT layer lives (``<deploy_root>/defaults/verb-policy.toml``; ``""`` = none resolvable)
    deploy_path: str = ""
    #: True when a deployment-layer file is merged in (X13 S5-3)
    deploy_merged: bool = False
    errors: List[str] = field(default_factory=list)
    #: one line per VALID override entry that NARROWS access relative to the default (stricter ``others``,
    #: dropped ``always`` ids, a per-target deny); the launcher reports each loudly (F-R2-5)
    narrowing: List[str] = field(default_factory=list)
    #: set when the file could not be used: every check denies (fail closed)
    invalid_reason: Optional[str] = None

    @property
    def invalid(self) -> bool:
        return self.invalid_reason is not None


@dataclass(frozen=True)
class Decision:
    outcome: str
    message: str = ""
    #: True when the caller must call again with a non-empty ``ack``
    needs_ack: bool = False
    #: always | others-allow | warn | confirmed | first-call | ack-missing | others-deny |
    #: policy-invalid | unknown-verb
    reason: str = ""
    verb: str = ""
    target: Optional[str] = None
    caller: str = ""
    #: the (sanitised, one-line, <= 200 chars) ack that confirmed a confirm-twice call ("" when the
    #: entry has require_ack = false and none was given)
    ack: Optional[str] = None
    #: warn mode: sessions to alert (the caller of check does the notifying)
    alert_to: Tuple[str, ...] = ()
    window_seconds: Optional[float] = None
    policy_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None and v != "" and v != ()}


class PendingStore:
    """In-memory confirm-twice state: ``(caller, verb, target) -> first-call time``. Bounded."""

    def __init__(self) -> None:
        self._d: Dict[Tuple[str, str, Optional[str]], float] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> Dict[Tuple[str, str, Optional[str]], float]:
        with self._lock:
            return dict(self._d)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


_DEFAULT_PENDING = PendingStore()


@dataclass
class Ctx:
    """Everything ``check`` reads besides its arguments (all injectable)."""

    policy: Policy
    now: Callable[[], float] = time.time
    pending: PendingStore = field(default_factory=lambda: _DEFAULT_PENDING)
    #: predicate inputs (e.g. {"context_used": 71.0}); see PREDICATES
    facts: Mapping[str, Any] = field(default_factory=dict)


# --- loading ----------------------------------------------------------------------------


def override_path_for(runtime_root: Optional[str]) -> str:
    from ..config import runtime_root_default

    root = os.path.expanduser(runtime_root) if runtime_root else runtime_root_default()
    return os.path.join(root, POLICY_FILENAME)


def _clean_text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str) or len(value) > limit:
        return None
    if any(ch in value for ch in "\r\n\x00") or any(ord(ch) < 0x20 and ch != "\t" for ch in value):
        return None
    return value


def _validate_rule_fields(where: str, raw: Mapping[str, Any], allowed: frozenset, errs: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    unknown = sorted(str(k) for k in raw if k not in allowed)
    if unknown:
        errs.append(f"{where}: unknown key(s) {unknown} (allowed: {sorted(allowed)})")
    if "always" in raw:
        a = raw["always"]
        if not isinstance(a, list) or not all(isinstance(x, str) and CALLER_RE.match(x) for x in a):
            errs.append(f"{where}.always: must be a list of instance ids matching {CALLER_RE.pattern}")
        else:
            out["always"] = tuple(dict.fromkeys(a))
    if "others" in raw:
        if raw["others"] not in OTHERS_VALUES:
            errs.append(f"{where}.others: must be one of {list(OTHERS_VALUES)} (got {raw['others']!r})")
        else:
            out["others"] = raw["others"]
    if "warning" in raw:
        w = _clean_text(raw["warning"], MAX_WARNING_CHARS)
        if w is None or not w.strip():
            errs.append(f"{where}.warning: must be a non-empty single-line string <= {MAX_WARNING_CHARS} chars")
        else:
            bad = sorted({f for f in _FIELD_RE.findall(w) if f not in _PLACEHOLDERS})
            stray = _FIELD_RE.sub("", w)
            if bad or "{" in stray or "}" in stray:
                errs.append(f"{where}.warning: only the placeholders {sorted(_PLACEHOLDERS)} are allowed "
                            f"(found {bad or 'a stray brace'})")
            else:
                out["warning"] = w
    if "require_ack" in raw:
        if not isinstance(raw["require_ack"], bool):
            errs.append(f"{where}.require_ack: must be true or false")
        else:
            out["require_ack"] = raw["require_ack"]
    if "alert_to" in raw:
        at = raw["alert_to"]
        if not isinstance(at, list) or not all(isinstance(x, str) and CALLER_RE.match(x) for x in at):
            errs.append(f"{where}.alert_to: must be a list of instance ids matching {CALLER_RE.pattern}")
        else:
            out["alert_to"] = tuple(dict.fromkeys(at))
    if "when" in raw:
        wn = raw["when"]
        if not isinstance(wn, list) or not all(isinstance(x, str) for x in wn):
            errs.append(f"{where}.when: must be a list of predicate expressions such as \"name(arg)\"")
        else:
            ok = True
            for expr in wn:
                m = _WHEN_RE.match(expr.strip())
                if m is None:
                    errs.append(f"{where}.when: cannot parse {expr!r} (expected name or name(number, ...))")
                    ok = False
                elif m.group(1) in UNIMPLEMENTED:
                    errs.append(f"{where}.when: predicate {m.group(1)!r} is reserved but NOT implemented yet: "
                                "a policy file may not reference it")
                    ok = False
                elif m.group(1) not in PREDICATES:
                    errs.append(f"{where}.when: unknown predicate {m.group(1)!r} (closed set: "
                                f"{sorted(set(PREDICATES) - UNIMPLEMENTED)})")
                    ok = False
            if ok:
                out["when"] = tuple(x.strip() for x in wn)
    return out


def _rule_consistency(where: str, rule: Rule, errs: List[str]) -> None:
    if rule.others in ("confirm-twice", "warn") and not rule.warning:
        errs.append(f"{where}: others = {rule.others} requires a warning")
    if rule.alert_to and rule.others != "warn":
        errs.append(f"{where}.alert_to: only applies to others = warn")


def parse_policy(text: str, *, source_path: str) -> Policy:
    """Validate policy TEXT strictly. Never raises; a fault yields an INVALID policy."""
    pol = Policy(source_path=source_path)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        pol.errors.append(f"{source_path}: TOML parse error: {exc}")
        pol.invalid_reason = pol.errors[-1]
        return pol
    errs: List[str] = []
    unknown = sorted(str(k) for k in data if k not in TOP_KEYS)
    if unknown:
        errs.append(f"unknown top-level key(s) {unknown}")
    if data.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA} (got {data.get('schema')!r})")
    verbs = data.get("verbs")
    if not isinstance(verbs, dict) or not verbs:
        errs.append("[verbs] must be a non-empty table of [verbs.<name>] tables")
        verbs = {}
    for name, raw in verbs.items():
        where = f"verbs.{name}"
        if not isinstance(name, str) or not VERB_RE.match(name):
            errs.append(f"{where}: verb name must match {VERB_RE.pattern}")
            continue
        if not isinstance(raw, dict):
            errs.append(f"{where}: must be a table")
            continue
        fields = _validate_rule_fields(where, {k: v for k, v in raw.items() if k != "targets"},
                                       VERB_KEYS - {"targets"}, errs)
        window = DEFAULT_WINDOW_SECONDS
        if "confirm_window_seconds" in raw:
            w = raw["confirm_window_seconds"]
            if isinstance(w, bool) or not isinstance(w, (int, float)) or \
                    not WINDOW_FLOOR_SECONDS <= float(w) <= WINDOW_CEILING_SECONDS:
                errs.append(f"{where}.confirm_window_seconds: must be a number in "
                            f"[{WINDOW_FLOOR_SECONDS:g}, {WINDOW_CEILING_SECONDS:g}] (got {w!r})")
            else:
                window = float(w)
        if "others" not in fields:
            errs.append(f"{where}.others: required")
        rule = Rule(always=fields.get("always", ()), others=fields.get("others", DENY),
                    warning=fields.get("warning", ""), when=fields.get("when", ()),
                    require_ack=fields.get("require_ack", True), alert_to=fields.get("alert_to", ()))
        targets: Dict[str, Rule] = {}
        traw = raw.get("targets", {})
        if not isinstance(traw, dict):
            errs.append(f"{where}.targets: must be a table of per-target tables")
            traw = {}
        for tname, tr in traw.items():
            twhere = f"{where}.targets.{tname}"
            if not isinstance(tname, str) or not TARGET_RE.match(tname):
                errs.append(f"{twhere}: target id must match {TARGET_RE.pattern}")
                continue
            if not isinstance(tr, dict):
                errs.append(f"{twhere}: must be a table")
                continue
            tf = _validate_rule_fields(twhere, tr, TARGET_KEYS, errs)
            trule = Rule(always=tf.get("always", rule.always), others=tf.get("others", rule.others),
                         warning=tf.get("warning", rule.warning), when=tf.get("when", rule.when),
                         require_ack=tf.get("require_ack", rule.require_ack),
                         alert_to=tf.get("alert_to", rule.alert_to))
            _rule_consistency(twhere, trule, errs)
            targets[tname] = trule
        _rule_consistency(where, rule, errs)
        if not any(e.startswith(where) for e in errs):
            pol.verbs[name] = VerbPolicy(rule=rule, targets=targets, window_seconds=window)
    if errs:
        pol.errors = [f"{source_path}: {e}" for e in errs]
        pol.verbs = {}
        pol.invalid_reason = f"{len(errs)} fault(s): {pol.errors[0]}" + (" ..." if len(errs) > 1 else "")
    return pol


def _read_text(path: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None, "absent"
    except OSError as exc:
        return None, f"unreadable: {exc}"
    if len(raw) > MAX_FILE_BYTES:
        return None, f"larger than {MAX_FILE_BYTES} bytes"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "not UTF-8"


_RANK = {"allow": 0, "warn": 0, "confirm-twice": 1, "deny": 2}


def _narrowing_notes(where: str, default_rule: Rule, new_rule: Rule, opath: str) -> List[str]:
    out: List[str] = []
    if _RANK[new_rule.others] > _RANK[default_rule.others]:
        out.append(f"{opath}: {where}.others: {default_rule.others} -> {new_rule.others}")
    dropped = [a for a in default_rule.always if a not in new_rule.always]
    if dropped:
        out.append(f"{opath}: {where}.always drops {dropped}")
    return out


def _invalid(path: str, dpath: str, opath: str, overridden: bool, msg: str, errors: Optional[List[str]] = None,
             deploy: str = "") -> Policy:
    pol = Policy(source_path=path, default_path=dpath, override_path=opath, overridden=overridden, deploy_path=deploy)
    pol.errors = list(errors) if errors else [msg]
    pol.invalid_reason = msg
    return pol


def deploy_path_for(deploy_root: Optional[str] = None) -> str:
    """``<deploy_root>/defaults/verb-policy.toml``: the DEPLOYMENT layer (X13 S5-3). Resolved lazily through
    ``deploy_data`` (nothing at import); ``""`` when no deploy root resolves."""
    from .. import deploy_data

    return deploy_data.data_path(POLICY_FILENAME, deploy_root)


def load_policy(runtime_root: Optional[str] = None, *, override_path: Optional[str] = None,
                default_path: Optional[str] = None, deploy_path: Optional[str] = None) -> Policy:
    """The EFFECTIVE policy: the shipped (neutral) default, then the DEPLOYMENT file
    (``<deploy_root>/defaults/verb-policy.toml``), then the machine-config override, each merged on per verb.
    ``deploy_path=""`` switches the deployment layer off (tests, snapshots). Never raises; see the module doc for
    the invalid (fail-closed) cases."""
    opath = override_path if override_path is not None else override_path_for(runtime_root)
    dpath = default_path if default_path is not None else DEFAULT_POLICY_PATH
    ypath = deploy_path if deploy_path is not None else deploy_path_for()
    text, why = _read_text(dpath)
    if text is None:
        return _invalid(dpath, dpath, opath, False, f"{dpath}: shipped default policy {why}")
    base = parse_policy(text, source_path=dpath)
    if base.invalid:
        base.default_path, base.override_path, base.deploy_path = dpath, opath, ypath
        return base
    eff = Policy(verbs=dict(base.verbs), source_path=dpath, default_path=dpath, override_path=opath,
                 deploy_path=ypath, verb_sources={v: dpath for v in base.verbs})
    for layer_path, is_override in ((ypath, False), (opath, True)):
        if not layer_path:
            continue
        otext, why = _read_text(layer_path)
        if otext is None and why != "absent":        # present but unusable: fail closed, never fall back
            return _invalid(layer_path, dpath, opath, True, f"{layer_path}: {why}", deploy=ypath)
        if otext is None:
            continue
        over = parse_policy(otext, source_path=layer_path)
        if over.invalid:
            over.default_path, over.override_path, over.deploy_path, over.overridden = dpath, opath, ypath, True
            return over
        for v, ovp in over.verbs.items():            # per-verb replacement
            if v not in REQUIRED_VERBS:              # F-R2-5: a typo'd verb name would silently lose intent
                eff.errors.append(f"{layer_path}: verbs.{v}: INVALID: not a verb the harness exposes (a typo?); "
                                  "the entry is ignored")
                continue
            dvp = eff.verbs[v]                       # the rule in force so far (default, or the deploy layer)
            eff.narrowing += _narrowing_notes(f"verbs.{v}", dvp.rule, ovp.rule, layer_path)
            for t, tr in ovp.targets.items():
                eff.narrowing += _narrowing_notes(f"verbs.{v}.targets.{t}", dvp.targets.get(t, dvp.rule), tr, layer_path)
            eff.verbs[v] = ovp
            eff.verb_sources[v] = layer_path
        eff.source_path = layer_path
        eff.overridden = eff.overridden or is_override
        if not is_override:
            eff.deploy_merged = True
    missing = sorted(REQUIRED_VERBS - set(eff.verbs))
    if missing:
        return _invalid(eff.source_path, dpath, opath, eff.overridden,
                        f"{dpath}: required verb(s) missing from the effective policy: {missing}", deploy=ypath)
    return eff


def default_ctx(policy: Policy, *, now: Optional[Callable[[], float]] = None,
                facts: Optional[Mapping[str, Any]] = None) -> Ctx:
    """A context on the process-wide pending store (production use)."""
    return Ctx(policy=policy, now=now or time.time, facts=facts or {})


# --- checking ---------------------------------------------------------------------------


def sanitize_ack(ack: Any) -> str:
    """A one-line, printable, length-bounded rendering of a reason (logged verbatim-ish)."""
    if not isinstance(ack, str):
        return ""
    text = "".join(ch if (ch.isprintable() and ch not in "\r\n\t") else " " for ch in ack)
    return " ".join(text.split())[:MAX_ACK_CHARS]


def _rule_for(vp: VerbPolicy, target: Optional[str]) -> Rule:
    """The rule that governs ``target``: its override when one exists, else the verb's own."""
    return vp.targets[target] if target is not None and target in vp.targets else vp.rule


def _restriction_applies(rule: Rule, ctx: Ctx) -> bool:
    """``when`` scopes the RESTRICTION (``others``): it applies iff every named predicate holds. A
    predicate that cannot be evaluated counts as holding (the safe direction: restrict)."""
    for expr in rule.when:
        m = _WHEN_RE.match(expr)
        try:
            args = tuple(float(x) for x in m.group(2).split(",")) if m and m.group(2) else ()
            if not PREDICATES[m.group(1)](ctx.facts, args):  # type: ignore[union-attr]
                return False
        except Exception:  # noqa: BLE001
            continue
    return True


def render_warning(rule: Rule, verb: str, target: Optional[str]) -> str:
    what = f"{verb}({target})" if target else verb
    return rule.warning.format_map({"verb": verb, "target": target or "", "what": what})


def static_outcome(policy: Policy, verb: str, caller_id: Any, target: Optional[str] = None,
                   facts: Optional[Mapping[str, Any]] = None) -> str:
    """The outcome WITHOUT touching confirm-twice state: ``allow`` | ``confirm`` | ``deny``
    (``warn`` counts as ``allow``: it proceeds). Used to decide whether a verb is VISIBLE (only
    ``deny`` hides it)."""
    if policy.invalid or not isinstance(caller_id, str):
        return DENY
    vp = policy.verbs.get(verb)
    if vp is None:
        return DENY
    rule = _rule_for(vp, target)
    if caller_id in rule.always or not _restriction_applies(rule, Ctx(policy=policy, facts=facts or {})):
        return ALLOW
    return {"allow": ALLOW, "warn": ALLOW, "confirm-twice": CONFIRM, "deny": DENY}[rule.others]


def visible_targets(policy: Policy, verb: str, caller_id: Any, targets: List[str],
                    facts: Optional[Mapping[str, Any]] = None) -> List[str]:
    """The targets for which ``verb`` is not ``deny`` for this caller (sorted). Empty = the tool
    is hidden from this caller."""
    return sorted(t for t in targets if static_outcome(policy, verb, caller_id, t, facts) != DENY)


def consume_confirmation(ctx: Ctx, caller_id: str, verb: str, target: Optional[str]) -> None:
    """Spend a confirm-twice confirmation that ``check(..., consume=False)`` allowed. The verb calls
    this only once its own validation has passed, so a failed validation does not burn the ack."""
    with ctx.pending._lock:
        ctx.pending._d.pop((caller_id, verb, target), None)


def check(verb: str, caller_id: Any, target: Optional[str] = None, ack: Any = None,
          ctx: Optional[Ctx] = None, consume: bool = True) -> Decision:
    """THE policy question. See the module doc for the confirm-twice state machine. With
    ``consume=False`` an allowing confirmation is NOT spent (call :func:`consume_confirmation`)."""
    if ctx is None:
        ctx = default_ctx(load_policy())
    pol = ctx.policy
    caller = caller_id if isinstance(caller_id, str) else ""
    base = {"verb": verb, "target": target, "caller": caller, "policy_path": pol.source_path}
    where = f"{pol.source_path}"
    if pol.invalid:
        return Decision(DENY, f"{verb} is DISABLED: the verb policy is invalid ({pol.invalid_reason}). "
                              f"Fix {where} (the operator's file).", reason="policy-invalid", **base)
    vp = pol.verbs.get(verb)
    if vp is None or not caller:
        return Decision(DENY, f"{verb} has no entry in the verb policy ({where}): denied.",
                        reason="unknown-verb" if vp is None else "policy-invalid", **base)
    rule = _rule_for(vp, target)
    if caller in rule.always:
        return Decision(ALLOW, reason="always", **base)
    if rule.others == "allow" or not _restriction_applies(rule, ctx):
        return Decision(ALLOW, reason="others-allow", **base)
    if rule.others == "warn":
        return Decision(ALLOW, render_warning(rule, verb, target), reason="warn", alert_to=rule.alert_to, **base)
    if rule.others == "deny":
        what = f"{verb}({target})" if target else verb
        return Decision(DENY, f"{what} is not available to {caller} (verb policy: others = deny).",
                        reason="others-deny", **base)
    # -- confirm-twice
    now = ctx.now()
    window = vp.window_seconds
    key = (caller, verb, target)
    with ctx.pending._lock:
        store = ctx.pending._d
        for k in [k for k, t0 in store.items() if now - t0 > window or t0 > now + 1.0]:
            del store[k]
        while len(store) >= MAX_PENDING:              # bounded: drop the oldest
            del store[min(store, key=store.get)]
        first = store.get(key)
        note = sanitize_ack(ack)
        if first is None:
            store[key] = now
            return Decision(CONFIRM, render_warning(rule, verb, target), needs_ack=True, reason="first-call",
                            window_seconds=window, **base)
        if not note and not rule.require_ack:
            if consume:
                del store[key]
            return Decision(ALLOW, reason="confirmed", ack="", window_seconds=window, **base)
        if not note:
            return Decision(CONFIRM, f"{verb}: this is your second call but it carries no 'ack'. Pass a "
                                     "non-empty ack (your reason: who you coordinated with and why) to proceed; "
                                     f"the confirmation window closes {max(0.0, window - (now - first)):.0f}s "
                                     "from now and is not extended.",
                            needs_ack=True, reason="ack-missing", window_seconds=window, **base)
        if consume:
            del store[key]                            # consumed: the next call confirms again
    return Decision(ALLOW, reason="confirmed", ack=note, window_seconds=window, **base)


def report_errors(policy: Policy) -> None:
    """One loud stderr line per fault (used by the launcher; g27b f)."""
    for err in policy.errors:
        sys.stderr.write(f"gnomon harness: ⚠ verb policy: {err}\n")
