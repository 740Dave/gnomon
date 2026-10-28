"""The leading-'!' rule (operator ruling 2026-10-02; X43 Diamond F3).

Claude Code runs a prompt whose first character is '!' as a shell command (bash mode),
outside the Bash tool and its PreToolUse deny layer. So the harness refuses to TYPE such a
payload for any source but the operator's own keystrokes.

This module is a dependency-free leaf (stdlib only, nothing at import time) so every layer can
import it: ``hooks`` (the gate decision), ``writer`` (the no-gate admit), ``compact_gate`` (the
self/hub screens), ``actuator`` and ``post_compact_boot`` (the boot-text validator).

UNCONDITIONAL: there is deliberately NO flag or config key for this rule. The machine config is
writable by the agent the rule constrains, so a switch there is a bypass. Reverting it takes a code
change and a Diamond.
"""

from __future__ import annotations

from typing import Any, List, Optional

#: Provenance ``reason`` the gate records on a refusal (also the refusal reason token).
REASON = "leading-bang"
GATE_REASON = ("leading-bang: a payload starting with '!' would run as a shell command in Claude "
               "Code's bash mode (pilot ruling dsd-1a2b3c4d5e3 D2)")


#: Marker in a PipelineResult trace: the refusal fired on the FINAL (post-transform) content.
POST_TRANSFORM_TRACE = "post-transform:leading-bang"
#: Rule citation carried by the inject-refused record (same text as compact_gate.BANG_RULE).
RULE = "pilot ruling dsd-1a2b3c4d5e3 D2 (2026-10-02): refuse a leading '!' from non-operator sources"


#: Reason token for the control-byte half of the union screen (same token the compact gate's
#: self/hub screens have always used).
REASON_CONTROL = "control-bytes"
CONTROL_GATE_REASON = ("control-bytes: a C0 control character other than tab/newline, DEL, or a C1 "
                       "control (U+0080-U+009F) "
                       "(ESC, bare CR, ^U, backspace, NUL, CSI, bracketed-paste markers) can rewrite the "
                       "prompt or end a paste early and start a new prompt line; a lone surrogate "
                       "(not encodable as UTF-8) is refused under the same token")
#: Bracketed-paste delimiters. Both contain ESC, so the control-byte rule already refuses them; they
#: are named here so the intent (an argument typed INSIDE a paste must not close it) is explicit.
PASTE_DELIMITERS = ("\x1b[200~", "\x1b[201~")


