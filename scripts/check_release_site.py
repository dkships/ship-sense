"""Check the scorecard, model coverage, charts and disclosures in Chromium."""
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


def check_generation_chart(page, base, output, width):
    response = page.goto(base + 'generations.svg', wait_until='networkidle')
    assert response.status == 200
    geometry = page.locator('svg').evaluate('''svg => {
        const bounds = node => {
            const box = node.getBBox();
            return {left: box.x, right: box.x + box.width,
                    top: box.y, bottom: box.y + box.height};
        };
        return {
            view: {width: svg.viewBox.baseVal.width, height: svg.viewBox.baseVal.height},
            texts: [...svg.querySelectorAll('text')].map(bounds),
            rows: [...svg.querySelectorAll('.frow')].map(row => ({
                label: bounds(row.querySelector('.flabel')),
                delta: bounds(row.querySelector('.fnum')),
                scores: [...row.querySelectorAll('.fsprev, .fscurr')].map(bounds)
            }))
        };
    }''')
    assert len(geometry['rows']) == 14
    for text in geometry['texts']:
        assert 0 <= text['left'] < text['right'] <= geometry['view']['width'], text
        assert 0 <= text['top'] < text['bottom'] <= geometry['view']['height'], text
    for row in geometry['rows']:
        for score in row['scores']:
            assert score['left'] >= row['label']['right'] + 8, row
            assert score['right'] <= row['delta']['left'] - 8, row
    assert 'slight upgrade' not in page.locator('svg').text_content()
    page.screenshot(path=str(output / f'generations-{width}.png'), full_page=True)
    return len(geometry['rows'])


def check_page(page, base, output, width, height):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
    page.on('requestfailed', lambda request: errors.append(request.url))
    page.set_viewport_size({'width': width, 'height': height})
    response = page.goto(base, wait_until='networkidle')
    assert response.status == 200
    assert page.get_by_role('heading', name='Product judgment, under uncertainty').is_visible()
    assert page.locator('.focal .fscore').is_visible()
    assert '95% CI' in page.locator('.focal').inner_text()
    assert page.locator('#model-scores tbody tr').count() == 18
    assert page.locator('#previous-scores tbody tr').count() == 14
    assert page.locator('.gcard').count() == 14
    first_name = page.locator('#model-scores .model').first.bounding_box()
    assert first_name['width'] >= 130
    if width < 760:
        assert not page.locator('#model-scores th').nth(8).is_visible()
    assert page.locator('.matrix tbody tr').count() == 17
    assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
    scores = json.loads((ROOT / 'docs/decision-scores.json').read_text())
    for model in scores['models']:
        if model['is_baseline']:
            continue
        row = page.locator(f'tr[data-model="{model["name"]}"]')
        assert row.count() == 1
        assert row.locator('.score .num').inner_text() == f'{model["score"]["value"]:.1f}'
        assert '95% CI' in row.locator('.score').inner_text()
    page.screenshot(path=str(output / f'first-screen-{width}.png'))
    page.screenshot(path=str(output / f'scores-{width}.png'), full_page=True)
    page.locator('nav.jump').get_by_role('link', name='Leaderboard', exact=True).click()
    assert page.url.endswith('#leaderboard')
    page.locator('#leaderboard').screenshot(path=str(output / f'leaderboard-{width}.png'))
    previous = page.locator('#generations details')
    previous.locator('summary').click()
    assert not page.locator('#previous-scores').is_visible()
    previous.locator('summary').press('Enter')
    assert page.locator('#previous-scores').is_visible()
    page.locator('#limits summary').click()
    assert page.get_by_text('Scoring details and reproducibility', exact=True).is_visible()
    assert 'public inputs' in page.locator('#limits details').inner_text()
    for link in page.locator('a').all():
        href = link.get_attribute('href')
        if href and not href.startswith(('http:', 'https:', '#')):
            assert page.request.get(base + href).status == 200, href
    page.get_by_role('link', name='Previous overall and grading audit', exact=True).click()
    page.wait_for_load_state('networkidle')
    assert page.get_by_text('Provisional results. No official ranking.').is_visible()
    assert page.locator('tbody tr').count() == 32
    assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
    generation_rows = check_generation_chart(page, base, output, width)
    assert not errors, errors
    return {'viewport': [width, height], 'console_errors': errors, 'overflow': False,
            'current_rows': 18, 'previous_rows': 14, 'generation_cards': 14,
            'generation_chart_rows': generation_rows, 'generation_chart_labels': 'no overlap or clipping',
            'matrix_rows': 17, 'all_model_scores_match': True, 'historical_scores': 'passed'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'docs')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            base = f'http://127.0.0.1:{server.server_port}/'
            results = []
            for width, height in ((1440, 1000), (390, 844)):
                page = browser.new_page(java_script_enabled=False)
                results.append(check_page(page, base, args.output, width, height))
                page.goto(base + 'history/v3.0/docs/index.html', wait_until='networkidle')
                page.screenshot(path=str(args.output / f'original-first-screen-{width}.png'))
                page.close()
            page = browser.new_page(viewport={'width':1200,'height':630}, device_scale_factor=1)
            page.goto(base + 'card.svg', wait_until='networkidle')
            page.screenshot(path=str(args.output / 'card.png'), omit_background=False)
            page.close()
            browser.close()
        (args.output / 'checks.json').write_text(json.dumps(results, indent=2) + '\n')
        print(json.dumps(results, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
