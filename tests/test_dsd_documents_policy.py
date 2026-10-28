"""(2)(4): the ONE open policy (classify), the send-time record (describe_for_persist),
the UI view (view_documents) and the Open action (open_document). Real files in a hermetic tmp tree
(g31); the opener is always an injected recorder - nothing here launches anything."""
import os
import stat

import pytest

from dsd_documents_support import docs_markdown, make_tree, sha
from gnomon.dsd import documents as D


@pytest.fixture
def t(tmp_path):
    return make_tree(tmp_path)


def _c(t, path):
    return D.classify(path, [t["root"]])


# ---- classify: the positive case and every NAMED refusal ---------------------------------------

def test_a_regular_document_in_an_allowed_root_is_openable(t):
    c = _c(t, t["top"])
    assert c["ok"] and c["code"] == "ok" and c["resolved"] == os.path.realpath(t["top"])
    assert c["size"] == os.path.getsize(t["top"])


def test_extension_check_is_case_insensitive(t):
    p = os.path.join(t["root"], "LOUD.MD")
    open(p, "w").write("x")
    assert _c(t, p)["ok"]


def test_missing_file_is_named(t):
    c = _c(t, os.path.join(t["root"], "nope.md"))
    assert not c["ok"] and c["code"] == D.REFUSAL_MISSING


def test_relative_and_empty_paths_are_refused(t):
    assert _c(t, "notes.md")["code"] == D.REFUSAL_RELATIVE
    assert _c(t, "./notes.md")["code"] == D.REFUSAL_RELATIVE
    assert _c(t, "")["code"] == D.REFUSAL_MISSING
    assert _c(t, "/x\x00/y.md")["code"] == D.REFUSAL_MISSING


def test_directory_and_app_bundle_are_not_regular_files(t):
    app = os.path.join(t["root"], "Evil.app")
    os.makedirs(os.path.join(app, "Contents", "MacOS"))
    assert _c(t, app)["code"] == D.REFUSAL_NOT_REGULAR
    assert _c(t, t["root"])["code"] == D.REFUSAL_NOT_REGULAR


def test_fifo_is_not_a_regular_file_and_is_never_read(t):
    p = os.path.join(t["root"], "pipe.md")
    os.mkfifo(p)
    assert _c(t, p)["code"] == D.REFUSAL_NOT_REGULAR
    entries, warns = D.describe_for_persist(f"### D1 · q\n**Documents:** {p}\n", [t["root"]])
    assert entries[0]["status"] == D.REFUSAL_NOT_REGULAR and entries[0]["sha256"] == "" and warns


@pytest.mark.parametrize("name", ["run.sh", "go.command", "x.py", "tool.tool", "thing.exe", "noext", "a.md.sh",
                                  "a.md.command", "page.html.py", "x.pdf.app"])
def test_non_document_extensions_are_refused_even_if_not_executable(t, name):
    p = os.path.join(t["root"], name)
    open(p, "w").write("echo hi\n")
    c = _c(t, p)
    assert not c["ok"] and c["code"] == D.REFUSAL_EXTENSION, (name, c)


def test_an_executable_document_is_refused(t):
    p = os.path.join(t["root"], "sneaky.md")
    open(p, "w").write("# hi\n")
    os.chmod(p, 0o755)
    c = _c(t, p)
    assert not c["ok"] and c["code"] == D.REFUSAL_EXECUTABLE
    for mode in (0o744, 0o654, 0o601 | 0o010):                  # any single x bit counts
        os.chmod(p, mode)
        assert _c(t, p)["code"] == D.REFUSAL_EXECUTABLE, oct(mode)
    os.chmod(p, 0o644)
    assert _c(t, p)["ok"]


def test_path_outside_the_roots_is_refused(t):
    p = os.path.join(t["outside"], "secret.md")
    open(p, "w").write("s")
    assert _c(t, p)["code"] == D.REFUSAL_OUTSIDE_ROOTS


def test_dotdot_traversal_out_of_a_root_is_refused(t):
    p = os.path.join(t["outside"], "secret.md")
    open(p, "w").write("s")
    via = os.path.join(t["root"], "..", "elsewhere", "secret.md")
    assert _c(t, via)["code"] == D.REFUSAL_OUTSIDE_ROOTS


