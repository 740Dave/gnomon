"""(live part): check_human_routes must test the NORMALISED text in its cheap prefilter.

Before the fix the prefilter looked for 'api' / '%2f' in the RAW lower-cased text and returned before _route_squash
ran, so any shape that splits the literal api (a backslash, quote splits, '+' concatenation, a shell variable, a
percent-encoded letter) was allowed without analysis (confirm-proof delta-3 F-7).

Mutation pin: restoring the raw-text prefilter kills every SPLIT_DENY case (see RUN_SUMMARY-x56-live.md).
STATED LIMITS (interpreter run-time escapes) are pinned at their CURRENT behaviour, see the last section.
"""

import os
import re

import pytest

from gnomon.harness import deny_guard as G

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
U = "http://127.0.0.1:8770"

# route tails under each stem (after the api segment)
STEMS = {
    "dsd": "/dsd/submit/abc",
    "submit": "/dsd/sub\\mit/abc",
    "apps-confirm": "/apps/x/confirm",
    "apps-settings": "/apps/x/settings",
    "apps-actions": "/apps/x/actions/go",
}

# ways of writing the literal 'api' without the three letters being adjacent in the raw text
CURL_SPLITS = {
    "plain": "api",
    "backslash": "ap\\i",
    "backslash-first": "a\\pi",
    "dquote": 'a"p"i',
    "squote": "a'p'i",
    "plus": '"ap"+"i"',
    "var-split": "ap${E}i",
    "pct-letter": "%61pi",
    "pct-letter-p": "a%70i",
    "pct-letter-i": "ap%69",
    "pct-upper": "%41pi",
}


def _curl(split, stem):
    return "curl -s -X POST " + U + "/" + CURL_SPLITS[split] + STEMS[stem]


def _py(split, stem):
    seg = CURL_SPLITS[split]
    # inside a double-quoted shell -c string; the backslash form needs doubling to survive the shell
    seg = seg.replace('"', "'") if '"' in seg else seg
    return ('python3 -c "import urllib.request as u; u.urlopen(\'' + U + "/" + seg + STEMS[stem] + '\')"')


SPLIT_NAMES = [k for k in CURL_SPLITS if k != "plain"]


@pytest.mark.parametrize("stem", list(STEMS))
@pytest.mark.parametrize("split", list(CURL_SPLITS))
def test_curl_to_a_human_route_is_denied_however_api_is_spelled(split, stem):
    with pytest.raises(G.Deny) as ei:
        G.check_human_routes(_curl(split, stem))
    assert ei.value.args and str(ei.value).startswith(G.ROUTE_TAG) or G.ROUTE_TAG in str(ei.value)


@pytest.mark.parametrize("stem", list(STEMS))
@pytest.mark.parametrize("split", list(CURL_SPLITS))
def test_python_urllib_to_a_human_route_is_denied_however_api_is_spelled(split, stem):
    with pytest.raises(G.Deny):
        G.check_human_routes(_py(split, stem))


@pytest.mark.parametrize("split", SPLIT_NAMES)
def test_decide_reports_the_route_tag_for_split_api(split):
    r = G.decide(_curl(split, "apps-confirm"))
    assert r and r.startswith(G.ROUTE_TAG), (split, r)


# ------------------------------------------------------------------------------------------------ controls
def test_control_plain_client_call_is_denied():
    with pytest.raises(G.Deny):
        G.check_human_routes("curl -s -X POST " + U + "/api/dsd/submit/abc")


@pytest.mark.parametrize("cmd", [
    "git commit -m 'curl ap\\i/apps/x/confirm is blocked'",
    "grep -n 'a\"p\"i/dsd/submit' notes.md",
    "echo the api/apps/x/confirm route is the operator's",
    "echo 'a\"p\"i/apps/x/confirm is described in the docs'",
    "curl -s " + U + "/ap\\i/health",
    "curl -s " + U + "/%61pi/health",
    "ls -la",
    "",
])
def test_control_prose_or_other_routes_are_allowed(cmd):
    G.check_human_routes(cmd)            # must not raise
    assert G.decide(cmd) is None, cmd


# ------------------------------------------------------------------------------------------------ source pin
def _fn_body(src, name):
    return src.split("def " + name, 1)[1].split("\n\n\n", 1)[0]


