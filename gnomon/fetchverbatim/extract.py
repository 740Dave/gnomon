"""Deterministic body decoding, HTML-to-markdown conversion and section narrowing. NO model is in this path:
the same bytes always produce the same text, byte for byte (stdlib ``html.parser`` only).

HTML conversion rules (the whole contract; the golden test pins the output):

* ``script``, ``style``, ``template``, ``noscript``, ``head``, ``svg``, ``iframe``, ``object``, ``embed``, ``canvas``,
  comments and processing instructions are dropped. The ONLY title taken is the FIRST ``<title>`` that is a direct
  child of ``<head>`` (itself at the document root or directly under ``<html>``), emitted as a first ``# `` line when
  non-empty; every other ``<title>`` (in a body, in noscript/template/iframe/..., a second one in head) is dropped
  with its text and never concatenated with the real one;
* elements marked ``hidden`` or ``aria-hidden="true"`` or whose inline style FINALLY sets ``display:none`` or
  ``visibility:hidden|collapse`` (``!important`` and spacing/case variants included) are dropped WITH their subtree and
  COUNTED in ``omitted_hidden_elements`` (a page can hide text from a human reader; the result says how many such
  elements were left out, never silently). RESIDUAL: hiding by a stylesheet CLASS, ``opacity:0``, ``font-size:0`` or
  off-screen positioning is NOT detected (no CSS engine);
* conversion is LINEAR in the input (no per-tag stack rescans, no repeated string concatenation) and can be given a
  deadline (:func:`html_to_markdown_bounded`), in which case it stops and says so instead of overrunning;
* ``h1``-``h6`` -> ATX headings; ``p``/``div``/``section`` etc. -> paragraphs separated by one blank line;
  ``ul``/``ol`` items -> ``- `` / ``1. ``; ``pre`` -> fenced block, whitespace kept; ``blockquote`` -> ``> ``;
  ``br`` -> newline; table rows -> cells joined by `` | ``; links -> ``[text](href)`` only for http(s)/relative/mailto
  hrefs (any other scheme, e.g. javascript:, keeps the text and drops the target); images -> ``[image: alt]``;
* character references are decoded; whitespace outside ``pre`` collapses to single spaces.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Tuple

_SKIP = {"script", "style", "template", "noscript", "head", "svg", "iframe", "object", "embed", "canvas"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "form", "fieldset", "figure",
          "figcaption", "address", "details", "summary", "dl", "dt", "dd", "table", "tr", "caption", "hr", "body", "html"}
#: start tags that may appear inside <head> (HTML5 "in head"); any OTHER start tag, or non-whitespace text, ends the head
#: implicitly (``</head>`` and ``<body>`` are optional in HTML5).
_HEAD_OK = {"base", "basefont", "bgsound", "link", "meta", "noframes", "script", "style", "template", "title", "noscript",
            "head", "html"}
_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_IMPORTANT = re.compile(r"\s*!\s*important\s*$", re.I)
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.S)
_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|([^\n\r\f0-9a-fA-F]))")


def _css_unescape(text: str) -> str:
    """Decode CSS escapes (``\\70`` -> ``p``; ``\\x`` -> ``x``). A trailing ``\\9`` yields a TAB that stays in the
    identifier (``none\\9`` is not ``none``: that old hack is invalid in current browsers)."""
    def sub(m):
        if m.group(1):
            cp = int(m.group(1), 16)
            return chr(cp) if 0 < cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF else "\ufffd"
        return m.group(2)
    return _CSS_ESCAPE.sub(sub, text)


def _style_hides(style: str) -> bool:
    """Does an inline style FINALLY set display:none or visibility:hidden|collapse? (last declaration wins).
    CSS comments are removed first and CSS escapes are decoded AFTER trimming, as a browser tokenises them."""
    display = visibility = ""
    for decl in _CSS_COMMENT.sub("", style).split(";"):
        prop, sep, val = decl.partition(":")
        if not sep:
            continue
        prop = _css_unescape(prop.strip()).lower()
        val = _css_unescape(_IMPORTANT.sub("", val.strip())).lower()
        if prop == "display":
            display = val
        elif prop == "visibility":
            visibility = val
    return display == "none" or visibility in ("hidden", "collapse")


_SAFE_HREF = re.compile(r"^(https?:|mailto:|//|/|\.|#|\?|[A-Za-z0-9_~%-][^:]*$)", re.I)


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[Tuple[str, str]] = []   # (kind, text); kind in {'li','block','pre'}
        self.cur: List[str] = []
        self.stack: List[str] = []
        self.hidden_from: Optional[int] = None
        self.skip_from: Optional[int] = None
        self.omitted_hidden = 0
        self.title_parts: List[str] = []
        self.in_title = False
        self.title_state = 0             # 0: none taken yet, 1: reading the taken title, 2: done
        self.pos: Dict[str, List[int]] = {}   # tag -> stack indices of its open occurrences (O(1) end-tag lookup)
        self.heading: Optional[int] = None
        self.pre = 0
        self.quote = 0
        self.lists: List[List] = []      # [ordered?, counter]
        self.links: List[Optional[str]] = []
        self.in_cell = 0
        self.li_open = False

    # --- block assembly ---
    def _flush(self) -> None:
        text = "".join(self.cur)
        self.cur = []
        if self.pre:
            body = text.strip("\n")
        else:
            body = re.sub(r"[ \t\r\n\f]+", " ", text).strip()
        if not body:
            return
        kind = "block"
        if self.pre:
            body = "```\n" + body + "\n```"
            kind = "pre"
        elif self.heading:
            body = "#" * self.heading + " " + body
        if self.quote and not self.pre:
            body = "\n".join("> " + ln for ln in body.split("\n"))
        if kind == "block" and self.li_open:
            kind = "li"
        self.blocks.append((kind, body))

    def _suppressed(self) -> bool:
        return self.hidden_from is not None or self.skip_from is not None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        a: Dict[str, Optional[str]] = {k.lower(): v for k, v in attrs}
        if self.stack and self.stack[-1] == "head" and tag not in _HEAD_OK:
            self.handle_endtag("head")        # HTML5 implicit head end: body content has started (only when the head is the innermost open element: a start
            # tag inside <head><noscript>/<template>/... stays inside that non-rendered container)
        if tag in _VOID:
            if not self._suppressed():
                self._void(tag, a)
            return
        idx = len(self.stack)
        self.stack.append(tag)
        self.pos.setdefault(tag, []).append(idx)
        if tag == "title":
            under_head = idx >= 1 and self.stack[idx - 1] == "head" and (idx == 1 or (idx == 2 and self.stack[0] == "html"))
            if under_head and self.title_state == 0 and self.hidden_from is None:
                self.title_state = 1
                self.in_title = True   # the first <title> directly under <head> (head is otherwise skipped)
            elif not self._suppressed():
                self.skip_from = idx   # any other <title> is not rendered page text: drop it and its text
            return
        if self._suppressed():
            return
        if tag in _SKIP:
            self.skip_from = len(self.stack) - 1
            return
        if ("hidden" in a or (a.get("aria-hidden") or "").strip().lower() == "true"
                or _style_hides(a.get("style") or "")):
            self.hidden_from = len(self.stack) - 1
            self.omitted_hidden += 1
            return
        if tag in _HEADINGS:
            self._flush(); self.heading = _HEADINGS[tag]
        elif tag == "pre":
            self._flush(); self.pre += 1
        elif tag == "blockquote":
            self._flush(); self.quote += 1
        elif tag in ("ul", "ol"):
            self._flush(); self.lists.append([tag == "ol", 0])
        elif tag == "li":
            self._flush()
            self.li_open = True
            if self.lists:
                self.lists[-1][1] += 1
                self.cur.append(f"{self.lists[-1][1]}. " if self.lists[-1][0] else "- ")
            else:
                self.cur.append("- ")
        elif tag in ("td", "th"):
            if self.in_cell:
                self.cur.append(" | ")
            self.in_cell += 1
        elif tag == "a":
            href = (a.get("href") or "").strip()
            if href and _SAFE_HREF.match(href):
                self.links.append(href); self.cur.append("[")
            else:
                self.links.append(None)
        elif tag in _BLOCK:
            self._flush()

    def _void(self, tag: str, a: Dict[str, Optional[str]]) -> None:
        if tag == "br":
            self.cur.append("\n" if self.pre else "\u0000BR\u0000")
        elif tag == "hr":
            self._flush()
        elif tag == "img":
            alt = (a.get("alt") or "").strip()
            if alt:
                self.cur.append(f"[image: {alt}]")

    def handle_endtag(self, tag: str) -> None:
        opened = self.pos.get(tag)
        if tag in _VOID or not opened:
            return
        if tag == "title" and self.in_title:
            self.in_title = False
            self.title_state = 2
        idx = opened[-1]
        closing = self.stack[idx:]
        del self.stack[idx:]
        for t in reversed(closing):          # every element is pushed once and popped once: amortised O(1) per tag
            self.pos[t].pop()
        if self.skip_from is not None:
            if idx > self.skip_from:
                return
            self.skip_from = None
            return
        if self.hidden_from is not None:
            if idx > self.hidden_from:
                return
            self.hidden_from = None
            return
        for t in reversed(closing):
            self._close(t)

    def _close(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag in _HEADINGS:
            self._flush(); self.heading = None
        elif tag == "pre":
            self._flush(); self.pre = max(0, self.pre - 1)
        elif tag == "blockquote":
            self._flush(); self.quote = max(0, self.quote - 1)
        elif tag in ("ul", "ol"):
            self._flush()
            if self.lists:
                self.lists.pop()
            if self.blocks and self.blocks[-1][0] == "li":
                self.blocks[-1] = ("li-end", self.blocks[-1][1])   # a list boundary: the next block is a new paragraph
        elif tag == "li":
            self._flush(); self.li_open = False
        elif tag in ("td", "th"):
            pass
        elif tag == "tr":
            self.in_cell = 0; self._flush()
        elif tag == "a":
            if self.links:
                href = self.links.pop()
                if href is not None:
                    self.cur.append(f"]({href})")
        elif tag in _BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
            return
        if data.strip() and self.stack and self.stack[-1] == "head":
            self.handle_endtag("head")        # non-whitespace text directly in <head> starts the body
        if self._suppressed():
            return
        self.cur.append(data)

    def handle_comment(self, data: str) -> None:  # dropped
        return

    def close(self) -> None:
        super().close()
        self._flush()


@dataclass
class ConvertResult:
    markdown: str
    omitted_hidden: int
    complete: bool          # False: the deadline stopped the conversion; ``markdown`` is the converted prefix
    consumed_chars: int     # characters of the input that were fed to the parser
    reason: Optional[str] = None   # why it is incomplete (None when complete)


def assemble_parts(title_text: str, blocks: List[Tuple[str, str]], omitted: int) -> Tuple[str, int]:
    """Join ``(kind, text)`` blocks (and an optional title) into the final markdown. Linear: one list join."""
    parts: List[str] = []
    prev = ""
    title = re.sub(r"\s+", " ", title_text).strip()
    if title:
        parts.append("# " + title)
        prev = "block"
    for kind, b in blocks:
        if kind != "pre":
            b = re.sub(r" *\n *", "\n", b.replace("\u0000BR\u0000", "\n"))
        if prev:
            parts.append("\n" if (kind in ("li", "li-end") and prev == "li") else "\n\n")
        parts.append(b)
        prev = kind
    out = "".join(parts)
    return out + ("\n" if out else ""), omitted


def _assemble(conv: _Converter) -> Tuple[str, int]:
    return assemble_parts("".join(conv.title_parts), conv.blocks, conv.omitted_hidden)


def html_to_markdown_bounded(html: str, deadline: float, *, chunk: int = 32768,
                             clock: Callable[[], float] = time.monotonic) -> ConvertResult:
    """Convert in chunks, checking ``clock()`` against ``deadline`` before each chunk. Returns the converted
    prefix with ``complete=False`` when the deadline hits (the caller states the truncation); never overruns by more
    than one chunk's work."""
    conv = _Converter()
    i, n = 0, len(html)
    complete = True
    while i < n:
        if clock() >= deadline:
            complete = False
            break
        conv.feed(html[i:i + chunk])
        i += chunk
    if complete:
        conv.close()
    else:
        conv._flush()
    md, omitted = _assemble(conv)
    return ConvertResult(md, omitted, complete, min(i, n))


