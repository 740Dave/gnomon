This repository is the Gnomon engine, the harness, local hub and web dashboard with which one person runs and leads a team of Claude Code agent sessions on one Mac. The remainder of this document uses the FactLattice format.

<!-- gnomon-platform-lts:start -->
## [GP-PLATFORM] The Gnomon Platform

- kind: platform
- goal: The Gnomon Platform makes it easy to lead an outstanding AI team that remembers, collaborates with you and produces extraordinary results.
- scope: four fundamental problems that come up when one person leads a team of AI agents
- provides-wrappers: value-added wrappers around native AI services
- provides-mcp: a range of MCP services
- provides-web-interface: a web interface for managing agent interactions and multiple projects (workspaces), each with their own agent profiles and capabilities
- problem-memory: an agent starts each session without what the team learned before, and even when a memory store exists it may answer without consulting it
- solution-memory-storage: specs, decisions and each agent's memory live as plain-text FactLattice files that factlattice-engine can query
- model-independence: the memory layer is independent of any model or vendor: FactLattice files are plain text, factlattice-engine runs no model inference, and it serves the graph over MCP, an open protocol, to any MCP client that can launch a local stdio server (measured: Claude Code and OpenAI Codex CLI); an agent built on another model can read and write the same knowledge graph
- harness-scope: only the Gnomon engine harness is tied to one agent today (Claude Code, on macOS); Codex support and Windows and Linux support are planned work in the harness, not limits of the memory layer
- solution-memory-rendering: each agent's definition is rendered at launch with its memory inlined, and a failed expansion is shown loudly
- problem-ambiguity: given an unclear spec, or too little context, an agent may guess silently
- solution-ambiguity-document: the agent puts the choice to the pilot as a decision document, with options, tradeoffs and a recommendation
- solution-ambiguity-answer: the pilot's answer is recorded and delivered back to the agent
- problem-agent-behaviour: a confident report can rest on a check that never ran
- solution-agent-behaviour-record: the harness records every action it takes with its source
- solution-agent-behaviour-status: status is reported from evidence about the live object
- solution-agent-behaviour-review: every change is reviewed by someone who did not write it
- problem-synthesis: what a decision needs is scattered across many documents
- solution-synthesis: decisions, specs and the links between them form one graph that factlattice-engine can follow, so an agent can gather what bears on a choice
- design-principles: https://github.com/740Dave/gnomon/blob/main/docs/design-principles.md
- design-principles-covers: pillars of outstanding teams (among them compelling purpose, psychological safety, complementary strengths between the human and the agents) and the principles of the pilot's experience
- source: README.md between the gnomon-platform-opening markers, "Why Gnomon Exists"

### [GP-GNOMON-ENGINE] Gnomon engine

- part-of: [GP-PLATFORM]
- repository: https://github.com/740Dave/gnomon
- role: the heart of the platform
- required: for the full experience of the platform
- provides: the harness that runs the team, and the web dashboard the pilot uses to lead it
- status: draft release
- includes-decisions: the Gnomon engine ships its own decision-support documents (gnomon/dsd/); the Decision engine is the same capability as a standalone project
- source: README.md between the gnomon-platform-opening markers, "The projects that make up the Gnomon Platform", Gnomon engine row
- source-status: README.md of https://github.com/740Dave/gnomon ("Draft. This README is being written for the first public release.") and the header of its docs/design-principles.md (status: draft)

### [GP-FACTLATTICE-ENGINE] factlattice-engine

