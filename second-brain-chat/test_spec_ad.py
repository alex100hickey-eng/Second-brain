"""The guards on a spec ad. It goes out unsolicited, under Alex's name, and is the first work
a brand ever sees from him — so what it may say is worth pinning."""
import importlib.util
import os

import pytest

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "spec_ad.py")
_spec = importlib.util.spec_from_file_location("spec_ad", PATH)
sa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sa)

GOOD = dict(headline="Swim you can actually swim in", body="Full coverage. Made to move.",
            cta="Shop the collection", allow=[])


def test_good_copy_passes():
    assert sa.copy_problems(**GOOD) == []


def test_refuses_claims_nobody_can_back():
    """Every one of these is a claim someone has to stand behind, and on a spec ad for a brand
    nobody has spoken to yet, nobody can."""
    for text, expect in [
        ("40% off this week", "a percentage"),
        ("The #1 swimsuit for real swimmers", '"#1"'),
        ("Clinically tested for sensitive skin", "a medical claim"),
        ("Loved by 12,000 reviews", "a review count"),
        ("Guaranteed to last all season", "an absolute claim"),
        ("Now $49 for a limited time", "a price"),
    ]:
        problems = sa.copy_problems(headline=text, body="", cta="", allow=[])
        assert any(expect in p for p in problems), f"{text!r} slipped through"


def test_a_claim_read_on_their_own_site_can_be_allowed():
    """The guard exists to stop invention, not to stop a brand's own published claim from
    appearing in an ad about that brand."""
    assert sa.copy_problems(headline="UPF 50+, 40% off through Sunday", body="", cta="",
                            allow=["a percentage"]) == []


def test_refuses_copy_the_feed_would_cut_off():
    long_head = "A headline that simply keeps going and going well past anything a phone screen "
    assert any("truncates" in p for p in
               sa.copy_problems(headline=long_head, body="", cta="", allow=[]))
    assert any("no headline" in p for p in sa.copy_problems(headline="", body="", cta="", allow=[]))
    assert any("nobody reads it" in p for p in
               sa.copy_problems(headline="Fine", body="x" * 200, cta="", allow=[]))


def test_shared_fabrication_guard_is_the_email_one():
    """Imported from splitframe_daily, not copied — two copies drift, and this one decides
    whether a stranger receives a claim about their own product that nobody checked."""
    assert sa.fabrication_risk is not None
    src = open(PATH).read()
    assert "def fabrication_risk" not in src


def test_html_escapes_copy():
    page = sa.build_html("A & B", "<script>alert(1)</script>", "", "", __file__, "light",
                         (1080, 1350))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page and "A &amp; B" in page


def test_crop_defaults_to_top():
    """"center" is the one crop that reliably cuts a model's head off; it did exactly that on
    the first ad this rendered."""
    page = sa.build_html("B", "H", "", "", __file__, "light", (1080, 1350))
    assert "background-position: center top" in page
    page = sa.build_html("B", "H", "", "", __file__, "light", (1080, 1350), "bottom")
    assert "background-position: center bottom" in page


def test_no_send_capability():
    """It renders a file. It does not mail it — every outbound email goes through the one
    guarded path on the Mac, and a second one hiding in a render script would be worse than
    the first because nobody would think to look here."""
    src = open(PATH).read()
    for banned in ("smtplib", "GMAIL_SEND", "send_email", "outbox"):
        assert banned not in src, f"{banned} appears in spec_ad.py"


# ---- layouts -------------------------------------------------------------------------------
# The card layout's HTML, captured from main at 9f443b9 BEFORE --layout existed. Every approved
# static was rendered from exactly this page, so it is pinned byte for byte: a layout change that
# moves one character of it would silently change what "re-render" means for those brands.
FIXTURE_IMAGE_BYTES = b"\x89PNG spec-ad fixture"

CARD_GOLDEN_LIGHT_4x5 = (
    '<!doctype html>\n'
    '<meta charset="utf-8">\n'
    '<style>\n'
    '  @page { margin: 0 }\n'
    '  * { box-sizing: border-box; margin: 0; padding: 0 }\n'
    '  html, body { width: 1080px; height: 1350px; }\n'
    '  body {\n'
    '    background: #F4F1EC; color: #14110E;\n'
    '    font-family: "Helvetica Neue", -apple-system, Helvetica, Arial, sans-serif;\n'
    '    display: flex; flex-direction: column;\n'
    '  }\n'
    '  .shot {\n'
    '    flex: 1 1 auto; min-height: 0;\n'
    '    background-image: url("data:image/png;base64,iVBORyBzcGVjLWFkIGZpeHR1cmU=");\n'
    '    background-size: cover; background-position: center top;\n'
    '  }\n'
    '  .copy { flex: 0 0 auto; padding: 64px 72px 72px; }\n'
    '  .brand {\n'
    '    font-size: 26px; letter-spacing: .22em; text-transform: uppercase;\n'
    '    color: #5C564E; margin-bottom: 26px;\n'
    '  }\n'
    '  h1 {\n'
    '    font-size: 74px; line-height: 1.06;\n'
    '    letter-spacing: -.022em; font-weight: 700; text-wrap: balance;\n'
    '  }\n'
    '  p { margin-top: 24px; font-size: 32px; line-height: 1.38; color: #5C564E; }\n'
    '  .cta {\n'
    '    display: inline-block; margin-top: 40px; padding: 20px 38px; border-radius: 999px;\n'
    '    background: #14110E; color: #F4F1EC; font-size: 28px; font-weight: 600;\n'
    '  }\n'
    '</style>\n'
    '<div class="shot"></div>\n'
    '<div class="copy">\n'
    '  <div class="brand">Calypsa &amp; Co</div>\n'
    '  <h1>Swim you can actually swim in</h1>\n'
    '  <p>Full coverage. Made to move.</p>\n'
    '  <div class="cta">Shop the collection</div>\n'
    '</div>\n'
)