class ScreenRefused(ValueError):
    """Raised by the last-line writer guard when the union screen refuses a non-operator payload.
    ``reasons`` lists the tokens (``leading-bang`` / ``control-bytes``)."""

    def __init__(self, message: str, reasons: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.reasons: List[str] = list(reasons or [])


class LeadingBangRefused(ScreenRefused):
    """Raised by the last-line writer guard (and the no-gate raw ``inject``) when bytes that start
    with '!' would be typed for a non-operator source (a :class:`ScreenRefused` carrying
    ``leading-bang``)."""

    def __init__(self, message: str, reasons: Optional[List[str]] = None) -> None:
        super().__init__(message, reasons if reasons is not None else [REASON])


def has_leading_bang(text: Any) -> bool:
    r"""True when the first character of ``text`` that is neither whitespace nor an invisible
    format character (Unicode category Cf: U+200B-U+200F, U+202A-U+202E, U+2060-U+2064, U+FEFF,
    ...) is ``!`` or NFKC-folds to one (fullwidth U+FF01, small U+FE57).

    SCOPE: only the START of the payload. An inject is typed as ONE prompt: a multi-line text is a
    single bracketed paste (``injector.submit_writes``) and a bare CR is refused as a control byte,
    so no interior line is ever submitted as its own prompt line and only the prompt's first
    character selects a mode. Leading blank lines are whitespace here, so a payload of newline +
    ``!cmd`` IS refused."""
    if not isinstance(text, str):
        return False
    import unicodedata

    for c in text:
        if c.isspace() or unicodedata.category(c) == "Cf":
            continue
        return c == "!" or unicodedata.normalize("NFKC", c).startswith("!")
    return False


def has_control_bytes(text: Any) -> bool:
    r"""True when ``text`` holds a C0 control char other than ``\t`` / ``\n``, DEL, a C1 control
    (U+0080-U+009F; r2d: they are the 8-bit forms of ESC-introduced sequences, CSI U+009B, DCS U+0090,
    OSC U+009D, and a TUI that honours one can eat or rewrite what follows), a lone surrogate
    (U+D800-U+DFFF: a ``str`` that cannot be encoded as UTF-8, so the single write site would raise
    ``UnicodeEncodeError`` at the moment of typing; r2d), or a bracketed-paste delimiter. (This is the predicate the compact
    gate's self/hub screens have always used, moved into the leaf so the final-bytes screen cannot
    drift from it. The allowed set is exactly ``\t`` and ``\n``: a ``\n`` inside an injected text is
    always typed inside ONE bracketed paste (``injector.payload_phases``), so it never submits a
    line; a bare CR does.) Bytes that are decoded into text before they are typed
    (``writer.inject``'s raw bytes, a directive file, a control frame) reach this predicate as code
    points: a raw 0x9B byte surrogate-escapes to a lone surrogate, a UTF-8 C2 9B decodes to U+009B,
    and both are refused here."""
    if not isinstance(text, str):
        return False
    for ch in text:
        o = ord(ch)
        if (o < 0x20 and ch not in "\t\n") or 0x7F <= o <= 0x9F or 0xD800 <= o <= 0xDFFF:
            return True
    return any(d in text for d in PASTE_DELIMITERS)


def split_chunk_leads_with_bang(raw: bytes, size: int) -> bool:
    """True when, were ``raw`` (one UNBRACKETED single write) cut into ``size``-byte pieces by the
    PTY read split (r2d, r2c F3: macOS hands the reader at most 1022 bytes per ``read()`` and Claude
    Code keeps only the last unbracketed chunk), any piece after the first would START (past
    whitespace / invisible format characters) with a '!'. Control-free input is assumed (the union
    screen has already refused control bytes anywhere in the text). A piece cut mid-character begins
    with UTF-8 continuation bytes; those are dropped (``errors='ignore'``) so the first whole
    character decides, the conservative reading."""
    for start in range(size, len(raw), size):
        if has_leading_bang(raw[start:start + size].decode("utf-8", "ignore")):
            return True
    return False


def screen_text(text: Any) -> Optional[List[str]]:
    """THE shared screen (r2c; g71-amendment-r2b (1)): the UNION of every byte class a
    non-operator injection may not carry, applied to the early check, the gate's post-pipeline
    re-check, ``approve()`` and the writer funnel. Reasons, or ``None`` when the text may pass:

    * ``control-bytes`` -- :func:`has_control_bytes` (C0, DEL, C1, lone surrogates, paste delimiters);
    * ``leading-bang``  -- :func:`has_leading_bang`.

    Stateless, no flag, no config."""
    reasons: List[str] = []
    if has_control_bytes(text):
        reasons.append(REASON_CONTROL)
    if has_leading_bang(text):
        reasons.append(REASON)
    return reasons or None


def screen_event(content: Any, meta: Any = None) -> Optional[List[str]]:
    """:func:`screen_text` on an event's content, plus the control-byte rule on the text typed
    INSIDE the compact paste (``meta['compact_argument']``, which ``Injector._emit_compact`` types
    from meta, not from ``content``, so a hook that rewrote only one of the two is still caught)."""
    reasons = list(screen_text(content) or [])
    if isinstance(meta, dict):
        arg = meta.get("compact_argument")
        if arg and REASON_CONTROL not in reasons and has_control_bytes(str(arg)):
            reasons.append(REASON_CONTROL)
    return reasons or None


def gate_reason(reasons: Optional[List[str]]) -> str:
    """Human text for a refusal's reasons."""
    parts = []
    if REASON_CONTROL in (reasons or []):
        parts.append(CONTROL_GATE_REASON)
    if REASON in (reasons or []) or not parts:
        parts.append(GATE_REASON)
    return "; ".join(parts)


def refusal_for(reasons: Optional[List[str]]) -> ScreenRefused:
    """The exception the writer backstop raises: LeadingBangRefused when a bang is among the
    reasons (the round-2b contract), else the base ScreenRefused."""
    reasons = list(reasons or [])
    if REASON in reasons:
        return LeadingBangRefused(gate_reason(reasons), reasons)
    return ScreenRefused(gate_reason(reasons), reasons)