def test_symlink_file_pointing_out_of_the_roots_is_refused(t):
    target = os.path.join(t["outside"], "secret.md")
    open(target, "w").write("s")
    link = os.path.join(t["root"], "innocent.md")
    os.symlink(target, link)
    c = _c(t, link)
    assert c["code"] == D.REFUSAL_OUTSIDE_ROOTS and c["resolved"] == os.path.realpath(target)


def test_symlinked_directory_pointing_out_is_refused(t):
    os.makedirs(os.path.join(t["outside"], "d"))
    open(os.path.join(t["outside"], "d", "x.md"), "w").write("s")
    os.symlink(os.path.join(t["outside"], "d"), os.path.join(t["root"], "dlink"))
    assert _c(t, os.path.join(t["root"], "dlink", "x.md"))["code"] == D.REFUSAL_OUTSIDE_ROOTS


def test_symlink_to_an_executable_script_inside_the_root_is_refused_by_extension(t):
    script = os.path.join(t["root"], "payload.sh")
    open(script, "w").write("#!/bin/sh\n")
    os.chmod(script, 0o755)
    link = os.path.join(t["root"], "readme.md")
    os.symlink(script, link)
    assert _c(t, link)["code"] == D.REFUSAL_EXTENSION          # the RESOLVED file's extension counts


def test_symlink_inside_the_root_to_a_good_document_is_fine_and_resolves(t):
    link = os.path.join(t["root"], "alias.md")
    os.symlink(t["top"], link)
    c = _c(t, link)
    assert c["ok"] and c["resolved"] == os.path.realpath(t["top"])


def test_a_sibling_directory_sharing_a_name_prefix_is_not_inside_the_root(tmp_path):
    root = tmp_path / "docs-root"
    evil = tmp_path / "docs-root-evil"
    root.mkdir()
    evil.mkdir()
    (evil / "x.md").write_text("x")
    assert D.classify(str(evil / "x.md"), [str(root)])["code"] == D.REFUSAL_OUTSIDE_ROOTS


def test_a_symlinked_root_is_compared_by_real_path(tmp_path):
    real = tmp_path / "cloud" / "data-home"
    real.mkdir(parents=True)
    (real / "x.md").write_text("x")
    link = tmp_path / "data-home"
    os.symlink(real, link)
    assert D.classify(str(link / "x.md"), [str(link)])["ok"]            # root named via the symlink
    assert D.classify(str(real / "x.md"), [str(link)])["ok"]            # file named via the real dir


NAMES = ["data-home", "work-a", "work-b", "web-c", "tools-d", "tools-e", os.path.join(".claude", "ctx-f"), "marketing-g"]


def test_gnomon_ships_no_extra_document_root_and_the_deployment_table_supplies_them(tmp_path, monkeypatch, put_deploy_data):
    """Owner ruling 2026-10-04 (P7): the deployment's directory names are deploy data (document-roots.json), never code.
    The neutral default adds none; the real list is staged for the cut-over under ~/gnomon-port-work/reroot/deploy/."""
    assert D.allowed_relpaths() == ()                               # a fresh install: nothing extra
    put_deploy_data("document-roots", {"relpaths": NAMES})
    assert list(D.allowed_relpaths()) == NAMES
    for n in NAMES[:-1]:                                            # the last one is deliberately absent
        os.makedirs(tmp_path / n)
    got = D.allowed_roots(str(tmp_path))
    assert got == [os.path.realpath(str(tmp_path / n)) for n in NAMES[:-1]]   # a missing root is omitted
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "work-a" / "n.md").write_text("n")
    assert D.classify("~/work-a/n.md")["ok"]                        # ~ expands at call time, deployment roots
    assert D.classify(str(tmp_path / "n.md"))["code"] == D.REFUSAL_MISSING


@pytest.mark.parametrize("bad", ["/etc", "~/x", "../up", "a/../../b", "", 5, None])
def test_a_deployment_relpath_that_is_not_a_plain_relative_path_is_dropped_and_recorded(put_deploy_data, bad):
    from gnomon import deploy_data

    put_deploy_data("document-roots", {"relpaths": ["ok-dir", bad]})
    assert D.allowed_relpaths() == ("ok-dir",)
    assert any("document-roots" in f["file"] for f in deploy_data.fault_summary())


