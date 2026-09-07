"""Check the release page and provisional score audit in desktop and mobile Chromium."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check_page(page, base, output, width, height):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("requestfailed", lambda request: errors.append(request.url))
    page.set_viewport_size({"width": width, "height": height})
    response = page.goto(base, wait_until="networkidle")
    assert response.status == 200
    assert page.get_by_text("Provisional scores. No validated model ranking.").is_visible()
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    page.screenshot(path=str(output / f"release-{width}.png"), full_page=True)
    for link in page.locator("a").all():
        href = link.get_attribute("href")
        if href and not href.startswith(("http:", "https:", "#")):
            assert page.request.get(base + href).status == 200, href
    page.get_by_role("link", name="Provisional scores and intervals").click()
    page.wait_for_load_state("networkidle")
    assert page.get_by_text("Provisional results. No official ranking.").is_visible()
    assert page.locator("tbody tr").count() == 32
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    page.locator("summary").click()
    assert page.get_by_text("465 candidate comparisons").is_visible()
    page.screenshot(path=str(output / f"scores-{width}.png"), full_page=True)
    assert not errors, errors
    return {"viewport": [width, height], "console_errors": errors, "overflow": False, "score_rows": 32}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "docs")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            base = f"http://127.0.0.1:{server.server_port}/"
            results = []
            for width, height in ((1440, 1000), (390, 844)):
                page = browser.new_page()
                results.append(check_page(page, base, args.output, width, height))
                page.close()
            browser.close()
        (args.output / "checks.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
