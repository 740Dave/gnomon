"""Gnomon WEB port — slice 2 DSD frontend assertions.

Guards the DSD tab's presence + look + wiring as a REGRESSION check (the whole-file
no-monospace scan in test_web_design.py already covers the DSD CSS/HTML additions;
these are DSD-specific):

* the DSD dual-pane panel exists in the SPA shell and the DSD tab is now LIVE
  (no "soon" stub);
* the DSD comment ``textarea`` inherits the sans font (NO monospace, even for a
 user-text field — the exact thing the design forbids and the CTk build used
  Courier for);
* the thin client is wired to the reused-controller JSON seam (present / submit /
  list / browse / arrivals) and carries the auto-switch poll.
"""

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "web", "static")
_CSS = os.path.join(_STATIC, "styles.css")
_HTML = os.path.join(_STATIC, "index.html")
_JS = os.path.join(_STATIC, "app.js")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _strip_css_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


# --- the DSD panel exists and the tab is live --------------------------------

def test_dsd_panel_is_a_dual_pane_not_a_stub():
    html = _read(_HTML)
    assert 'id="panel-dsd"' in html
    assert "gm-dsd-left" in html and "gm-dsd-right" in html
    assert 'id="dsd-list"' in html and 'id="dsd-detail"' in html


def test_dsd_tab_no_longer_marked_soon():
    html = _read(_HTML)
    m = re.search(r'data-tab="dsd">(.*?)</button>', html, flags=re.DOTALL)
    assert m, "DSD tab button not found"
    assert "soon" not in m.group(1).lower()


# --- no monospace, even for the comment textarea -----------------------------

def test_dsd_comment_textarea_inherits_font():
    body = _strip_css_comments(_read(_CSS))
    m = re.search(r"\.gm-dsd-comment\s*\{[^}]*\}", body, flags=re.DOTALL)
    assert m, "no .gm-dsd-comment rule found"
    assert re.search(r"font-family\s*:\s*inherit", m.group(0)), \
        ".gm-dsd-comment must set font-family: inherit (no monospace)"


# --- wired to the reused-controller JSON seam --------------------------------

def test_js_calls_the_dsd_api_seam():
    js = _read(_JS)
    for path in ("/api/dsd/list", "/api/dsd/present/", "/api/dsd/submit/",
                 "/api/dsd/browse/", "/api/dsd/arrivals", "/api/dsd/status"):
        assert path in js, f"frontend does not call {path}"


def test_js_carries_the_auto_switch_poll():
    js = _read(_JS)
    assert "pollDsdArrivals" in js
    assert 'activateTab("dsd")' in js   # arrival focuses the DSD tab
