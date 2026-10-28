"""Pattern redaction of secret-shaped strings in an app's log tail (g73-amendment-4 rule 5; round-2 R2-F6).

The host returns the last lines of an app's log in a status for a failed app. Round 2 redacted only the CURRENT
ui_token (read from a file that is deleted once the app stops) and the ``gnomon_ui_token=`` URL form, so the second
poll of a stopped app returned the bare token. Redaction is now by SHAPE, applied on EVERY poll and independent of
any token file: it never needs to know a secret to hide it.

What it redacts (each replaced by ``<redacted>``):
  * ``gnomon_ui_token=...`` (the Open URL form) and any ``<name>=value`` / ``<name>: value`` where the name contains
    token, secret, password, passwd, api_key, apikey, authorization, bearer or credential;
  * the JSON form ``"password": "x"`` (the name's closing quote before the colon is allowed); a QUOTED value is replaced
    whole, to its closing quote or, when the quote is never closed (a line cut mid-quote), to the end of the line (R3D-F4);
  * ``Authorization: Bearer ...`` / ``Basic ...`` values;
  * JSON Web Tokens (three dot-separated base64url parts, the first starting ``eyJ``);
  * well-known prefixed keys: ``sk-``, ``ghp_``/``gho_``/``ghs_``/``github_pat_``, ``xox[bpas]-``, ``AKIA`` + 16;
  * any run of 40 or more URL-safe characters (the host mints ``secrets.token_urlsafe(32)`` = 43 of A-Za-z0-9_-; a
    token with characters glued on either side is still caught) and any run of 32 or more hex digits.
    A line longer than MAX_LINE_CHARS is cut (round 3c: the match cost is bounded). Round 3d: a cut that lands inside a
    word also replaces that trailing partial word, so what the cut leaves behind cannot be a prefix of a secret.
  Not covered, stated: a token re-encoded (base64 of the token), split across lines, or with separators inserted.

Tradeoff, stated: a 40+ character identifier or a long hex digest in a log is hidden too (a false positive costs a
readable line; a false negative costs a secret).
"""

from __future__ import annotations

import re
from typing import Iterable, List

REDACTED = "<redacted>"

#: ROUND 3c (R3-F1): every pattern here is LINEAR in the line length. No leading unbounded quantifier retried from each
#: start position (the old ``[\w.-]*`` before the keyword was quadratic, cubic for 'token'*N); every quantifier that can
#: be retried is bounded; a start anchor stops a match restarting inside a run; and a line is CAPPED before matching
#: (:data:`MAX_LINE_CHARS`) so the worst case is bounded by a constant, with no reliance on a timeout. The keyword is
#: found by itself (the name text before it is not part of what is replaced), then at most 64 name characters, then the
#: separator.
MAX_LINE_CHARS = 2048
TRUNCATED_MARK = "<line truncated>"

_NAMED = re.compile(
    r"(?i)((?:token|secret|passw(?:or)?d|passwd|api[_-]?key|apikey|authorization|bearer|credential)[\w.-]{0,64}"
    r"[\"']?[ \t\r\f\v]{0,64}(?:=|%3[Dd]|:)[ \t\r\f\v]{0,64})(?:Bearer[ \t]+|Basic[ \t]+)?"
    r"(\"(?:[^\"\\]|\\.)*\"?|'(?:[^'\\]|\\.)*'?|[^\s&,;\"']+)")
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)[ \t]{1,64}[A-Za-z0-9._~+/=-]{8,}")
#: anchored at the start of a url-safe run (the char before must not be one), so ``eyJ-eyJ-eyJ-...`` does not restart a
#: scan of the whole run at every boundary; a JWT's signature part is 40+ url-safe characters for HS256 and up, which
#: ``_TOKEN43`` also catches wherever the JWT sits
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_PREFIXED = re.compile(
    r"(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[bpas]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})")
#: any run of 40+ url-safe characters: the minted token is 43, and a run LONGER than that (the token with characters
#: glued on either side) must not escape just because it is no longer exactly 43
_TOKEN43 = re.compile(r"[A-Za-z0-9_-]{40,}")
_HEX32 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])")


def redact_line(line: str) -> str:
    """Redact one log line. Linear in ``min(len(line), MAX_LINE_CHARS)``. A longer line is CUT (what is dropped can
    leak nothing), with a visible mark."""
    if not line:
        return line
    cut = len(line) > MAX_LINE_CHARS
    if cut:
        # R3C-F3: the cut can land inside a secret-shaped run; the surviving prefix would be shorter than every shape
        # rule above. When the cut lands mid-word (the next character is not whitespace) the trailing whitespace-free run
        # is replaced, whole, BEFORE the patterns run: a partial word at the cut is never shown.
        partial = not line[MAX_LINE_CHARS].isspace()
        line = line[:MAX_LINE_CHARS]
        if partial:
            k = len(line)
            while k > 0 and not line[k - 1].isspace():            # linear: the start of the trailing word
                k -= 1
            line = line[:k] + REDACTED
    line = _NAMED.sub(lambda m: m.group(1) + REDACTED, line)
    line = _BEARER.sub(lambda m: m.group(1) + " " + REDACTED, line)
    for rx in (_JWT, _PREFIXED, _TOKEN43, _HEX32):
        line = rx.sub(REDACTED, line)
    return line + (TRUNCATED_MARK if cut else "")


def redact_lines(lines: Iterable[str]) -> List[str]:
    return [redact_line(l) for l in lines]
