"""Round 7 item 3 (P2-2): ``gnomon-relaunch <session_dir> --strip-derived-keys [--dry-run]``, the rollback step of the recovery section in
docs/BREAK-GLASS-relaunch.md: put back the launch.json keys a flag-ON relaunch derived, from the newest backup that predates them."""
import json
import os

import pytest

from gnomon import relaunch as R

DERIVED = {"resume_session_id": "11111111-2222-4333-8444-555555555555", "resume_cwd": "/somewhere/sess",
           "resume_keys_derived": {"at": "20261005T000000000Z"}, "resume_prompt": "relaunched at 20261005T000000Z; continue",
           "resume_prompt_once": "ONE SHOT", "resume_prompt_once_meta": {"armed_by": "x"}, "resume_prompt_once_log": [{"event": "armed"}]}
STICKY = "STICKY PROMPT"


class _Sys:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def ps(self):
        return list(self.rows)


def _session(tmp_path, *, derived=True, backups=("old",)):
    sd = tmp_path / "sess"
    (sd / ".claude").mkdir(parents=True)
    base = {"instance_id": "alpha-agent", "model": "keep-me", "flags": {"relaunch_derives_resume_keys": True},
            "session_id": "11111111-2222-4333-8444-555555555555", "transcript_cwd": str(sd), "resume_prompt": STICKY}
    cur = dict(base, **DERIVED) if derived else dict(base)
    (sd / ".claude" / "launch.json").write_text(json.dumps(cur, indent=2))
    stamps = {"old": ("20261005T000000000Z", base), "newer": ("20261005T010000000Z", dict(base, **DERIVED))}
    for b in backups:
        st, body = stamps[b]
        (sd / ".claude" / f"launch.json.bak-{st}").write_text(json.dumps(body, indent=2))
    return str(sd), base


def _cfg(sd):
    return json.load(open(os.path.join(sd, ".claude", "launch.json")))


def test_strip_restores_the_pre_derivation_keys_and_touches_nothing_else(tmp_path, capsys):
    sd, base = _session(tmp_path, backups=("old", "newer"))               # the NEWEST backup already carries derived keys: it must be skipped
    assert R.strip_derived_keys(_Sys(), sd, dry_run=False) == R.EXIT_OK
    out = capsys.readouterr().out
    assert "launch.json.bak-20261005T000000000Z" in out and "launch.json.bak-20261005T010000000Z" not in out.split("restoring from")[1].split("\n")[0]
    cfg = _cfg(sd)
    assert cfg == base                                                    # derived keys gone, the sticky prompt back, `flags` and the rest untouched
    assert cfg["flags"] == {"relaunch_derives_resume_keys": True} and cfg["model"] == "keep-me"


def test_dry_run_prints_the_change_and_writes_nothing(tmp_path, capsys):
    sd, _ = _session(tmp_path)
    before = open(os.path.join(sd, ".claude", "launch.json"), "rb").read()
    assert R.strip_derived_keys(_Sys(), sd, dry_run=True) == R.EXIT_OK
    out = capsys.readouterr().out
    assert "--dry-run: nothing changed" in out and "resume_session_id" in out and "REMOVED" in out and "ONE SHOT" not in out
    assert open(os.path.join(sd, ".claude", "launch.json"), "rb").read() == before


def test_nothing_to_strip_without_the_marker(tmp_path, capsys):
    sd, _ = _session(tmp_path, derived=False)
    before = open(os.path.join(sd, ".claude", "launch.json"), "rb").read()
    assert R.strip_derived_keys(_Sys(), sd, dry_run=False) == R.EXIT_OK
    assert "nothing to strip" in capsys.readouterr().out
    assert open(os.path.join(sd, ".claude", "launch.json"), "rb").read() == before


