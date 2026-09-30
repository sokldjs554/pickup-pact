"""Entry-point contracts; financial and recovery handlers remain unchanged."""
from html.parser import HTMLParser
from fastapi.testclient import TestClient
from demo.main import app


class Elements(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.stack, self.ids = [], {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if 'id' in data:
            self.ids[data['id']] = (tag, data, [x[1].get('id') for x in self.stack])
        if tag not in {'input', 'meta', 'link', 'br', 'img', 'hr', 'source'}:
            self.stack.append((tag, data))

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                self.stack = self.stack[:i]
                return


def test_home_offers_a_disclosed_one_click_start():
    with TestClient(app) as client:
        html = client.get('/').text
    nodes = Elements(html).ids
    assert 'quickStart' in nodes, 'The default entry still has no quick-start action.'
    assert nodes['quickStart'][0] == 'button'
    assert 'quickStartSummary' in nodes
    assert 'guidePanel' in nodes
    assert '실제 주문이나 결제는 되지 않아요' in html


def test_manual_choices_are_available_but_collapsed_initially():
    with TestClient(app) as client:
        html = client.get('/').text
    nodes = Elements(html).ids
    assert 'customizeOrder' in nodes, 'Advanced choices still occupy the entry screen.'
    tag, attrs, _ = nodes['customizeOrder']
    assert tag == 'details' and 'open' not in attrs
    for field in ['destination', 'deadline', 'budget', 'detour', 'priority', 'coupon', 'points']:
        assert 'customizeOrder' in nodes[field][2], field


def test_guide_assets_keep_existing_static_safety_headers():
    with TestClient(app) as client:
        for path in ['/route-assets/guide-flow.js', '/route-assets/guide.css']:
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code)
            assert response.headers['x-content-type-options'] == 'nosniff'
        assert client.get('/route-assets/not-a-real-file.js').status_code == 404
