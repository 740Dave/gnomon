# The Gnomon Platform: Gnomon-Engine Repo

<!-- gnomon-platform-opening:start -->
**The Gnomon Platform makes it easy to lead an outstanding AI team that remembers, collaborates with you and produces extraordinary results.**

## Why Gnomon Exists

The Gnomon Platform is built to address four fundamental problems that come up when one person leads a team of AI agents. By so doing, the Gnomon Platform enables more effective collaboration between humans and AI team members. This extensible platform provides value-added wrappers around native AI services, enables a range of MCP services, and provides a web interface for managing agent interactions and multiple projects (workspaces), each with their own agent profiles and capabilities.

| Problem | What the Gnomon Platform does about it |
|---|---|
| **Memory.** An agent starts each session without what the team learned before, and even when a memory store exists it may answer without consulting it. | Specs, decisions and each agent's memory live as plain-text FactLattice files that factlattice-engine can query. Each agent's definition is rendered at launch with its memory inlined, and a failed expansion is shown loudly. |
| **Ambiguity.** Given an unclear spec, or too little context, an agent may guess silently. | The agent puts the choice to you as a decision document, with options, tradeoffs and a recommendation. Your answer is recorded and delivered back to the agent. |
| **Agent behaviour.** A confident report can rest on a check that never ran. | The harness records every action it takes with its source, status is reported from evidence about the live object, and every change is reviewed by someone who did not write it. |
| **Synthesis.** What a decision needs is scattered across many documents. | Decisions, specs and the links between them form one graph that factlattice-engine can follow, so an agent can gather what bears on a choice. |

