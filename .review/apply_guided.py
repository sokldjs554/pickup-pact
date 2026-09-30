"""One-time, hash-checked application of the reviewed local diff to this branch."""
from pathlib import Path
import hashlib,json,re

def blob(data):
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()

def replace(s, before, after):
    assert s.count(before)==1, before
    return s.replace(before,after,1)

manifest=json.loads(Path('.review/manifest.json').read_text())
for name, hashes in manifest.items():
    p=Path(name); original=p.read_bytes(); assert blob(original)==hashes['before'],name
    s=original.decode()
    if name=='demo/route/api.py':
        s=replace(s,"'selection-state.js'}", "'selection-state.js','guide-flow.js','guide.css'}")
    elif name=='demo/route/product.js':
        s=replace(s,'let routeBootStarted=false;', 'let routeBootStarted=false,routeBootFinished=false;')
        s=replace(s,"notify('화면을 불러오지 못했어요. 잠시 후 새로고침해 주세요.',true);}",
                    "notify('화면을 불러오지 못했어요. 잠시 후 새로고침해 주세요.',true);}finally{routeBootFinished=true;}")
    elif name=='demo/route/index.html':
        s=replace(s,'</head>', '<link rel="stylesheet" href="/route-assets/guide.css"><script defer src="/route-assets/guide-flow.js"></script></head>')
        s=replace(s,'<body>', '<body class="entry-home">')
        s=replace(s,'</div></div><div class="hero-art"', '</div>'+Path('.review/entry.txt').read_text()+'</div><div class="hero-art"')
        s=replace(s,'<form id="intentForm">', '<details id="customizeOrder"><summary>시간·메뉴·혜택 직접 고르기</summary><form id="intentForm">')
        s=replace(s,'</form><div id="lockedIntent"', '</form></details><div id="lockedIntent"')
        s=replace(s,'<main>\n', '<main>\n'+Path('.review/panel.txt').read_text()+'\n')
    elif name=='.github/workflows/route-product.yml':
        s=replace(s,'      - name: Verify presentation labels', '      - name: Guided progress follows persisted server states\n        run: node scripts/verify_guided_stage.cjs\n      - name: Verify presentation labels')
        s=replace(s,'      - name: Recoverable merchant transfer over real HTTP', '      - name: Guided quick start and page-closed recovery desktop/mobile three times\n        run: python scripts/verify_guided_browser.py --repeat 3 --output verification/route-product/guided-browser\n      - name: Recoverable merchant transfer over real HTTP')
        s=replace(s,'      - name: Same-commit public recoverable transfers', "      - name: Same-commit public guided entry\n        if: github.ref == 'refs/heads/main'\n        run: python scripts/verify_guided_browser.py --repeat 3 --base-url https://pickup-pact-demo.onrender.com --expected-commit '${{ github.sha }}' --output verification/route-live/guided-browser\n      - name: Same-commit public recoverable transfers")
    else:
        pos=s.index('from playwright.sync_api import')
        s=s[:pos]+'from route_browser_helpers import reveal_manual_choices\n'+s[pos:]
        lines=[]
        for line in s.splitlines(keepends=True):
            if any("page.locator('#"+x+"')" in line for x in ['coupon','points','budget','findRoutes']) and any(x in line for x in ['.select_option(','.fill(','.click(']):
                lines.append(line[:len(line)-len(line.lstrip())]+'reveal_manual_choices(page)\n')
            lines.append(line)
        s=''.join(lines)
    result=s.encode();assert blob(result)==hashes['after'],(name,blob(result),hashes['after'])
    p.write_bytes(result)
print('All reviewed before/after file hashes match.')