def test_prefilter_is_applied_to_the_squashed_text_in_source():
    """X56's property, against X58's structure. The old shape was ``_route_squash(text)`` called inside check_human_routes before an
    ``"api" not in`` test; X58 moved normalisation into ``_route_forms`` (every normalised reading, escape-decoded ones included) and the
    prefilter is ``any("api" in f ...) for f in forms``. What this checks: (1) the function normalises FIRST (no test of the literal ``"api"`` or ``%2f`` appears in code before ``forms = _route_forms(text)``); (2) the ``"api"`` / ``"%2f"`` prefilter tests those forms and never the raw or
    merely lower-cased text; (3) ``_route_forms`` itself decodes escapes and squashes (it calls _decode_escapes, strips quote/backslash
    splits via _strip_squash and collapses ``//`` and ``/./``), and ``_route_squash`` is still its most-collapsed reading, so a mutation
    that tests the raw text or drops the decode is caught by the SPLIT_DENY cases and by this pin."""
    src = open(os.path.join(ROOT, "gnomon", "harness", "deny_guard.py"), encoding="utf-8").read()
    body = _fn_body(src, "check_human_routes")
    assert "low = text.lower()" not in body
    assert "forms = _route_forms(text)" in body
    before = body.split("forms = _route_forms(text)", 1)[0]
    before = re.sub(r'"""(.|\n)*?"""', "", before)                       # the docstring and comments may talk about "api"
    before = "\n".join(l for l in before.splitlines() if not l.strip().startswith("#"))
    assert '"api"' not in before and "'api'" not in before and "%2f" not in before       # nothing is tested before normalising
    assert 'any("api" in f or "%2f" in f for f in forms)' in body
    for raw_test in ('"api" in text', '"api" not in text', "'api' in text", '"api" in text.lower()', '"%2f" in text'):
        assert raw_test not in body, raw_test
    forms_body = _fn_body(src, "_route_forms")
    assert "_decode_escapes(text)" in forms_body and "_strip_squash(raw)" in forms_body and '"/./"' in forms_body
    assert "_route_forms(text)[-1]" in _fn_body(src, "_route_squash")


# ------------------------------------------------------------------------------------------------ stated limits
# Interpreter / shell RUN-TIME escapes that decode to a letter only when the program runs. The guard reads text, never
# executes it, so these pass TODAY. Each is pinned at its measured current behaviour (g89); closing any of them is a
# separate change and moves it into SPLIT_DENY. Reason: spec 13 stated-limits-security-flags.
LIMIT_REASON = "stated limit, spec 13 stated-limits-security-flags: run-time escape, text never spells the letter"
STATED_LIMITS = {
    "python-hex": "python3 -c \"import urllib.request as u; u.urlopen('" + U + "/\\x61pi/apps/x/confirm')\"",
    "python-unicode": "python3 -c \"import urllib.request as u; u.urlopen('" + U + "/\\u0061pi/apps/x/confirm')\"",
    "python-octal": "python3 -c \"import urllib.request as u; u.urlopen('" + U + "/\\141pi/apps/x/confirm')\"",
    "bash-ansi-c": "curl -s -X POST " + U + "/$'\\x61'pi/apps/x/confirm",
    "bash-ansi-c-octal": "curl -s -X POST " + U + "/$'\\141'pi/apps/x/confirm",
    "printf-subst": "curl -s -X POST " + U + "/$(printf '\\x61')pi/apps/x/confirm",
    "echo-e-subst": "curl -s -X POST " + U + "/$(echo -e '\\x61')pi/apps/x/confirm",
}
# filled in from the measurement: names the guard already denies today (recorded, not assumed; measured 2026-10-09 on the batch-land tip)
# X58 (batch land fix round 2): the deny guard's ``_route_forms`` now also reads the ESCAPE-DECODED text (_decode_escapes), so the five
# forms that spell the letter with a \x / \u / octal / $'..' escape in the command text are DENIED. They stay pinned, now as denials.
# printf-subst and echo-e-subst (the letter only exists after a command substitution runs) are still stated limits and still pass.
STATED_LIMITS_DENIED_TODAY = {"python-hex", "python-unicode", "python-octal", "bash-ansi-c", "bash-ansi-c-octal"}


@pytest.mark.parametrize("name", list(STATED_LIMITS))
def test_stated_limit_current_behaviour_is_pinned(name):
    cmd = STATED_LIMITS[name]
    denied = False
    try:
        G.check_human_routes(cmd)
    except G.Deny:
        denied = True
    assert denied == (name in STATED_LIMITS_DENIED_TODAY), (name, LIMIT_REASON)
