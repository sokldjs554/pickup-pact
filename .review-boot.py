import base64,gzip,hashlib,json,subprocess,zlib
from pathlib import Path
packed=base64.b64decode(Path('.review-boot.b64').read_text(),validate=True)
assert hashlib.sha256(packed).hexdigest()=='6bb7bf10bf2c48bd1ca94b9e5e4663c6ebd4bacba3eb51eb86756c25e413babb'
rows=json.loads(gzip.decompress(packed))
allowed={'demo/route/product.js','scripts/verify_bootstrap_order.cjs','scripts/verify_bootstrap_browser.py','scripts/verify_benefits_browser.py','docs/bootstrap-review-2026-09-26.md'}
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
for p,value in ready:
    p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(value)
subprocess.run(['node','scripts/verify_bootstrap_order.cjs'],check=True)
subprocess.run(['git','add','--',*sorted(allowed)],check=True)
subprocess.run(['git','diff','--cached','--check'],check=True)