The Gnomon design principles ([design-principles](https://github.com/740Dave/gnomon/blob/main/docs/design-principles.md)) also describe the pillars of outstanding teams that the platform builds on (among them compelling purpose, psychological safety, and complementary strengths between the human and the agents) and the principles of the pilot's experience. If you find Gnomon useful and are curious about its approach to human-AI collaboration, that document is worth a read.

## The projects that make up the Gnomon Platform

| Project | What it does for the team | Repository |
|---|---|---|
| **Gnomon engine:** The heart of the platform, and required for the full experience | The harness that runs the team, and the web dashboard you, the pilot, use to lead it. | https://github.com/740Dave/gnomon |
| **factlattice-engine:** Gives agents a memory graph they can query, traverse and update over MCP, kept as plain-text FactLattice (`*.lts`) files that people can read, diff and version too. | The team's memory and shared knowledge. Agent definitions embed factlattice-engine queries for essential knowledge; agents query anything else when they need it, and can update their own or the team's shared memory graph; the next query sees the change. | https://github.com/740Dave/factlattice |
| **Decision engine:** decision-support documents (DSDs) and the decisions-made graph | A single, consistent way for multiple agents to ask you for decisions, with every decision you make stored persistently in FactLattice files that factlattice-engine can query. | https://github.com/740Dave/decision-engine |
| **Gnomon plugins:** Gnomon supports platform-wide plugins and workspace-specific plugins. | A library of optional, platform-wide capability enhancements. Workspace-specific plugins are typically developed by users to meet a particular project's needs. | https://github.com/740Dave/gnomon-plugins |

<!-- gnomon-platform-opening:end -->



> **Draft.** This README is being written for the first public release. Sections marked **TODO** depend on facts that are not settled yet; nothing in them should be relied on.

## What it is

The Gnomon engine runs a team of Claude Code agent sessions on one Mac and lets one person, the pilot, lead them.

- **Your real terminal.** Each agent session opens in your own terminal (iTerm2 or Terminal.app) inside a small, transparent harness. The harness does not emulate a terminal: you work with the agent's real interface, as you would without Gnomon.
- **One place for what goes into a session.** Notices, reminders and relayed messages reach a session through one write site in the harness. Each one is recorded with its source, and the harness holds its own text back while you have a half-typed line or a dialog open (the one stated exception saves and restores a long-idle half-typed line around a compaction).
- **A local hub.** Every harnessed session connects to a hub on your machine, which carries messages and tool calls between sessions, the dashboard and you. The hub and the dashboard listen only on the local machine.
- **A web dashboard.** The dashboard shows the live sessions and is where you answer the questions your agents put to you.
- **Decision documents.** When an agent needs a decision from you, it sends a decision document (DSD): the question, the options, their tradeoffs and a recommendation. You answer in the dashboard, your answer is recorded, and it is delivered back to the agent. Answers are stored as plain-text FactLattice files.
- **Agent definitions rendered at launch.** Each agent starts from a definition rendered fresh for that session, with content from its memory and the team's shared knowledge inlined through factlattice-engine. A failed expansion is shown loudly, never left as a quietly incomplete agent.
- **Self-verbs.** Agents look after their own sessions (for example, asking for their own compaction) through tools in their own tool list, with their identity supplied by the harness.
- **Guard rails, honestly described.** A hook inspects shell commands and file edits an agent is about to make and refuses a small set of dangerous or protected ones. It is a filter for accidents and honest mistakes, with stated gaps, not a security boundary. See the threat model in the design principles.

New behaviour is built behind a flag and switched on once it has been reviewed, so some flags are on by default: 15 of the 66 registered flags at this commit (the Config tab lists every flag with a one-line description and its state, and `gnomon/flags.py` is the registry). Several features described in the design principles are specified but not on by default; the principles say which.

## How it is designed

The design principles, including the threat model, are in [`docs/design-principles.md`](docs/design-principles.md) (generated from the source file [`docs/design-principles.lts`](docs/design-principles.lts)). They open with the platform's goal and the problems it addresses, then state each technical principle with a test of the form "If ..., the design has failed".

In these docs the human who runs Gnomon is called the **pilot**. In code, configuration and API names the same person is called the **operator**.

## Requirements

- **macOS.** The engine uses macOS facilities (AppleScript to open terminal windows, launchd). The test suite below was run on macOS only. Windows and Linux support is planned.
- **Claude Code.** The harness depends on Claude Code's hook interface, terminal behaviour and transcript layout. It has been measured against Claude Code 2.1.284 and 2.1.287. Support for **Codex** is in progress.
- **Python 3.12 or later** (`requires-python` in `pyproject.toml`). The headless core uses only the standard library.
- **For the dashboard**: FastAPI and uvicorn, installed with the `web` extra (`pip install -e '.[web]'`).
- **FactLattice** (optional): the DSD writer and the graph viewer import it lazily and report a clear error when it is absent. FactLattice is not on PyPI; it is published at https://github.com/740Dave/factlattice. Install it from there with `pip install 'factlattice @ git+https://github.com/740Dave/factlattice'`, or with this package's `factlattice` extra, which points to the same repository.
- **factlattice-engine** (optional MCP server; its service name is `lts-engine`): the default MCP server set names `lts-engine` and `gnomon`; it is used for memory and for rendering agent definitions. factlattice-engine ships in the FactLattice repository and runs as `python3 -m factlattice_engine.server`; Gnomon expects an `lts-engine` command on your `PATH`, so add a small wrapper script with that name that runs it (the FactLattice README shows how to register the server).

## Quickstart

1. **Install** from a checkout: `pip install -e '.[web]'`.
2. **Data and configuration.** Runtime state lives under one data root, `~/gnomon-data` by default. To put it elsewhere, create `~/.config/gnomon/roots.toml` (or point the environment variable `GNOMON_CONFIG` at a file) with `data_root = "~/somewhere"` and `deploy_root = "~/somewhere-else"`. The machine configuration is `<data root>/config.json`; it is optional, and an absent or partial file resolves to defaults. Flags are set in it under `"flags"`, for example `{"flags": {"agent_mail": true}}`.
3. **An agent profile** is a JSON file at `<data root>/agents/<id>/profile.json`. A minimal one:

   ```json
   {
     "instance_id": "alpha-architect",
     "display_name": "Alpha Architect",
     "claude_md_source": "~/gnomon-data/agents/alpha-architect/definition.md",
     "bootstrap_prompt": "Read your definition, then say what you will do first.",
     "terminal": "iterm2"
   }
   ```

   `claude_md_source` is the agent's definition (a markdown file that is rendered fresh for every session). `terminal` is `iterm2` or `terminal_app`.
4. **Start the dashboard and the hub** (they run in one process; both listen on `127.0.0.1` only): `cd web && python3 -m backend.server`. The default port is 8770; set `GNOMON_WEB_PORT` (and `GNOMON_WEB_HOST`) to change it. Open `http://127.0.0.1:8770/`.
5. **Launch your first agent** from the Agents tab: pick the profile and launch it; the session opens in your terminal.
6. **Send it a decision document to answer.** (**TODO**: a short worked example of the agent-side call that sends a decision document does not exist yet.)

## Tests

`pip install -e '.[test]'` and run `pytest` from the repository root. The suite is a public subset of the project's own tests; none of it needs a live Claude Code install or a browser, and it makes no network connections beyond loopback. Tests that need FastAPI are not collected unless you install the `web-test` extra (`pip install -e '.[web-test]'`), and tests that need FactLattice are not collected without it; `pytest` prints how many files were skipped for each. Measured on macOS with Python 3.13, in a fresh virtual environment holding only the test dependencies (pytest; for `web-test` also FastAPI, uvicorn and httpx) with the tests run from the repository root: 2698 passed and 3 skipped with the core install, 3078 passed and 3 skipped with `web-test`, in about four to five minutes each. With Gnomon itself installed by `pip install -e '.[test]'`, the core run gave 2692 passed, 6 failed and 3 skipped: the six are in `tests/test_child_python_tree.py`, which asserts that a child Python process cannot import the tree, and an editable install makes it importable. Some test files contain deliberate invisible and bidirectional Unicode characters (they test the code that strips or refuses them), so GitHub's hidden-Unicode notice on those files is expected.

## Known limits

- **Restarting the dashboard cuts running sessions off from the hub.** The hub listens on a new port each time the dashboard process starts, and a running session keeps the address it was given at launch, so after a restart its hub tools fail until the session is relaunched. A fix (a fixed hub port) is in the next update.
- The guard rails and the threat model have their own stated limits; see the design principles.

## Security

See `SECURITY.md` for how to report a problem. Read the threat model in the design principles before relying on any control: Gnomon assumes cooperative agents that run as your own user.

## Differences from the internal build

This repository is a cleaned export of a project that was first built for one team. Compared with that build:

- The default data root is `~/gnomon-data` (the roots file above overrides it), and the folder for agent profiles is `agents/` under it.
- A deployment-specific MCP server that the internal build also listed by default is not in the default MCP server set; the defaults are `lts-engine` and `gnomon`.
- The agent-definition template symbol that named the shared data directory is `DATA_ROOT` (default `~/gnomon-data`).
- The list of MCP servers that a launcher agent must never hold in its default set is empty (`REFUSED_DEFAULT_MCP_SERVERS` in `gnomon/config.py`); a deployment can fill it.
- The one-time converter modules (`gnomon.workspace.convert`, `apply`, `rules`) are not included, and neither are the project's specifications, its cut-over tools, or most of its tests.
- The dashboard's tab for agents is labelled "Agents"; user-facing messages call the person who runs Gnomon "the pilot". Some code, configuration and wire-field names still use older words from the internal build; they are not renamed.

## Licence

MIT. See `LICENSE`.
