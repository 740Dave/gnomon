"""Seed case — proves the pipe end to end (acceptance-3).

A headless agent is asked to write one file with one exact line. Evaluators
check: the run exited 0; D40's transcript exists; the file exists with the exact
content; the transcript holds a ``Write`` tool call naming that file; the session
provenance log holds the launch's agent-def render record and NO failed render;
the operator ledger holds a ``finished exit 0`` record and NO ``deny``; the run
stayed within a tight turn/cost budget on a haiku model.
"""

from ..case import EvalCase
from ..evaluators import (BudgetGuard, FileMatches, ModelUsed, NoRecord, ProvenanceHas, RunSucceeded, ToolCalled,
                          TranscriptPresent)

SEED_FILE = "gm_evals_seed.txt"
SEED_CONTENT = "GM-EVALS-SEED-OK"

SEED_WRITE_FILE = EvalCase(
    name="seed-write-file",
    description="headless agent writes one exact file; proves launch -> evidence -> verdict",
    prompt=(f"Use the Write tool to create a file named {SEED_FILE} in the current working directory. "
            f"Its entire content must be exactly this one line: {SEED_CONTENT}\n"
            "Do not do anything else. When the file is written, reply with the single word DONE."),
    model="haiku",
    timeout_s=180,
    evaluators=(
        RunSucceeded(),
        TranscriptPresent(),
        FileMatches(SEED_FILE, equals=SEED_CONTENT),
        ToolCalled("Write", input_match={"file_path": {"$regex": r"gm_evals_seed\.txt$"}}),
        ProvenanceHas({"kind": "agent-def-render", "event": "launch"}),
        NoRecord({"kind": "agent-def-render", "status": "failed"}),
        ProvenanceHas({"kind": "agent-launch", "decision": "finished", "headless": True, "exit_code": 0},
                      source="launch_log"),
        NoRecord({"decision": "deny"}, source="launch_log"),
        ModelUsed("haiku"),
        BudgetGuard(max_turns=6, max_cost_usd=0.25),
    ),
)
