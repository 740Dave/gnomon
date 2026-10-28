"""X58 fix round 4: the owner's seven bypass CLASSES for the human-only route rule (g76), as STRINGS handed to ``deny_guard.decide``.

Authorized defensive testing of this product's own hook guard, by its owner, before it goes live. Nothing here is executed and nothing is sent over a
network: every case is a string, and every route / path string is built from pieces (as in tests/test_x58_route_rule_f1_p2.py). Each
case that would reach a human-only route on 127.0.0.1 if it were run on this machine (the Apps confirm / settings routes, the DSD submit
route) must DENY at the tip, alone AND combined with a ``/../`` elsewhere in the command (the F2 shape). A variant beyond what a static
guard can see (a value computed at run time) is marked KNOWN_LIMIT in its name and asserts TODAY's behaviour; it is not a rule.
C7 (ssh to a non-localhost host) records the outcome only: a remote 127.0.0.1 is not ours.

Table mode:  X58_TREE=<tree> python3 -P tests/test_x58_route_classes.py   prints class, case, variant, outcome for that tree (live = ~/gnomon).
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TREE = os.path.realpath(os.environ.get("X58_TREE") or ROOT)
if TREE != ROOT:
    sys.path.insert(0, TREE)
    for _m in [m for m in sys.modules if m == "gnomon" or m.startswith("gnomon.")]:
        del sys.modules[_m]
from gnomon.harness import deny_guard as G  # noqa: E402

H = "http://127.0.0.1:8" + "770"
A = "/" + "api" + "/"
C = "con" + "firm"
S = "set" + "tings"
SUB = "sub" + "mit"
CURL = "cu" + "rl -X POST "
DOTS = "/../"                                    # the unrelated dot-segment elsewhere in the command (the F2 shape)


def _curl(url, quote="'"):
    return CURL + quote + url + quote


def _with_elsewhere(cmd):
    """The same command plus a ``/../`` OUTSIDE the route: a header here (the other placements are covered in test_x58_round3.py)."""
    return cmd + " -H 'X: " + DOTS + "'"


#: (class, name, command, known_limit_expectation)  known_limit_expectation is None for an ordinary case (asserts DENY); for a KNOWN_LIMIT
#: case it is the outcome TODAY ("allow" or "deny") that the test pins.
CASES = []


def case(cls, name, cmd, known_limit=None):
    CASES.append((cls, name, cmd, known_limit))


ROUTE_X = H + A + "apps/x/x/"                     # a route prefix that needs ONE ``..`` resolved before ``confirm``

# ------------------------------------------------------------------------------------------------ C1: dots split by quoting / concatenation
case("C1", "single-quoted dots", CURL + ROUTE_X + "'.''.'/" + C)
case("C1", "double-quoted dots", CURL + ROUTE_X + '"."".""/"' + C)
case("C1", "empty quotes between the dots", CURL + ROUTE_X + ".''./" + C)
case("C1", "backslash between the dots", CURL + ROUTE_X + ".\\./" + C)
case("C1", "empty expansion between the dots", CURL + ROUTE_X + ".${E}./" + C)
case("C1", "empty braces expansion between the dots", CURL + ROUTE_X + ".${EMPTY}./" + C)
case("C1", "quoted slash after the dots", CURL + ROUTE_X + "..'/'" + C)
case("C1", "python concatenation of the dots", "python3 -c \"import urllib.request as u; u.urlopen(u.Request('" + ROUTE_X + ".'+'./" + C + "', data=b'x'))\"")
case("C1", "node concatenation of the dots", "node -e \"fetch('" + ROUTE_X + ".'+'./" + C + "', {method: 'POST'})\"")

# ------------------------------------------------------------------------------------------------ C2: dots as shell / printf / ANSI-C escapes
case("C2", "ansi-c hex dots", CURL + ROUTE_X + "$'\\x2e\\x2e'/" + C)
case("C2", "ansi-c octal dots", CURL + ROUTE_X + "$'\\056\\056'/" + C)
case("C2", "ansi-c unicode dots", CURL + ROUTE_X + "$'\\u002e\\u002e'/" + C)
case("C2", "ansi-c one escaped dot", CURL + ROUTE_X + ".$'\\x2e'/" + C)
case("C2", "backslash-escaped dots", CURL + ROUTE_X + "\\.\\./" + C)
case("C2", "hex escape in double quotes (python reads it)", "python3 -c \"import urllib.request as u; u.urlopen(u.Request('" + ROUTE_X + "\\x2e\\x2e/" + C + "', data=b'x'))\"")
case("C2", "printf command substitution KNOWN_LIMIT", CURL + ROUTE_X + "$(printf '\\x2e\\x2e')/" + C, known_limit="allow")
case("C2", "echo -e command substitution KNOWN_LIMIT", CURL + ROUTE_X + "$(echo -e '\\x2e\\x2e')/" + C, known_limit="allow")

# ------------------------------------------------------------------------------------------------ C3: percent-encoded dots in a token
case("C3", "single encoding lower", CURL + ROUTE_X + "%2e%2e/" + C)
case("C3", "single encoding upper", CURL + ROUTE_X + "%2E%2E/" + C)
case("C3", "one dot encoded, first", CURL + ROUTE_X + "%2e./" + C)
case("C3", "one dot encoded, second", CURL + ROUTE_X + ".%2e/" + C)
case("C3", "double encoding lower", CURL + ROUTE_X + "%252e%252e/" + C)
case("C3", "double encoding upper", CURL + ROUTE_X + "%252E%252E/" + C)
case("C3", "mixed single and double", CURL + ROUTE_X + "%252e%2e/" + C)
case("C3", "encoded segment letter", CURL + H + A + "apps/x/%63onfirm")
case("C3", "double-encoded segment letter", CURL + H + A + "apps/x/%2563onfirm")
case("C3", "encoded dots in a quoted url", _curl(ROUTE_X + "%2e%2e/" + C))

# ------------------------------------------------------------------------------------------------ C4: the route assembled across tokens / variables
case("C4", "api split by an empty braces expansion", CURL + H + "/ap${E}i/apps/x/" + C)
case("C4", "segment split by an empty expansion", CURL + H + A + "apps/x/con${E}firm")
case("C4", "two adjacent quoted fragments", CURL + "'" + H + A + "apps/x/'" + C)
case("C4", "base in one quoted token, path in the next", CURL + '"' + H + '"' + A + "apps/x/" + C)
case("C4", "variable holds the prefix", "P=" + A + "apps/x; " + CURL + H + "$P/" + C)
case("C4", "two variables", "U=" + H + "; R=" + A + "apps; " + CURL + "${U}${R}/x/" + C)
case("C4", "variable holds the last segment", "L=" + C + "; " + CURL + H + A + "apps/x/$L")
case("C4", "assembled with an empty expansion and a dot-segment", CURL + H + A + "apps/x/x/..${E}/" + C)
case("C4", "dsd submit across two tokens", CURL + H + A + "dsd/" + '"' + SUB + '"')
case("C4", "settings across two variables", "B=" + A + "apps/x; T=" + S + "; " + CURL + H + "${B}/${T}")
case("C4", "value computed at run time KNOWN_LIMIT", "V=$(printf con)firm; " + CURL + H + A + "apps/x/$V", known_limit="allow")
case("C4", "value read from a file KNOWN_LIMIT", "V=$(cat /tmp/x58-segment); " + CURL + H + A + "apps/x/$V", known_limit="allow")

# ------------------------------------------------------------------------------------------------ C5: mixed case in the path segments
case("C5", "upper-case api and apps", CURL + H + "/API/APPS/x/" + C)
case("C5", "mixed-case confirm", CURL + H + A + "apps/x/Con" + "FIRM")
case("C5", "mixed-case settings", CURL + H + A + "Apps/x/Set" + "Tings")
case("C5", "mixed-case dsd submit", CURL + H + "/Api/DSD/" + "Sub" + "Mit")
case("C5", "mixed case with a dot-segment", CURL + H + A + "Apps/x/x/../Con" + "Firm")

# ------------------------------------------------------------------------------------------------ C6: nesting
_inner = CURL + H + A + "apps/x/" + C
_inner_dd = CURL + ROUTE_X + "%2e%2e/" + C
for _wrap in ("bash -c", "sh -c", "zsh -c"):
    case("C6", _wrap + " double-quoted", _wrap + ' "' + _inner + '"')
    case("C6", _wrap + " single-quoted with a dot-segment", _wrap + " '" + _inner_dd.replace("'", "") + "'")
case("C6", "bash -c nested in sh -c", "sh -c 'bash -c \"" + _inner + "\"'")
case("C6", "python -c urlopen", "python3 -c \"import urllib.request as u; u.urlopen(u.Request('" + H + A + "apps/x/" + C + "', data=b'x'))\"")
case("C6", "python -c urlopen with a dot-segment", "python3 -c \"import urllib.request as u; u.urlopen(u.Request('" + ROUTE_X + "../" + C + "', data=b'x'))\"")
case("C6", "node -e fetch", "node -e \"fetch('" + H + A + "apps/x/" + C + "', {method: 'POST'})\"")
case("C6", "env prefix", "env FOO=1 " + _inner)
case("C6", "xargs from a pipe", "echo '" + H + A + "apps/x/" + C + "' | xargs " + CURL)
case("C6", "eval string", "eval '" + _inner + "'")
case("C6", "eval string with a dot-segment", "eval '" + _inner_dd.replace("'", "") + "'")
case("C6", "command substitution wrapper", "echo $(" + _inner + ")")
case("C6", "backtick wrapper", "echo `" + _inner + "`")
case("C6", "subshell", "(" + _inner + ")")

# ------------------------------------------------------------------------------------------------ C7: ssh to a NON-localhost host (outcome recorded, no deny asserted)
C7 = [
    ("ssh buildbox, plain", "ssh build-box " + _inner),
    ("ssh buildbox, quoted remote command", "ssh build-box '" + _inner + "'"),
    ("ssh buildbox, dot-segment", "ssh build-box '" + _inner_dd.replace("'", "") + "'"),
]


def verdict(cmd):
    return G.decide(cmd, "/tmp")


def _variants(cmd):
    return (("alone", cmd), ("with /../ elsewhere", _with_elsewhere(cmd)))


_IDS = ["%s %s" % (c, n) for c, n, _, _ in CASES]


@pytest.mark.parametrize("cls,name,cmd,known", CASES, ids=_IDS)
def test_route_classes_alone_and_with_a_dotdot_elsewhere(cls, name, cmd, known):
    for label, text in _variants(cmd):
        v = verdict(text)
        if known is None:
            assert v is not None and v.startswith(G.ROUTE_TAG), (cls, name, label, v)
        else:                                                  # KNOWN_LIMIT: pin today's behaviour, do not invent a rule
            assert (v is not None) == (known == "deny"), (cls, name, label, "known-limit expectation %s, got %s" % (known, v))


def test_c7_ssh_to_a_remote_host_is_recorded_not_asserted():
    outcomes = {name: [("DENY" if verdict(t) else "ALLOW") for _, t in _variants(cmd)] for name, cmd in C7}
    print("C7 outcomes (alone, with /../ elsewhere):", outcomes)
    assert set(outcomes) == {n for n, _ in C7}               # recorded; a remote 127.0.0.1 is not ours, so nothing is asserted about the value


def test_the_controls_stay_allowed():
    for cmd in (CURL + H + A + "health", CURL + H + A + "apps/x/status", "ls /../ && cd ../.. && ls ./x/../y",
                "git commit -m 'document " + A + "apps/x/" + C + " (operator route)'"):
        for _label, text in _variants(cmd):
            if text.startswith("git"):
                text = cmd
            assert verdict(text) is None, text


def test_docstrings_are_not_stale():
    """Round 4 P3: the retracted D10 wording and the 'empty segments' claim are gone from the docstrings."""
    assert "never touched" not in (G._without_data_heredocs.__doc__ or "")
    assert "Empty segments are KEPT" in (G._resolve_tokens.__doc__ or "")


if __name__ == "__main__":
    print("TREE", G.__file__)
    for cls, name, cmd, known in CASES:
        row = [("DENY" if verdict(t) else "ALLOW") for _, t in _variants(cmd)]
        print("%-3s | %-52s | alone %-5s | +/../ %-5s | %s" % (cls, name, row[0], row[1], "KNOWN-LIMIT" if known else ""))
    for name, cmd in C7:
        row = [("DENY" if verdict(t) else "ALLOW") for _, t in _variants(cmd)]
        print("C7  | %-52s | alone %-5s | +/../ %-5s | outcome only" % (name, row[0], row[1]))