CARD_GOLDEN_DARK_1x1 = (
    '<!doctype html>\n'
    '<meta charset="utf-8">\n'
    '<style>\n'
    '  @page { margin: 0 }\n'
    '  * { box-sizing: border-box; margin: 0; padding: 0 }\n'
    '  html, body { width: 1080px; height: 1080px; }\n'
    '  body {\n'
    '    background: #14110E; color: #F7F4EF;\n'
    '    font-family: "Helvetica Neue", -apple-system, Helvetica, Arial, sans-serif;\n'
    '    display: flex; flex-direction: column;\n'
    '  }\n'
    '  .shot {\n'
    '    flex: 1 1 auto; min-height: 0;\n'
    '    background-image: url("data:image/png;base64,iVBORyBzcGVjLWFkIGZpeHR1cmU=");\n'
    '    background-size: cover; background-position: center bottom;\n'
    '  }\n'
    '  .copy { flex: 0 0 auto; padding: 64px 72px 72px; }\n'
    '  .brand {\n'
    '    font-size: 26px; letter-spacing: .22em; text-transform: uppercase;\n'
    '    color: #A49C90; margin-bottom: 26px;\n'
    '  }\n'
    '  h1 {\n'
    '    font-size: 60px; line-height: 1.06;\n'
    '    letter-spacing: -.022em; font-weight: 700; text-wrap: balance;\n'
    '  }\n'
    '  p { margin-top: 24px; font-size: 32px; line-height: 1.38; color: #A49C90; }\n'
    '  .cta {\n'
    '    display: inline-block; margin-top: 40px; padding: 20px 38px; border-radius: 999px;\n'
    '    background: #F7F4EF; color: #14110E; font-size: 28px; font-weight: 600;\n'
    '  }\n'
    '</style>\n'
    '<div class="shot"></div>\n'
    '<div class="copy">\n'
    '  <div class="brand">Calypsa</div>\n'
    '  <h1>A much longer headline that wraps onto &lt;two&gt; lines</h1>\n'
    '  \n'
    '  \n'
    '</div>\n'
)


def _fixture_image(tmp_path):
    img = tmp_path / "fixture.png"
    img.write_bytes(FIXTURE_IMAGE_BYTES)
    return str(img)


def _cli(img, *extra):
    return ["render", "--brand", "Calypsa & Co", "--headline", "Swim you can actually swim in",
            "--body", "Full coverage. Made to move.", "--cta", "Shop the collection",
            "--image", img, "--out", "/nonexistent/never-written.png", "--dry-run", *extra]


def test_card_html_is_unchanged_from_main(tmp_path):
    img = _fixture_image(tmp_path)
    assert sa.build_html("Calypsa & Co", "Swim you can actually swim in",
                         "Full coverage. Made to move.", "Shop the collection", img, "light",
                         (1080, 1350), "top") == CARD_GOLDEN_LIGHT_4x5
    assert sa.build_html("Calypsa", "A much longer headline that wraps onto <two> lines", "", "",
                         img, "dark", (1080, 1080), "bottom", "card") == CARD_GOLDEN_DARK_1x1
    # No --theme resolves to the light theme the card always defaulted to.
    assert sa.build_html("Calypsa & Co", "Swim you can actually swim in",
                         "Full coverage. Made to move.", "Shop the collection", img, None,
                         (1080, 1350), "top") == CARD_GOLDEN_LIGHT_4x5


def test_card_is_the_default_layout_and_dry_run_prints_its_html(tmp_path, capsys):
    img = _fixture_image(tmp_path)
    assert sa.main(_cli(img)) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK (dry run): Calypsa & Co 4:5 passes every guard")
    assert CARD_GOLDEN_LIGHT_4x5 in out
    assert sa.main(_cli(img, "--layout", "card")) == 0
    assert CARD_GOLDEN_LIGHT_4x5 in capsys.readouterr().out


