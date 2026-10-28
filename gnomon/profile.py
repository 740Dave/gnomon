"""agent profile storage (D7/D21/D31, AQ1).

A profile is the launch configuration for one agent ("agent"). For v1
it is stored as PLAIN, UI-EDITABLE JSON — there is NO structured add/edit form
(AQ1 / editing-surface-resolved). The later GUI simply
lists and opens the JSON; it does not generate a form.

Location (D31)::

    ~/gnomon-data/agents/<instance_id>/profile.json

This is RUNTIME data, deliberately external to the project checkout (D21/D31):
the project stays a clean git install on any machine, runtime state lives under
the separate, per-user-private ``~/gnomon-data`` home. ``profile.json`` sits in
the same per-instance directory that accumulates ``handovers/`` across sessions.

⚠ FLAG (provenance): the exact ``profile.json``
path is architect-INFERRED from D21 (co-locate profile with handovers) + D31
(runtime/project split). The operator ratified the split and the no-form ruling but did
not separately re-state the file path. It is carried here as the ratified
inference, not an independent operator directive.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


# The default MCP server set wired into a launched session (
# default-mcp-servers-wired-per-agent, D9). ⛔ a measurement-surface server is deliberately NOT in
# this set — a launcher agent has no business holding a measurement
# surface (D9). The renderer/launcher builds .mcp.json from this list.
DEFAULT_MCP_SERVERS: tuple[str, ...] = ("lts-engine", "gnomon")

#: The default terminal launch-template key (see ``terminal.py``).
DEFAULT_TERMINAL: str = "iterm2"


def default_intelligences_root() -> str:
    """The agents folder, the per-instance runtime home (D31; round-2: ``config.agents_root_default``, the roots file's ``agents_root``)."""
    from .config import agents_root_default

    return agents_root_default()


@dataclass
class Profile:
    """One agent launch profile.

    Fields (the v1 schema — plain JSON, no form):

    * ``instance_id`` — the agent's stable id; names its runtime directory.
    * ``display_name`` — human-facing label in the Agents list.
    * ``identity_class`` — e.g. ``domain_agent`` (carried, not interpreted here).
    * ``model`` — the model the session runs on (carried through to the profile;
      how it reaches ``claude`` is a launcher/argv concern, not stored logic).
    * ``claude_md_source`` — path to the role-def / CLAUDE.md TEMPLATE that the
      launcher renders into the session ``.claude/CLAUDE.md`` (see ``launch.py``).
    * ``mcp_servers`` — the MCP server set to wire into ``.mcp.json``. Defaults
      to :data:`DEFAULT_MCP_SERVERS` (never a measurement-surface server).
    * ``bootstrap_prompt`` — the cold-start prompt, passed to the child as argv
 at spawn; never typed.
    * ``terminal`` — the launch-template key (``iterm2`` / ``terminal_app``).
    * ``extra_args`` — (fix-pass WS4, 2026-09-27) extra ``claude`` CLI args,
      appended after the harness's own flags and before ``-- <prompt>``, e.g.
      ``["--dangerously-load-development-channels", "server:<name>"]``. Default
      empty. Validated at launch (list of non-empty strings, no bare ``--``).
    """

    instance_id: str
    display_name: str = ""
    identity_class: str = "domain_agent"
    model: str = ""
    claude_md_source: str = ""
    mcp_servers: List[str] = field(default_factory=lambda: list(DEFAULT_MCP_SERVERS))
    bootstrap_prompt: str = ""
    terminal: str = DEFAULT_TERMINAL
    extra_args: List[str] = field(default_factory=list)
    #: PTY-hooks WS-C (2026-09-27): per-instance stall-inject OVERRIDE, merged
    #: over the machine config's global default (``{"enabled": false}`` turns the
    #: resume keep-alive off for this instance). ``None`` = inherit.
    stall_inject: Optional[Dict[str, object]] = None
    #: PTY-hooks WS-G: may THIS agent launch other agents? OFF by default; it
    #: also needs the machine config's ``agent_launch_enabled``. Depth/fan-out
    #: caps apply regardless.
    agent_launch_enabled: bool = False
    #: fix-pass (2026-09-27): the ENABLE path for the fail-loud compaction-start
    #: confirmer. ``launcher.py`` reads ``confirm_compact`` /
    #: ``confirm_compact_timeout`` from the session ``launch.json`` at startup
    #: (``validate_confirm_compact_config``), but nothing WROTE them there — the
    #: launch.json builder only wrote ``context_thresholds``. These three OPTIONAL
    #: fields let a profile turn the confirmer on and override the context
    #: thresholds; ``launch.py`` propagates them into launch.json before the
    #: harness reads it. Defaults preserve current behaviour exactly:
    #:   * ``confirm_compact=False`` — confirmer OFF unless the profile opts in.
    #:   * ``confirm_compact_timeout=None`` — omitted → harness uses its default.
    #:   * ``context_thresholds=None`` — fall back to the machine-config list; a
    #:     non-empty list OVERRIDES it (per-instance monitor thresholds).
    confirm_compact: bool = False
    confirm_compact_timeout: Optional[float] = None
    context_thresholds: Optional[List[int]] = None
    #: RESUME MODE (session adoption, 2026-09-28): restore an EXISTING claude
    #: session (one created OUTSIDE the harness) under the harness PTY instead of
    #: cold-starting. All three OPTIONAL; ``None`` = fresh cold-start (unchanged).
    #:   * ``resume_session_id`` — the claude session UUID to ``--resume``. When
    #:     set, ``build_child_argv`` builds ``... --resume <id>`` and OMITS the
    #:     ``-- <bootstrap_prompt>`` append (a resumed session carries its context).
    #:   * ``resume_cwd`` — the ORIGINAL session's cwd. REQUIRED alongside
    #:     ``resume_session_id``: ``claude --resume <id>`` resolves the session by
    #:     id WITHIN the current project dir
    #:     (``~/.claude/projects/<mangled-cwd>/<id>.jsonl``), so the child MUST be
    #:     spawned in that cwd (not the harness session_dir).
    #:   * ``resume_prompt`` — an optional gentle re-orient turn appended after
    #:     ``--`` in resume mode. Empty/``None`` → no turn (pure resume).
    resume_session_id: Optional[str] = None
    resume_cwd: Optional[str] = None
    resume_prompt: Optional[str] = None
    #: PERSONALITY MEMORY (``gnomon.memory``, 2026-09-28): where the
    #: deterministic ``remember`` writer appends this agent's memories.
    #: Both OPTIONAL; ``None`` = resolve by convention:
    #:   * ``personality_corpus_path`` — the lifelog ``.lts`` file. Default
    #:     ``<intelligences_root>/<instance_id>/personality/lifelog.lts``.
    #:   * ``personality_prefix`` — the node-id prefix (``AA`` for alpha-architect).
    #:     Default: read from the file's own ``<PREFIX>-LIFELOG`` root node id.
    #: ⛔ These are OPERATOR-set (profile.json); the agent-facing surfaces never
    #: accept a path/corpus argument — an agent can only write its own self.
    personality_corpus_path: Optional[str] = None
    personality_prefix: Optional[str] = None
    #: per-instance feature-flag OVERRIDE ``{<name>: bool}``,
    #: winning over the machine config's ``flags``. ``None`` = inherit. Read
    #: only through :func:`gnomon.flags.flag`.
    flags: Optional[Dict[str, bool]] = None
    #: Security posture D3 (operator ruling 2026-09-29): EXACT env var
    #: names (no globs) this agent's child is allowed to inherit despite the
    #: default credential scrub (``*_API_KEY`` / ``*_TOKEN`` / ``*_SECRET`` /
    #: ``AWS_*`` / ``AZURE_*``), e.g. ``["GITHUB_TOKEN"]``. ``None`` = scrub all.
    #: Likely candidates (deliberately NOT exempted — a profile names what it needs):
    #: ``GH_TOKEN`` / ``GITHUB_TOKEN`` (gh, git over https), ``HF_TOKEN`` /
    #: ``HUGGING_FACE_HUB_TOKEN`` (Hugging Face), ``ANTHROPIC_API_KEY`` /
    #: ``ANTHROPIC_AUTH_TOKEN`` (only if claude itself authenticates via env).
    #: Already kept without passthrough: AWS_REGION, AWS_DEFAULT_REGION, AWS_PROFILE,
    #: AWS_CONFIG_FILE, AZURE_CONFIG_DIR (config, not secrets).
    #: Validated at launch (``harness.spawn.validate_env_passthrough``).
    env_passthrough: Optional[List[str]] = None
    #: the lts corpus this agent OWNS procedures in, the only corpus its
    #: ``sync_my_skills`` self-verb will project skills from (``skills/verb.resolve_corpus``). ``None``
    #: = unknown = the verb refuses. OPERATOR-set (profile.json); never an agent-facing argument.
    skill_corpus: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "Profile":
        if not isinstance(data, dict):
            raise ValueError("profile data must be a JSON object")
        instance_id = data.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("profile requires a non-empty string 'instance_id'")
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        # Tolerate (preserve-by-ignoring) unknown keys rather than crashing on a
        # hand-edited file that carries an extra note. We only lift known fields.
        kwargs = {k: v for k, v in data.items() if k in known and k != "instance_id"}
        prof = cls(instance_id=instance_id, **kwargs)  # type: ignore[arg-type]
        if not prof.mcp_servers:
            prof.mcp_servers = list(DEFAULT_MCP_SERVERS)
        return prof


class ProfileStore:
    """CRUD over per-instance ``profile.json`` files under an agents root.

    Every operation is a plain file read/write of UI-editable JSON — there is no
    database and no schema migration. ``root`` defaults to
    ``~/gnomon-data/agents`` and is injectable for tests.
    """

    def __init__(self, root: Optional[str] = None) -> None:
        self._root = os.path.abspath(
            os.path.expanduser(root if root is not None else default_intelligences_root())
        )

    @property
    def root(self) -> str:
        return self._root

    def instance_dir(self, instance_id: str) -> str:
        _validate_instance_id(instance_id)
        return os.path.join(self._root, instance_id)

    def profile_path(self, instance_id: str) -> str:
        return os.path.join(self.instance_dir(instance_id), "profile.json")

    # -- CRUD ----------------------------------------------------------------

    def create(self, profile: Profile) -> str:
        """Write a NEW profile. Refuses to clobber an existing one."""
        path = self.profile_path(profile.instance_id)
        if os.path.exists(path):
            raise FileExistsError(f"profile already exists: {path}")
        return self._write(profile, path)

    def read(self, instance_id: str) -> Profile:
        path = self.profile_path(instance_id)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            raise KeyError(f"no profile for instance_id {instance_id!r}") from None
        return Profile.from_dict(data)

    def update(self, profile: Profile) -> str:
        """Overwrite an EXISTING profile. Refuses to create by update."""
        path = self.profile_path(profile.instance_id)
        if not os.path.exists(path):
            raise KeyError(f"no profile for instance_id {profile.instance_id!r}")
        return self._write(profile, path)

    def upsert(self, profile: Profile) -> str:
        """Create or overwrite — the convenience the GUI 'Save' button wants."""
        return self._write(profile, self.profile_path(profile.instance_id))

    def delete(self, instance_id: str) -> None:
        path = self.profile_path(instance_id)
        try:
            os.remove(path)
        except FileNotFoundError:
            raise KeyError(f"no profile for instance_id {instance_id!r}") from None

    def list_ids(self) -> List[str]:
        """Instance ids that have a ``profile.json``, sorted."""
        if not os.path.isdir(self._root):
            return []
        ids: List[str] = []
        for name in os.listdir(self._root):
            if os.path.isfile(os.path.join(self._root, name, "profile.json")):
                ids.append(name)
        return sorted(ids)

    def list_profiles(self) -> List[Profile]:
        return [self.read(i) for i in self.list_ids()]

    # -- internals -----------------------------------------------------------

    def _write(self, profile: Profile, path: str) -> str:
        # ⛔ SECURITY (slice-2b cold audit MEDIUM): the per-instance dir is
        # <root>/<instance_id>; a pre-planted symlink there would send the write
        # off-root even for a SAFE-shaped id. Resolve-and-contain BEFORE makedirs.
        ensure_contained(self._root, os.path.dirname(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Atomic-ish: write a temp then replace, so a crash mid-write can't leave
        # a truncated profile the GUI would fail to parse.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(profile.to_dict(), fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
        return path


# The id becomes a single on-disk path component; NAME_MAX is 255 on macOS/most
# POSIX filesystems. A longer id raises an uncaught OSError(ENAMETOOLONG) deep in
# makedirs at launch / DSDStore.persist (slice-2b cold audit LOW). Cap it here so
# the refusal is a clean ValueError at the validation site, not a crash later.
MAX_INSTANCE_ID_LEN = 255


def _validate_instance_id(instance_id: str) -> None:
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("instance_id must be a non-empty string")
    # The id names a directory; forbid separators and traversal so a profile
    # cannot escape the agents root.
    if instance_id in (".", "..") or "/" in instance_id or "\\" in instance_id or "\0" in instance_id:
        raise ValueError(f"unsafe instance_id: {instance_id!r}")
    if len(instance_id) > MAX_INSTANCE_ID_LEN:
        raise ValueError(
            f"instance_id too long: {len(instance_id)} > {MAX_INSTANCE_ID_LEN} chars"
        )


def ensure_contained(root: str, target: str) -> str:
    """Refuse a write whose REAL path escapes *root* — a symlink-target guard.

    ``_validate_instance_id`` rejects a traversal-SHAPED id, but a SAFE-shaped id
    can still escape if an attacker pre-plants a symlink at ``<root>/<id>`` (or any
    prefix) pointing outside: ``os.makedirs(..., exist_ok=True)`` then follows it
    and the write lands off-root (slice-2b cold audit MEDIUM). ``realpath``
    resolves every symlink component, so we can compare the resolved target to the
    resolved root BEFORE any makedirs/open. Returns the resolved target on success;
    raises ``ValueError`` otherwise. Non-existent leaf components are fine —
    realpath resolves the existing prefix (including a planted symlink) and appends
    the rest lexically.
    """
    root_real = os.path.realpath(root)
    target_real = os.path.realpath(target)
    if target_real != root_real and not target_real.startswith(root_real + os.sep):
        raise ValueError(
            f"path escapes root: {target!r} resolves to {target_real!r}, "
            f"outside {root_real!r}"
        )
    return target_real
