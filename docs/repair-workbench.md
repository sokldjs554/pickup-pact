# Pickup Pact 취소·정산 복구 작업대

## 목적
기존 reconciler의 결과를 읽기만 하는 보고서가 아니라, **저장된 증거 → 서버 판정 → 승인 계획 → 모의 조치 원장**을 직접 사용하는 검증 화면이다. 기존 프로젝트를 유지한다. 실제 결제·환불·실물 POS·PG·Kafka 호출은 하지 않는다.

`python -m app.workbench --db .repair-review/review.sqlite --port 8765`로 로컬 실행한다. 먼저 `pip install -r requirements-workbench.txt`를 실행하고 `PYTHONPATH`를 저장소 루트와 `services/reconciler`로 설정한다. Windows PowerShell은 `$env:PYTHONPATH=".;services/reconciler"`, Linux는 `PYTHONPATH=.:services/reconciler`다. 접속 주소는 `http://127.0.0.1:8765/repair-lab`이다. 서버는 기본적으로 루프백에만 바인딩한다.

## 실제 사용 흐름
1. 취소 전달 지연 샘플을 선택한다. 엔진이 정산 9,000 KRW와 주문 적립 90 PTS를 구분한다.
2. 계획 조회에서 전표 ID, 금액, 단위와 증거 버전을 확인한다.
3. 승인 후 모의 기록을 선택한다. 동일 DB 트랜잭션 안에서 승인 계획, 모의 조치, 모의 역분개 증거를 기록한다.
4. 동일 승인 재전송은 기존 기록만 반환한다. 서버와 브라우저를 다시 열어도 DB와 샘플 ID가 남는다.
5. 누락 증거 샘플은 WAIT_FOR_EVIDENCE 상태에서 시작한다. 누락 증거를 받은 다음에만 계획을 생성한다.
6. 충돌 증거, 취소·수령 모순, 부분취소, 복수 전표는 계획과 조치를 차단한다.

## 구현한 안전 경계
- 입력 검증: 시간대 없는 시각과 잘못된 제조량은 HTTP 422. 미래 schema와 불명확한 금융 값은 자동 조치 금지.
- 범위 제한: 전체취소·단일 정산/적립 전표만 지원. 부분취소, 복수 전표, 다른 목적의 보상 포인트 회수는 검토 대상이다.
- 계획 결합: 승인 내용은 서버에 저장한 계획의 전표 ID·금액·단위·증거 해시·버전과 같아야 한다.
- 버전 검사: 계획 생성 뒤 새 증거가 저장되면 이전 승인은 409. `BEGIN IMMEDIATE`로 같은 DB에서 검사와 기록을 직렬화한다.
- 중복 방지: session + target event + repair unique key. 동일 승인 24개 동시 요청에서 한 실행만 기록한다.
- 내구성: SQL transaction으로 evidence, plan status, effects가 함께 커밋된다. 강제 종료 후 재시도한다.
- UI: 고정된 합성 샘플만 사용, 외부 결제 연동 없음, DOM 데이터는 textContent로 렌더링. 페이지는 no-store/CSP, standalone app은 외부 Origin 변경 요청을 거절한다.

## 테스트
```
PYTHONPATH=.:services/reconciler python -m pytest -q services/reconciler/tests
PYTHONPATH=.:services/reconciler python -m scripts.repair_audit.run --output comparison.json
PYTHONPATH=.:services/reconciler python -m scripts.repair_audit.verify_workbench_browser --repeat 3
```
브라우저 검증에는 `pip install playwright` 및 `playwright install chromium`이 추가로 필요하다. 기본 Playwright 브라우저 대신 시스템 브라우저를 쓰려면 `REPAIR_BROWSER_PATH`를 설정한다.

## 판정/비교 결과의 의미
13개 기존 합성 상황 × 5개 입력 순서의 비교에서는 원본 25/65, 기준 구현과 수정본 각각 65/65가 유지된다. 기준 대비 우위는 입증되지 않았다. 신규 scope/store/UI 테스트는 이 65회와 별도의 회귀 검증이다. 65회는 65종 실운영 사고가 아니며 실제 돈이 이동한 횟수도 아니다.

## 검증 범위와 남은 경계
로컬에서는 복원한 reconciler 부분집합을 실행했다. 전체 clone은 DNS 제한으로 실패했다. 기존 JVM·Kafka·PostgreSQL·고객/점주 데모는 새 GitHub 전체 CI에서 따로 확인한다.

이 환경의 Chromium은 실제 HTTP 탐색을 ERR_BLOCKED_BY_ADMINISTRATOR로 차단했다. `set_content`나 가짜 fetch로 성공을 대체하지 않는다. 실제 브라우저 회귀는 새 GitHub CI 결과로만 통과 여부를 판단한다.

이 작업대는 SQLite 기반 **합성 승인 기록**이다. 실제 금융서비스의 인증·권한·결제대행사, 취소와 제조 시작의 단일 승인 권한, 부분환불·다중 전표 귀속, 분산 저장소 간 전역 버전 펜스는 완료된 것으로 주장하지 않는다. 이후 도착할 모든 사실의 완전성을 증명하는 장치도 아니다. DB상의 현재 증거를 고정해 조치 내용이 바뀌지 않도록 하는 검증이다.

## 구버전 테스트 변경 이유
기존 causal-order 테스트 중 금액이 생략된 전표 4개는 정상 전표 금액을 제공하도록 fixture를 강화했다. 인과관계 관련 기대 단언은 그대로 유지했다. 금액 누락 시 차단해야 한다는 새 독립 회귀 테스트를 추가했다. 이전 자동 허용을 되살려서 테스트를 맞추지 않았다.