def html_to_markdown(html: str) -> Tuple[str, int]:
    """``(markdown, omitted_hidden_elements)``: the unbounded form (one feed)."""
    conv = _Converter()
    conv.feed(html)
    conv.close()
    return _assemble(conv)


# ---------------------------------------------------------------- the isolated child ----

def _child_main(argv: List[str]) -> int:
    """Entry point of the conversion CHILD PROCESS (see ``convert_proc.py``): reads the page text (UTF-8) on stdin,
    converts it in chunks and streams JSON-line frames on stdout, flushing after each, so that a parent which has to
    KILL this process (html.parser can spend unbounded time inside one ``feed()``) still holds everything finished so far.
    Frames: ``{"b": [kind, text], "p": chars_fed}`` for each FINISHED block (the newest block is withheld until a later
    one exists because a list end can still re-label it), ``{"t": title}`` once the title is closed, and a final
    ``{"done": true, "omitted": n, "consumed": n}``. A missing ``done`` frame means the conversion did NOT complete."""
    import json
    import sys
    try:
        import resource
        cpu = int(float(argv[0])) + 2
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))     # belt: the kernel kills a runaway child even if the parent is gone
    except Exception:  # noqa: BLE001
        pass
    data = sys.stdin.buffer.read().decode("utf-8", errors="surrogatepass")
    out = sys.stdout.buffer

    def emit(obj) -> None:
        out.write(json.dumps(obj, ensure_ascii=True).encode("ascii") + b"\n")
        out.flush()

    conv = _Converter()
    sent, title_sent, i, n, chunk = 0, False, 0, len(data), 65536
    while i < n:
        conv.feed(data[i:i + chunk])
        i += chunk
        while sent < len(conv.blocks) - 1:
            emit({"b": list(conv.blocks[sent]), "p": min(i, n)})
            sent += 1
        if conv.title_state == 2 and not title_sent:
            emit({"t": "".join(conv.title_parts)})
            title_sent = True
    conv.close()
    while sent < len(conv.blocks):
        emit({"b": list(conv.blocks[sent]), "p": n})
        sent += 1
    emit({"done": True, "omitted": conv.omitted_hidden, "consumed": n, "title": "".join(conv.title_parts)})
    return 0


