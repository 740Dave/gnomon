"""A DSD-answer notice must not be typed into an open AskUserQuestion dialog.

Incident 2026-10-03 02:15:31Z (a DSD incident): the notice + CR were typed
while AskUserQuestion was open; the CR selected the first option 83 ms later, the
notice never reached the model, and the harness still logged written+acked.
"""
import json

from gnomon.harness.modal_prompt import pending_modal_tool, transcript_modal_reader
from gnomon.harness.notice_inbox import DsdNoticeInbox

DSD = {"dsd_id": "dsd-1a2b3c4d5e1", "root_id": "DEC-DSD-1A2B3C4D5E1", "text": "t"}


def _asst(tool, tid):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": tool, "input": {}}]}}


def _result(tid):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(path)


def test_open_askuserquestion_is_detected_and_answered_one_is_not(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_asst("Bash", "b1"), _result("b1"), _asst("AskUserQuestion", "q1")])
    assert pending_modal_tool(p) == "AskUserQuestion"
    p = _write(tmp_path / "t2.jsonl", [_asst("AskUserQuestion", "q1"), _result("q1")])
    assert pending_modal_tool(p) is None
    p = _write(tmp_path / "t3.jsonl", [_asst("ExitPlanMode", "e1")])
    assert pending_modal_tool(p) == "ExitPlanMode"


def test_ordinary_pending_tool_and_unreadable_transcript_are_not_modal(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_asst("Bash", "b1")])  # a long Bash is not a dialog
    assert pending_modal_tool(p) is None
    assert pending_modal_tool(str(tmp_path / "missing.jsonl")) is None
    (tmp_path / "junk.jsonl").write_text("not json\n{\n")
    assert pending_modal_tool(str(tmp_path / "junk.jsonl")) is None


def _inbox(modal, injected):
    return DsdNoticeInbox(inject=lambda *a, **k: injected.append(a) or None, ack=lambda _f: None,
                          quiet_seconds=0.0, ready=lambda: True, modal_open=modal)


def test_notice_is_held_while_dialog_open_then_delivered_once_it_closes():
    injected, state = [], {"modal": "AskUserQuestion"}
    inbox = _inbox(lambda: state["modal"], injected)
    out = inbox.offer(dict(DSD))
    assert out["decision"] == "held" and injected == []          # NOTHING typed into the dialog
    inbox.tick()
    assert injected == [] and inbox.held_ids() == [DSD["dsd_id"]]
    state["modal"] = None                                         # the human answered the dialog
    inbox.tick()
    assert len(injected) == 1 and inbox.held_ids() == []          # now delivered, exactly once
    inbox.tick()
    assert len(injected) == 1


def test_modal_reader_error_never_wedges_delivery():
    injected = []

    def boom():
        raise RuntimeError("transcript gone")

    inbox = _inbox(boom, injected)
    assert inbox.offer(dict(DSD))["decision"] == "delivered" and len(injected) == 1


def test_no_modal_reader_is_todays_behaviour():
    injected = []
    assert _inbox(None, injected).offer(dict(DSD))["decision"] == "delivered"


def test_reader_follows_session_dir_transcript(tmp_path, monkeypatch):
    p = _write(tmp_path / "t.jsonl", [_asst("AskUserQuestion", "q1")])
    monkeypatch.setattr("gnomon.harness.modal_prompt.resolve_transcript_path", lambda sd: p)
    assert transcript_modal_reader("x")() == "AskUserQuestion"
