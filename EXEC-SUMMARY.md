# Gnomon: Executive Summary

> The entry point for people. Read this to decide whether Gnomon addresses a problem you have.
> Then read [README.md](README.md) for installation and the mechanics,
> [docs/design-principles.md](docs/design-principles.md) for the design and its threat model, and
> [FOR-LLMS.md](FOR-LLMS.md) if you are an AI assistant (it holds the same facts as short, checkable lines).

Every claim below points at a file in this repository, so you can check it. Where something is only
specified, off by default, or not measured, the text says so.

---

## The one-paragraph version

Gnomon is the heart of the Gnomon Platform: the part that runs a team of Claude Code agent sessions on
one Mac and lets one person, the **pilot**, lead them. It has a small harness that sits between each
agent session and your own terminal, a local hub that carries messages and tool calls between sessions,
and a web dashboard where you see the live sessions and answer the questions your agents put to you.
The platform's aim is stated in the README: to make it easy to lead an outstanding AI team that
remembers, collaborates with you and produces extraordinary results. Gnomon is a draft for its first
public release (`README.md`, the "Draft" notice), and it is built for one person on one Mac.

---

## Terms used here

- **Pilot:** the person who runs Gnomon and the agent sessions on their machine (`docs/design-principles.lts`, glossary).
- **Harness:** the small process between an agent session and your terminal; it carries the session's traffic in and out. The pilot's own keystrokes and one start-up prompt handler pass without the harness's text gate (`docs/design-principles.lts:171`).
- **Hub:** the local service every harnessed session connects to; it carries messages and tool calls between sessions, the dashboard and you.
- **FactLattice and `.lts` files:** structured plain-text files with headings, ids and properties, kept as team memory, specs and decisions. **factlattice-engine** is the separate project that indexes and queries them; its service name is `lts-engine` (`README.md` line 62; glossary).
- **MCP server and "stdio":** a tool server that an agent program can call. A "stdio" server is started as a command and talks over its standard input and output; Gnomon registers `lts-engine` that way (`gnomon/config.py:169`). "Any MCP client able to launch a local stdio server" means any agent program that can start such a command.
- **Flag and Config tab:** a flag is a named on/off setting for a behaviour, with one declared default; the dashboard's Config tab lists every flag and its state (`README.md` line 47; glossary).
- **Self-verb:** a tool in an agent's own tool list that acts on that agent's own session or records, for example asking for its own compaction.
- **Deny guard:** the hook that inspects each shell command and file edit an agent is about to make and refuses a small set of dangerous or protected ones; a filter for accidents, not a security boundary.
- **Compaction:** Claude Code replacing a long conversation with a summary so the session can continue.

---

## The problems

The README names four problems that come up when one person leads a team of AI agents
(`README.md`, "Why Gnomon Exists"). They are listed here in its words, lightly shortened.

**Memory.** An agent starts each session without what the team learned before, and even when a memory
store exists it may answer without consulting it.

**Ambiguity.** Given an unclear spec, or too little context, an agent may guess silently.

**Agent behaviour.** A confident report can rest on a check that never ran.

**Synthesis.** What a decision needs is scattered across many documents.

---

## What Gnomon does about each

**Memory.** Each agent's definition (the file that says what the agent is and what it must do) is
re-created fresh whenever a session starts, with the agent's own notes and the team's shared knowledge
written into it. Memory lives in plain-text FactLattice files, which a separate project,
factlattice-engine, can query. If a piece of memory cannot be loaded, the definition says so in plain
words at that spot ("your memory was NOT loaded"), and the top of the file says how many such failures
there were, rather than leaving a quietly incomplete agent
(`gnomon/lts_expand.py:77-85`, `gnomon/agent_def.py:171-174`). An agent can also record a new memory
for itself with one call, and the call checks that the memory can be read back
(`gnomon/memory.py:19-27`).

