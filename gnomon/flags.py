"""Feature flags.

One helper, :func:`flag`, is the ONLY way Gnomon code reads a feature flag.

Source order (first that SETS the flag wins):

1. the profile override — a profile's ``flags`` dict (``Profile.flags`` or a raw
   profile dict's ``"flags"`` key);
2. the machine config — ``flags`` in ``~/gnomon-data/config.json``, read through
   the existing :class:`gnomon.config.ConfigStore`;
3. the default — the caller's explicit ``default=`` if given, else the registry
   default in :data:`KNOWN_FLAGS`, else ``False``.

A source only SETS a flag with a real ``bool``. A string ``"false"`` or an int is
ignored (logged once), never coerced — ``bool("false")`` is ``True``, and coercion
would turn a flag ON from a hand-edit meant to turn it off.

Names must match :data:`FLAG_NAME_RE`. An invalid name RAISES under pytest (or with
``GNOMON_FLAGS_STRICT=1``) so a typo fails the suite; at runtime it returns the default
with a one-time warning, so a typo can never crash the dashboard. A valid-but-
unregistered name returns its default and is logged once per process.

Pure stdlib; no app-layer imports (``config.py`` is package-root for the same reason).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("gnomon.flags")
# g48d: shared modules log normally. A HOOK process silences logging at its own entry point
# (gnomon.hook_silence.silence_logging), never here: a module-level NullHandler would also silence
# these warnings in long-lived processes (web, hub, launcher) that have no root handler.

FLAG_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class FlagSpec:
    description: str
    default: bool = False


#: The flag registry. Every roadmap-cited flag (``- flag:`` in
#: the roadmap) is registered here, default OFF, except
#: the two display features that shipped ON before flags existed (4D, X2).
#: ``tests/test_rmx6_flags.py`` re-greps the roadmap, so a new cited flag that is
#: not registered here fails the suite.
#: SECURITY flags (confirm-proof fix round, audit F1). They are default ON; turning one OFF through the Config tab
#: needs a passed human-presence check, an agent write to the machine config is denied by the harness deny layer, and
#: the server shows a visible warning (startup line + dashboard banner) while either is OFF. Anything that later adds
#: a security-relevant flag adds its name HERE (checklist g82).
SECURITY_FLAGS: tuple = ("confirm_proof", "dsd_submit_proof")
#: security settings that are TOP-LEVEL keys of the machine config, not entries of its ``flags`` block (so not in
#: KNOWN_FLAGS and never read through :func:`flag`). They follow the same g82 rules as SECURITY_FLAGS: default ON, turning one OFF
#: needs the presence check (``ConfigStore.save``), an agent write to the machine config is denied by the harness deny layer, and
#: the server shows a warning while one is OFF. ``guard_fail_closed``: the deny guard refuses when it cannot start.
SECURITY_SETTINGS: tuple = ("guard_fail_closed",)

KNOWN_FLAGS: Dict[str, FlagSpec] = {
    # shipped features (default ON — they were live before this registry)
    "dsd_queued_display": FlagSpec(
        "DSD list shows the answer-notice chip (queued for / delivered / expired).", True),
    "notice_hold_chip": FlagSpec(
        "session row shows the held-notice chip from /api/sessions notice_hold.", True),
    "dsd_review_presence": FlagSpec(
        "DSD review activity feeds operator presence: the review page beacons {dsd_id} "
        "(throttled; never content) and the stall nudge says which DSD the operator is "
        "reviewing (read-only presence; ON by design, pilot 2026-09-29).", True),
    "build_stamp_chip": FlagSpec(
        "session row shows the stale/unknown-build chip; header shows the "
        "dashboard's own stale build (display-only; the launch.json stamp is unconditional).", True),
    # roadmap-cited, not yet built (default OFF)
    "context_aware_stall_nudge": FlagSpec(
        "operator activity beacons + context-aware stall nudge.", False),
    "otel_export": FlagSpec("OpenTelemetry export, switchable.", False),
    "otel_log_prompts": FlagSpec(
        "with otel export on, also export user prompt TEXT (OTEL_LOG_USER_PROMPTS=1).", False),
    "build_stamp": FlagSpec("build/version stamp + stale-build chip.", False),
    "auto_golive_smoke": FlagSpec("golive_smoke runs automatically on hub start (read-only check; shipped ON).", True),
    "quiescent_state": FlagSpec("task-done -> quiescent stall state.", False),
    "idle_snooze": FlagSpec(
        "the idle_snooze self-verb (tap = delay the idle nudge 60 min; tap again = asked; "
        "confirm = no idle/stall/resume nudges until the operator returns or new work arrives). Default OFF "
        "until the Diamond passes; the architect flips it.", False),
    "open_work_list": FlagSpec(
        "Slice 1: each agent keeps a list of its own open work (self verbs work_add, "
        "work_update, work_done, work_drop, work_list); only listed work earns reminders (15 to 2880 min, workspace default "
        "60); the timed idle/stall/active-elsewhere nudge and idle_snooze are retired; the operator's declared status "
        "(operator_status_set) and a presence line ride on every wake-up; a bounded digest joins the boot render. OFF = "
        "today's harness, byte-identical. Machine-config only (a session cannot turn it on). Default OFF until the Diamond "
        "passes; the architect flips it.", False),
    "guard_start_failure_banner": FlagSpec(
        "Deny guard cannot start (the operator's selection, D1 of his DSD), slice 1: a red banner on every dashboard tab while the "
        "deny guard's start-up failure log has a line from the last 24 hours (the count and the newest time), shown also, with its reason, "
        "when the log cannot be read; it clears by itself 24 hours after the newest line. OFF = today's page and routes, byte-identical. "
        "Default OFF until the Diamond passes; the architect flips it.", False),
    "sync_my_skills": FlagSpec(
        "the sync_my_skills self-verb (an agent turns its own corpus's skill-projection PROC "
        "nodes into ~/.claude/skills/<name>/ launchers: dry run first, own corpus only, collisions refused, "
        "every call logged). Default OFF until the Diamond passes; the architect flips it.", False),
    "effect_ledger": FlagSpec("emit_started / emit_completed ledger records.", False),
    "macro_token_budget": FlagSpec("per-macro token budget in {{lts:}} macros.", False),
    "restart_verb": FlagSpec("restart verb + UI button.", False),
    "evals_scaffold": FlagSpec("evals scaffold.", False),
    "evals_seed_cases": FlagSpec("seed eval cases.", False),
    "evals_harnessed_variant": FlagSpec("harnessed eval variant.", False),
    "deliverable_ledger": FlagSpec("deliverable ledger.", False),
    "deliverable_open": FlagSpec(
        "Deliverables tab 'Open' button (operator 2026-10-03): POST /api/deliverables/open opens a ledger entry, "
        "named BY ID, with /usr/bin/open and marks it seen. Default ON (the operator asked for it); only takes "
        "effect while deliverable_ledger is ON. Set false to remove the button and refuse the endpoint.",
        True),
    "apps_tab": FlagSpec(
        "(spec 13): the Apps tab, the seven app_* hub verbs and the converged legacy "
        "launch_app / terminate_app / list_running_apps. OFF: no Apps route exists, the new verbs are not "
        "advertised, and the legacy three behave exactly as before.", False),
    "scheduler": FlagSpec(
        "(spec 14), stage 1a: the Scheduler and Run Log tabs, the six sched_* hub verbs, the "
        "30 s tick and confirmed own-session timed injects. OFF: no tab, no route, no verb is advertised, no tick "
        "fires and nothing is sent. Also needs confirm_proof ON (a job is confirmed only after the macOS "
        "device-owner check). Default OFF; turning it ON is the operator's call after a passing Diamond on the live tree.",
        False),
    "confirm_proof": FlagSpec(
        "/ g76: the Apps tab confirm and settings routes make the server ask macOS for "
        "device-owner authentication (Touch ID, else the login password) AFTER the click and BEFORE committing; "
        "a failed / cancelled / timed-out check refuses. Default ON (the operator chose it). OFF = previous behaviour.", True),
    "dsd_submit_proof": FlagSpec(
        "/ g76: a DSD submit must carry recent in-tab keyboard/pointer activity; the server "
        "refuses one without it and records the evidence on every minted answer. Default ON (the operator chose it). "
        "OFF = previous behaviour.", True),
    "fetch_verbatim": FlagSpec(
        "the guarded verbatim web-fetch tool (fetch_verbatim). OFF: the tool is not advertised and a call "
        "is refused. Default OFF; the plug-in host wiring and MCP registration are not built yet.", False),
    "markdown_viewer": FlagSpec("read-only markdown viewer.", False),
    "session_mcp_from_profile": FlagSpec(
        "Hub-MCP wiring fix: deliver the profile's mcp_servers (incl. the gnomon hub, resolved to the "
        "current hub url) to the child via --mcp-config. Default OFF. BLOCKER (operator decision): the hub does not identify the "
        "calling session (send_dsd_for_human_review takes any registered instance_id), so do not turn ON until "
        "the hub binds the caller. No Config-tab row (g56): not for the operator to toggle yet. Since "
        "delivery is REFUSED unless the hub publishes binding: on.", False),
    "hub_caller_binding": FlagSpec(
        "(g60(b), g60c): the hub binds each caller to the instance whose launch credential "
        "it presents (per-launch token; the hub holds its sha256), refuses unscoped tools and identity arguments "
        "that differ from the binding, parks a re-register without the holder's hash, admits an unheld instance "
        "only with a matching pending-launch record (0600, machine-local), checks Host/Origin on /mcp, "
        "and publishes binding: on in rendezvous.json. Default OFF = today's hub, byte-identical. No Config-tab "
        "row (g56) until it is Diamond-reviewed and goes live with the relaunch.", False),
    "search_transcript_verb": FlagSpec("search_transcript self-verb.", False),
    "plan_usage_verb": FlagSpec(
        "slice 1: the plan_usage self-verb (read-only). The statusline write and "
        "harness.plan_usage.read() are NOT flagged.", False),
    "agent_mail": FlagSpec(
        "V1: the agent mail tool: self-verbs mail_list, mail_read, mail_draft, mail_to_operator (label-scoped "
        "reading, drafts only, delivery into the operator's own mailbox; never a send) and the server's /api/mail/* "
        "routes + Mail panel. OFF: no verb is advertised, no route exists, the page is unchanged.", False),
    "alerts": FlagSpec(
        "(core): the one Gnomon alert service. ON: GET /api/alerts, POST /api/alerts/<id>/dismiss and "
        "/api/alerts/dismiss-all, the stacked alert column in the web page, the macOS notification for severity "
        ">= warning, the agent alert(title, body, severity, link) self-verb, and the two proof producers (the "
        "modal-hold stuck-hold and the DSD-arrival event). OFF: none of those exists (the routes answer the "
        "unknown-path 404, the page carries no alert script, the verb is not advertised, no producer raises). "
        "Alerts are UI only and are never typed into an agent's PTY. Default OFF until the Diamond passes; the "
        "architect flips it.", False),
    "restart_service": FlagSpec(
        "the restart_service self-verb (who may call it, and whether a second confirming call is "
        "needed, is the central verb policy; D52 preflight, verification, notices). "
        "Default OFF; turning it ON is the pilot's call.", False),
    "progress_entries": FlagSpec(
        "the memory self-verb gains the type progress: a short progress entry in a per-session file "
        "beside the agent's lifelog (stamped written-at and session by the code, a byte bound per file with one "
        "warning at 80%), and memory(type=progress, recall=true) reads back this session's entries since its last "
        "compaction. OFF: the type is not offered and is refused like any unknown type; nothing is written. "
        "Default OFF; turning it ON is the operator's call after a passing Diamond on the tree that is live.", False),
    "provenance_spans": FlagSpec("provenance re-emitted as spans.", False),
    "away_watch_report": FlagSpec("away-watch / morning report.", False),
    "narration_panes": FlagSpec("narration + notice voice panes.", False),
    "two_signal_link_health": FlagSpec("two-signal link health.", False),
    "fleet_roster": FlagSpec("fleet roster panes.", False),
    "break_glass_launch": FlagSpec("break-glass recovery launch.", False),
    "model_effort_picker_applied": FlagSpec("model/effort picker applied at relaunch.", False),
    "subagent_chips": FlagSpec("subagent chips.", False),
    "embedding_adjacency": FlagSpec("embedding adjacency.", False),
    "usage_metrics": FlagSpec("automatic usage-metrics indexing + web panel (the report CLI works regardless). Default OFF 2026-09-29: fix-pass audit found short single-token secrets (ghp_, AKIA) persisting in trigger/subtype; re-enable after the fix.", False),
    "post_compact_boot": FlagSpec(
        "after a compaction the harness adds the per-role boot line to the SessionStart:compact "
        "context AND submits it as a prompt (only on an empty operator line, source: harness, trigger: "
        "post-compact-boot); the post-compact resume nudge is then not sent. Default OFF until a live check "
        "(acceptance 5).", False),
    "transcript_archive": FlagSpec(
        "durable transcript archive (every session's main and subagent transcripts kept beyond Claude "
        "Code's cleanup; the web job archives once at start and every 10 minutes while ON). Default ON since the "
        "2026-10-27 revision (the operator's decision, recorded on the roadmap): it protects data.", True),
    "workspace_model": FlagSpec(
        "the workspace/project scope model (gnomon.workspace). S1 ships the "
        "scaffolding + the read-only converter dry-run/verify (the converter CLI needs no flag); this flag "
        "gates every RUNTIME consumer of the model (S2+). Default OFF; the cutover (S3) is the pilot's go.", False),
    # X26 feature-usage telemetry
    "feature_events": FlagSpec(
        "the feature_event emitter appends one categories-only line per Gnomon feature use to a "
        "per-process-kind daily JSONL, and the background indexer loads it into the usage DB every "
        "FU_INDEX_INTERVAL_S. Default ON: the pilot chose to have it on by default (2026-09-30). "
        "Takes effect per session at its next relaunch (harness code); switch it OFF in machine config to stop "
        "emitting and indexing.", True),
    "dsd_id_lookup": FlagSpec(
        "the DSD tab gets a box to open any DSD by its id (typed or pasted; spaces, quotes, capitals and "
        "a DEC-DSD-... answer id are cleaned up). Pending opens as a form, answered read-only. Default OFF until "
        "its review passes; the toggle is in the Configuration tab (Harness / advanced).", False),
    "hook_noop_events": FlagSpec(
        "with feature_events ON, also record a no-op event (outcome noop) when a hook point "
        "fires and no consumer is attached. Default OFF; turn it ON to check, before a release or over a day of use, that every hook point has a consumer. "
        "The user turns it on and off with the toggle in the Configuration tab (Harness / advanced), which writes "
        "this flag into the machine config (the pilot); a hook process reads it fresh on "
        "each invocation, a long-lived harness process reads it once at start.", False),
    "subagent_summary_md": FlagSpec(
        "when a subagent finishes, the SubagentStop hook writes its returned text (last_assistant_message) "
        "to a UNIQUE SUMMARY file under the calling session's dir (subagent-summaries/SUMMARY-<utc>-<type>-<id>.md), "
        "or to the SUMMARY-PATH its brief declares when that path passes confinement; header + capped text, never "
        "overwrites, bounded and non-blocking, logged to injection_log (kind subagent-summary) and as a feature "
        "event. Default OFF until one live run (the pilot). The hook is registered at "
        "harness launch (a session needs a relaunch to get it); once registered, flipping this flag takes effect "
        "on the next subagent stop with no further relaunch.", False),
    # (2026-10-02): operator broadcasts arrive whole.
    "inject_long_line_as_paste": FlagSpec(
        "the harness types a single-line injection longer than LONG_LINE_PASTE_BYTES (512) as an "
        "explicit bracketed paste (a long /command keeps its token bare), so Claude Code cannot keep only the "
        "last PTY read chunk of it (the 2026-10-02 \\all truncation). Default ON: it is the defect fix "
        "(OFF restores the legacy unbracketed write); read once at harness launch.", True),
    "modal_hold": FlagSpec(
        "hold every harness-originated injection at the gate while an "
        "AskUserQuestion / ExitPlanMode / tool-permission dialog may be open (a typed CR would answer it), "
        "released in order once it closes and the operator's line is free; a hold older than 30 min is made "
        "visible (stuck-hold), never released. Default ON: it is a safety fix. OFF = the hold is not wired "
        "(live-master behaviour, the CR can answer a dialog); read once at harness launch.", True),
    "operator_messages_log": FlagSpec(
        "every operator message (\\all, \\<instance> DM, every \\command line) is saved IN FULL "
        "with its sha256 (and the hub's recipient list) to <DATA root>/operator-messages/operator-messages.jsonl "
        "(0600, rotated). Default ON: purely additive logging; read once at harness launch.", True),
    "operator_delivery_check": FlagSpec(
        "after a broadcast/DM is typed into a session, the harness reads that session's transcript "
        "(read-only) for the prompt Claude Code submitted, records match/mismatch/unverified with the delivered "
        "sha256, and a mismatch warns the SENDING operator via the hub naming the sessions. Default ON: "
        "additive (never changes what is typed); read once at harness launch.", True),
    "relaunch_derives_resume_keys": FlagSpec(
        "gnomon-relaunch may resume a session that was launched FRESH "
        "(launch.json has session_id + transcript_cwd but no resume keys): it derives resume_session_id/resume_cwd "
        "only when exactly ONE recent transcript matches (else it refuses loudly), writes them atomically with a "
        "backup, sets the neutral 'relaunched at <UTC>' resume_prompt instead of the sticky one, and supports a "
        "deliberate one-shot prompt (--prompt-once); the harness refuses to spawn a resume whose transcript file is "
        "missing and, when resume_cwd is the session dir itself, delivers the definition as the project CLAUDE.md "
        "(no claudeMdExcludes, no --add-dir). With it OFF every one of those paths is byte-for-byte the previous "
        "behaviour (the resume rail still refuses a fresh launch). Default OFF; flip it only after a passing Diamond "
        "and the live trial in docs/build-logs/RUN_SUMMARY-relaunch.md (a real resumed boot proves the definition loaded). "
        "Read at each gnomon-relaunch run and at each harness start.", False),
    "master_write_nonblocking": FlagSpec(
        "a session no longer freezes at start when the "
        "harness types a long text before claude has read its input. With it ON the harness's write to claude's terminal never "
        "blocks the loop that carries claude's output to your screen and your keystrokes to claude: bytes wait in one ordered "
        "queue and go out as claude's terminal accepts them (keystrokes and harness texts keep their order and are never mixed "
        "mid-text), and a harness text (a reminder, a notice, a boot text) issued before claude has produced its first output "
        "is held until it has. Your own keystrokes are never held. With it OFF the write is exactly as before (blocking). "
        "Default OFF. Read once at harness launch, from the machine config only (never from the session's own launch.json).", False),
    "compact_plain_messages": FlagSpec(
        "Compact messages in plain words: when an agent's compact request is accepted the reply says that the "
        "interrupt IS the compact and not a refusal by the operator; the context an agent boots with after a compact "
        "gains one line saying when the compact completed and that any 'call request_compact' step in its summary is "
        "already done; and the 'are you sure' reply below the threshold says what the operator sees and what the "
        "second call does. With it OFF all three texts are exactly as before. Default OFF. Read from the machine "
        "config at the moment each text is built.", False),
    "subagent_compaction_check": FlagSpec(
        "GM-DEFECT-SUBAGENT-COMPACTION-TREATED-AS-THE-SESSIONS: a helper agent's compaction no longer counts as the agent's "
        "own. With it ON, the compact hooks check the session's own transcript for a new compaction row; when there is none "
        "the event is logged as 'subagent compaction, ignored' and the agent is not told to redo its start-up (no boot "
        "prompt, no idle-threshold change, no end of a snooze, no line restore, no post-compact definition render). If the "
        "transcript cannot be read the old behaviour stands and the reason is logged. With it OFF everything is exactly as "
        "before. Default OFF. Read from the machine config at the moment each hook runs.", False),
    "dsd_notice_inline": FlagSpec(
        "Your answer reaches the agent in the notice itself, and you can see when notices are being held back. With it ON: the notice "
        "an agent is sent when you answer carries, for each decision, the option you chose (its text and position), whether it "
        "matched the agent's recommendation, your comment word for word (cut with a note if the whole notice would pass 4,000 "
        "characters) and the answer's node id, built from the saved answer; if that cannot be built or typed the agent gets the "
        "short pointer notice as before. Each answer saved also records, on its node, which option position you chose, how many "
        "options there were, where the recommendation sat, whether you commented and how long the answer took, so answers can be "
        "counted. The agent's context reply gains a held_notices count (notices held longer than 2 minutes), the dashboard's "
        "notice summary reports the same count per agent, and the Decisions tab shows it beside the agent's name. With it OFF none "
        "of that is there. Default OFF. Read from the machine config when asked.", False),
    "dsd_self_verbs": FlagSpec(
        "Agents file and manage their own decisions with four calls on their own tool server: send_dsd (checked before it is "
        "filed: a decision with no options, no answer slot or a recommendation that matches no option is refused by name), "
        "my_dsds (their decisions and your answers), withdraw_dsd and replace_dsd (only their own, only while still pending; "
        "the record is kept and your Decisions tab shows it as withdrawn or replaced). Each call acts as the agent that makes it "
        "and cannot name another. A running session gets the calls at its next launch. With it OFF the calls do not exist. "
        "Default OFF. Read from the machine config when a session launches and again at each call.", False),
    "app_activity_presence": FlagSpec(
        "idle messages say which registered app you are active in. With it ON, the web job asks each running "
        "app that declares an activity_path (and whose port is held by the process Gnomon started) every 10 seconds when "
        "you last used it, keeps only that time in a small file beside the DSD-review ones, and an agent's idle message "
        "then reads \"<name> is active in <app> (<N>s ago). Do what you can without him.\" It is information only: "
        "it changes no gate, never clears a snooze, and carries no content from the app. The web job reads it each "
        "poll; a running harness reads it at its next relaunch. Default OFF until the Diamond passes.", False),
    "waiting_on_operator_alert": FlagSpec(
        "when an agent's terminal shows a permission prompt that waits on you, the harness raises ONE warning "
        "alert (plus a macOS notification) naming the agent, the question and the first line of the command (secret-shaped "
        "text masked, 120 characters), with the time left when the prompt shows a countdown, and the dashboard shows a "
        "waiting badge on that agent's row until the prompt closes. No alert for the agent whose console you typed in "
        "during the last 5 minutes (the badge still shows). A confidential workspace shows only the agent's name. It "
        "needs the alerts flag to DELIVER the alert and the modal_hold flag to see the prompt close; with alerts OFF the "
        "badge and the session-log events still appear. It is on by default by the operator's choice, made after its review passed); turn it off here or with flags.waiting_on_operator_alert false in the machine "
        "config. Machine config only; a running harness reads it at its next launch.", True),
    "workspace_scope_core": FlagSpec(
        "S2: the workspace scope core (gnomon.workspace: the I9 tier-0..3 stamping library, "
        "the typed writers for items and topics, the relaunch bundle written at launch and read by gnomon-relaunch, "
        "the restrictive-on-skip settings resolver and the alert event log). With it OFF nothing is written and "
        "no launch, relaunch or settings path changes. Default OFF; turning it ON needs the S3 cutover's "
        "scaffold (machine.json, a role definition under its home workspace) and is the pilot's go.", False),
}


def _default_config_store():
    from .config import ConfigStore

    return ConfigStore()


#: Test seam: a zero-arg callable returning a ConfigStore. Production reads the
#: machine default ``~/gnomon-data/config.json``.
_config_store = _default_config_store

_warned: set = set()
_warned_lock = threading.Lock()


def _warn_once(key: Tuple[str, str], level: int, msg: str) -> None:
    with _warned_lock:
        if key in _warned:
            return
        _warned.add(key)
    log.log(level, msg)


def _reset_for_tests() -> None:
    with _warned_lock:
        _warned.clear()


def _strict() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or os.environ.get("GNOMON_FLAGS_STRICT") == "1"


def _profile_flags(profile: Any) -> Optional[dict]:
    if profile is None:
        return None
    flags = profile.get("flags") if isinstance(profile, dict) else getattr(profile, "flags", None)
    return flags if isinstance(flags, dict) else None


def _machine_flags(config: Any) -> Optional[dict]:
    if config is None:
        try:
            config = _config_store().load()
        except Exception as exc:  # noqa: BLE001 - a broken config must never crash a caller
            _warn_once(("config-load", type(exc).__name__), logging.WARNING,
                       f"gnomon flags: machine config unreadable ({exc}); using defaults")
            return None
    flags = getattr(config, "flags", None)
    return flags if isinstance(flags, dict) else None


def _fallback(name: str, default: Optional[bool]) -> bool:
    if default is not None:
        return bool(default)
    spec = KNOWN_FLAGS.get(name)
    return spec.default if spec is not None else False


def resolve(name: str, *, profile: Any = None, config: Any = None,
            default: Optional[bool] = None) -> Tuple[bool, str]:
    """``(value, source)`` where source is ``profile`` / ``machine`` / ``default``.

    Same rules as :func:`flag`; exposed for the ``/api/flags`` listing.
    """
    if not isinstance(name, str) or not FLAG_NAME_RE.match(name):
        if _strict():
            raise ValueError(f"invalid feature-flag name {name!r}: must match {FLAG_NAME_RE.pattern}")
        _warn_once(("invalid", repr(name)), logging.WARNING,
                   f"gnomon flags: invalid flag name {name!r} (must match {FLAG_NAME_RE.pattern}); "
                   "returning the default")
        return (bool(default) if default is not None else False), "default"
    if name not in KNOWN_FLAGS:
        _warn_once(("unknown", name), logging.WARNING,
                   f"gnomon flags: unknown flag {name!r} (not in KNOWN_FLAGS); returning the default")
    for source, flags in (("profile", _profile_flags(profile)), ("machine", _machine_flags(config))):
        if flags is None or name not in flags:
            continue
        val = flags[name]
        if isinstance(val, bool):
            return val, source
        _warn_once(("non-bool", source + ":" + name), logging.WARNING,
                   f"gnomon flags: {source} value for {name!r} is {val!r}, not a bool; ignored")
    return _fallback(name, default), "default"


def flag(name: str, *, profile: Any = None, default: Optional[bool] = None,
         config: Any = None) -> bool:
    """Is feature flag ``name`` on? Profile override > machine config > default.

    ``default=None`` means "the registry default, else False"; an explicit bool
    overrides the registry. ``config`` is an injectable
    :class:`~gnomon.config.GnomonConfig` (else the machine store is
    read). Never raises at runtime; raises ``ValueError`` on an invalid name under
    test.
    """
    return resolve(name, profile=profile, config=config, default=default)[0]


def effective_flags(*, profile: Any = None, config: Any = None) -> List[Dict[str, Any]]:
    """Every registered flag, plus any flag key present in the config or
    profile that is NOT registered (shown ``known: False`` rather than hidden),
    with its effective value and source."""
    if config is None:
        try:
            config = _config_store().load()
        except Exception:  # noqa: BLE001
            config = None
    names = list(KNOWN_FLAGS)
    for extra in (_machine_flags(config) or {}, _profile_flags(profile) or {}):
        for k in extra:
            if isinstance(k, str) and FLAG_NAME_RE.match(k) and k not in names:
                names.append(k)
    rows = []
    for name in names:
        spec = KNOWN_FLAGS.get(name)
        value, source = resolve(name, profile=profile, config=config if config is not None else _Empty)
        rows.append({"name": name, "description": spec.description if spec else "(unregistered)",
                     "default": spec.default if spec else False, "value": value,
                     "source": source, "known": spec is not None})
    return rows


class _Empty:
    """A config stand-in with no flags (used when the store could not be read, so
    the listing does not re-read a broken store once per flag)."""

    flags: Dict[str, bool] = {}
