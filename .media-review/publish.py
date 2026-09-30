"""Publish only the media whose real browser run and file hashes were reviewed."""
from pathlib import Path
import hashlib
import json
import shutil
import sys
import zipfile

root = Path(sys.argv[1]).resolve()
repo = Path.cwd()
source_commit = '878390efecfddd00e97f8243784e13c05b7b0000'
artifact_id = 11094890834
artifact_digest = '2bc1c7aecb4893dcb7c97827cc6f2aa13b34ade5217fced82186ec0821a65e16'
report = json.loads((root/'media/capture.json').read_text())
assert report['recorded_release_commit'] == source_commit
assert report['recording_scope'] == 'ci_local_http'
assert report['runtime']['payment_transport'] == report['runtime']['merchant_transport'] == 'http'
assert report['runtime']['payment_storage_ready'] and report['runtime']['automatic_recovery']
assert report['capture_count'] == 1 and report['cash_due'] == 3200
assert report['points_spent'] == 1000 and report['points_earned'] == 32
assert not report['page_errors']
proof = report['final_reconciliation']
assert proof['status'] == 'MATCH' and proof['terminal'] and proof['order_id'] == report['order_id']
assert all(c['status']=='MATCH' for c in proof['checks'].values())
assert proof['payment']['captured_krw'] == 3200 and proof['payment']['held_krw'] == 0
assert 35 < report['edited_duration_seconds'] < 70
for name, digest in report['media_sha256'].items():
    assert '/' not in name and '\\' not in name
    assert hashlib.sha256((root/'media'/name).read_bytes()).hexdigest() == digest, name
rows = json.loads((root/'browser/results.json').read_text())
assert len(rows) == 30 and all(r['result']=='passed' for r in rows)
tabs = json.loads((root/'tabs/results.json').read_text())
assert len(tabs) == 6 and all(r['result']=='passed' and r['observer_refreshed_without_reload'] for r in tabs)
with zipfile.ZipFile(root/'tested-source.zip') as archive:
    for name in archive.namelist():
        if not name.endswith('/') and name.startswith(('demo/', 'services/', 'contracts/', 'scripts/', 'tests/')):
            assert (repo/name).read_bytes() == archive.read(name), name
out = repo/'docs/media/payment'
assert not out.exists(), 'Do not replace existing media silently.'
out.mkdir(parents=True)
for name in ['pickup-pact-payment.mp4','pickup-pact-payment.gif','capture.json','01-home.png','02-approval-unknown.png','04-rejected.png','05-pending.png','06-recovered.png','07-merchant.png','08-receipt.png']:
    shutil.copy2(root/'media'/name, out/name)
shutil.copy2(root/'browser/mobile-capture_reply_lost-result.png',out/'mobile-result.png')
raw = Path(report['source_video'])
assert not raw.is_absolute() and '..' not in raw.parts and raw.parts[0] == 'raw'
(out/raw).parent.mkdir(parents=True,exist_ok=True)
shutil.copy2(root/'media'/raw,out/raw)
shutil.copy2(root/'browser/results.json',out/'browser-results.json')
shutil.copy2(root/'tabs/results.json',out/'tabs-results.json')
for n in range(1,4):shutil.copy2(root/f'tests-{n}.log',out/f'tests-{n}.log')
manifest = {'artifact_id':artifact_id,'run_id':36711223851,'artifact_sha256':artifact_digest,
    'recorded_commit':source_commit,'feature_commit':'36db020cb57c2d1d2994fc1dc9890761e95f0200',
    'scope':'CI actual HTTP application and independent PG/merchant processes; not public deployment',
    'files':{str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*') if p.is_file()}}
(out/'publication.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
p=repo/'README.md';text=p.read_text()
start=text.index('## 새 첫 화면과 41초 체험 영상')
end=text.index('## 고객이 직접 해보는 흐름', start)
section=Path('.media-review/readme-section.txt').read_text().replace('VIDEO_SECONDS',str(report['edited_duration_seconds']))
p.write_text(text[:start]+section+'\n'+text[end:])
(repo/'docs/payment-release-verification-2026-09-30.md').write_text(Path('.media-review/verification.txt').read_text())
p=repo/'docs/payment-implementation-status-2026-09-30.md';text=p.read_text()
p.write_text(text.replace('\n\n','\n\n> 이 문서는 최초 로컬 구현 단계의 기록입니다. 최신 반복 실행·브라우저·촬영 결과는 [PR 검증 기록](payment-release-verification-2026-09-30.md)을 따릅니다.\n\n',1))
print(json.dumps({'published_files':len(manifest['files']),'duration':report['edited_duration_seconds'],'scope':manifest['scope']},ensure_ascii=False))