def test_the_extension_list_is_exactly_the_ratified_one():
    assert D.DOC_EXTENSIONS == frozenset("." + e for e in
        "md txt lts smd json jsonl csv tsv pdf html htm png jpg jpeg svg log".split())


# ---- send-time record ---------------------------------------------------------------------------

def test_describe_for_persist_records_sha_size_mtime(t):
    entries, warns = D.describe_for_persist(docs_markdown(t), [t["root"]])
    assert warns == []
    assert [(e["scope"], e["index"], e["name"]) for e in entries] == [
        ("top", 0, "top-notes.md"), ("D1", 0, "design.pdf"), ("D1", 1, "data.csv")]
    top = entries[0]
    assert top["status"] == "ok" and top["sha256"] == sha(t["top"])
    assert top["size"] == os.path.getsize(t["top"]) and top["mtime"] == os.path.getmtime(t["top"])


def test_missing_document_warns_loudly_but_is_still_recorded(t):
    gone = os.path.join(t["root"], "gone.md")
    entries, warns = D.describe_for_persist(f"### D1 · q\n**Documents:** {gone}; {t['top']}\n", [t["root"]])
    assert [e["status"] for e in entries] == ["missing", "ok"]
    assert entries[0]["sha256"] == "" and "gone.md" in entries[0]["warning"]
    assert len(warns) == 1 and "NOT RECORDED" in warns[0] and "missing" in warns[0]


def test_refused_documents_are_recorded_by_name_but_never_read(t, monkeypatch):
    secret = os.path.join(t["outside"], "secret.md")
    open(secret, "w").write("s")
    sh = os.path.join(t["root"], "x.sh")
    open(sh, "w").write("s")
    read = []
    real = D.sha256_of
    monkeypatch.setattr(D, "sha256_of", lambda p, **k: (read.append(p), real(p, **k))[1])
    entries, warns = D.describe_for_persist(f"### D1 · q\n**Documents:** {secret}; {sh}\n", [t["root"]])
    assert [e["status"] for e in entries] == [D.REFUSAL_OUTSIDE_ROOTS, D.REFUSAL_EXTENSION]
    assert all(e["sha256"] == "" for e in entries) and len(warns) == 2
    assert read == [], "a refused path must never be opened for hashing"


def test_no_documents_means_no_entries_and_no_warnings():
    assert D.describe_for_persist("### D1 · q\n**What this is:** x\n", []) == ([], [])


def test_sha256_of_refuses_oversize_instead_of_reading(tmp_path):
    p = tmp_path / "big.md"
    p.write_bytes(b"x" * 100)
    assert D.sha256_of(str(p), max_bytes=10) == ("", D.REFUSAL_TOO_LARGE)
    assert D.sha256_of(str(p))[0] == sha(str(p))


# ---- the UI view: changed since sent ------------------------------------------------------------

def _raw(t):
    entries, _ = D.describe_for_persist(docs_markdown(t), [t["root"]])
    return {"documents": entries}


def test_view_unchanged_then_changed_then_missing(t):
    raw, md = _raw(t), docs_markdown(t)
    v = D.view_documents(raw, md, [t["root"]])
    assert [d["changed"] for d in v] == [False, False, False] and all(d["openable"] for d in v)
    assert v[0]["current_sha256"] == v[0]["sent_sha256"] == sha(t["top"])
    open(t["top"], "w").write("top notes v2 EDITED\n")
    v = D.view_documents(raw, md, [t["root"]])
    assert [d["changed"] for d in v] == [True, False, False]
    assert v[0]["current_sha256"] == sha(t["top"]) != v[0]["sent_sha256"]
    os.unlink(t["d1b"])
    v = D.view_documents(raw, md, [t["root"]])
    assert v[2]["changed"] is True and v[2]["missing_now"] is True and v[2]["openable"] is False
    assert v[2]["refusal"] == D.REFUSAL_MISSING