**Ambiguity.** When an agent needs a decision from you, it sends a decision document: the question,
the options, their tradeoffs and a recommendation. You answer in the dashboard; your answer is recorded
as a plain-text file and delivered back to the agent (`README.md`, "What it is"; `gnomon/dsd/writer.py:1-18`).
A decision document opens with every option unset; the recommendation is shown as a label, and each answer records
whether it agreed with the recommendation (`docs/design-principles.lts:230`).

**Agent behaviour.** The problem is that a confident report can rest on a check that never ran. Said
plainly: this repository contains no mechanism that checks whether an agent's report is true. What it has
is narrower. It records what the harness itself types into sessions, and it guards shell commands and file
edits. Text the harness types (a notice, a reminder, a relayed message) is meant to pass through one write
site and be recorded with its source (`README.md`, "What it is"; `docs/design-principles.lts:164-175`; the
record is `ProvenanceLog` in `gnomon/harness/hooks.py:584`). The principles state their own exceptions:
the pilot's own keystrokes, and one start-up prompt handler, are not gated; and when the record cannot be
written, harness-initiated text is not typed, but other injections still go ahead with a counter and a
warning (`docs/design-principles.lts:171` for the exemptions and `docs/design-principles.lts:350` for the record-fault path; `FOR-LLMS.md`, `GNE-HARNESS`). This summary repeats those
statements; the write site was not traced in code for it. The harness holds its own text back while you
have a half-typed line or a dialog open. A guard on shell commands and file edits refuses a small set of
dangerous ones. Reporting status "only from evidence about the live object" (`docs/design-principles.lts`,
principle DP-GN-10) and review of every change by someone who did not write it (DP-GN-23) are design
principles and working practice, not code you can run here.

**Synthesis.** This is mostly factlattice-engine's job, not this repository's. Decisions, specs and the
links between them are kept as one graph of FactLattice files, and factlattice-engine can follow those
links. What Gnomon adds is the wiring: agents get the `lts-engine` tool server by default
(`gnomon/config.py:54`), and Gnomon includes a graph viewer (`gnomon/app/graph_viewer.py`; `README.md` line 61).

---

## What is built, what is measured, and what is not

**Built and covered by tests you can run.** The public test suite is a subset of the project's own
tests; it covers, among much else, the decision-document writer and reader
(`tests/test_dsd_answers_reader.py`, `tests/test_dsd_live.py`) and the deny guard (`tests/test_confirm_proof_deny_guard.py`).
The measured result is in `README.md`, "Tests", and is described under "How to check" below.

**Built, but not covered by this repository's public tests.** Scoped to what was searched in `tests/`:
the macro expander that renders agent definitions (`gnomon/lts_expand.py`) has no public test (no file
mentions `lts_expand` or `LtsExpander`), and the memory-recording function (`gnomon/memory.py`) has none
either. Definition rendering (`gnomon/agent_def.py`) is reached by one public test, and only through the
launcher with a definition that has no macros in it
(`tests/test_x13s1_settings_registry.py:95-107`, calling `render_agent_def` at `gnomon/launch.py:366`), so
macro expansion is not exercised there. Other tests import `gnomon.memory` and `gnomon.agent_def` for other
functions (`tests/test_compact_ignored_readers.py:22` imports `last_compaction`;
`tests/test_relaunch_strip_derived_keys.py:185` imports `edit_launch_json`). So you cannot run a check of
macro expansion or of the memory write from here. The project's other tests are not part of this export
(`README.md`, "Differences from the internal build").