- part-of: [GP-PLATFORM]
- repository: https://github.com/740Dave/factlattice
- service-name: `lts-engine`
- role: the team's memory and shared knowledge
- provides: a memory graph agents can query, traverse and update over MCP
- storage: plain-text FactLattice (`.lts`) files that people can read, diff and version
- use-in-definitions: agent definitions embed factlattice-engine queries for essential knowledge
- use-by-agents: agents query anything else when they need it, and can update their own or the team's shared memory graph
- write-visibility: the next query sees the change
- model-independence: no model inference in the engine; any agent, of any model or vendor, whose MCP client can launch a local stdio server can use the same graph (measured: Claude Code and OpenAI Codex CLI); a client limited to remote MCP servers over HTTP needs a bridge; the files themselves need no engine at all to read
- measured-other-client: on 2026-10-28 the maintainers ran OpenAI Codex CLI 0.164.0 (a GPT-6 model) against its own factlattice-engine MCP server process, with the same code and registry, and so the same graph, as a Claude Code session; Codex fetched a node with all its properties, traversed its edges, and wrote a new node through the typed writer, and the Claude Code session then read back exactly what Codex wrote (Codex needs the server's tools approved in its configuration)
- os: POSIX; the MCP server, `call` command line and typed writers import `fcntl`, so not Windows; the optional always-on embedder uses launchd (macOS only); the repository records testing on macOS only
- status: draft release
- readme-status: FactLattice 1.0 — DRAFT; notation and four engine layers implemented and tested
- source: README.md between the gnomon-platform-opening markers, "The projects that make up the Gnomon Platform", factlattice-engine row
- source-status: README.md of https://github.com/740Dave/factlattice, "Status"
- source-service-name: README.md of https://github.com/740Dave/factlattice, engine description

### [GP-DECISION-ENGINE] Decision engine

- part-of: [GP-PLATFORM]
- repository: https://github.com/740Dave/decision-engine
- provides: decision-support documents (DSDs) and the decisions-made graph
- role: a single, consistent way for multiple agents to ask the pilot for decisions
- storage: every decision stored persistently in FactLattice files that factlattice-engine can query
- status: placeholder
- source: README.md between the gnomon-platform-opening markers, "The projects that make up the Gnomon Platform", Decision engine row
- source-status: README.md of https://github.com/740Dave/decision-engine, "This repository" ("Coming soon.")

### [GP-GNOMON-PLUGINS] Gnomon plugins

- part-of: [GP-PLATFORM]
- repository: https://github.com/740Dave/gnomon-plugins
- kinds: platform-wide plugins and workspace-specific plugins
- role: a library of optional, platform-wide capability enhancements
- workspace-plugins: typically developed by users to meet a particular project's needs
- status: placeholder
- source: README.md between the gnomon-platform-opening markers, "The projects that make up the Gnomon Platform", Gnomon plugins row
- source-status: README.md of https://github.com/740Dave/gnomon-plugins, "This repository" ("Coming soon.")

### Which parts of the Gnomon Platform to use: a decision matrix [GP-ADOPTION-MATRIX]

- part-of: [GP-PLATFORM]
- kind: decision-matrix
- question: which projects of the Gnomon Platform does a given need call for
- rule: read the rows in order; use the first row whose need matches; the status column says whether the project can be used today
- row-1-need: only a structured way for agents to ask a human for decisions, and a record of the answers, without the rest of the platform
- row-1-use: [GP-DECISION-ENGINE]; it writes plain FactLattice files, and [GP-FACTLATTICE-ENGINE] is optional, for querying them; [GP-GNOMON-ENGINE] also includes this capability, if the whole engine is wanted
- row-1-status: not available yet: the repository is a placeholder
- row-2-need: only a memory graph agents can query, traverse and write, kept as plain-text files
- row-2-use: [GP-FACTLATTICE-ENGINE] alone, as a Python library, a command line, or an MCP server registered in an MCP client; no other platform project is needed
- row-2-status: draft release
- row-3-need: to lead a team of Claude Code agents on one Mac, with shared memory, decision documents and a web dashboard
- row-3-use: [GP-GNOMON-ENGINE] with [GP-FACTLATTICE-ENGINE]; decision documents are built into [GP-GNOMON-ENGINE]
- row-3-status: Gnomon engine draft release; factlattice-engine draft release
- row-4-need: optional extras such as printing, PDF handling or attention lights
- row-4-use: [GP-GNOMON-PLUGINS], plugins for [GP-GNOMON-ENGINE]
- row-4-status: not available yet: the repository is a placeholder
- row-5-need: an agent or model other than Claude Code, for example OpenAI Codex
- row-5-use: for memory, [GP-FACTLATTICE-ENGINE] today: any MCP client that can launch a local stdio server can query, traverse and write the same graph, with no dependency on Claude Code (see its measured-other-client line); to run the team in a harness, [GP-GNOMON-ENGINE] supports Claude Code only today
- row-5-status: factlattice-engine: usable from MCP clients that launch local stdio servers, two measured (draft release); Gnomon engine: Claude Code only, Codex support stated as in progress in its README
- row-6-need: an operating system other than macOS
- row-6-use: [GP-FACTLATTICE-ENGINE] is POSIX: its MCP server, `call` command line and typed writers import the POSIX-only `fcntl` module, so they cannot run on Windows; its optional always-on embedder uses launchd, which is macOS only; the repository records no testing outside macOS; [GP-GNOMON-ENGINE] requires macOS
- row-6-status: Gnomon engine: macOS only, Windows and Linux support stated as planned in its README; factlattice-engine: not Windows, other POSIX systems untested
- source: the status and role lines of the project nodes above; README.md of https://github.com/740Dave/gnomon, Requirements; README.md of https://github.com/740Dave/decision-engine, This repository; factlattice/authoring/edit.py of https://github.com/740Dave/factlattice (the fcntl import)
<!-- gnomon-platform-lts:end -->

## [GNE-ENGINE] Gnomon engine

- kind: repository-summary
- is: the Gnomon engine; runs a team of Claude Code agent sessions on one Mac and lets one person, the pilot, lead them
- part-of: [GP-PLATFORM]
- components: harness, local hub, web dashboard, decision documents (DSDs), agent definitions rendered at launch, self-verbs, deny guard
- term-pilot: the human who runs Gnomon and the agent sessions on their machine; called `operator` in code, configuration and API names
- term-gnomon: in the design principles below their opening layer, a bare "Gnomon" means the Gnomon engine
- readme-status: draft for the first public release; sections marked TODO are not to be relied on
- design-principles-status: draft, version 0.5; 23 technical principles (DP-GN-1 to DP-GN-23), 5 experience principles (EP-GN-1 to EP-GN-5)
- design-principles-test-form: each technical principle ends with a test of the form "If ..., the design has failed"
- licence: MIT; file `LICENSE`
- source: README.md lines 1-4, 34-36, 49-53, 109-111; docs/design-principles.lts frontmatter, GN-PRINCIPLES, GN-GLOSSARY (gnomon-engine, pilot, terminology)

## [GNE-REQUIREMENTS] Requirements

- os: macOS only; uses AppleScript to open terminal windows, and launchd
- os-tested: test suite run on macOS only
- claude-code: required by the Gnomon harness, which runs Claude Code sessions and depends on its hook interface, terminal behaviour and transcript layout (README.md line 58)
- claude-code-measured-versions: 2.1.284, 2.1.287
- python: 3.12 or later (`requires-python` in `pyproject.toml`)
- headless-core-dependencies: standard library only
- dashboard-dependencies: FastAPI and uvicorn, via the `web` extra: `pip install -e '.[web]'`
- factlattice: optional; imported lazily by the DSD writer and the graph viewer, which report a clear error when it is absent; not on PyPI; install `pip install 'factlattice @ git+https://github.com/740Dave/factlattice'` or this package's `factlattice` extra
- factlattice-engine: optional MCP server, service name `lts-engine`; used for memory and for rendering agent definitions; ships in the FactLattice repository; runs as `python3 -m factlattice_engine.server`
- factlattice-engine-wiring: Gnomon expects an `lts-engine` command on `PATH`; add a wrapper script of that name that runs the server
- default-mcp-servers: `lts-engine`, `gnomon`
- terminals: iTerm2, Terminal.app
- source: README.md lines 38, 55-62

## [GNE-QUICKSTART] Quickstart

- install: `pip install -e '.[web]'` from a checkout
- data-root-default: `~/gnomon-data`
- data-root-override: `~/.config/gnomon/roots.toml`, or a file named by environment variable `GNOMON_CONFIG`, with keys `data_root` and `deploy_root`
- machine-config: `<data root>/config.json`; optional; absent or partial file resolves to defaults
- flag-setting: in machine config under `"flags"`, e.g. `{"flags": {"agent_mail": true}}`
- agent-profile-path: `<data root>/agents/<id>/profile.json`
- agent-profile-keys: `instance_id`, `display_name`, `claude_md_source` (the agent definition, a markdown file rendered fresh for every session), `bootstrap_prompt`, `terminal`
- agent-profile-terminal-values: `iterm2`, `terminal_app`
- start-dashboard-and-hub: `cd web && python3 -m backend.server`; dashboard and hub run in one process
- listen-address: `127.0.0.1` only
- port-default: 8770; override with `GNOMON_WEB_PORT` and `GNOMON_WEB_HOST`
- url: `http://127.0.0.1:8770/`
- first-launch: Agents tab, pick the profile, launch; the session opens in the pilot's terminal
- dsd-worked-example: TODO in the README; an example of the agent-side call that sends a decision document does not exist yet
- source: README.md lines 64-83

## [PROC-QUICK-START] Quickstart procedure

- kind: procedure
- part-of: [GNE-ENGINE]
- relates-to: [GNE-QUICKSTART], [GNE-REQUIREMENTS]
- goal: from a checkout, to a first agent session running under Gnomon
- scope: only the steps the README's Quickstart gives; a verify line says "not given in README" where the README gives no way to check the step
- step-1: install from a checkout: `pip install -e '.[web]'`
- step-1-verify: not given in README
- step-2: data and configuration; runtime state lives under one data root, `~/gnomon-data` by default; to move it, create `~/.config/gnomon/roots.toml` (or point the environment variable `GNOMON_CONFIG` at a file) with `data_root = "~/somewhere"` and `deploy_root = "~/somewhere-else"`; the machine configuration is `<data root>/config.json`, optional, and an absent or partial file resolves to defaults; flags go under `"flags"`, for example `{"flags": {"agent_mail": true}}`
- step-2-verify: not given in README
- step-3: create an agent profile, a JSON file at `<data root>/agents/<id>/profile.json`; the README's minimal profile has keys `instance_id`, `display_name`, `claude_md_source`, `bootstrap_prompt`, `terminal`; `claude_md_source` is the agent's definition, a markdown file rendered fresh for every session; `terminal` is `iterm2` or `terminal_app`
- step-3-verify: not given in README
- step-4: start the dashboard and the hub, which run in one process and listen on `127.0.0.1` only: `cd web && python3 -m backend.server`; the default port is 8770, changed with `GNOMON_WEB_PORT` (and `GNOMON_WEB_HOST`)
- step-4-verify: open `http://127.0.0.1:8770/` (with the default port)
- step-5: launch the first agent from the Agents tab: pick the profile and launch it
- step-5-verify: the session opens in your terminal
- step-6: send the agent a decision document to answer; the README marks this TODO: a short worked example of the agent-side call that sends a decision document does not exist yet
- step-6-verify: not given in README
- prerequisites: see [GNE-REQUIREMENTS] (macOS, Claude Code for the harness, Python 3.12 or later)
- source: README.md lines 64-83; the prerequisites line points at README.md lines 55-62

## [GNE-TESTS] Tests

- run: `pip install -e '.[test]'`, then `python -m pytest` from the repository root (the README text says `pytest`; the counts below were measured with `python -m pytest`)
- scope: a public subset of the project's own tests
- external-needs: none; no live Claude Code install, no browser, no network beyond loopback
- web-tests: collected only with the `web-test` extra (`pip install -e '.[web-test]'`)
- factlattice-tests: collected only with FactLattice installed
- skip-report: `pytest` prints how many files were skipped for each missing extra
- measured-core: 2698 passed, 3 skipped; macOS, Python 3.13; fresh virtual environment with pytest only, Gnomon not installed into it; about four minutes
- measured-core-editable-install: with `pip install -e '.[test]'`, 2692 passed, 6 failed, 3 skipped; the six failures are in `tests/test_child_python_tree.py`, which asserts a child Python process cannot import the tree, and an editable install makes it importable
- measured-web-test: 3078 passed, 3 skipped; macOS, Python 3.13; fresh virtual environment with pytest, FastAPI, uvicorn and httpx, Gnomon not installed into it; about five minutes; not measured with an editable install
- hidden-unicode-notice: expected on GitHub; some test files contain deliberate invisible and bidirectional Unicode characters to test the code that strips or refuses them
- source: README.md line 87

## [GNE-HARNESS] Harness

- is: the small process between an agent session and the pilot's terminal; carries everything in and out of the session
- mechanism: transparent pseudo-terminal proxy in the pilot's real terminal; rendering, keys and escape handling stay with the terminal; no terminal emulator
- terminal-launch: through a configurable launch-command template
- browser-rule: a web page never starts a terminal; the browser asks, a local daemon acts
- write-site: pilot keystrokes and harness-originated text converge on one write site; the injection gate there holds, refuses or writes each piece of harness text
- gate-exemptions: the pilot's own keystrokes; one start-up prompt handler, identified by how it is called
- gate-coverage-rule: a Gnomon code path that writes to a session without passing the gate is a defect
- long-text: bracketed paste with a separate submit, never one oversized raw write
- first-prompt: passed as a launch argument, not typed
- input-line-empty: receives harness text after a short quiet moment
- input-line-partly-typed: harness text held until the pilot sends or clears the line
- input-line-unknown-state: treated as empty only after several minutes with no pilot typing
- input-line-reader-failure: line treated as busy
- open-dialog: holds harness text until it closes or a person releases it; no time-based release
- compaction-exception: by default, a half-typed line left untouched for a long idle period is saved, the session compacts, and the text is restored; nothing else clears the pilot's line
- dialog-and-line-detection: best-effort, with stated gaps
- side-channel: guidance that cannot wait is read by the agent at its next tool call, not typed
- lifecycle: harness never compacts or redirects a session on its own judgement; context thresholds produce notices that grow louder near the limit; the only forced compaction is Claude Code's own
- premature-request-handling: reflective refusal; a second, reasoned call overrides it
- session-end-from-outside: only the pilot, or the confirmed time limit of a pilot-confirmed scheduled job that started the session
- reminders: only for items on the agent's own open-work list, at an interval it chose
- action-record: every piece of text the harness types and every action it takes is recorded with kind, source and outcome
- action-record-fault: for harness-initiated text the record is reserved before typing and nothing is typed if it cannot be written; for other injections the injection proceeds with a counter and a warning
- source: README.md lines 38-39; docs/design-principles.lts GN-GLOSSARY (harness, injection-gate), DP-GN-1, DP-GN-2, DP-GN-3, DP-GN-4, DP-GN-17

## [GNE-HUB] Hub

- is: local service every harnessed session connects to; carries messages and tool calls between sessions, the dashboard and the pilot
- process: same process as the dashboard
- listen: local machine only (`127.0.0.1`)
- local-only-limit: keeps the services off the network; does not protect them from other programs on the same machine or from browser pages that send requests to local addresses
- port: a new port each time the dashboard process starts; see [GNE-KNOWN-LIMITS]
- identity-binding: per-launch credential issued by Gnomon; a name in a tool argument is checked against it; behind a flag that defaults to off
- tool-access-registry: every hub tool declares in one registry who may call it: the pilot, the credential's agent, or anyone
- source: README.md lines 40, 81, 91; docs/design-principles.lts GN-GLOSSARY (hub), GN-PRINCIPLES threat-model, DP-GN-6

## [GNE-DASHBOARD] Dashboard

- is: web dashboard; shows the live sessions; where the pilot answers decision documents
- server: `web/backend/server.py`, started as `python3 -m backend.server` from `web/`
- tabs-named: Agents (launches profiles), Config (lists every flag with a one-line description and its state)
- agent-written-text: shown as plain text, size-limited, labelled as that agent's words; never rendered as markup
- flagged-tab-or-route: absent when its flag is off; answers as if it did not exist
- team-view: lists live sessions today; a team view across workspaces is planned, not built
- confirm-rule: agents propose installs, scheduled jobs and changes; nothing takes effect until the pilot confirms in the dashboard; no agent-callable tool performs a confirm
- confirm-binding: the confirm records the definition and the code that will run; any later change refuses the next run until the pilot confirms again
- source: README.md lines 41, 47, 81-82; docs/design-principles.lts EP-GN-3, DP-GN-7, DP-GN-8, DP-GN-12

## [GNE-DSD] Decision documents

- is: decision-support document (DSD); a structured question to the pilot with the options, their tradeoffs and a recommendation
- flow: agent sends a DSD; pilot answers in the dashboard; answer recorded and delivered back to the agent
- storage: answers stored as plain-text FactLattice files
- options-default: every option unset when opened; recommendation shown as a label
- answer-record: whether the answer followed the recommendation, plus any comment the pilot added
- answer-notice: fixed template filled only with strictly validated identifiers
- related-project: GP-DECISION-ENGINE
- source: README.md lines 25, 42; docs/design-principles.lts EP-GN-2, DP-GN-7, DP-GN-12

## [GNE-AGENT-DEFINITIONS] Agent definitions

- is: the file that describes an agent (role, tools, standing instructions); profile key `claude_md_source`
- rendering: fresh at every session start, with the agent's durable memory and referenced graph content from factlattice-engine inlined
- failure-rule: a failed expansion is shown loudly; the renderer reports every outcome other than success to the session and the pilot
- memory-location: the agent's own factlattice-engine corpus; each agent writes only to its own, by convention
- shared-text: one included fragment, never pasted copies
- source: README.md lines 43, 80; docs/design-principles.lts GN-GLOSSARY (agent-definition), DP-GN-21

## [GNE-USES-FACTLATTICE-ENGINE] How Gnomon uses factlattice-engine

- kind: usage
- part-of: [GNE-ENGINE]
- relates-to: [GP-FACTLATTICE-ENGINE], [GNE-AGENT-DEFINITIONS], [GNE-DSD]
- scope: only what the code in this repository does; each line ends with `file:line` pointers into this repository at the commit that carries this file
- mechanism: Gnomon imports `factlattice_engine` in-process (`config.build_store`, `tools.LtsEngine`, `authoring.add_node`); it does not call a running `lts-engine` MCP server for these jobs (gnomon/dsd/live.py:8-14, gnomon/dsd/live.py:93-98, gnomon/lts_expand.py:357-362, gnomon/memory.py:455)
- registry: the corpus registry is found from env `GNOMON_LTS_CORPUS_CONFIG`, then env `FACTLATTICE_CORPUS_CONFIG`, then the `lts-engine` entry in `~/.mcp.json`; Gnomon never guesses a location (gnomon/app/graph_viewer.py:70-97)
- agent-sessions: separately, the default MCP server set that agents get names `lts-engine` and `gnomon`, so an agent can query the graph itself (gnomon/config.py:54; README.md line 62)
- rendering-macro: an agent definition contains markers of the form `{{lts: <corpus>:<SELECTOR> [key=value ...] [form=full|digest] [order=newest|file] [budget=N] [empty=ok] }}`; the renderer replaces each with the result of a query on that one corpus (syntax: gnomon/lts_expand.py:11-13; `order`: gnomon/lts_expand.py:32-35; `budget`: gnomon/lts_expand.py:36-53; `empty=ok`: gnomon/lts_expand.py:54-59)
- rendering-selector: `<SELECTOR>` is one node id or an id glob, matched case-sensitively against every node of that corpus; a node from any other corpus can never appear (gnomon/lts_expand.py:15-20)
- rendering-filters: `key=value` keeps nodes whose property equals the value (any element of a list-valued property); several filters are ANDed (gnomon/lts_expand.py:21-24)
- rendering-full: `form=full` (default) renders each matched node whole: title, every property, body prose (gnomon/lts_expand.py:25-26)
- rendering-digest: `form=digest` renders one line per node (id, `when` if present, summary), newest first by default, with no count cutoff, and a closing line saying "complete (no cutoff)" and how to fetch one node in full (gnomon/lts_expand.py:27-31, gnomon/lts_expand.py:542-562)
- rendering-digest-budget: with `budget=N` (approximate tokens, characters divided by 4) entries are withheld whole, never cut mid-entry; one overflow line names how many were shown and the `lts_find(..., tier="digest")` call for the rest, and the digest footer then says it is not complete (gnomon/lts_expand.py:36-45, gnomon/lts_expand.py:558-561, gnomon/lts_expand.py:591-611)
- rendering-moments: the definition is re-rendered at launch, resume, spawn and before a compaction, each as a new numbered file that is never overwritten, plus the delivered `.claude/CLAUDE.md` (gnomon/agent_def.py:3-8, gnomon/agent_def.py:17-22)
- freshness: each query goes to an engine built with `self_heal=True`; a stale corpus that cannot be healed, or a truncated (incomplete) result, is a failure, never a partial expansion (gnomon/lts_expand.py:357-362, gnomon/lts_expand.py:459-464)
- failure-banner: a marker whose expansion fails (registry unresolved, engine not importable or not buildable, corpus unregistered or stale, malformed query, or zero matches) is replaced in the rendered text by a visible line: `lts expansion FAILED for <query>: <reason> — your memory was NOT loaded; read <corpus files> directly and flag it to the pilot` (gnomon/lts_expand.py:77-85, gnomon/lts_expand.py:279-284, gnomon/lts_expand.py:514-517)
- failure-zero-is-failure: zero matches is a failure unless the marker says `empty=ok`, which renders a visible "0 nodes matched" notice instead; engine, registry and corpus failures still fail loudly under `empty=ok` (gnomon/lts_expand.py:54-57, gnomon/lts_expand.py:469-478)
- failure-in-rendered-file: the header of the rendered file carries `lts_expansion=<status>`, and when any expansion failed it adds a line saying how many failed and that "that memory was NOT loaded" (gnomon/agent_def.py:157-174, gnomon/agent_def.py:436-448)
- failure-recorded: the render, its expansion status and every failure are also written to the session's `launch.json` and its provenance log (gnomon/agent_def.py:12-14)
- failure-render-error: if the whole render fails, a usable previous definition is kept and a banner line is prepended to it; with none, the unexpanded source is delivered under a banner, or a banner-only definition (gnomon/agent_def.py:269-326)
- failure-cli: the command-line renderer can be run strict, so any failed expansion aborts instead of leaving a banner (gnomon/agent_render.py:281-285, gnomon/agent_render.py:304-307)
- dsd-write: an answered decision document is written as typed nodes through `factlattice_engine.authoring.add_node`, the function the `lts_add_node` tool fronts, so an illegal property key cannot be written and each write is checked against the real parser (gnomon/dsd/writer.py:1-10, gnomon/dsd/writer.py:172, gnomon/dsd/writer.py:271-276)
- dsd-files: the nodes of each answered decision document go into their own `<id>.lts` file inside the answers corpus directory; a legacy single answers file is read only, to detect an id already answered (gnomon/dsd/writer.py:12-18, gnomon/dsd/writer.py:120-128)
- dsd-no-local-index: Gnomon builds no index of its own for answers; the engine reads the corpus files directly (gnomon/dsd/writer.py:20-24)
- dsd-live-check: whether an answer is queryable is checked live by building the engine's store from the registry and asking whether the corpus is loaded and reachable and the just-written node is visible; when it is not, the dashboard says so loudly (gnomon/dsd/live.py:3-24, gnomon/dsd/writer.py:24-28)
- memory-verb: the `memory` self-verb records one memory in the calling agent's own personality corpus; the corpus file is resolved from the harness-supplied agent identity, with no path or corpus argument (gnomon/harness/self_mcp.py:199-215, gnomon/memory.py:29-36)
- memory-write: the write goes through `factlattice_engine.authoring.add_node` and its gate: the new node is the only change, and nothing on any existing node moves; a refusal writes nothing and is returned as a one-line reason (gnomon/memory.py:11-18, gnomon/memory.py:455-462)
- memory-readback: after the write the node is read back through the engine (with self-heal); if it cannot be read back, or differs, the receipt says "INDEX NOT FRESH" and the result is not ok even though the file was written (gnomon/memory.py:19-27, gnomon/memory.py:478-499)
- memory-next-session: a saved memory is expanded into the agent's definition at the next render by the same macros (gnomon/harness/self_mcp.py:205-207, gnomon/agent_def.py:3-5)
- other-readers: the graph viewer also builds the engine's store in-process and queries it (gnomon/app/graph_viewer.py:102-103)
- files-not-harness-bound: the memory files, DSD answers and the data that agent definitions pull in at render time are plain FactLattice `.lts` files (memory: `agents/<id>/personality/lifelog.lts`, gnomon/memory.py:3-8 and gnomon/memory.py:106; DSD answers: one `<id>.lts` file each, gnomon/dsd/writer.py:12-14 and gnomon/dsd/answers_reader.py:1-8; definition data read at render: gnomon/lts_expand.py:11-13); any MCP client that can launch a local stdio server can read them through factlattice-engine (the server is registered as a stdio command, gnomon/config.py:169; README.md line 62); it is the Gnomon harness that runs Claude Code, not these files
- optional-dependency: if the engine cannot be imported, the DSD writer and the graph viewer report an error, and the renderer shows the failure banner; the headless core does not need it (README.md line 61; gnomon/lts_expand.py:77-85)
- tests-in-this-repo: scoped to what was searched in `tests/`: this repository's public tests exercise the DSD writer and reader (for example tests/test_dsd_answers_reader.py, tests/test_dsd_live.py, tests/test_dsd_answer_header_id.py); the macro expander (`gnomon/lts_expand.py`) has no public test (no file mentions `lts_expand` or `LtsExpander`); the memory-recording call (`gnomon/memory.py`) has no public test; definition rendering (`gnomon/agent_def.py`) is reached by one public test, tests/test_x13s1_settings_registry.py:95-107, through the launcher (render_agent_def is called at gnomon/launch.py:366) with a definition that has no macros, so macro expansion is not exercised there; other tests import `gnomon.memory` and `gnomon.agent_def` for other functions (tests/test_compact_ignored_readers.py:22 imports `last_compaction`; tests/test_relaunch_strip_derived_keys.py:185 imports `edit_launch_json`); positive control for the search: `deny_guard` is mentioned in 70 lines under `tests/`
- not-confirmed-in-public-code: that the next query of a running `lts-engine` server sees a memory or answer written by Gnomon is a property of factlattice-engine (stated in its README), not shown by any code in this repository; Gnomon's own check is the read-back and live check above
- source: the file:line pointers in each line, at the commit that carries this file

## [GNE-SELF-VERBS] Self-verbs

- is: a tool in an agent's own tool list that acts on its own session, its own processes, a service it works on, or its own records
- example: asking for its own compaction; restarting a service it works on
- caller-identity: supplied by the harness; a self-verb never takes the caller's session as an argument
- enablement: on only when the agent definition lists it AND the verb's flag permits it AND the verb policy permits it; the definition only narrows
- listed-but-forbidden: stays off; the go-live check reports it as listed but not permitted
- default-state: several self-verbs, and deriving a session's self-verbs from its definition, are behind flags that default to off
- scope-claim: keeps each agent in its own scope within the cooperative-agent model; not a security boundary
- source: README.md line 44; docs/design-principles.lts GN-PRINCIPLES status, GN-GLOSSARY (self-verb, verb-policy), EP-GN-4, DP-GN-5, DP-GN-6

## [GNE-DENY-GUARD] Deny guard

- is: a hook that inspects each shell command, and each file write or edit through Claude Code's editing tools, that an agent is about to make
- action: refuses a small set of dangerous or protected ones
- class: obvious-filter layer for accidents and honest mistakes; not a security boundary
- gaps: stated; kept on the project's hardening list
- pattern-kill: refuses common pattern-kill commands and points to a session-scoped tool that lists what it will signal first
- change-rule: the guard is only ever extended; a legitimate protected change is met by a new self-verb, never by loosening the guard
- control-cannot-run: by default the operation continues and the failure is loud and recorded
- source: README.md line 45; docs/design-principles.lts GN-GLOSSARY (deny-guard), DP-GN-11, DP-GN-14, DP-GN-18

## [GNE-THREAT-MODEL] Threat model

- assumption: cooperative agents, running as the pilot's own operating-system user, with a shell
- consequence: nothing in Gnomon can stop a determined agent from going around Gnomon; no storage on the machine is out of such an agent's reach
- controls-govern: Gnomon's own surfaces only (its tools, hub, dashboard routes and the input of the sessions it runs)
- controls-designed-for: accidents, honest mistakes, misdirected agents; protecting anything done in the pilot's name or that cannot be undone
- out-of-scope: a deliberately hostile program running as the pilot; a hostile agent using its shell; network attackers
- out-of-scope-findings: kept on the hardening list for a later security review
- never-and-cannot: every "never" and "cannot" in the design principles holds only within this model
- security-setting-rule: a security setting in agent-reachable storage is a known limit, not a closed control
- reporting: `SECURITY.md`
- source: README.md lines 45, 92, 94-96; docs/design-principles.lts GN-PRINCIPLES threat-model, DP-GN-14

## [GNE-FLAGS] Flags policy

- rule: new behaviour sits behind a flag that defaults to off; turning it on is a separate act
- readme-statement: new behaviour is built behind a flag and switched on once reviewed, so some flags default to on
- on-by-default-count: 15 of 66 registered flags at this commit
- on-by-default-breakdown: counted from `gnomon/flags.py` (`KNOWN_FLAGS`): 66 registered, 15 default on, 51 default off; 2 of the 15 are the security flags `confirm_proof` and `dsd_submit_proof`, on from the start (`gnomon/flags.py:54-58`; `docs/design-principles.lts:237-246`); the other 13 are behaviour flags that README.md line 47 says were switched on after review; no public file in this repository gives a per-flag reason beyond that
- default-on-condition: a behaviour flag defaults to on only as a deliberate, recorded deployment choice
- registry: `gnomon/flags.py`; every flag has one declared default in one registry; no reading code supplies its own default
- inspection: effective value inspectable with the layer it came from; Config tab lists every flag with description and state
- setting: machine config `"flags"` key
- security-controls: on from the start; the design principle says that by default a security control has no off switch, and that where a deployment keeps one it defaults to on and a missing setting reads as on (docs/design-principles.lts:247, DP-GN-8); in the code, the two security flags `confirm_proof` and `dsd_submit_proof` and the top-level setting `guard_fail_closed` do have an off switch: from the Config tab they can be turned off only after a passed human-presence check (gnomon/config.py:525-539 refuses a save without one; web/backend/server.py:3008-3027 asks for it), and the server shows a visible warning while one is off (web/backend/server.py:2847-2856); that is not the only route: editing the machine config file by hand turns them off with no check, because loading that file has no integrity check; an agent's write to that file is meant to be refused by the harness deny guard, which is a filter for accidents with known misses, not a security boundary (gnomon/flags.py:54-63); that switch is a known limit
- lasting-state: a flag whose on path writes lasting state says what turning it off does with that state
- specified-but-off: hub credential binding (DP-GN-6); several self-verbs and definition-derived self-verbs (DP-GN-5)
- source: README.md lines 47, 67; docs/design-principles.lts GN-PRINCIPLES status, GN-GLOSSARY (flag), DP-GN-8

## [GNE-KNOWN-LIMITS] Known limits

- hub-restart: restarting the dashboard cuts running sessions off from the hub; the hub listens on a new port each time the dashboard process starts, a running session keeps the address it was given at launch, and its hub tools fail until it is relaunched; fix (a fixed hub port) is in the next update
- guard-and-threat-model: have their own stated limits; see [GNE-DENY-GUARD], [GNE-THREAT-MODEL]
- stale-session-detection: detecting a session that runs behind the current setup is intended, not built; hook settings, tool lists, the hub address and flags given at launch stay fixed for a session's life
- team-view: planned, not built
- platforms: the Gnomon engine runs on macOS only (README.md line 57)
- dsd-example: missing (README TODO)
- dialog-detection: best-effort, with stated gaps
- confirm-record-integrity: protected within the cooperative-agent model; where stronger proof is not built, it is a stated known limit
- platform-results: the expected results of the platform's approach are hypotheses, not measured; no study published
- source: README.md lines 57-58, 83, 89-92; docs/design-principles.lts GN-PRINCIPLES status, GN-ROOTS evidence-status, EP-GN-3, DP-GN-3, DP-GN-7, DP-GN-19

## [GNE-EXPORT-DIFFERENCES] Differences from the internal build

- nature: a cleaned export of a project first built for one team
- data-root: `~/gnomon-data`; agent profiles under `agents/`
- default-mcp-servers: `lts-engine`, `gnomon`; a deployment-specific server is removed from the defaults
- template-symbol: `DATA_ROOT` (default `~/gnomon-data`) names the shared data directory in agent-definition templates
- refused-default-mcp-servers: empty (`REFUSED_DEFAULT_MCP_SERVERS` in `gnomon/config.py`); a deployment can fill it
- not-included: one-time converter modules (`gnomon.workspace.convert`, `apply`, `rules`), the project's specifications, its cut-over tools, most of its tests
- naming: dashboard tab "Agents"; user-facing messages say "the pilot"; some code, configuration and wire-field names keep older words, not renamed
- source: README.md lines 98-107

## [GNE-PROCESS] Development process

- review: every change reviewed by someone who did not write it, on the exact version that will be installed
- red-before-green: each fix's test seen failing before the fix and passing after
- every-session-code: Claude Code hooks, their imports, the definition renderer and the hub are loaded by every running session; changes to them are built on a separate copy, kept inert behind a flag, installed only after an independent review of that copy, then compared byte for byte
- hook-imports: do no work at import time; the imported set is tracked
- concurrency-claims: stated as a failure rate over many runs
- rules: recorded in the open; a changed rule keeps its old text marked with its replacement
- neutrality: the design principles state that the code and tests carry no deployment-specific names, roles, brands or data, and that a neutrality test fails if a deployment-specific entry appears (docs/design-principles.lts:326-330, DP-GN-15); this repository contains no such test (searched `tests/` and the file list at 7b9847c), because most of the project's own tests are not part of this export (README.md line 106)
- process-scope: the lines above are the project's stated practice (docs/design-principles.lts, DP-GN-9, DP-GN-15, DP-GN-23); none of them is an artefact a reader can check from this repository
- source: docs/design-principles.lts DP-GN-9, DP-GN-15, DP-GN-23

## [GNE-FILE-MAP] File map

- readme: `README.md`
- security: `SECURITY.md`
- licence: `LICENSE`
- packaging: `pyproject.toml` (extras `test`, `web`, `web-test`, `factlattice`)
- design-principles-source: `docs/design-principles.lts`
- design-principles-generated: `docs/design-principles.md`, generated from the `.lts` source
- flag-registry: `gnomon/flags.py`
- mcp-defaults-config: `gnomon/config.py`
- dashboard-server: `web/backend/server.py`
- package-subdirectories: `gnomon/harness/`, `gnomon/hub/`, `gnomon/dsd/`, `gnomon/scheduler/`, `gnomon/appshost/`, `gnomon/worklist/`, `gnomon/workspace/`, `gnomon/mail/`, `gnomon/skills/`, `gnomon/evals/`, `gnomon/fetchverbatim/`, `gnomon/app/`, `gnomon/native/`
- web-subdirectories: `web/backend/`, `web/static/`, `web/alerts_ui/`, `web/apps_ui/`, `web/guard_banner_ui/`, `web/ledger_ui/`, `web/mail_ui/`, `web/scheduler_ui/`, `web/work_ui/`
- tests: `tests/`
- other: `scripts/`, `assets/brand/`
- source: file tree of commit 7b9847c (`git ls-tree -r --name-only`); roles from README.md lines 51, 59-60, 81, 47, 103
