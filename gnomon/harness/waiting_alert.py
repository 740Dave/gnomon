"""(spec): the alert for an agent whose terminal waits on the operator.

This module holds the PURE parts: what text a waiting permission prompt shows, how a secret-shaped string is masked, and what
the alert says. It is imported lazily (only when the flag ``waiting_on_operator_alert`` is ON), so with the flag OFF nothing
here is even loaded.

What may leave the session, and where it persists (checklist g144):

* the session's own ``injection_log.jsonl`` (synced) is ALLOWLISTED: it carries the dialog kind, the episode, times, the title
  line's first word only when it is one of :data:`TITLE_WORDS`, the countdown seconds, a length count and the confidential flag.
  It carries NO screen text of any kind, masked or not;
* the question block (at most :data:`BLOCK_MAX_LINES` lines and :data:`BLOCK_MAX_CHARS` characters, plain text, each line cut to
  :data:`LINE_MAX_CHARS`, secret-shaped text masked) goes into (1) the stored alert while the prompt is open (the dashboard badge
  reads it from there; it is removed when the prompt closes: see :func:`closed_text`) and (2) a macOS notification, whose copy
  macOS keeps under the operator's control;
* for a session whose workspace is confidential (or whose setting cannot be read) NONE of the question text goes anywhere:
  only the agent's name and the dialog kind;
* every step that reads screen text (the dialog reader, the masker, the confidential read) runs on the alert WORKER thread, on
  input bounded before any matching; the terminal-reading thread only copies a slice of its tail and hands it off.

Masking is pattern-based. It can miss an unusual secret; that limit is stated in the spec and the go-live check.

Known limits of the masker (plain list; the go-live check repeats the first line):

* It masks too MUCH in places. After a mysql-family command, any later word that starts ``-p`` (``-print``, ``-path``, ``cp -pr``) is
  cut at the ``p``, and so is a single-dash word that reaches a ``p`` through no-argument letters only (``-vpS``, ``-NBpS``); a
  ``;`` or ``|`` glued after a ``-p`` value is masked with it (``mysql -pS;ls`` hides ``;ls``); ``mysql -p`` followed by a glued
  option hides that option. A word that starts with a value-taking option letter is left whole (``-hdb-prod``, ``-uapp``, ``-Ddbp``,
  ``-Pp3306``), so a ``p`` in a host or user name is not cut; the letter set is one list for the family, so a tool whose letter
  means something else (``mysqldump -e`` takes no argument) can show a password written as ``-epS``. Over-masking is the safe direction.
* It only knows the commands in ``_PASSWORD_P_CMDS`` (plus sshpass). A password given to any other command as ``-pVALUE`` or by a
  word it has no keyword for is not seen (mysqlslap, the other ``mariadb-*`` names, and so on; psql is not in the list because its
  ``-p`` is a port). A new password-taking command is added to that list with a test (checklist g152).
* A value written ``$'...'`` after a keyword (``DB_PASSWORD=$'x'``, ``--password $'x'``) and a keyword value written as a substitution
  (``password=$(echo x)``, ``TOKEN=${X:-x}``) are cut at the first quote or blank, not read as one shell word; only the ``-p`` rules
  read whole shell words.
* A secret under 32 characters with no keyword (password, token, key ...) beside it is not masked; neither is one in an environment
  variable whose name has no such word, one in a query parameter under another name, or one split by a zero-width character.
* It reads the first command line of the dialog only, after the screen text has been cut to 400 characters per line; a secret that
  is cut off is never shown, but nothing is said about it.
* What the screen shows is not what was typed: wrapped lines, redraws and anything the terminal drew in a layout this code has not
  seen can hide or split a secret. No real Claude Code prompt layout has been measured.

stdlib only; never raises into its callers (every public function is total).
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from .modal_prompt import DIALOG_KINDS, DIALOG_PERMISSION, WAITING_ALERT_KINDS

#: the question block bounds (spec requirement-question-text)
BLOCK_MAX_LINES = 6
BLOCK_MAX_CHARS = 400
#: a command line (and every other line of the block) is cut to this many characters (spec requirement-command-text)
LINE_MAX_CHARS = 120
MASK = "********"

#: the alert's source / key grammar (checklist g143): source ``harness:<instance id>``, key ``waiting:<instance id>:<episode>``;
#: the episode is a counter ModalGuard keeps in memory for the life of the harness process (starts at 1, +1 per hold episode
#: opened; a relaunch starts again at 1, safe because the alert service de-duplicates only within 600 s).
SOURCE_PREFIX = "harness:"
KEY_PREFIX = "waiting:"
#: waiting alerts may use at most this many of the alert service's 5 shared macOS notifications per 5 minutes
MACOS_SHARE = 3

#: the plain wording for every dialog kind (g123: one closed list, one text per member; a test enumerates DIALOG_KINDS).
KIND_LABEL = {
    "permission-prompt": "a permission prompt",
    "AskUserQuestion": "a question",
    "ExitPlanMode": "a plan approval",
}
#: outcomes the close event may carry; the dashboard shows any other value as 'unknown'.
OUTCOMES = ("answered", "timed out", "unknown")

NOT_CAPTURED = "question text not captured"
NO_COUNTDOWN = "no countdown shown"


def kind_label(kind: Any) -> str:
    return KIND_LABEL.get(kind, "a prompt") if isinstance(kind, str) else "a prompt"


# --- masking -------------------------------------------------------------------------------------------------------------
#
# Every step below is a linear scan: one regex per class, each a SINGLE quantifier over a character class (no nested quantifiers,
# no alternation under a star), and everything else plain string scanning. The input is also bounded (MAX_MASK_INPUT) so no input
# can make this slow. It runs on the alert worker thread, never on the terminal-reading thread.

#: no input longer than this is looked at (callers pass lines already cut to LINE_SCAN_CHARS); the rest is dropped, never shown
MAX_MASK_INPUT = 4000
_TOKEN = re.compile(r"[A-Za-z0-9_.-]+")
_RUN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_SECRET_WORDS = ("password", "passwd", "passphrase", "pwd", "pass", "secret", "token", "key", "credential", "auth", "cookie",
                 "private")
_HEADER_NAMES = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "apikey"})
_HEADER_SUFFIXES = ("-token", "-key", "-secret", "-auth", "-password", "-pass", "-credential")
_SCHEMES = ("bearer ", "basic ", "token ", "digest ")
_VALUE_END = " \t&;|\"'),}"
#: (prefix, minimum total length): a token that starts so and is at least this long is a key
_KEY_PREFIXES = (("sk-", 11), ("sk_live_", 16), ("sk_test_", 16), ("ghp_", 20), ("gho_", 20), ("ghu_", 20), ("ghs_", 20),
                 ("ghr_", 20), ("github_pat_", 27), ("glpat-", 18), ("xoxa-", 13), ("xoxb-", 13), ("xoxp-", 13), ("xoxr-", 13),
                 ("xoxs-", 13), ("AKIA", 20), ("ASIA", 20), ("AIza", 24), ("npm_", 24), ("pypi-", 25), ("hf_", 23), ("eyJ", 20))
_HEX = frozenset("0123456789abcdefABCDEF")
#: commands whose ``-pVALUE`` is a password; a bare ``-p`` (mkdir -p, ssh -p 2222) is never masked
#: sshpass options that take an ARGUMENT word (sshpass man page: -f file, -d number, -P prompt, -p password); inside the sshpass
#: scope that word is consumed before the scope can close. -e, -v, -h and -V take none.
_SSHPASS_ARG_OPTS = frozenset({"-P", "-f", "-d"})
#: Short options of the mysql client family that take a VALUE, so the rest of the word is that value and not a password. Source: the
#: MySQL 8.0 Reference Manual, "mysql Client Options" (-D database, -e execute, -h host, -P port, -S socket, -u user), plus
#: -O set-variable and -T tab from "mysqldump" and "mysqladmin Options" (-O name=value). Letters whose meaning differs between the
#: tools (c, d, i, r, w) are deliberately NOT here; -e is here as the ruling names it although mysqldump's -e takes no argument.
_VALUE_SHORT = frozenset("DehPSuOT")
_PASSWORD_P_CMDS = frozenset({"mysql", "mysqldump", "mysqladmin", "mariadb", "mariadb-dump", "mysqlcheck", "mysqlimport", "mysqlshow",
                              "mysqlbinlog", "mysqlpump", "mariadb-admin"})
#: URL query parameter names whose value is masked (also any name ending _token / _key / _secret)
_QUERY_SECRET_NAMES = frozenset({"code", "token", "sig", "signature", "key", "secret", "password", "passwd", "pwd", "pass",
                                 "access_token", "refresh_token", "id_token", "auth", "apikey", "api_key", "api-key", "client_secret"})
_QUERY_SUFFIXES = ("_token", "_key", "_secret", "-token", "-key", "-secret")
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


def _query_spans(s: str) -> List[Tuple[int, int]]:
    """Values of URL query parameters (after a ``?``) whose name is a secret word. A single forward scan per ``?``; a scan ends at
    the first character that is not part of a ``name=value&...`` chain, so the whole pass is linear."""
    n = len(s)
    out: List[Tuple[int, int]] = []
    pos = s.find("?")
    while pos != -1 and pos < n:
        i = pos + 1
        while i < n:
            st = i
            while i < n and s[i] in _NAME_CHARS:
                i += 1
            name = s[st:i].lower()
            if i >= n or s[i] != "=" or i == st:
                break
            v = i + 1
            e = v
            while e < n and s[e] not in "& \t\"'#":
                e += 1
            if name in _QUERY_SECRET_NAMES or name.endswith(_QUERY_SUFFIXES):
                out.append((v, e))
            i = e
            if i < n and s[i] == "&":
                i += 1
            else:
                break
        pos = s.find("?", max(i, pos + 1))
    return out


def _value_span(s: str, v: int) -> Tuple[int, int]:
    n = len(s)
    if v >= n:
        return v, v
    if s[v] in "\"'":
        c = s.find(s[v], v + 1)
        return (v + 1, n) if c == -1 else (v + 1, c)
    e = v
    while e < n and s[e] not in _VALUE_END:
        e += 1
    return v, e


_CURLY_CLOSE = {"\u2018": "\u2019", "\u201c": "\u201d"}


_BLANKS = " \t\n"      # the shell's blanks: NOT str.isspace(), so NBSP, U+2003 and U+3000 are part of the word


def _cluster_p_index(tok: str) -> int:
    """Index of the ``p`` that starts a password in a single-dash word after a mysql-family command, else -1. Walk the letters: a
    value-taking option letter (``_VALUE_SHORT``) ends the walk, the rest being that option's value (``-hdb-prod``, ``-uapp``); a
    non-letter ends it too; reaching ``p`` through no-argument letters (``-vpS``, ``-NBpS``) is the password."""
    for i in range(1, len(tok)):
        ch = tok[i]
        if ch == "p":
            return i
        if ch in _VALUE_SHORT or not ch.isalpha():
            return -1
    return -1


def _shell_word_end(s: str, v: int) -> int:
    """The index where the shell word that starts at ``v`` ends: the first blank (space, tab, newline) outside any quote or
    substitution. Followed as quoting: single quotes, double quotes (a backslash escapes the next character), ``$'...'`` (backslash
    escapes), curly quotes, ``$( ... )``, ``${ ... }`` and backticks, with nesting counted (a substitution may hold blanks and quotes of
    its own). A backslash outside quotes escapes the next character. If a quote or substitution is never closed the word runs to the
    end of the line. One forward pass over a stack of open contexts, linear."""
    n = len(s)
    i = v
    stack: List[Tuple[str, str, bool]] = []      # (kind, closer, backslash escapes): kind 'q' = a quote, 's' = a substitution
    while i < n:
        c = s[i]
        top = stack[-1] if stack else None
        if top is not None and top[0] == "q":
            if c == "\\" and top[2]:
                i += 2
                continue
            if c == top[1]:
                stack.pop()
                i += 1
                continue
            if top[2] or top[1] in "\"\u201d":     # a double-quote context: substitutions still open inside it
                if c == "$" and i + 1 < n and s[i + 1] in "({":
                    stack.append(("s", ")" if s[i + 1] == "(" else "}", True))
                    i += 2
                    continue
                if c == "`":
                    stack.append(("s", "`", True))
            i += 1
            continue
        if c in _BLANKS and top is None:
            return i
        if c == "\\":
            i += 2
            continue
        if c == "'" or c == '"' or c in _CURLY_CLOSE:
            ansi = c == "'" and i > v and s[i - 1] == "$"
            stack.append(("q", _CURLY_CLOSE.get(c, c), c != "'" or ansi))
        elif c == "$" and i + 1 < n and s[i + 1] in "({":
            stack.append(("s", ")" if s[i + 1] == "(" else "}", True))
            i += 2
            continue
        elif c == "`":
            if top is not None and top[1] == "`":
                stack.pop()
            else:
                stack.append(("s", "`", True))
        elif top is not None and c == top[1]:
            stack.pop()
        elif top is not None and top[1] == ")" and c == "(":
            stack.append(("s", ")", True))
        i += 1
    return n


def _spans(s: str) -> List[Tuple[int, int]]:
    n = len(s)
    sp: List[Tuple[int, int]] = []
    covered = 0                                          # tokens inside a span already taken are skipped: the scan stays linear
    seen: set = set()                                    # the command words met so far (lower case), for the -p rules
    sshpass_open = False                                 # True from the word sshpass until the command it wraps begins
    for m in _TOKEN.finditer(s):
        a, e = m.span()
        if a < covered:
            continue
        tok = m.group()
        low = tok.lower()
        lead = low.lstrip("-")
        for pref, minlen in _KEY_PREFIXES:
            if tok.startswith(pref) and len(tok) >= minlen:
                sp.append((a, e))
                break
        if tok[0] == "-" and tok[1:2] != "-" and len(tok) >= 2:
            # a -p GLUED to its value: mask the WHOLE shell word after the p, whatever its first character or quoting (the password is
            # never parsed; the word is masked). Unbalanced quotes mask to the end of the line. A short-option cluster that holds a p
            # (-vpS, -NBp'S') is read the same way after a mysql-family command; sshpass has only its own -p.
            if seen & _PASSWORD_P_CMDS:
                pi = _cluster_p_index(tok)
            else:
                pi = 1 if (sshpass_open and tok[1:2] == "p") else -1
            if pi != -1:
                if pi < len(tok) - 1:
                    sp.append((a + pi + 1, _shell_word_end(s, a + pi + 1)))
                elif e < n and s[e] not in _BLANKS:
                    sp.append((e, _shell_word_end(s, e)))
        keyish = any(w in lead for w in _SECRET_WORDS)
        header = lead in _HEADER_NAMES or lead.endswith(_HEADER_SUFFIXES)
        j = e
        if j < n and s[j] in "\"'":
            j += 1                                       # the closing quote of a JSON key
        k = j
        while k < n and s[k] in " \t":
            k += 1
        sep = s[k] if k < n else ""
        if (keyish or header) and sep in ("=", ":"):
            v = k + 1
            if v < n and s[v] == "=":
                v += 1
            while v < n and s[v] in " \t":
                v += 1
            if header and sep == ":":
                for sch in _SCHEMES:
                    if s[v:v + len(sch)].lower() == sch:
                        v += len(sch)
                        break
                end = v
                while end < n and s[end] not in "\"'":
                    end += 1
                sp.append((v, end))
            else:
                sp.append(_value_span(s, v))
        elif tok == "-p" and sshpass_open and j == e and k > e and k < n:
            sp.append((k, _shell_word_end(s, k)))        # sshpass's own -p VALUE: the next whole shell word
        elif tok in _SSHPASS_ARG_OPTS and sshpass_open and e < n and (s[e] not in _BLANKS or k < n):
            # -P prompt / -f file / -d fd, separate or glued (-f/tmp/pw, -P'a b'): its argument word is consumed, not masked, so the
            # scope stays open for the -p that follows
            covered = max(covered, _shell_word_end(s, e if s[e] not in _BLANKS else k))
        elif sshpass_open and len(tok) > 2 and tok[:2] in _SSHPASS_ARG_OPTS:
            pass                                         # -Pprompt, -ffile, -dN glued: an sshpass option, not a password flag
        elif tok.startswith("-") and k > e and k < n and s[k] != "-":
            # a flag followed by its value: --password VALUE, --token VALUE, curl -u user:pass, curl -b name=value
            vs = _value_span(s, k)
            val = s[vs[0]:vs[1]]
            if keyish or (lead in ("u", "user") and ":" in val) or (lead == "b" and "=" in val):
                sp.append(vs)
        seen.add(lead)
        if lead == "sshpass" and not tok.startswith("-"):
            sshpass_open = True
        elif sshpass_open and not tok.startswith("-"):
            sshpass_open = False                         # the wrapped command (ssh ...): a later -p is not sshpass's
        if sp:
            covered = max(covered, sp[-1][1])
    sp.extend(_query_spans(s))
    low_s = s.lower()
    pos = low_s.find("bearer")
    while pos != -1:
        k = pos + 6
        st = k
        while k < n and s[k] in " \t":
            k += 1
        if k > st:
            sp.append(_value_span(s, k))
        pos = low_s.find("bearer", pos + 6)
    pos = s.find("://")
    while pos != -1:
        st = pos + 3
        e = st
        while e < n and s[e] not in "/ \t\"'":
            e += 1
        at = s.find("@", st, e)
        if at != -1:
            colon = s.find(":", st, at)
            if colon != -1:
                sp.append((colon + 1, at))
        pos = s.find("://", pos + 3)
    for m in _RUN.finditer(s):
        run = m.group()
        if all(c in _HEX for c in run):
            sp.append(m.span())
            continue
        has_d = any(c.isdigit() for c in run)
        has_l = any(c.islower() for c in run)
        has_u = any(c.isupper() for c in run)
        slashy = "/" in run or "+" in run
        if (has_d and has_l and has_u) or (not slashy and has_d and (has_l or has_u)):
            sp.append(m.span())
    return sp


def mask_secrets(text: Any) -> str:
    """``text`` with secret-shaped text replaced by asterisks: the value after ``password`` / ``pass`` / ``secret`` / ``token`` /
    ``key`` / ``credential`` / ``auth`` / ``cookie`` words with ``=`` or ``:`` (env assignments such as ``DB_PASS=``, JSON
    ``"password": "x"``, ``password: x``), the value of an Authorization / Cookie / ``*-Token`` / ``*-Key`` / ``*-Secret`` header,
    ``--password VALUE`` style flags, ``curl -u user:pass`` and ``-b name=value``, the password in a URL, a ``Bearer`` token,
    known key prefixes (sk-, ghp_, github_pat_, xox*, AKIA, AIza, JWT ...), and runs of 32 or more characters of hex or base64
    (``/``, ``+``, ``=`` included; a run with ``/`` or ``+`` must mix upper, lower and digits so ordinary paths survive).
    Over-masking is the safe direction. Linear time; total: on any failure it shows nothing rather than something unmasked."""
    try:
        s = text if isinstance(text, str) else ("" if text is None else str(text))
        s = s[:MAX_MASK_INPUT]
        spans = sorted(x for x in _spans(s) if x[1] > x[0])
        out: List[str] = []
        pos = 0
        for a, e in spans:
            if e <= pos:
                continue
            a = max(a, pos)
            out.append(s[pos:a])
            out.append(MASK)
            pos = e
        out.append(s[pos:])
        return "".join(out)
    except Exception:  # noqa: BLE001
        return MASK


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(0, n - 1)].rstrip() + "…"


# --- reading the dialog out of the stripped screen tail ---------------------------------------------------------------
#
# Runs on the alert WORKER thread only. The input is bounded BEFORE any regex: at most SCAN_LINES lines, each cut to LINE_SCAN_CHARS.

SCAN_LINES = 60
LINE_SCAN_CHARS = 400
#: the only words of a dialog's title line that may be written to the session log (ruling: an allowlist, never screen text)
TITLE_WORDS = frozenset({"Bash", "Edit", "Write", "Create", "Fetch", "Read", "Delete", "Update", "Search", "Tool", "Web", "Notebook"})

_BOX_CHARS = "│┃┆┇┊┋║╭╮╰╯┌┐└┘├┤┬┴┼" \
             "─━═╔╗╚╝"
_BOX_TABLE = {ord(c): " " for c in _BOX_CHARS}
_TOP_BORDER = re.compile("[╭┌╔]")
_BOTTOM_BORDER = re.compile("[╰└╚]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_OPTION_RE = re.compile(r"^[❯▶►>*\s]{0,6}\d{1,2}[.)]\s")
_FIRST_OPTION_RE = re.compile(r"^[❯▶►>*\s]{0,6}1[.)]\s")
_COMMAND_TITLE_RE = re.compile(r"^([a-z]{1,12} )?command$", re.I)


class Dialog(NamedTuple):
    #: the question block as plain text (masked, bounded) WITHOUT the command line, or None when none was found. Alert/badge only:
    #: never written to the session log
    question: Optional[str]
    #: seconds left on a countdown line in the dialog, or None when none is shown
    seconds_left: Optional[int]
    #: what happens at zero (``deny``), or None
    action: Optional[str]
    #: the same block WITH the command line (masked, cut): for the alert only
    full: Optional[str] = None
    #: the first word of the title line when it is one of TITLE_WORDS, else None (the only screen text the log may carry)
    title_word: Optional[str] = None
    #: the length of the block in characters (a count, not text)
    length: int = 0


def _clean_line(raw: str) -> str:
    return _CTRL.sub("", raw.translate(_BOX_TABLE)).strip()


def read_dialog(plain_tail: str) -> Dialog:
    """The dialog the screen tail shows NOW. Bounded: only the last SCAN_LINES lines, each cut to LINE_SCAN_CHARS, are looked at.
    Total: never raises."""
    try:
        return _read_dialog(plain_tail if isinstance(plain_tail, str) else "")
    except Exception:  # noqa: BLE001
        return Dialog(None, None, None)


def _read_dialog(tail: str) -> Dialog:
    raw_lines: List[str] = [ln[:LINE_SCAN_CHARS] for ln in tail.splitlines()[-SCAN_LINES:]]
    cleaned = [_clean_line(r) for r in raw_lines]
    q: Optional[int] = None
    first_opts = [i for i, c in enumerate(cleaned) if _FIRST_OPTION_RE.match(c)]
    if first_opts:
        i = first_opts[-1] - 1
        looked = 0
        while i >= 0 and looked < 4:
            if cleaned[i]:
                looked += 1
                if cleaned[i].endswith("?"):
                    q = i
                    break
            i -= 1
    if q is None:
        for i in range(len(cleaned) - 1, -1, -1):
            if cleaned[i].endswith("?"):
                q = i
                break
    if q is None:
        return Dialog(None, *_countdown("".join(raw_lines[-8:])), None)
    seq: List[str] = [cleaned[q]]
    top = q
    j = q - 1
    steps = 0
    while j >= 0 and steps < 12:
        raw, c = raw_lines[j], cleaned[j]
        if _BOTTOM_BORDER.search(raw) or _OPTION_RE.match(c) or _is_footer(c) or _TOP_BORDER.search(raw):
            break
        seq.append(c)
        top = j
        steps += 1
        j -= 1
    lines = list(reversed(seq))
    out: List[Any] = []
    i = 0
    while i < len(lines):
        out.append((lines[i], False))
        if _COMMAND_TITLE_RE.match(lines[i]):
            k = i + 1
            while k < len(lines) and not lines[k]:
                k += 1
            if k < len(lines):
                out.append((lines[k], True))
                k += 1
                while k < len(lines) and lines[k]:
                    k += 1
            i = k
            continue
        i += 1
    kept = [(ln, is_cmd) for ln, is_cmd in out if ln][-BLOCK_MAX_LINES:]
    kept = [(_cut(mask_secrets(ln), LINE_MAX_CHARS), is_cmd) for ln, is_cmd in kept]
    full = _cut("\n".join(t for t, _c in kept), BLOCK_MAX_CHARS) if kept else None
    nocmd = [t for t, c in kept if not c]
    block = _cut("\n".join(nocmd), BLOCK_MAX_CHARS) if nocmd else None
    word = None
    if kept:
        first = kept[0][0].split(None, 1)
        if first and first[0] in TITLE_WORDS:
            word = first[0]
    secs, action = _countdown("".join(raw_lines[top:]))
    return Dialog(block, secs, action, full, word, len(full) if full else 0)


def read_countdown(plain_tail: str) -> Tuple[Optional[int], Optional[str]]:
    """``(seconds, action)`` from the last lines of the screen tail, or ``(None, None)``: no screen text is kept or returned.
    Bounded like :func:`read_dialog`. Total."""
    try:
        lines = [ln[:LINE_SCAN_CHARS] for ln in (plain_tail if isinstance(plain_tail, str) else "").splitlines()[-12:]]
        return _countdown("".join(lines))
    except Exception:  # noqa: BLE001
        return None, None


def _is_footer(c: str) -> bool:
    low = c.lower()
    return (low.startswith("esc ") and any(w in low for w in ("cancel", "exit", "dismiss"))) or low.startswith(
        ("(esc)", "enter to ", "press enter", "tab/arrow", "ctrl+"))


def _countdown(region: str) -> Tuple[Optional[int], Optional[str]]:
    """'automatically deny this request in M:SS' found by plain string search on the whitespace-free text (so redraws do not break
    it): no regex, linear."""
    flat = "".join(region.split())
    key = "thisrequestin"
    end = len(flat)
    for _ in range(6):
        idx = flat.rfind(key, 0, end)
        if idx == -1:
            return None, None
        end = idx
        before = flat[max(0, idx - 40):idx]
        p = before.rfind("automatically")
        word = before[p + 13:] if p != -1 else ""
        after = flat[idx + len(key): idx + len(key) + 12]
        m_end = 0
        while m_end < len(after) and after[m_end].isdigit():
            m_end += 1
        if p != -1 and word.isalpha() and 0 < m_end <= 4 and len(after) >= m_end + 3 and after[m_end] == ":" \
                and after[m_end + 1:m_end + 3].isdigit():
            return int(after[:m_end]) * 60 + int(after[m_end + 1:m_end + 3]), word.lower()
    return None, None


def fmt_clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


# --- the alert text ------------------------------------------------------------------------------------------------------

def alert_title(agent: str) -> str:
    return f"{agent or 'An agent'} is waiting on you"


def alert_body(kind: str, question: Optional[str], seconds_left: Optional[int], action: Optional[str], *,
               confidential: bool) -> str:
    """The alert body. Confidential: the kind only. Otherwise: the kind and the countdown on the first line (the macOS copy is
    cut short, so what matters most comes first), then the question block, or 'question text not captured'."""
    if confidential:
        return f"Waiting on {kind_label(kind)}."
    if seconds_left is None:
        head = f"Waiting on {kind_label(kind)}; {NO_COUNTDOWN}."
    else:
        head = (f"Waiting on {kind_label(kind)}; Claude Code will automatically {action or 'act on'} this request in "
                f"{fmt_clock(seconds_left)} ({seconds_left} s left).")
    return head + "\n" + (question if question else NOT_CAPTURED)


def closed_title(agent: str) -> str:
    return f"{agent or 'An agent'} is no longer waiting on you"


def closed_text(kind: str, outcome: str) -> str:
    """The text the stored alert is replaced with when the prompt closes: it carries NO command or question text."""
    out = outcome if outcome in OUTCOMES else "unknown"
    return f"The prompt ({kind_label(kind)}) closed: {out}."


# --- confidential workspaces ------------------------------------------------------------------------------------------------

#: the closed set of reasons ``read_confidential`` may give (and the only values ``confidential_why`` may hold in the session log)
CONFIDENTIAL_WHY = frozenset({"feature-inactive", "no-session", "no-workspace", "bundle-unreadable", "no-home-workspace",
                              "record-unreadable", "value-not-boolean", "flag-true", "flag-false", "unreadable", "check-raised",
                              "unknown"})


def read_confidential(session_dir: Optional[str]) -> Tuple[bool, str]:
    """``(confidential, why)`` for the session, read NOW (never cached). Ruling (product owner, 2026-10-27):

    * the workspace feature is INACTIVE (``workspace_scope_core`` OFF, or no relaunch bundle: no workspace is defined for this
      session): nothing can be marked confidential, so ``(False, why)`` and the alert shows the masked command line (D3);
    The ``why`` is ALWAYS a code from the closed set :data:`CONFIDENTIAL_WHY` (never workspace-derived text: it is written to the
    session log).

    * the session belongs to a DEFINED workspace: its ``confidential`` setting is read from the workspace record. A record or
      setting that cannot be read, or is not a boolean, answers ``(True, why)``: the safe direction, with the reason, so the caller
      can record it. A bundle that exists but is corrupt also counts as defined-and-unreadable.

    Never raises."""
    try:
        from .. import flags as _flags

        if not _flags.flag("workspace_scope_core"):
            return False, "feature-inactive"
        from ..workspace import bundle as _bundle
        from ..workspace import settings_registry as _sr
        from ..workspace.model import read_workspace_record
        from ..workspace.settings_resolve import SettingsResolver, layer_from_canonical

        if not session_dir:
            return False, "no-session"
        try:
            b = _bundle.read_bundle(session_dir)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", None) == "bundle-missing":
                return False, "no-workspace"
            return True, "bundle-unreadable"
        slug = str((b.home_workspace or {}).get("slug") or "")
        if not slug:
            return True, "no-home-workspace"
        reg = _sr.load()
        rec, reason = read_workspace_record(b.deploy_root, slug)
        if rec is None:
            return True, "record-unreadable"
        res = SettingsResolver(reg, workspace=layer_from_canonical(reg, "workspace", rec.get("settings") or {})).get("confidential")
        v = res.value
        if not isinstance(v, bool):
            return True, "value-not-boolean"
        return v, ("flag-true" if v else "flag-false")
    except Exception as exc:  # noqa: BLE001
        return True, "unreadable"


class WaitingHooks(NamedTuple):
    """What the harness hands the guard when the flag is ON. All three are called on the terminal-reading thread and must not
    block: ``on_open`` / ``on_close`` only hand work to the alert service's bounded queue."""
    on_open: Any        # (info: dict) -> None
    on_close: Any       # (info: dict) -> None
    confidential: Any   # () -> (bool, why)