def test_feed_layout_markers(tmp_path, capsys):
    img = _fixture_image(tmp_path)
    page = sa.build_html("A & B", "Made <in> house", "One short line.", "Shop now", img, None,
                         (1080, 1350), "top", "feed")
    assert 'class="layout-feed"' in page
    assert "position: absolute; inset: 0" in page                  # full-bleed photo
    assert 'class="scrim"' in page and "linear-gradient(to bottom" in page
    assert "height: 608px" in page                                 # bottom 45% of 1350
    assert '<div class="wordmark">A &amp; B</div>' in page
    assert "Made &lt;in&gt; house" in page and "<in>" not in page
    assert '"Avenir Next"' in page and "sans-serif" in page        # system face + fallback
    assert "background: #FFFFFF; color: #14110E" in page           # white pill CTA
    assert "fonts.googleapis" not in page and "http" not in page.split("base64,")[0]
    # dry run prints it through the CLI too
    assert sa.main(_cli(img, "--layout", "feed")) == 0
    out = capsys.readouterr().out
    assert "4:5 feed passes every guard" in out and 'class="layout-feed"' in out


def test_feed_respects_ratio_theme_and_focus(tmp_path):
    img = _fixture_image(tmp_path)
    page = sa.build_html("B", "H", "", "", img, "light", (1080, 1080), "bottom", "feed")
    assert "height: 1080px" in page and "height: 486px" in page    # 1:1, 45% scrim
    assert "background-position: center bottom" in page
    assert "rgba(244,241,236," in page                             # light theme: paper scrim
    dark = sa.build_html("B", "H", "", "", img, None, (1080, 1350), "top", "feed")
    assert "rgba(11,9,8," in dark and "background-position: center top" in dark


def test_editorial_layout_markers(tmp_path, capsys):
    img = _fixture_image(tmp_path)
    page = sa.build_html("A & B", "Made <in> house", "One short line.", "Shop now", img, None,
                         (1080, 1350), "top", "editorial")
    assert 'class="layout-editorial"' in page
    assert "flex: 0 0 62%" in page                                 # photo = top 62%
    assert '"Iowan Old Style"' in page and "serif;" in page
    assert "font-variant-caps: all-small-caps" in page             # body in small caps
    assert "text-decoration: underline" in page                    # CTA as underlined text
    assert "border-radius" not in page                             # ...not a button
    assert "Made &lt;in&gt; house" in page and "A &amp; B" in page
    # The fixture is not a decodable image, so the panel falls back to the fixed near-black.
    assert "background: #14110E; color: #F7F4EF" in page
    assert sa.main(_cli(img, "--layout", "editorial", "--ratio", "1:1")) == 0
    out = capsys.readouterr().out
    assert "1:1 editorial passes every guard" in out and "height: 1080px" in out


def test_editorial_panel_is_sampled_from_the_photo(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    photo = tmp_path / "teal.png"
    im = Image.new("RGB", (120, 120), (255, 255, 255))            # white seamless backdrop...
    im.paste((20, 110, 120), (30, 30, 90, 110))                    # ...with a teal product on it
    im.save(photo)
    dom = sa.dominant_colour(str(photo))
    assert dom is not None and abs(dom[0] - 20) < 12 and abs(dom[1] - 110) < 12
    page = sa.build_html("B", "H", "", "", str(photo), None, (1080, 1350), "top", "editorial")
    panel, ink = sa.editorial_palette(dom, "auto")
    assert f"background: {sa._hex(panel)}; color: {sa._hex(ink)}" in page
    # Whatever the photo, the body type clears 6:1 on the panel, in every theme.
    for theme in ("auto", "light", "dark"):
        p, i = sa.editorial_palette(dom, theme)
        assert sa._contrast(p, i) >= 6.0
    assert sa.editorial_palette(dom, "dark")[1] == sa.INK_LIGHT
    assert sa.editorial_palette(dom, "light")[1] == sa.INK_DARK


def test_unknown_layout_errors_cleanly(tmp_path, capsys):
    img = _fixture_image(tmp_path)
    with pytest.raises(SystemExit) as e:
        sa.main(_cli(img, "--layout", "poster"))
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice: 'poster'" in err and "card" in err and "editorial" in err
    with pytest.raises(ValueError, match="unknown layout 'poster'"):
        sa.build_html("B", "H", "", "", img, None, (1080, 1350), "top", "poster")


def test_the_claim_guard_runs_for_every_layout(tmp_path, capsys):
    img = _fixture_image(tmp_path)
    for layout in sa.LAYOUTS:
        args = _cli(img, "--layout", layout)
        args[args.index("--headline") + 1] = "The #1 swimsuit, 40% off"
        assert sa.main(args) == 1
        out = capsys.readouterr().out
        assert "NOT rendered" in out and '"#1"' in out and "a percentage" in out


def test_dry_run_elides_a_real_photo(tmp_path):
    """A real product photo is ~1 MB of base64; a dry run is a check, not a dump."""
    page = 'url("data:image/jpeg;base64,' + "A" * 50000 + '")'
    short = sa.elide_photo(page)
    assert len(short) < 200 and "photo elided" in short
    assert sa.elide_photo('url("data:image/png;base64,iVBORw==")') == 'url("data:image/png;base64,iVBORw==")'
