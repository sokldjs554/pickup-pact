"""Apply only the exact locally tested source diff on its dedicated feature branch."""
import base64
import hashlib
import lzma
from pathlib import Path
import subprocess

branch = subprocess.check_output(['git','branch','--show-current'],text=True).strip()
assert branch == 'feat/merchant-payment-20260930', branch
subprocess.run(['git','diff','--exit-code','c39cadda2f6a8c6916c21f4d4a62c828eaafc722','HEAD','--','demo','services','contracts','scripts','tests'],check=True)
encoded = ''.join(Path(f'.review/payment-{i}.b64').read_text('ascii') for i in range(5))
assert len(encoded) == 69144
compressed = base64.b64decode(encoded,validate=True)
assert len(compressed) == 51856
assert hashlib.sha256(compressed).hexdigest() == 'acfffac1c203517030dd38663c1983ab4095a6e0a408d1491dc358b089107301'
decoder = lzma.LZMADecompressor(memlimit=512_000_000)
patch = decoder.decompress(compressed,max_length=300_000)
assert decoder.eof and not decoder.unused_data and len(patch) == 267234
assert hashlib.sha256(patch).hexdigest() == '5f8393bdd849dc65ea3a580390bbac950e32c096b0b70d7eb15acc1a7a23993c'
paths = [line.split(' b/',1)[1] for line in patch.decode().splitlines() if line.startswith('diff --git ')]
assert len(paths) == 42
assert all(p.startswith(('demo/route/','scripts/','tests/route/','docs/','contracts/')) and '..' not in Path(p).parts for p in paths)
subprocess.run(['git','apply','--check','--unidiff-zero','-'],input=patch,check=True)
subprocess.run(['git','apply','--unidiff-zero','-'],input=patch,check=True)
subprocess.run(['git','add','--',*paths],check=True)
subprocess.run(['git','diff','--cached','--check'],check=True)
actual = subprocess.check_output(['git','diff','--cached','--name-only'],text=True).splitlines()
assert set(actual) == set(paths), actual
print('Applied 42 files; compressed and decompressed SHA256 match reviewed local source.')
