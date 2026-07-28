from pathlib import Path

HUD_DIR = Path(__file__).resolve().parents[1] / "hud"


def test_index_html_has_no_inline_style_block():
    html = (HUD_DIR / "index.html").read_text()
    assert "<style>" not in html


def test_index_html_links_external_stylesheet():
    html = (HUD_DIR / "index.html").read_text()
    assert '<link rel="stylesheet" href="styles.css">' in html


def test_styles_css_exists_and_nonempty():
    css = HUD_DIR / "styles.css"
    assert css.exists()
    assert len(css.read_text()) > 100
