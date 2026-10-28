"""robots.txt evaluation and the deterministic HTML conversion."""

from gnomon.fetchverbatim import extract, robots

TOKEN = "fetch-verbatim"


def test_robots_basic_disallow_names_the_rule():
    d = robots.evaluate("User-agent: *\nDisallow: /private\n", TOKEN, "/private/x")
    assert d.allowed is False and d.rule == "Disallow: /private" and d.group == "*"
    assert robots.evaluate("User-agent: *\nDisallow: /private\n", TOKEN, "/public").allowed is True


def test_robots_longest_match_and_allow_wins_ties():
    txt = "User-agent: *\nDisallow: /a\nAllow: /a/b\n"
    assert robots.evaluate(txt, TOKEN, "/a/b/c").allowed is True
    assert robots.evaluate(txt, TOKEN, "/a/c").allowed is False
    assert robots.evaluate("User-agent: *\nDisallow: /a\nAllow: /a\n", TOKEN, "/a").allowed is True


def test_robots_specific_group_beats_star_and_wildcards():
    txt = "User-agent: *\nDisallow: /\n\nUser-agent: fetch-verbatim\nDisallow: /x$\nDisallow: /*.pdf\n"
    assert robots.evaluate(txt, TOKEN, "/page").allowed is True
    assert robots.evaluate(txt, TOKEN, "/x").allowed is False
    assert robots.evaluate(txt, TOKEN, "/x/y").allowed is True
    assert robots.evaluate(txt, TOKEN, "/dir/a.pdf").allowed is False
    assert robots.evaluate("User-agent: googlebot\nDisallow: /\n", TOKEN, "/x").allowed is True   # not our group


def test_robots_empty_disallow_allows_and_comments_and_crlf():
    assert robots.evaluate("User-agent: *\r\nDisallow:\r\n", TOKEN, "/x").allowed is True
    assert robots.evaluate("# c\nUser-agent: * # all\nDisallow: /s # secret\n", TOKEN, "/s").allowed is False
    assert robots.evaluate("", TOKEN, "/x").allowed is True


def test_robots_txt_itself_is_always_fetchable():
    assert robots.evaluate("User-agent: *\nDisallow: /\n", TOKEN, "/robots.txt").allowed is True


GOLDEN_HTML = ('<!doctype html><html><head><title> The  Page </title><style>p{x:y}</style><script>alert("x")</script></head>'
               '<body><!-- a comment --><h1>Head &amp; One</h1><p>Hello <b>big</b>   world <a href="/rel">link</a> '
               '<a href="javascript:evil()">js</a>.<br>Next line</p>'
               '<div hidden>HIDDEN ONE</div><div style="display: none">HIDDEN TWO<p>nested</p></div><span aria-hidden="true">HIDDEN THREE</span>'
               '<ul><li>one<li>two</ul><ol><li>first</li><li>second</li></ol>'
               '<pre>  keep\n    this</pre><blockquote>quoted</blockquote>'
               '<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>'
               '<img src="x.png" alt="a logo"><h2>Section Two</h2><p>Body two.</p><h3>Sub</h3><p>deep</p><h2>Section Three</h2><p>Body three.</p>'
               '<noscript>no script</noscript></body></html>')
GOLDEN_MD = ("# The Page\n\n# Head & One\n\nHello big world [link](/rel) js.\nNext line\n\n- one\n- two\n\n1. first\n2. second\n\n"
             "```\n  keep\n    this\n```\n\n> quoted\n\na | b\n\n1 | 2\n\n[image: a logo]\n\n## Section Two\n\nBody two.\n\n### Sub\n\ndeep\n\n"
             "## Section Three\n\nBody three.\n")


def test_golden_html_conversion_is_byte_exact():
    md, hidden = extract.html_to_markdown(GOLDEN_HTML)
    assert md == GOLDEN_MD
    assert hidden == 3


def test_conversion_is_deterministic():
    assert extract.html_to_markdown(GOLDEN_HTML) == extract.html_to_markdown(GOLDEN_HTML)


def test_hidden_text_never_appears_and_scripts_never_appear():
    md, _ = extract.html_to_markdown(GOLDEN_HTML)
    for needle in ("HIDDEN", "alert", "evil", "p{x:y}", "no script", "a comment"):
        assert needle not in md


def test_malformed_html_does_not_raise():
    for h in ("<p>unclosed <b>bold", "</div></div>stray", "<<<>>>", "<div hidden><p>a", "<a href=>x</a>", ""):
        extract.html_to_markdown(h)


def test_narrow_exact_beats_contains_and_stops_at_same_or_higher_level():
    md = GOLDEN_MD
    out, info = extract.narrow_to_section(md, "section  TWO")
    assert info == {"requested": "section  TWO", "matched": True, "heading": "Section Two", "match_kind": "exact"}
    assert out == "## Section Two\n\nBody two.\n\n### Sub\n\ndeep\n"
    out, info = extract.narrow_to_section(md, "three")
    assert info["match_kind"] == "contains" and out == "## Section Three\n\nBody three.\n"
    out, info = extract.narrow_to_section(md, "Sub")
    assert out == "### Sub\n\ndeep\n"


def test_narrow_without_a_match_returns_everything_and_says_so():
    out, info = extract.narrow_to_section(GOLDEN_MD, "nonexistent")
    assert out == GOLDEN_MD and info["matched"] is False and info["heading"] is None


def test_narrow_ignores_headings_inside_code_fences():
    md = "# A\n\n```\n## Fake\n```\n\n## Real\n\ntext\n"
    out, info = extract.narrow_to_section(md, "Fake")
    assert info["matched"] is False
    assert extract.narrow_to_section(md, "Real")[0] == "## Real\n\ntext\n"


def test_narrow_never_adds_text():
    out, _ = extract.narrow_to_section(GOLDEN_MD, "Section Two")
    assert out.strip() in GOLDEN_MD


def test_decode_body_charsets_and_replacement_reporting():
    assert extract.decode_body("café".encode("latin-1"), "text/plain; charset=iso-8859-1")[0] == "café"
    assert extract.decode_body(b"\xef\xbb\xbfhi", "text/plain")[0] == "hi"
    text, cs, repl = extract.decode_body(b"bad \xff byte", "text/plain; charset=utf-8")
    assert "�" in text and repl is True
    assert extract.decode_body(b"<meta charset=utf-8>ok", "text/html")[1] == "utf-8"
    assert extract.decode_body(b"x", "text/plain; charset=nonsense")[1] == "utf-8"
