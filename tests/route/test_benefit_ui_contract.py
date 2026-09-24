from fastapi.testclient import TestClient
from demo.main import app

def test_benefits_and_comparison_are_available_in_customer_entrypoint():
    c=TestClient(app);html=c.get('/').text
    for required in ['id="coupon"','id="points"','id="runExperiment"','benefits-ui.js','benefits.css']:
        assert required in html
    for asset in ['benefits-ui.js','benefits.css']:
        assert c.get('/route-assets/'+asset).status_code==200
    assert c.get('/route-assets/../store.py').status_code==404

def test_route_openapi_snapshot_matches_current_contract():
    import json
    from pathlib import Path
    doc=json.loads(Path('contracts/route-benefits.openapi.json').read_text())
    runtime=app.openapi()
    assert doc['paths']=={k:v for k,v in runtime['paths'].items() if k.startswith('/api/route/')}
    for name,schema in doc['components']['schemas'].items():
        assert runtime['components']['schemas'][name]==schema


def test_coupon_selector_contains_all_three_usable_benefits():
    from html.parser import HTMLParser

    class CouponOptions(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_coupon = False
            self.values = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'select' and attrs.get('id') == 'coupon':
                self.in_coupon = True
            if self.in_coupon and tag == 'option':
                self.values.append(attrs.get('value'))

        def handle_endtag(self, tag):
            if tag == 'select':
                self.in_coupon = False

    parser = CouponOptions()
    parser.feed(TestClient(app).get('/').text)
    assert parser.values == ['', 'welcome500', 'wave1000', 'morning10']
