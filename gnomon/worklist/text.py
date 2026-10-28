"""Small text rules shared by the list: control characters are stripped (g71), never trusted."""

from __future__ import annotations


#: line/paragraph separators and the invisible format characters that can reorder or hide text (bidi controls, zero-width, BOM, tag chars).
#: ZWJ (U+200D) is kept so emoji sequences survive.
_DROP = frozenset([0x2028, 0x2029, 0x200B, 0x200C, 0x200E, 0x200F, 0xFEFF] + list(range(0x202A, 0x202F)) + list(range(0x2060, 0x2065))
                  + list(range(0x2066, 0x206A)) + list(range(0xE0000, 0xE0080)))


def clean_text(s: str) -> str:
    """A run of newline / CR / tab becomes ONE space; every other control character (C0, DEL, C1, U+2028/2029) is dropped; ends trimmed."""
    out = []
    gap = False
    for c in s:
        o = ord(c)
        if c in "\n\r\t":
            if not gap:
                out.append(" ")
            gap = True
            continue
        gap = False
        if o < 32 or o == 127 or 0x80 <= o <= 0x9F or o in _DROP:
            continue
        out.append(c)
    return "".join(out).strip()