def test_view_does_not_claim_changed_when_nothing_was_recorded(t):
    # a record that never captured a sha (missing at send) cannot be "changed since sent"
    gone = os.path.join(t["root"], "later.md")
    entries, _ = D.describe_for_persist(f"### D1 · q\n**Documents:** {gone}\n", [t["root"]])
    open(gone, "w").write("now it exists")
    v = D.view_documents({"documents": entries}, "", [t["root"]])
    assert v[0]["changed"] is False and v[0]["sent_status"] == "missing" and v[0]["openable"] is True


def test_view_falls_back_to_the_markdown_listing_without_send_time_facts(t):
    v = D.view_documents({}, docs_markdown(t), [t["root"]])
    assert [d["name"] for d in v] == ["top-notes.md", "design.pdf", "data.csv"]
    assert all(d["sent_sha256"] == "" and d["changed"] is False for d in v)


def test_view_carries_the_named_refusal_so_the_button_can_say_why(t):
    p = os.path.join(t["root"], "run.sh")
    open(p, "w").write("x")
    entries, _ = D.describe_for_persist(f"### D1 · q\n**Documents:** {p}\n", [t["root"]])
    v = D.view_documents({"documents": entries}, "", [t["root"]])
    assert v[0]["openable"] is False and v[0]["refusal"] == D.REFUSAL_EXTENSION and v[0]["refusal_reason"]


def test_documents_at_send_value():
    es = [{"scope": "top", "name": "a.md", "sha256": "ab" * 32, "status": "ok"},
          {"scope": "D1", "name": "b c.pdf", "sha256": "", "status": "missing"},
          {"scope": "D2", "name": "d.md", "sha256": "cd" * 32, "status": "ok"}]
    assert D.documents_at_send(es) == f"top:a.md sha256:{'ab' * 32}; D1:b c.pdf sha256:(missing); D2:d.md sha256:{'cd' * 32}"
    assert D.documents_at_send(es, scope="D1") == "b c.pdf sha256:(missing)"
    assert D.documents_at_send(es, scope="D9") == ""


# ---- open_document: index-based, never a path -------------------------------------------------

class Rec:
    def __init__(self):
        self.calls = []
        self.texts = []

    def __call__(self, path, as_text=False):
        self.calls.append(path)
        self.texts.append(as_text)


def _open(t, scope, index, raw=None, opener=None, md=None):
    opener = opener or Rec()
    r = D.open_document(raw if raw is not None else _raw(t), md if md is not None else docs_markdown(t),
                        scope, index, opener=opener, roots=[t["root"]])
    return r, opener


def test_open_calls_the_injected_opener_once_with_the_resolved_path(t):
    r, op = _open(t, "D1", 1)
    assert r["ok"] and r["name"] == "data.csv"
    assert op.calls == [os.path.realpath(t["d1b"])]


@pytest.mark.parametrize("scope,index,code", [
    ("top", 1, D.REFUSAL_BAD_INDEX), ("top", 99, D.REFUSAL_BAD_INDEX), ("D1", 2, D.REFUSAL_BAD_INDEX),
    ("D2", 0, D.REFUSAL_BAD_INDEX), ("D9", 0, D.REFUSAL_BAD_INDEX),
    ("top", -1, D.REFUSAL_BAD_INDEX), ("top", "0", D.REFUSAL_BAD_INDEX), ("top", 0.0, D.REFUSAL_BAD_INDEX),
    ("top", True, D.REFUSAL_BAD_INDEX), ("top", None, D.REFUSAL_BAD_INDEX),
    ("../x", 0, D.REFUSAL_BAD_SCOPE), ("", 0, D.REFUSAL_BAD_SCOPE), ("d1", 0, D.REFUSAL_BAD_SCOPE),
    (None, 0, D.REFUSAL_BAD_SCOPE), ("D1/../top", 0, D.REFUSAL_BAD_SCOPE), (["top"], 0, D.REFUSAL_BAD_SCOPE),
])
def test_unknown_index_or_scope_is_refused_by_name_and_opens_nothing(t, scope, index, code):
    op = Rec()
    with pytest.raises(D.DocumentRefused) as ei:
        _open(t, scope, index, opener=op)
    assert ei.value.code == code and op.calls == []


