"""X13 workspace model — slice S1.

S1 is SCAFFOLDING plus the one-time converter's ``dry-run`` and ``verify`` modes. It is
READ-ONLY on every live root: the converter writes only into an explicit scratch ``--out``
directory, enforced by a process-wide audit-hook write guard (:mod:`.writeguard`), not by
convention.

FLAG: :data:`FLAG_NAME` (``workspace_model``) is registered in ``flags.KNOWN_FLAGS``,
default OFF. S1 has no runtime read site (nothing in the running dashboard consumes the model
yet), so the converter CLI does not require it: a read-only tool must be runnable without
editing live config. The first runtime consumer (S2, the scope library) reads it through
``flags.flag(FLAG_NAME)`` with no inline default (g2).

EXTENSION POINTS created here (STANDING ORDER, g16: later work must honour or wire into each;
they are also listed in the S1 report so the Diamond can check the checklist rule landed):

1. ``settings-registry.json``: every settings key read anywhere must be registered with one
   default, a type and a scope-floor (I7). Adding a key to config.py / launch.json / the flags
   registry without a registry row fails ``tests/test_x13s1_settings_registry.py``.
2. The converter DATA directory of a deployment (``<deploy_root>/workspaces/<ws>/converter/``):
   ``corpus-map.json`` (ordered corpus -> owner table; a newly registered corpus that matches no rule is
   UNTAGGED and fails ``verify``), ``seeds.json``, and ``overrides/<stem>.json`` (per-source node ->
   ``<ws>/<slug>``, one file per source, one schema, unknown top-level keys refused by
   :func:`convert.load_overrides`). These live in the DEPLOYMENT, never in this package (ruling-f4).
3. The converter RULES PLUGIN SEAM (:mod:`.rules`): a deployment's ``rules.py`` exposes ``RULES`` (a
   :class:`rules.ConverterRules`: kind rule table, DATA-root store table, spec claims, heuristics). A new
   engine rule that needs a deployment literal adds a field to ``ConverterRules``; it never hard-codes it.
4. ``ENTITY_DISPOSITION`` in :mod:`.entities`: every ``-*`` node has a converter disposition; a
   new entity row in the spec without one fails ``tests/test_x13s1_entity_dispositions.py`` (g25 reader
   with a floor).
5. ``prefix-registry.json`` (DATA) built by :mod:`.registry`: add-backlog / add-corpus /
   add-workspace (S2) must refuse a prefix already registered, honouring ``legacy-shared``.
6. :mod:`.writeguard`: ANY new converter code path that writes must go through a path under the
   scratch out dir; the guard raises otherwise. Extending the converter to ``--apply`` (S3) means
   changing the guard deliberately, with the operator's cutover go. Its docstring states what it does NOT
   cover; a new write/IPC primitive the converter starts using must be added to the guard WITH a test.
7. :mod:`.fingerprint`: the before/after stat proof. A new live root the converter reads must be
   added to ``convert.read_roots`` so the proof covers it (g31 (c) in spirit).
8. The ``deploy_root`` config key (``config.DEFAULT_DEPLOY_ROOT``, read through :mod:`.deploy`): the ONE
   place the DEPLOY root's physical name lives (provisional until the operator picks it). Nothing else in the
   package or its tests may spell it; the deployment's own tests assert the key points at its directory.
9. ``tests/test_x13s1_neutrality.py``: NEUTRALITY of this package (code AND tests, D49 D1). Its
   identifier list is the contract: a new deployment-specific literal is added to the list (and the
   literal moves to the deployment), never allow-listed into the package.
"""

FLAG_NAME = "workspace_model"
SLICE = "X13-S1"

# ---------------------------------------------------------------------------------------------------------------
# X13 S2: the SCOPE CORE (flag ``workspace_scope_core``, default OFF; see :mod:`.scopebase`).
#
# MODULES: ``scopebase`` (flag gate, errors, IO helpers), ``machine`` (machine.json), ``model`` (workspace.json /
# projects.json; WORKSPACE CONFIG), ``scope`` (the I9 tier-0..3 stamping library), ``store`` (lts / corpus-registry /
# topic substrates), ``writers`` (the typed writers), ``audit`` (read-only I9/I4/I3 audits), ``ownership`` (I1),
# ``events`` (the append-only machine event log), ``settings_resolve`` (the layered resolver), ``bundle`` (the relaunch
# bundle), ``launchhook`` (launch-time prepare/write), ``safety`` (the I6 registry and file-access tracer).
#
# I6 SPLIT: ``bundle``, ``events``, ``machine``, ``settings_resolve``, ``settings_registry``, ``scopebase`` and ``safety``
# are SAFETY modules: their import closure never reaches workspace-config code (``safety.FORBIDDEN_FOR_SAFETY``).
#
# EXTENSION POINTS created by S2 (STANDING ORDER g16; each must be honoured by later work, and each has a guard test):
# 10. ``safety.SAFETY_PATHS``: a new machine safety path (wind-down, usage trigger, bootstrap jobs, BREAK-GLASS verb, the
#     attention read model) is registered with a runner in the SAME change and is then traced for I6. An ``unbuilt`` row
#     names the slice that must flip it (g34 (h)).
# 11. ``ownership.OWNERSHIP``: a new entity (an ENT node) needs an owner row; the I1 test parses the spec and fails on a gap.
# 12. ``writers.SCOPE_KEYS`` and ``scope.KINDS``: a new record kind that carries a project is stamped through
#     ``scope.stamp`` (add a ``KindSpec``) and its scope keys are refused by the direct-edit guard; never stamp by hand.
# 13. the settings registry row ``restrictive-on-skip`` + ``restrictive-value``: a setting whose unreadable workspace layer
#     could hide a restriction names its most restrictive value; ``event_log_path`` is the first machine key registered by S2.
# 14. ``bundle``: anything a relaunch must know about the workspace is added to the bundle AT LAUNCH (a new layer or field),
#     never read from ``workspaces/**`` at relaunch. ``launchhook.prepare`` is where it is assembled.
# 15. ``events.append_event`` is the ONE way to raise an alert/notice: per-target receipts are follow-up events (``ref``);
#     a re-tag is an appended ``retag`` event (the log is append-only).
# 16. ``tests/test_x13s2_safety_paths.py``'s ``SAFETY_MODULES`` / neutrality scope: a new S2-style module is added to the
#     neutrality scan (``tests/test_x13s1_neutrality.py`` covers every ``x13s*`` test and this package).
# ---------------------------------------------------------------------------------------------------------------
SCOPE_CORE_FLAG_NAME = "workspace_scope_core"
