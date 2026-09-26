"""Import only the reviewed local change set; validate every old/new byte hash."""
import base64,gzip,hashlib,json,subprocess,zlib
from pathlib import Path

root=Path.cwd()
parts=sorted((root/'.review-transfer').glob('part-*.b64'))
assert len(parts)==8
text=''.join(p.read_text() for p in parts)
# One duplicated carrier token from text transport; bind the restored archive
# to the already verified local digest. Never accept any other byte difference.
assert text.count('HBNUSXDcQcQcEE')==1
text=text.replace('HBNUSXDcQcQcEE','HBNUSXDcQcEE')
packed=base64.b64decode(text,validate=True)
assert hashlib.sha256(packed).hexdigest()=='78cdb38e45a7a87614c0bcb366ca74f1c4a997d27672feb466a9602f4a99531c'
rows=json.loads(gzip.decompress(packed));assert len(rows)==37
validated=[];seen=set()
for row in rows:
    rel=Path(row['path'])
    assert not rel.is_absolute() and '..' not in rel.parts
    assert rel.parts[0] in {'README.md','contracts','demo','docs','scripts','tests'}
    assert row['path'] not in seen;seen.add(row['path'])
    dest=root/rel
    assert not dest.is_symlink()
    if row['base'] is None:
        assert not dest.exists();old=b''
    else:
        old=dest.read_bytes();assert hashlib.sha256(old).hexdigest()==row['base'],row['path']
    data=base64.b64decode(row['data'],validate=True)
    if old:
        reader=zlib.decompressobj(zdict=old[-32768:]);new=reader.decompress(data)+reader.flush()
        assert reader.eof and not reader.unused_data
    else:new=zlib.decompress(data)
    assert hashlib.sha256(new).hexdigest()==row['sha'],row['path']
    assert row['mode'] in {0o644,0o755}
    validated.append((dest,new,row['mode'],row['path']))
# Nothing is written until every preimage and intended result has been checked.
for dest,data,mode,name in validated:
    dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data);dest.chmod(mode)
    print(name,hashlib.sha256(data).hexdigest())
subprocess.run(['git','add','--',*[r[3] for r in validated]],check=True)
subprocess.run(['git','diff','--cached','--check'],check=True)
subprocess.run(['git','diff','--cached','--stat'],check=True)
