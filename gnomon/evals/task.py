"""The eval ``task()`` — one headless agent run in an ISOLATED runtime.

``task(inputs)`` launches ``claude -p <prompt>`` through the existing
``harness.agent_launch.launch_child(headless=True)`` path (the marker wrapper,
the interim rails, the operator launch ledger, D40's pre-decided transcript
path) — it does NOT reimplement any of it — then loads the evidence the run
left (:class:`RunEvidence`).

Isolation: every run gets a FRESH tmp runtime root (``mkdtemp`` under
``work_root``, default ``$TMPDIR/gm-evals``). Its sessions, agents and
``agent_launch/launch_log.jsonl`` all live there; a work root under the live
``~/gnomon-data`` tree is refused before anything is created. (Claude Code
itself still writes the transcript under ``~/.claude/projects/<mangled tmp
cwd>/`` — its own location, recorded in the tmp session's ``launch.json``.)

Model: ``agent_launch``'s headless path does not pass ``--model``; the eval sets
``ANTHROPIC_MODEL`` in the child env via launch_child's injectable ``popen``
(default ``haiku`` — the cheapest). The ``ModelUsed`` evaluator verifies what
actually ran, from the transcript.

Live gate (rm2a2 F1): ``task()`` and ``make_task()`` themselves refuse unless
``GNOMON_EVALS_LIVE=1`` — not only the CLI — so a direct call cannot reach Popen.

Rails: an eval run is an OPERATOR act. Run from inside a harnessed agent (a
gnomon harness process ancestor) it is REFUSED before any launch-rail
query — otherwise the reservation (or its denial) would be written into that
agent's LIVE ``injection_log.jsonl``, and the eval would lean on that agent's
fan-out budget. ``allow_agent_launcher=True`` opts in, and the agent's own
rails at its proxy then decide. Nothing here bypasses a rail.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .case import CaseInputs
from .evidence import RunEvidence

LIVE_ENV = "GNOMON_EVALS_LIVE"
MODEL_ENV = "ANTHROPIC_MODEL"
DEFAULT_WORK_ROOT = os.path.join(tempfile.gettempdir(), "gm-evals")

EVAL_CLAUDE_MD = """# Gnomon eval agent

You are a short-lived headless agent launched by the Gnomon behavioural-evals
scaffold (`gnomon/evals/`). Do exactly what the prompt asks, in the current
working directory, and nothing else. Do not start any session bootstrap, do not load
other context, do not contact any service. Reply briefly when done.
"""


def _live_root() -> str:
    """The machine's LIVE DATA root (X13 S5-4: ``config.runtime_root_default``): an eval refuses to run under it."""
    from ..config import runtime_root_default

    return runtime_root_default()


class EvalLaunchRefused(Exception):
    """The eval refused to launch (isolation or rails). Nothing was launched."""


def live_enabled(env: Optional[dict] = None) -> bool:
    return (os.environ if env is None else env).get(LIVE_ENV) == "1"


def require_live(env: Optional[dict] = None) -> None:
    """rm2a2 F1: the live gate lives in task() itself, not only in the CLI."""
    if not live_enabled(env):
        raise EvalLaunchRefused(f"live eval refused: {LIVE_ENV}=1 is not set — task() launches a real agent "
                                "and costs tokens. Nothing was launched.")


@dataclass
class TaskOptions:
    work_root: Optional[str] = None
    claude_bin: Optional[str] = None
    allow_agent_launcher: bool = False
    live_roots: Sequence[str] = field(default_factory=lambda: (_live_root(),))
    #: seams (tests): an explicit Launcher, and the Popen launch_child uses
    launcher: Any = None
    popen: Optional[Callable[..., Any]] = None
    #: seam (tests): the ancestry probe (default ``agent_launch.find_gm_ancestor``).
    #: rm2a2 F3: when injected it is the ONLY ancestry used — both for this module's
    #: refusal gate AND passed through to ``resolve_launcher`` — so a test can never
    #: pass the gate on fake ancestry and then resolve against the real process tree.
    find_ancestor: Optional[Callable[[], Any]] = None
    #: seam (tests) for the F1 live gate: the mapping ``GNOMON_EVALS_LIVE`` is read from.
    #: ``None`` (the production default) = the real ``os.environ``. It only relocates
    #: WHERE the flag is read; the value must still be exactly ``"1"``.
    live_env: Optional[dict] = None


def assert_isolated(path: str, live_roots: Sequence[str]) -> None:
    """Refuse a path that resolves inside a live runtime root."""
    real = os.path.realpath(os.path.expanduser(path))
    for root in live_roots:
        r = os.path.realpath(os.path.expanduser(root))
        if real == r or real.startswith(r + os.sep):
            raise EvalLaunchRefused(f"eval work root {path!r} resolves under the LIVE runtime {root!r} "
                                    f"({real}) — refusing; use a tmp dir")