# ---------------------------------------------------------------- decoding ----

_CHARSET_PARAM = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9_.:-]+)", re.I)
_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", re.I)


def decode_body(body: bytes, content_type: str) -> Tuple[str, str, bool]:
    """``(text, charset_used, had_replacements)``. Order: BOM, Content-Type charset, <meta charset>, utf-8.
    Undecodable bytes become U+FFFD and ``had_replacements`` says so (never silent)."""
    import codecs
    if body.startswith(codecs.BOM_UTF8):
        return body[3:].decode("utf-8", errors="replace"), "utf-8", False
    candidates: List[str] = []
    m = _CHARSET_PARAM.search(content_type or "")
    if m:
        candidates.append(m.group(1))
    m = _META_CHARSET.search(body[:2048])
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for name in candidates:
        try:
            codecs.lookup(name)
        except LookupError:
            continue
        text = body.decode(name, errors="replace")
        return text, name.lower(), "�" in text and "�".encode(name, "ignore") not in body
    return body.decode("utf-8", errors="replace"), "utf-8", True  # unreachable (utf-8 is always found)


# ---------------------------------------------------------------- section narrowing ----

_ATX = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def narrow_to_section(markdown: str, hint: str) -> Tuple[str, Dict[str, object]]:
    """Deterministic heading match: the first ATX heading whose normalised text EQUALS the normalised hint, else
    the first heading that CONTAINS it. The section runs from that heading to the next heading of the same or a
    higher level. No match returns the text UNCHANGED with ``matched: false`` (the caller sees it was not narrowed)."""
    info: Dict[str, object] = {"requested": hint, "matched": False, "heading": None, "match_kind": None}
    want = _norm(hint)
    if not want:
        return markdown, info
    lines = markdown.split("\n")
    heads: List[Tuple[int, int, str]] = []   # (line index, level, text)
    in_fence = False
    for i, ln in enumerate(lines):
        if ln.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _ATX.match(ln)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    pick = next((h for h in heads if _norm(h[2]) == want), None)
    kind = "exact"
    if pick is None:
        pick = next((h for h in heads if want in _norm(h[2])), None)
        kind = "contains"
    if pick is None:
        return markdown, info
    start, level, text = pick
    end = len(lines)
    for i, lv, _ in heads:
        if i > start and lv <= level:
            end = i
            break
    info.update(matched=True, heading=text, match_kind=kind)
    return "\n".join(lines[start:end]).rstrip("\n") + "\n", info
