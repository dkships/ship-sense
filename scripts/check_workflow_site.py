"""Check a rendered v4 scorecard against its public scores in Chromium."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from playwright.sync_api import sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check_page(page, base, docs, output, width, height):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
    page.on('requestfailed', lambda request: errors.append(request.url))
    page.set_viewport_size({'width': width, 'height': height})
    response = page.goto(base, wait_until='networkidle')
    assert response.status == 200
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    data = json.loads((docs / 'v4-scores.json').read_text())
    workflow = json.loads((docs / 'workflow-scores.json').read_text())
    measures = {m['name']: m for m in workflow['models']}
    assert page.locator('tr[data-model]').count() == data['model_count']
    for model in data['models']:
        row = page.locator(f'tr[data-model="{model["name"]}"]')
        expected = [model['score'], *(model['dimensions'][d] for d in ('restraint', 'honesty', 'conviction')),
                    measures[model['name']]['honesty_field_accuracy'], measures[model['name']]['workflow_accuracy']]
        for cell, score in zip(row.locator('td.dim').all(), expected, strict=True):
            assert cell.inner_text().split()[0] == f'{score["value"]:.1f}'
            assert cell.locator('.ciq').inner_text() == f'95% CI {score["lo"]:.1f}–{score["hi"]:.1f}'
    geometry = page.locator('.wf-chart svg').evaluate('''svg => {
        const box = node => { const b = node.getBBox(); return {left:b.x,right:b.x+b.width,top:b.y,bottom:b.y+b.height}; };
        return {width:svg.viewBox.baseVal.width,height:svg.viewBox.baseVal.height,
            texts:[...svg.querySelectorAll('text')].map(box),
            rows:[...svg.querySelectorAll('.wf-chart-row')].map(r=>({label:box(r.querySelector('.wf-label')),value:box(r.querySelector('.wf-value'))}))};
    }''')
    for text in geometry['texts']:
        assert 0 <= text['left'] < text['right'] <= geometry['width'], text
        assert 0 <= text['top'] < text['bottom'] <= geometry['height'], text
    for row in geometry['rows']:
        assert row['label']['right'] < 292, row
        assert row['value']['left'] > 768, row
    for link in page.locator('a').all():
        href = link.get_attribute('href')
        if href and not href.startswith(('http:', 'https:', '#')):
            assert page.request.get(base + href).status == 200, href
    page.screenshot(path=str(output / f'v4-first-screen-{width}.png'))
    page.screenshot(path=str(output / f'v4-full-{width}.png'), full_page=True)
    page.locator('.wf-chart').screenshot(path=str(output / f'v4-chart-{width}.png'))
    page.get_by_role('link', name='All historical scores', exact=True).click()
    assert page.url.endswith('decision.html')
    assert page.get_by_text('Highest observed Decision score', exact=True).is_visible()
    assert not errors, errors
    return {'viewport': [width, height], 'models': data['model_count'], 'scores_match': True,
            'chart_geometry': 'passed', 'historical_page': 'passed', 'console_errors': errors, 'overflow': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docs', type=Path, default=Path(__file__).resolve().parents[1] / 'docs')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(args.docs)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            results = []
            for width, height in ((1440, 1000), (390, 844)):
                page = browser.new_page(java_script_enabled=False)
                results.append(check_page(page, f'http://127.0.0.1:{server.server_port}/', args.docs, args.output, width, height))
                page.close()
            browser.close()
        (args.output / 'checks.json').write_text(json.dumps(results, indent=2) + '\n')
        print(json.dumps(results, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
