"""Copy only the already reviewed media from a digest-pinned CI artifact."""
from pathlib import Path
import hashlib
import json
import shutil
import sys
import zipfile

root = Path(sys.argv[1]).resolve()
repo = Path.cwd()
report = json.loads((root/'media/capture.json').read_text())
assert report['recorded_release_commit'] == 'c048868fc2fd808952a66946525eb29c2bcbcfa7'
assert report['recording_scope'] == 'ci_local_http'
assert report['edited_duration_seconds'] == 41.12
assert report['capture_count'] == 1 and report['cash_due'] == 3200
assert report['points_spent'] == 1000 and report['points_earned'] == 32
assert not report['page_errors']
for name, digest in report['media_sha256'].items():
    assert '/' not in name and '\\' not in name
    assert hashlib.sha256((root/'media'/name).read_bytes()).hexdigest() == digest, name
rows = json.loads((root/'browser/results.json').read_text())
assert len(rows) == 6 and all(r['result']=='passed' and r['recovery_without_page'] and r['manual_recover_requests']==0 for r in rows)
with zipfile.ZipFile(root/'tested-source.zip') as archive:
    for name in archive.namelist():
        if not name.endswith('/') and name.startswith(('demo/', 'services/', 'contracts/')):
            assert (repo/name).read_bytes() == archive.read(name), name
out = repo/'docs/media/guided'
assert not out.exists(), 'Do not replace previously published media silently.'
out.mkdir(parents=True)
for name in ['pickup-pact-guided.mp4','pickup-pact-guided.gif','capture.json','01-home.png','04-rejected.png','05-pending.png','06-recovered.png','08-receipt.png']:
    shutil.copy2(root/'media'/name, out/name)
for source, target in [('mobile-01-home.png','mobile-home.png'),('mobile-04-recovered.png','mobile-recovered.png')]:
    shutil.copy2(root/'browser'/source, out/target)
raw = Path(report['source_video'])
assert not raw.is_absolute() and '..' not in raw.parts and raw.parts[0] == 'raw'
(out/raw).parent.mkdir(parents=True,exist_ok=True)
shutil.copy2(root/'media'/raw,out/raw)
shutil.copy2(root/'browser/results.json',out/'browser-results.json')
manifest = {'artifact_id':11081832755,'run_id':36681288764,
    'artifact_sha256':'6ffe17428a977946e242bf8ca86a14a8eb3c788be18a4b4e5b605e24237ae3a0',
    'recorded_commit':report['recorded_release_commit'],'feature_commit':'2fc77ae08247554957343e04f49db798d46867df',
    'scope':'CI actual HTTP server, not public Render deployment',
    'files':{str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*') if p.is_file()}}
(out/'publication.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
p = repo/'README.md';text=p.read_text()
start=text.index('**아래 39.72초 영상은 이전 앱의 자동 복구 녹화입니다.**')
end=text.index('## 고객이 직접 해보는 흐름',start)
text=text[:start]+'''## 새 첫 화면과 41초 체험 영상

**[41.12초 새 체험 영상](docs/media/guided/pickup-pact-guided.mp4)** · [촬영 원본·장면·검증 기록](docs/media/guided/capture.json) · [이번 변경의 검증 범위](docs/guided-demo-verification-2026-09-30.md)

[![설정 없이 시작하는 주문 보존 체험](docs/media/guided/pickup-pact-guided.gif)](docs/media/guided/pickup-pact-guided.mp4)

첫 화면에서 **이 조건으로 체험 시작**을 누르면 웨이브 라떼 1잔에 500원 쿠폰과 1,000P를 적용합니다. 다음 행동은 한 번에 하나씩 안내합니다.

**주문 → 매장 혼잡 → 새 매장 거절 → 응답 끊김 → 서버 자동 확인 → 수령·영수증**

금액이 2,800원에서 3,200원으로 바뀌는 이유와 변경 조건은 확인창에서 직접 동의합니다. 시간·음료·예산·혜택을 따로 고르는 기존 설정도 **내 조건으로 고르기**에서 사용할 수 있습니다.

**이 영상은 CI의 실제 HTTP 서버에서 녹화했습니다. 공개 Render 서버의 새 버전 배포를 증명하는 영상은 아닙니다.** 주문·매장별 독립 DB와 자동 복구 작업자를 사용했으며, 서버 응답을 가짜 성공으로 바꾸거나 화면 속 주문·금액을 편집하지 않았습니다. 장면은 정상 속도이고 장면 사이 대기만 덜었습니다. 제조는 화면에 표시한 체험 시계를 앞당겼습니다. 가상 매장·모의 결제이며 실제 카드 결제는 없습니다.

| 쉬운 첫 화면 | 거절 후 주문 보존 | 서버 자동 확인 후 복구 |
|---|---|---|
| ![빠른 시작과 접힌 상세 설정](docs/media/guided/01-home.png) | ![원래 주문과 혜택 유지](docs/media/guided/04-rejected.png) | ![같은 주문으로 새 매장 연결](docs/media/guided/06-recovered.png) |

<details>
<summary>모바일 첫 화면·복구 화면과 수령 영수증</summary>

<img src="docs/media/guided/mobile-home.png" alt="모바일 빠른 시작" width="280"> <img src="docs/media/guided/mobile-recovered.png" alt="모바일 서버 복구 후 주문" width="280">

![수령 후 한 번만 기록된 모의 결제](docs/media/guided/08-receipt.png)

</details>

**공개 반영 상태:** 이 가이드의 공개 배포와 동일 SHA 검증은 아직 완료로 표시하지 않습니다. 기존 39.72초 영상은 [이전 녹화](docs/media/pickup-pact-demo.mp4)로 보존하며 새 화면의 영상과 구분합니다.

'''+text[end:]
text=text.replace('## 고객이 직접 해보는 흐름\n','## 고객이 직접 해보는 흐름\n\n빠른 체험은 첫 화면의 시작 버튼과 상단 안내를 따라갑니다. 아래는 상세 조건을 직접 고르는 방법입니다.\n',1)
p.write_text(text)
print(json.dumps({'media_files':len(manifest['files']),'duration':41.12,'scope':manifest['scope']},ensure_ascii=False))
