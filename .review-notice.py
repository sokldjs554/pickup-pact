import base64,gzip,hashlib,json,subprocess,zlib
from pathlib import Path
packed=base64.b64decode(Path('.review-notice.b64').read_text(),validate=True)
assert hashlib.sha256(packed).hexdigest()=='25591ab57de61bd06511fad439793a96469f96f0dd26eb0988805e8e97c31c3e'
rows=json.loads(gzip.decompress(packed))
allowed={'demo/route/product.js','demo/route/handoff-ui.js','scripts/verify_operation_notice.cjs','scripts/verify_decision_browser.py','docs/operation-notice-review-2026-09-26.md'}
assert len(rows)==5 and {r['path'] for r in rows}==allowed
ready=[]
for row in rows:
    p=Path(row['path']);assert not p.is_symlink()
    if row['base'] is None:
        assert not p.exists();old=None
    else:
        old=p.read_bytes();assert hashlib.sha256(old).hexdigest()==row['base'],str(p)
    data=base64.b64decode(row['data'],validate=True)
    if old:
        dec=zlib.decompressobj(zdict=old[-32768:]);value=dec.decompress(data)+dec.flush();assert dec.eof and not dec.unused_data
    else:value=zlib.decompress(data)
    assert hashlib.sha256(value).hexdigest()==row['sha'],str(p)
    ready.append((p,value))
for p,value in ready:p.write_bytes(value)
subprocess.run(['node','scripts/verify_operation_notice.cjs'],check=True)
subprocess.run(['git','add','--',*sorted(allowed)],check=True)
subprocess.run(['git','diff','--cached','--check'],check=True)