#: no alert for the agent whose console the operator typed in within this many seconds (decision d2); the badge still shows
SUPPRESS_IF_TYPED_WITHIN_S = 300.0


def make_hooks(*, instance_id: Optional[str], session_dir: Optional[str], alerts_dir: Optional[str], record: Any,
               typed_here: Any = None, confidential: Any = None, submit: Any = None) -> WaitingHooks:
    """Wire the guard to the alert service for ONE session. ``alerts_dir`` is None when the alerts flag is OFF: the guard still
    records its events (the badge works) and nothing is delivered. ``record(what, **fields)`` writes a session-log record;
    ``typed_here() -> seconds | None`` is the operator-presence signal for THIS console. The guard calls ``on_open`` /
    ``on_close`` on the terminal-reading thread: they only put a job on the alert service's bounded queue
    (``submit_producer``: put without waiting; a full queue drops the alert and counts the drop); everything that can be slow
    (the presence check, building the text, the store write) runs inside the job on the worker thread."""
    from .. import alerts as _alerts
    from .operator_activity import display_name

    iid = str(instance_id or "unknown")
    store = _alerts.AlertStore(alerts_dir) if alerts_dir else None
    submit = submit or _alerts.submit_producer
    source = SOURCE_PREFIX + iid

    def _agent() -> str:
        try:
            return display_name(iid) or iid
        except Exception:  # noqa: BLE001
            return iid

    def _key(episode: Any) -> str:
        return f"{KEY_PREFIX}{iid}:{episode}"

    def on_open(info: Dict[str, Any]) -> None:
        """Called on the terminal-reading thread with ``info``: the episode, the dialog kind, the open time, ``screen`` (a bounded
        COPY of the stripped screen tail) and ``set_deadline``. It only hands a job to the bounded queue. EVERYTHING that reads
        the screen text (the confidential file read, the dialog reader, every masking pattern, the presence check, the text of the
        alert) runs inside the job, on the alert worker thread."""
        kind = info.get("dialog") or DIALOG_PERMISSION
        episode = info.get("episode")

        def build() -> Optional[Tuple[tuple, Dict[str, Any]]]:
            try:
                conf, why = (confidential or (lambda: read_confidential(session_dir)))()
            except Exception as exc:  # noqa: BLE001 - an unreadable setting is confidential
                conf, why = True, "check-raised"
            why = why if type(why) is str and why in CONFIDENTIAL_WHY else "unknown"      # total: a list or None is 'unknown'
            # a confidential session reads the countdown SECONDS only (a number, not screen text), so a timeout is still recorded
            # as timed out; its question and command are never read
            d = Dialog(None, *read_countdown(info.get("screen") or "")) if conf else read_dialog(info.get("screen") or "")
            # the session log is ALLOWLISTED: kind, the title's first word when it is one of TITLE_WORDS, the countdown seconds,
            # a length count. No screen text of any kind (not the question, not the command, not a masked form of either).
            record("screen-dialog-read", episode=episode, dialog=kind, confidential=bool(conf), confidential_why=why,
                   title_word=d.title_word, seconds_left=d.seconds_left,
                   countdown_action=("deny" if d.action == "deny" else (None if d.action is None else "other")), length=d.length)
            if d.seconds_left is not None and info.get("set_deadline") is not None:
                info["set_deadline"](episode, float(info.get("opened_ts") or time.time()) + d.seconds_left)
            if store is None:
                record("waiting-alert-not-delivered", episode=episode,
                       reason="the alerts flag is off: the badge and the session log still show the prompt")
                return None
            typed = typed_here() if typed_here is not None else None
            if typed is not None and typed <= SUPPRESS_IF_TYPED_WITHIN_S:
                record("waiting-alert-suppressed", episode=episode, seconds_since_typed=round(typed, 1),
                       reason="the operator typed in this console within the last 5 minutes; the badge still shows")
                return None
            body = alert_body(kind, d.full, d.seconds_left, d.action, confidential=bool(conf))
            return ((source, "warning", alert_title(_agent()), body, None),
                    {"key": _key(episode), "store": store, "macos_share": (KEY_PREFIX, MACOS_SHARE)})

        if submit("waiting", _build=build, store=store) is False:
            record("waiting-alert-dropped", episode=episode,
                   reason="the alert hand-off queue was full: this alert was dropped and counted; the badge shows the kind only")

    def on_close(info: Dict[str, Any]) -> None:
        if store is None:
            return
        kind, outcome = info.get("dialog") or DIALOG_PERMISSION, info.get("outcome") or "unknown"
        # the stored alert is REWRITTEN in place (the service's 'resolved' path, ``replace=True``): its command / question
        # text is gone and the column stops showing a prompt that is no longer open; no second alert, no notification.
        ok = submit("waiting", _build=lambda: ((source, "info", closed_title(_agent()), closed_text(kind, outcome), None),
                                               {"key": _key(info.get("episode")), "store": store, "replace": True, "macos": False}),
                    store=store)
        if ok is False:
            record("waiting-alert-close-dropped", episode=info.get("episode"),
                   reason="the alert hand-off queue was full: the stored alert keeps its text (its command line) until it ages out "
                          "of the column; a stated limit")

    return WaitingHooks(on_open, on_close, confidential or (lambda: read_confidential(session_dir)))


__all__ = ["DIALOG_KINDS", "DIALOG_PERMISSION", "WAITING_ALERT_KINDS", "Dialog", "read_dialog", "mask_secrets", "alert_title",
           "alert_body", "closed_title", "closed_text", "read_confidential", "WaitingHooks", "kind_label", "KIND_LABEL",
           "OUTCOMES", "NOT_CAPTURED", "NO_COUNTDOWN", "SOURCE_PREFIX", "KEY_PREFIX", "MACOS_SHARE"]
