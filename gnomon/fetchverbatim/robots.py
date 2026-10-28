"""A small deterministic robots.txt evaluator (RFC 9309 semantics) that names the rule it matched.

The standard library's ``urllib.robotparser`` answers allow/deny only, and the spec wants the refusal to say
"blocked by robots.txt: <rule>", so this evaluates the file itself:

* groups are keyed by ``User-agent`` product tokens; the group whose token is the LONGEST case-insensitive
  prefix-match of our token wins; ``*`` is the fallback; no group means allowed;
* inside the winning group the LONGEST matching ``Allow``/``Disallow`` path pattern wins, ``Allow`` wins a tie;
  ``*`` is a wildcard and a trailing ``$`` anchors the end; an empty ``Disallow`` allows everything;
* ``/robots.txt`` itself is always fetchable (it is how we got here).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import unquote


@dataclass(frozen=True)
class RobotsDecision:
    allowed: bool
    rule: Optional[str]       # e.g. "Disallow: /private" (None when no rule matched)
    group: Optional[str]      # the user-agent token of the group that applied


def _pattern_regex(pattern: str) -> "re.Pattern[str]":
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    rx = "".join(".*" if c == "*" else re.escape(c) for c in body)
    return re.compile("^" + rx + ("$" if anchored else ""))


def _norm_path(path: str) -> str:
    # Compare on the percent-decoded form of unreserved characters only; keep it simple and deterministic.
    return unquote(path, errors="replace") if "%" in path else path


def parse(text: str) -> List[Tuple[List[str], List[Tuple[str, str]]]]:
    """``[(user_agent_tokens, [(directive, path_pattern), ...]), ...]`` in file order."""
    groups: List[Tuple[List[str], List[Tuple[str, str]]]] = []
    agents: List[str] = []
    rules: List[Tuple[str, str]] = []
    in_rules = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if in_rules:
                groups.append((agents, rules))
                agents, rules, in_rules = [], [], False
            agents.append(value.lower())
        elif key in ("allow", "disallow"):
            if not agents:
                continue
            in_rules = True
            rules.append((key, value))
    if agents:
        groups.append((agents, rules))
    return groups


def evaluate(text: str, product_token: str, path_query: str) -> RobotsDecision:
    token = product_token.lower()
    path = _norm_path(path_query or "/")
    if path.split("?", 1)[0] == "/robots.txt":
        return RobotsDecision(True, None, None)
    best_len, best_rules, best_agent = -1, None, None
    star_rules = None
    for agents, rules in parse(text):
        for a in agents:
            if a == "*":
                star_rules = (star_rules or []) + rules
            elif a and token.startswith(a) and len(a) > best_len:
                best_len, best_rules, best_agent = len(a), rules, a
            elif a and token.startswith(a) and len(a) == best_len:
                best_rules = (best_rules or []) + rules
    if best_rules is None:
        best_rules, best_agent = star_rules, ("*" if star_rules is not None else None)
    if not best_rules:
        return RobotsDecision(True, None, best_agent)
    match_len, verdict, rule = -1, True, None
    for directive, pattern in best_rules:
        if directive == "disallow" and pattern == "":
            continue
        if not pattern:
            continue
        if _pattern_regex(_norm_path(pattern)).match(path):
            n = len(pattern.rstrip("$"))
            allow = directive == "allow"
            if n > match_len or (n == match_len and allow and not verdict):
                match_len, verdict = n, allow
                rule = f"{'Allow' if allow else 'Disallow'}: {pattern}"
    return RobotsDecision(verdict, rule, best_agent)