def _resolve_launcher(opts: TaskOptions):
    from ..harness import agent_launch as al

    if opts.launcher is not None:
        launcher = opts.launcher
    else:
        finder = opts.find_ancestor or al.find_gm_ancestor
        anc = finder()
        if anc is not None and not opts.allow_agent_launcher:
            raise EvalLaunchRefused(
                f"this process runs under a gnomon {anc.kind} (pid {anc.pid}, session "
                f"{anc.session_dir}) — an eval launch here would reserve against that agent's rails and write "
                "into its LIVE injection_log. Run evals from an operator shell, or pass "
                "--allow-agent-launcher to let that agent's own rails decide.")
        try:
            if opts.find_ancestor is not None:
                launcher = al.resolve_launcher(find_ancestor=lambda _pid=None: opts.find_ancestor())
            else:
                launcher = al.resolve_launcher()
        except al.LaunchRefused as exc:
            raise EvalLaunchRefused(f"launch rails refused: {exc}") from None
    return launcher


def task(inputs: CaseInputs, options: Optional[TaskOptions] = None) -> RunEvidence:
    """Run one case's prompt headlessly; return the evidence. Raises
    :class:`EvalLaunchRefused` when nothing could be launched."""
    from ..config import ConfigStore, GnomonConfig
    from ..harness.agent_launch import launch_child
    from ..harness.transcript import transcript_exit_check

    opts = options or TaskOptions()
    require_live(opts.live_env)                          # F1: first, before anything
    work_root = os.path.abspath(os.path.expanduser(opts.work_root or DEFAULT_WORK_ROOT))
    assert_isolated(work_root, opts.live_roots)          # before creating anything
    launcher = _resolve_launcher(opts)                   # rails/ancestry: before creating anything
    os.makedirs(work_root, exist_ok=True)
    root = tempfile.mkdtemp(prefix=f"{inputs.case_name}-", dir=work_root)
    assert_isolated(root, opts.live_roots)               # symlinked work root
    if getattr(launcher, "kind", None) == "operator":
        launcher.runtime_root = root   # the operator launch ledger lands in the tmp root

    claude_bin = opts.claude_bin or ConfigStore().load().claude_bin   # read-only
    config = GnomonConfig(runtime_root=root, claude_bin=claude_bin, default_mcp_servers=[],
                                 hook_manifest={}, agent_launch_enabled=False)
    claude_md = os.path.join(root, "eval-CLAUDE.md")
    with open(claude_md, "w", encoding="utf-8") as fh:
        fh.write(EVAL_CLAUDE_MD)
    profile_path = os.path.join(root, "profile.json")
    with open(profile_path, "w", encoding="utf-8") as fh:
        json.dump({"instance_id": f"gm-eval-{inputs.case_name}", "display_name": f"eval {inputs.case_name}",
                   "model": inputs.model, "claude_md_source": claude_md, "terminal": "terminal_app"}, fh, indent=2)

    # ALWAYS passed to launch_child explicitly (its own ``popen`` default is bound at
    # def time and would dodge a patched module Popen); resolved here at CALL time.
    base_popen = opts.popen or subprocess.Popen

    def popen_with_model(argv, **kw):
        env = dict(kw.get("env") or os.environ)
        env[MODEL_ENV] = inputs.model
        kw["env"] = env
        return base_popen(argv, **kw)

    t0 = time.monotonic()
    report = launch_child(profile_path, inputs.prompt, headless=True, hub_port=None, timeout=inputs.timeout_s,
                          config=config, launcher=launcher, allow_profile_file=True, popen=popen_with_model)
    duration = time.monotonic() - t0
    rep = report.to_dict()
    if not report.session_dir:
        raise EvalLaunchRefused(f"launch built no session: {report.error or 'no reason given'}")
    check = transcript_exit_check(report.session_dir)
    return RunEvidence.from_session(report.session_dir, runtime_root=root, case_name=inputs.case_name,
                                    launch_report=rep, transcript_check=check, requested_model=inputs.model,
                                    duration_s=duration)


def make_task(options: Optional[TaskOptions] = None) -> Callable[[CaseInputs], RunEvidence]:
    """A one-argument task for ``Dataset.evaluate_sync``. Gated like task() (F1)."""
    require_live((options or TaskOptions()).live_env)

    def _task(inputs: CaseInputs) -> RunEvidence:
        return task(inputs, options)

    _task.__name__ = "gm_headless_task"
    return _task
