# 쉬운 시작 화면과 주문 보존 체험 검증

## 바뀐 화면

대표 프로젝트는 Pickup Pact다. 첫 화면에 조건과 2,800원 모의 금액을 밝힌 빠른 시작을 추가했고, 기존 상세 조건은 접었다. 진행 중에는 다음 행동과 같은 주문 번호·현재 매장·연결된 포인트·모의 결제 완료 건수를 함께 보여준다. 거절과 응답 누락을 별도로 체험하고, 서버가 확인 중이면 성공 화면으로 넘어가지 않는다. 변경 조건과 금액 동의는 기존 확인창을 사용한다.

진행 상태는 `guide-flow.js`가 서버 응답에서 읽는다. 기존 `store.py`, `durable_operations.py`, `merchant_fleet.py`, `merchant_http.py`, `runtime.py`, `recovery_worker.py`, `benefits.py`, `agreement.py`는 바꾸지 않았다. API 수정은 가이드 정적 파일 두 개의 허용 목록 추가다. 일반 주문 화면과 기존 JVM/Kafka/Docker 검증은 별도로 유지한다.

## 실제로 확인한 근거

- 원본 main: `2e80bd73b15ead521011424d778d00b5540bf08e`.
- 화면 변경 기능 브랜치 검증: `2fc77ae08247554957343e04f49db798d46867df`; PR 시험용 병합 커밋 `c048868fc2fd808952a66946525eb29c2bcbcfa7`를 실제 실행했다. PR 성공을 main 배포 성공으로 세지 않는다.
- [전체 제품 검사 36681288598](https://github.com/sokldjs554/pickup-pact/actions/runs/36681288598): Python 반복 검사, HTTP 복구, 가이드·기존 주문·혜택·변경 조건·문구의 실제 데스크톱/모바일 검사가 통과했다. 각 브라우저 묶음은 데스크톱/모바일 각각 3회다. 공개 검사는 PR에서 생략되므로 공개 성공의 근거가 아니다.
- [실제 녹화 검사 36681288764](https://github.com/sokldjs554/pickup-pact/actions/runs/36681288764): 별도 HTTP 앱과 독립 매장 저장소, 자동 복구를 켜고 가이드 6회와 녹화를 수행했다. 브라우저를 닫은 뒤에도 GET 조회만으로 복구된 주문을 확인하고 같은 주문으로 수령했다.
- 최종 모의 수취액 3,200원·완료 결제 1건·포인트 1,000P 사용·32P 적립을 검사했다. 요청 로그에 수동 `recover` 명령이 없다. 이는 가상 매장의 검사이며 실결제 성과가 아니다.
- 녹화 아티팩트 `11081832755`의 SHA-256: `6ffe17428a977946e242bf8ca86a14a8eb3c788be18a4b4e5b605e24237ae3a0`. 원본 ZIP의 CRC와 내부 미디어 해시를 다시 대조했다. 새 영상은 41.12초이며 MP4/GIF 모두 전체 디코딩 검사를 통과했다. 주요 장면과 모바일 첫 화면·복구 화면을 직접 열어 확인했다.

[촬영 메타데이터](media/guided/capture.json) · [브라우저 6회 결과](media/guided/browser-results.json) · [게시한 파일 해시](media/guided/publication.json)

## 실패도 남긴다

첫 적용 워크플로는 제한된 Actions 토큰으로 워크플로를 바꾸려다 거절됐다. 앱 변경과 연결된 저장소 권한으로 하는 워크플로 변경을 분리했고, 일회성 소스 적용 워크플로는 제거했다.

첫 녹화는 사용자 흐름 검사를 마친 뒤 절대 영상 경로와 상대 출력 경로를 섞어 보고서 생성에 실패했다. 그 경로 계산을 실제 코드에서 회귀 검사하여 수정 전 실패, 수정 후 통과를 확인했다. 원본 실패 실행 `36679473829`를 보존한다.

이전 전체 검사 `36679580196`에서는 두 번째 모바일 회차의 상단 매장 화면 클릭이 20초 제한 안에 끝나지 않았다. 그때 화면/trace가 없어서 원인을 단정하지 않는다. 클릭·금액 단언·시간 제한을 느슨하게 바꾸지 않고 실패 화면, UI 위치, 리소스 시각, 전체 trace 보존을 추가했다. 위 새 실행에서는 모든 문구 회차가 통과했지만 단 한 번의 원래 시간 초과 원인을 확정한 것으로 설명하지 않는다.

## 공개 배포는 별도 남은 항목

Render 워크스페이스 선택 승인을 기다리는 동안 기존 공개 앱의 설정과 데이터를 변경하지 않았다. 새 가이드의 공개 배포, 정확한 `/health.release_commit`, 공개 데스크톱/모바일 반복 검사는 아직 완료로 표시하지 않는다. 브라우저에서 기존 앱이 열리는 것만으로 새 버전 배포를 확인했다고 하지 않는다.

이전 39.72초 공개 영상은 보존했다. 새 41.12초 영상은 CI 실제 HTTP 서버의 녹화이며 공개 Render 녹화가 아니다. 다중 호스트 고가용성, DB 파일 유실, 실가맹점·실결제 연동을 이 영상으로 증명하지 않는다.

## 재현

```bash
python -m pip install -r requirements-workbench.txt playwright
python -m playwright install chromium
PYTHONPATH=.:services/reconciler python -m pytest -q tests/route services/reconciler/tests demo/test_demo.py demo/test_repair_integration.py
node scripts/verify_guided_stage.cjs
python -m uvicorn demo.main:app --host 127.0.0.1 --port 10000
# 별도 터미널
python scripts/verify_guided_browser.py --repeat 3
```

녹화에는 ffmpeg와 한국어 글꼴이 필요하다. `guided-media.yml`이 의존성 설치·실제 HTTP 실행·촬영·인코딩을 수행하고 결과와 시험한 소스를 아티팩트로 남긴다. 금액, 서버 상태, 기록된 SHA가 다르면 정상 영상으로 게시하지 않는다.