**Specified but off by default.** New behaviour ships behind a flag. Of the 66 registered flags, 15 are
on by default (`README.md` line 47; the registry is `gnomon/flags.py`), which is consistent with "new behaviour defaults to off" because 2 of the 15 are security flags, on from the start (`gnomon/flags.py:54-58`), and the other 13 are behaviour flags the README says were switched on after review (no public file gives a per-flag reason). The two security flags and the setting `guard_fail_closed` can be switched off from the Config tab only after a passed human-presence check, and the server shows a warning while one is off (`gnomon/flags.py:54-63`; `gnomon/config.py:507-545`; `web/backend/server.py:2847-2856`, `web/backend/server.py:2953-3027`). That check is not the only route: editing the machine config file by hand switches them off with no check, because loading that file has no integrity check. The deny guard is meant to refuse an agent's write to that file, but it is a filter for accidents with known misses, not a security boundary. Binding each hub caller to the
session that launched it is specified and off (`hub_caller_binding`, `gnomon/flags.py:147`), as are
several self-verbs, including agent mail (`gnomon/flags.py:158`) and the restart verb (`gnomon/flags.py:170`).
The design principles list them as "specified but not on by default"
(`docs/design-principles.lts`, header `status`).

**Intended, not built.** Detecting that a running session is behind the current setup is intended, not built
(`docs/design-principles.lts`, header `status`). A team view across workspaces is planned, not built; the
dashboard lists the live sessions today (`docs/design-principles.lts:75`).

**Expected, not measured.** The results the platform hopes for (agents that start from what the team
knows, fewer silent guesses, reports you can trust) are stated in the design principles as hypotheses.
Their own words: "The results below are expected, not measured ... no study has been published yet"
(`docs/design-principles.lts`, "The problems the platform addresses").

---

## The honest limits

- **One person, one Mac.** The Gnomon engine runs on macOS, and its harness runs Claude Code sessions. The
  tests were run on macOS only (`README.md`, "Requirements"). The memory files and decision answers are plain
  FactLattice (`.lts`) text files that any MCP client able to launch a local stdio server can read through
  factlattice-engine (`gnomon/memory.py:3-8`, `gnomon/dsd/writer.py:12-14`, `gnomon/config.py:169`, `README.md` line 62).
- **Not a security boundary.** Gnomon assumes cooperative agents that run as your own user. Its guard
  catches accidents and honest mistakes; a hostile program running as you, or an agent determined to go
  around Gnomon, is outside what it can stop (`README.md`, "Security"; `SECURITY.md`; the threat model in
  `docs/design-principles.lts`). Its services listen on the local machine only, which keeps them off the
  network but does not protect them from other programs on the same machine.
- **A dashboard restart cuts running sessions off from the hub.** The hub takes a new port each time the
  dashboard starts, so running sessions lose their hub tools until relaunched. A fix (a fixed hub port)
  is stated as coming in the next update (`README.md`, "Known limits").
- **A draft.** The README's decision-document walk-through is a TODO (`README.md`, Quickstart step 6),
  and the design principles are version 0.5, a draft (`docs/design-principles.lts`, header).
- **A cleaned export.** This repository comes from a project first built for one team. Its
  specifications, most of its tests and its cut-over tools are not included (`README.md`, "Differences
  from the internal build").
- **Depends on other projects.** Memory, queries and the decision graph rely on factlattice-engine, a
  separate project whose current status is stated in the platform block of [FOR-LLMS.md](FOR-LLMS.md). The
  Decision engine and Gnomon plugins named in the README are also separate projects (same place for their
  status); the decision documents described above are implemented inside this repository (`gnomon/dsd/`).

---

## How to check

```bash
pip install -e '.[test]'
pytest
```

`README.md`, "Tests", gives the counts measured for this commit and the machine they were measured on.
Tests that need FastAPI or FactLattice are skipped unless you install the `web-test` or `factlattice`
extras; `pytest` reports how many files it skipped for each.

---

## Status

**Draft, for the first public release.** The harness, hub, dashboard, decision documents and agent
definition rendering are in the code; the items under "Specified but off by default", "Intended, not
built" and "Expected, not measured" above are not to be relied on as working. Licence: MIT
(`LICENSE`). To report a problem, see `SECURITY.md`.
