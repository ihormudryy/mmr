"""Guide tab template is the interactive user guide (info bubbles)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE_TMPL = ROOT / "web" / "templates" / "_guide_tab.html"


def test_guide_template_has_info_bubbles_and_sections():
    html = GUIDE_TMPL.read_text(encoding="utf-8")
    assert "macro info" in html
    assert 'class="info"' in html or "info(" in html
    assert "guide-wrap" in html
    assert "Manual buy on paper" in html
    assert "Pause new trading" in html