def test_unknown_dsd_record_is_refused(t):
    op = Rec()
    with pytest.raises(D.DocumentRefused) as ei:
        D.open_document(None, "", "top", 0, opener=op, roots=[t["root"]])
    assert ei.value.code == D.REFUSAL_UNKNOWN_DSD and op.calls == []


def test_the_policy_is_re_evaluated_at_open_time(t):
    raw = _raw(t)                                   # recorded as fine at send
    os.unlink(t["top"])
    outside = os.path.join(t["outside"], "swap.md")
    open(outside, "w").write("swapped")
    os.symlink(outside, t["top"])                   # swapped for a symlink out of the roots AFTER send
    op = Rec()
    with pytest.raises(D.DocumentRefused) as ei:
        _open(t, "top", 0, raw=raw, opener=op)
    assert ei.value.code == D.REFUSAL_OUTSIDE_ROOTS and op.calls == []


def test_a_tampered_persisted_path_still_hits_the_policy(t):
    raw = _raw(t)
    raw["documents"][0]["path"] = "/etc/hosts"
    op = Rec()
    with pytest.raises(D.DocumentRefused) as ei:
        _open(t, "top", 0, raw=raw, opener=op)
    assert ei.value.code in (D.REFUSAL_OUTSIDE_ROOTS, D.REFUSAL_EXTENSION) and op.calls == []


def test_the_open_function_has_no_path_parameter():
    import inspect
    params = set(inspect.signature(D.open_document).parameters)
    assert params == {"raw", "markdown", "scope", "index", "opener", "roots", "home"}
    assert not any("path" in p for p in params)


def test_system_opener_refuses_to_run_under_pytest(monkeypatch, t):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a process was launched"))
    with pytest.raises(RuntimeError, match="refuses to run under pytest"):
        D.system_opener(t["top"])


def test_system_opener_invokes_macos_open_with_the_path_when_not_under_pytest(monkeypatch, t):
    import subprocess

    seen = []

    class P:
        returncode = 0
        stderr = b""

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: (seen.append(argv), P())[1])
    D.system_opener("/x/y.md")
    assert seen == [["/usr/bin/open", "/x/y.md"]]
    seen.clear()
    D.system_opener("/x/y.html", True)
    assert seen == [["/usr/bin/open", "-t", "/x/y.html"]]
    seen.clear()
    P.returncode = 1
    with pytest.raises(D.DocumentRefused) as ei:
        D.system_opener("/x/y.md")
    assert ei.value.code == D.REFUSAL_OPENER_FAILED


@pytest.mark.parametrize("ext", [".html", ".htm", ".svg", ".HTML", ".Svg"])
def test_active_content_types_open_as_text_never_in_a_browser(t, ext):
    p = os.path.join(t["root"], "page" + ext)
    open(p, "w").write("<svg/>")
    md = f"### D1 · q\n**Documents:** {p}\n"
    entries, _ = D.describe_for_persist(md, [t["root"]])
    op = Rec()
    D.open_document({"documents": entries}, md, "D1", 0, opener=op, roots=[t["root"]])
    assert op.calls == [os.path.realpath(p)] and op.texts == [True]


@pytest.mark.parametrize("name", ["a.md", "a.txt", "a.pdf", "a.png"])
def test_other_allowed_types_use_plain_open(t, name):
    p = os.path.join(t["root"], name)
    open(p, "w").write("x")
    md = f"### D1 · q\n**Documents:** {p}\n"
    entries, _ = D.describe_for_persist(md, [t["root"]])
    op = Rec()
    D.open_document({"documents": entries}, md, "D1", 0, opener=op, roots=[t["root"]])
    assert op.texts == [False]


def test_a_symlink_named_md_pointing_at_an_html_file_opens_as_text(t):
    real = os.path.join(t["root"], "real.html")
    open(real, "w").write("<html/>")
    link = os.path.join(t["root"], "alias.md")
    os.symlink(real, link)
    md = f"### D1 · q\n**Documents:** {link}\n"
    entries, _ = D.describe_for_persist(md, [t["root"]])
    op = Rec()
    D.open_document({"documents": entries}, md, "D1", 0, opener=op, roots=[t["root"]])
    assert op.texts == [True]