def test_a_running_session_is_refused(tmp_path, capsys):
    sd, _ = _session(tmp_path)
    row = R.ProcRow(4242, 1, "Mon Oct  5 00:00:00 2026", f"/usr/bin/python3 -m gnomon.harness {os.path.realpath(sd)}")
    before = open(os.path.join(sd, ".claude", "launch.json"), "rb").read()
    assert R.strip_derived_keys(_Sys([row]), sd, dry_run=False) == R.EXIT_REFUSED
    assert "a harness is running" in capsys.readouterr().out
    assert open(os.path.join(sd, ".claude", "launch.json"), "rb").read() == before


def test_no_backup_that_predates_the_derivation_is_a_refusal_naming_the_hand_edit(tmp_path, capsys):
    sd, _ = _session(tmp_path, backups=("newer",))
    assert R.strip_derived_keys(_Sys(), sd, dry_run=False) == R.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "predates the derivation" in out and "Edit launch.json by hand" in out and _cfg(sd)["resume_session_id"]
    sd2 = tmp_path / "sess2"
    (sd2 / ".claude").mkdir(parents=True)
    (sd2 / ".claude" / "launch.json").write_text(json.dumps(dict(DERIVED)))
    assert R.strip_derived_keys(_Sys(), str(sd2), dry_run=False) == R.EXIT_REFUSED                # no backup at all


def test_the_option_is_reachable_from_main(tmp_path, capsys):
    sd, base = _session(tmp_path)
    assert R.main([sd, "--strip-derived-keys", "--dry-run"], sysm=_Sys()) == R.EXIT_OK
    assert _cfg(sd) != base
    assert R.main([sd, "--strip-derived-keys"], sysm=_Sys()) == R.EXIT_OK
    assert _cfg(sd) == base


# ---------------------------------------------------------------- round 8 (delta Diamond P2-1, P2-3, P2-4)
import subprocess
import sys


