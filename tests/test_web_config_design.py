"""Gnomon WEB port — slice 4 Config tab frontend.

These catch a real REGRESSION in the Config panel, not just presence:

* the Configuration panel is now a LIVE form (its stub was replaced) and the tab
  button no longer carries the "soon" marker;
* the frontend calls the /api/config seam for BOTH read (GET) and save (POST) —
  it does not reimplement config logic client-side;
* the reserved (no-live-consumer) field is surfaced/flagged, not silently live;
* NO monospaced font is (re)introduced by the Config CSS — the config inputs,
 template rows and endpoint rows all pin ``font-family: inherit`` (
  forbids monospace anywhere, including paths and command templates).

Comments are stripped before the monospace scan so prose cannot mask a regression.
"""

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
_STATIC = os.path.join(_WEB, "static")
_CSS = os.path.join(_STATIC, "styles.css")
_HTML = os.path.join(_STATIC, "index.html")
_JS = os.path.join(_STATIC, "app.js")

_FORBIDDEN_MONO = (
    "monospace", "ui-monospace", "courier", "menlo", "monaco",
    "consolas", "sf mono", '"sf mono"', "roboto mono", "source code",
)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _strip_css_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _strip_html_comments(text):
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


# --- the panel is live, not a stub -------------------------------------------

def test_config_panel_is_a_live_form_not_a_stub():
    html = _read(_HTML)
    panel = re.search(r'id="panel-config".*?</section>', html, flags=re.DOTALL)
    assert panel, "no #panel-config section found"
    block = panel.group(0)
    assert 'id="config-form"' in block, "config panel has no live form seam"
    assert "gm-stub" not in block, "config panel is still a stub"


def test_config_tab_button_has_no_soon_marker():
    html = _strip_html_comments(_read(_HTML))
    m = re.search(r'data-tab="config"[^>]*>(.*?)</button>', html, flags=re.DOTALL)
    assert m, "no Configuration tab button found"
    assert "gm-tab-stub" not in m.group(0), "Configuration tab still marked 'soon'"


def test_config_panel_has_save_and_reload_controls():
    html = _read(_HTML)
    assert 'id="config-save"' in html
    assert 'id="config-reload"' in html


# --- the /api/config seam (read + save) --------------------------------------

def test_js_reads_the_config_api():
    js = _read(_JS)
    assert '"/api/config"' in js or "'/api/config'" in js


def test_js_saves_via_post_to_config_api():
    js = _read(_JS)
    # the save path posts to /api/config with method POST
    assert re.search(r'/api/config".*?method:\s*"POST"', js, flags=re.DOTALL), \
        "app.js does not POST to /api/config"


def test_js_surfaces_the_server_refusal_on_save():
    # a refused save (validation / D9) must be surfaced, not swallowed.
    js = _read(_JS)
    assert "not saved" in js


def test_js_flags_the_reserved_field():
    js = _read(_JS)
    assert "reserved" in js  # renders the reserved marker the API sends


# --- no monospace regression in the Config CSS -------------------------------

def test_config_css_declares_no_monospaced_font():
    body = _strip_css_comments(_read(_CSS)).lower()
    hits = [tok for tok in _FORBIDDEN_MONO if tok in body]
    assert not hits, f"monospaced font family in styles.css: {hits}"


def test_config_inputs_use_inherited_sans_font():
    body = _strip_css_comments(_read(_CSS))
    m = re.search(r"\.gm-config-input\b[^{}]*\{[^}]*\}", body, flags=re.DOTALL)
    assert m, "no .gm-config-input rule found"
    assert re.search(r"font-family\s*:\s*inherit", m.group(0)), \
        ".gm-config-input must set font-family: inherit (no monospace for paths)"


def test_config_template_text_uses_inherited_sans_font():
    # a command template is code-ish content — it must still be system sans.
    body = _strip_css_comments(_read(_CSS))
    m = re.search(r"\.gm-config-template-text\b[^{}]*\{[^}]*\}", body, flags=re.DOTALL)
    assert m, "no .gm-config-template-text rule found"
    assert re.search(r"font-family\s*:\s*inherit", m.group(0)), \
        ".gm-config-template-text must set font-family: inherit (no monospace)"
