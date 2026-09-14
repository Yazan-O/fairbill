"""Kills: "the audit scene draws a bill line twice when the payload repeats it". Loads the
shipped web/ page in Chromium (no server: file://, the run is never started) and calls the
page's own auditRowsHtml with a payload that names line 5 twice.
Run: python -m pytest evals/test_page_dedupe.py -q   (needs playwright chromium)
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_audit_rows_draw_each_line_once():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 390, "height": 844})
        # the page asks for /static/app.js and /static/style.css: serve them from web/
        page.route("**/static/*", lambda route: route.fulfill(
            path=str(ROOT / "web" / route.request.url.rsplit("/", 1)[1])))
        page.route("**/api/**", lambda route: route.fulfill(status=204, body=""))
        page.route("http://fairbill.test/index.html", lambda route: route.fulfill(
            path=str(ROOT / "web/index.html"), content_type="text/html"))
        page.goto("http://fairbill.test/index.html")  # every request above is answered from web/
        n = page.evaluate("""() => {
          const lines = [{line_no:1,code:"36415",description:"Venipuncture",charge:46,units:1},
                         {line_no:5,code:"71046",description:"Chest X-ray",charge:449,units:1},
                         {line_no:5,code:"71046",description:"Chest X-ray",charge:449,units:1}];
          const html = auditRowsHtml(lines, new Map());
          return (html.match(/71046/g) || []).length;
        }""")
        b.close()
    assert n == 1
