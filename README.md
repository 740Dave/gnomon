# Gnomon engine

<!-- gnomon-platform-opening:start -->
**The Gnomon Platform makes it easy to lead an outstanding AI team that remembers, collaborates with you and produces extraordinary results.**

The Gnomon Platform is made of three projects:

| Project | What it does for the team | Repository |
|---|---|---|
| FactLattice (the format) and lts-engine (its engine) | the team's memory and shared knowledge | https://github.com/740Dave/factlattice |
| Gnomon engine | the harness that runs the team | coming |
| DSD engine (decision-support documents) and decision graph | how decisions reach you and are recorded | coming: to be forked out of the Gnomon engine |
<!-- gnomon-platform-opening:end -->

This repository is the Gnomon engine.

> **Draft.** This README is being written for the first public release. Sections marked **TODO** depend on facts that are not settled yet; nothing in them should be relied on.

## What it is

The Gnomon engine runs a team of Claude Code agent sessions on one Mac and lets one person, the pilot, lead them.

- **Your real terminal.** Each agent session opens in your own terminal (iTerm2 or Terminal.app) inside a small, transparent harness. The harness does not emulate a terminal: you work with the agent's real interface, as you would without Gnomon.
- **One place for what goes into a session.** Notices, reminders and relayed messages reach a session through one write site in the harness. Each one is recorded with its source, and the harness holds its own text back while you have a half-typed line or a dialog open (the one stated exception saves and restores a long-idle half-typed line around a compaction).
- **A local hub.** Every harnessed session connects to a hub on your machine, which carries messages and tool calls between sessions, the dashboard and you. The hub and the dashboard listen only on the local machine.
- **A web dashboard.** The dashboard shows the live sessions and is where you answer the questions your agents put to you.
- **Decision documents.** When an agent needs a decision from you, it sends a decision document (DSD): the question, the options, their tradeoffs and a recommendation. You answer in the dashboard, your answer is recorded, and it is delivered back to the agent. Answers are stored as plain-text FactLattice files.
- **Agent definitions rendered at launch.** Each agent starts from a definition rendered fresh for that session, with content from its memory and the team's shared knowledge inlined through lts-engine. A failed expansion is shown loudly, never left as a quietly incomplete agent.
- **Self-verbs.** Agents look after their own sessions (for example, asking for their own compaction) through tools in their own tool list, with their identity supplied by the harness.
- **Guard rails, honestly described.** A hook inspects shell commands and file edits an agent is about to make and refuses a small set of dangerous or protected ones. It is a filter for accidents and honest mistakes, with stated gaps, not a security boundary. See the threat model in the design principles.

New behaviour is built behind a flag and switched on once it has been reviewed, so some flags are on by default: 15 of the 66 registered flags at this commit (the Config tab lists every flag with a one-line description and its state, and `gnomon/flags.py` is the registry). Several features described in the design principles are specified but not on by default; the principles say which.

## How it is designed

The design principles, including the threat model, are in [`docs/design-principles.lts`](docs/design-principles.lts). They open with the platform's goal and the problems it addresses, then state each technical principle with a test of the form "If ..., the design has failed".

In these docs the human who runs Gnomon is called the **pilot**. In code, configuration and API names the same person is called the **operator**.

## Requirements

- **macOS.** The engine uses macOS facilities (AppleScript to open terminal windows, launchd). The test suite below was run on macOS only. (**TODO**: state Linux status.)
- **Claude Code.** The harness depends on Claude Code's hook interface, terminal behaviour and transcript layout. It has been measured against Claude Code 2.1.284 and 2.1.287. (**TODO**: state the supported version range.)
- **Python 3.12 or later** (`requires-python` in `pyproject.toml`). The headless core uses only the standard library.
- **For the dashboard**: FastAPI and uvicorn, installed with the `web` extra (`pip install -e '.[web]'`).
- **FactLattice** (optional): the DSD writer and the graph viewer import it lazily and report a clear error when it is absent. Install it with the `factlattice` extra. (**TODO**: state where the package is published.)
- **lts-engine** (optional MCP server): the default MCP server set names `lts-engine` and `gnomon`; `lts-engine` is expected on your `PATH` and is used for memory and for rendering agent definitions. (**TODO**: state where to get it.)

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

`pip install -e '.[test]'` and run `pytest` from the repository root. The suite is a public subset of the project's own tests; none of it needs a live Claude Code install or a browser, and it makes no network connections beyond loopback. Tests that need FastAPI are not collected unless you install the `web-test` extra (`pip install -e '.[web-test]'`), and tests that need FactLattice are not collected without it; `pytest` prints how many files were skipped for each. Measured on macOS with Python 3.13: 2655 passed and 37 skipped with the core install, 3031 passed and 41 skipped with `web-test`, in about six minutes each. Some test files contain deliberate invisible and bidirectional Unicode characters (they test the code that strips or refuses them), so GitHub's hidden-Unicode notice on those files is expected.

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

MIT. See `LICENSE`. (**TODO**: the copyright holder named there is a placeholder.)