def _hold_lock(path):
    """A DIFFERENT process holding the per-session worker lock the way run_worker does (flock, pid written inside)."""
    code = ("import fcntl,os,sys\n"
            "fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600)\n"
            "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
            "os.ftruncate(fd,0);os.pwrite(fd,str(os.getpid()).encode(),0)\n"
            "print(os.getpid(),flush=True)\n"
            "sys.stdin.read()\n")
    p = subprocess.Popen([sys.executable, "-c", code, path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    return p, int(p.stdout.readline())


def test_a_lock_held_by_another_process_is_refused_naming_the_holder(tmp_path, capsys):
    sd, _ = _session(tmp_path)
    lock = os.path.join(sd, ".claude", R.WORKER_LOCK_NAME)
    before = open(os.path.join(sd, ".claude", "launch.json"), "rb").read()
    proc, pid = _hold_lock(lock)
    try:
        for dry in (False, True):
            assert R.strip_derived_keys(_Sys(), sd, dry_run=dry) == R.EXIT_REFUSED
            out = capsys.readouterr().out
            assert f"pid {pid}" in out and "Nothing changed" in out
            assert open(os.path.join(sd, ".claude", "launch.json"), "rb").read() == before
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    assert R.strip_derived_keys(_Sys(), sd, dry_run=False) == R.EXIT_OK              # released: the same call now strips
    assert "resume_session_id" not in _cfg(sd)


def test_the_lock_is_released_after_a_strip(tmp_path):
    sd, _ = _session(tmp_path)
    assert R.strip_derived_keys(_Sys(), sd, dry_run=False) == R.EXIT_OK
    proc, pid = _hold_lock(os.path.join(sd, ".claude", R.WORKER_LOCK_NAME))          # would fail to lock if strip had kept it
    proc.stdin.close()
    proc.wait(timeout=10)
    assert pid > 0


def test_a_dry_run_creates_no_lock_file(tmp_path):
    sd, _ = _session(tmp_path)
    assert R.strip_derived_keys(_Sys(), sd, dry_run=True) == R.EXIT_OK
    assert not os.path.exists(os.path.join(sd, ".claude", R.WORKER_LOCK_NAME))


def test_a_never_settled_journal_is_reported_and_refused(tmp_path, capsys):
    from gnomon import resume_keys as rk

    sd, _ = _session(tmp_path)
    rk.journal_write(sd, {"pid": 777, "at": "2026-10-05T00:00:00Z", "keys": ["resume_session_id"], "before": {}})
    before = open(os.path.join(sd, ".claude", "launch.json"), "rb").read()
    for dry in (False, True):
        assert R.strip_derived_keys(_Sys(), sd, dry_run=dry) == R.EXIT_REFUSED
        out = capsys.readouterr().out
        assert "never settled" in out and "pid 777" in out and "Nothing changed" in out
        assert open(os.path.join(sd, ".claude", "launch.json"), "rb").read() == before
        assert rk.journal_read(sd) is not None                                          # left for the next relaunch to roll back
    rk.journal_clear(sd)
    assert R.strip_derived_keys(_Sys(), sd, dry_run=False) == R.EXIT_OK


def test_a_hand_made_backup_is_never_the_restore_source(tmp_path, capsys):
    """The reviewer's case: ``launch.json.bak-pre-x`` sorts AFTER the writer's ``bak-<yyyymmddTHHMMSSmmmZ>`` names and used to win."""
    sd, base = _session(tmp_path)                                                       # one writer-format pre-derivation backup ('old')
    stale = dict(base, resume_session_id="STALE-FROM-A-HAND-MADE-FILE")
    for name in ("launch.json.bak-pre-x", "launch.json.bak-20261006-item", "launch.json.bak-20261005T000000Z", "launch.json.bak-"):
        open(os.path.join(sd, ".claude", name), "w").write(json.dumps(stale))
    assert R.strip_derived_keys(_Sys(), sd, dry_run=False) == R.EXIT_OK
    out = capsys.readouterr().out
    assert "launch.json.bak-20261005T000000000Z" in out and "pre-x" not in out
    cfg = _cfg(sd)
    assert cfg == base and "STALE" not in json.dumps(cfg)


def test_only_hand_made_backups_means_no_restore_source(tmp_path, capsys):
    sd, base = _session(tmp_path, backups=())
    open(os.path.join(sd, ".claude", "launch.json.bak-pre-x"), "w").write(json.dumps(base))
    assert R.strip_derived_keys(_Sys(), sd, dry_run=False) == R.EXIT_REFUSED
    assert "predates the derivation" in capsys.readouterr().out and "resume_session_id" in _cfg(sd)


def test_the_backup_pattern_matches_exactly_what_the_writer_names(tmp_path):
    """Ties the strip's pattern to the writer: a backup the real edit_launch_json just wrote is accepted."""
    from gnomon.agent_def import edit_launch_json

    sd, _ = _session(tmp_path, backups=())
    bak = edit_launch_json(sd, set_keys={"model": "x"}, backup=True)
    assert R._relaunch_backup_re().match(os.path.basename(bak)), bak


def test_derived_keys_equal_the_keys_the_writers_update_function_writes():
    """P2-4: no second literal. Every key resume_keys._updates_for can write (derived pair, marker, neutral prompt, the one-shot trio)
    is in relaunch.DERIVED_KEYS and nothing else is; a key added to the writer fails here until it is added to the tuple."""
    from gnomon import resume_keys as rk

    d = rk.Derivation(ok=True, rid="r", rcwd="/c", transcript_path="/t", newest_epoch=1.0, activity_epoch=1.0, activity_source="x")
    _, _, keys_all = rk._updates_for(d, 1.0, "once", "me")
    _, _, keys_none = rk._updates_for(None, 1.0, "", "")
    assert set(keys_none) <= set(keys_all)
    assert set(R.DERIVED_KEYS) == set(keys_all)
    assert len(R.DERIVED_KEYS) == len(set(R.DERIVED_KEYS))
