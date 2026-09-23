# 주문 취소·정산 증거 검증 — 실제 수정 및 비교 기록

기준 원본: `sokldjs554/pickup-pact@801755bb422cd50ecbbf2fa6c0e275ff97fa9e75`.
수정 범위: 기존 Python reconciler의 증거 판정 및 projection 쓰기 차단. 새로운 앱이나 사업 아이디어를 추가하지 않는다.

## 먼저 재현한 문제

기존 엔진 테스트 8개는 그대로 통과했다. 그러나 새 증거 안전성 테스트 13개 중 10개는 원본에서 실패했다. 동일 event_id의 금융 내용이 충돌하거나 취소 확정과 수령 완료가 모순되는 경우에도 자동 역분개 지시가 반환됐고, 누락된 causation_id와 인과관계에 반하는 시계 순서도 처리하지 못했다. 도착만 늦은 결제 증거를 영구적인 미결제로 오판하는 경우도 있었다.

이는 합성 반례의 **조치 제안** 결과다. 실제 결제 사고나 실제 환불 건수가 아니다. 새 테스트에는 기존 버전에 없던 WAIT_FOR_EVIDENCE 판정 요구도 포함되어 있으므로 10개의 실패를 10개의 독립 운영 장애라고 해석하면 안 된다.

## 수정한 동작

- AUTO / WAIT_FOR_EVIDENCE / MANUAL_REVIEW를 구분한다.
- 동일 ID의 내용 충돌, 취소 확정·수령 완료의 모순, 인과관계 순환은 검토로 보낸다.
- 원인 이벤트가 누락된 경우 자동 처리하지 않고 필요한 ID를 반환한다. 증거가 도착하면 다시 평가한다.
- 명시적인 원인-결과 관계는 이벤트 시각보다 우선한다. 관계가 없는 사건의 시각 정렬은 기존 정책의 가정을 유지하며, 동일 시각의 금융 선후 관계는 임의 우선순위로 확정하지 않는다.
- 검토·대기 결과에는 실행 가능한 역분개나 projection 재생성 지시를 함께 넣지 않는다.
- projection 재생성 API는 미해결 증거에 HTTP 409를 반환하고, persistence 함수도 DB 연결 전에 다시 거부한다.
- 과거 클라이언트를 위해 repairs의 MANUAL_REVIEW 표시는 유지한다. 새 decision 값으로 대기와 검토를 구별한다. 차단된 canonical_state는 UNRESOLVED이며 금액 변경의 근거가 아니다.

## 비교 대상과 공정성

1. 원본 엔진: 위 커밋의 실제 engine.py. Git blob SHA `9279781d92a8f2db270f2cce734e2df7a90d5b41`를 실행 전에 확인한다.
2. 보수적인 기준 구현: 이번에 별도로 작성했다. 경쟁 앱이나 다른 지원자의 코드를 대변하지 않는다. 중복 ID 검사, 증거 누락 대기, 모순 격리, 인과관계 기반 재생을 갖춘다. candidate의 함수나 기대 정답을 호출하지 않는다.
3. 수정 엔진: 실제 app.engine.reconcile 함수.

정답은 cases.py에 별도로 선언했다. 동일한 13개 시나리오를 5가지 입력 순서로 실행한다. 65개는 독립적인 운영 사고 65종이 아니라 13종 × 5회다. 원본의 단순 received-order 진단 화면을 경쟁 기준으로 사용하지 않는다.

| 판정 기준 | 원본 엔진 | 보수적 기준 구현 | 수정 엔진 |
|---|---:|---:|---:|
| 기대 판정·금융 조치 집합 일치 | 25/65 | 65/65 | 65/65 |
| 허용되지 않은 금융 조치 제안이 포함된 입력 | 30/65 | 0/65 | 0/65 |
| 필요한 금융 조치를 누락한 입력 | 5/65 | 0/65 | 0/65 |

**수정본은 기준 구현보다 우수하지 않았다. 동률이다.** 이번 실험은 기존 코드의 안전성 결함을 찾아 고친 증거이지, 시장 독창성이나 다른 지원자 대비 우월함의 증거가 아니다. 기존 원본이 MANUAL_REVIEW와 역분개를 함께 제안한 경우도 위험 제안으로 센다. 실제 실행기가 검토 표시를 우선하면 조치를 차단할 수 있으므로 이를 실제 금융 피해로 바꾸어 표현하면 안 된다.

## 장애·재시작 검증

두 구현에 같은 SQLite 기반 시험용 실행기를 적용했다. inbox와 조치 의도(intent)를 한 트랜잭션에 기록하고, 주문·조치 종류의 unique key로 중복을 막는다. 다음 지점에서 별도 Python 프로세스를 os._exit(77)로 강제 종료한 뒤 DB를 다시 열고 같은 요청을 재전송했다.

- inbox 저장 직후 / commit 전 / commit 후 응답 전.
- 동일 요청 24개를 8개 worker로 동시에 재전달.
- 증거 누락 상태를 저장하고 재시작한 뒤 누락 이벤트를 제공.
- 취소·수령 모순에서 금융 조치 의도가 생성되지 않는지 확인.

38개 로컬 테스트를 세 번 실행해 모두 통과했다. 비교 결과의 비시간 필드 SHA-256도 세 번 같았다: `98baa2f7c4a0635b7d080a4e4845f1f0d0e2ce2b3465efc9fdb949e9df542d36`.

이 실행기는 로컬 내구성 검증 도구다. 실제 결제대행사, 실물 POS, Kafka, PostgreSQL의 실행 결과라고 주장하지 않는다. planner_time_us는 원시 참고값이며 서비스 p95/TPS 비교로 사용하지 않는다.

## 실행

Linux/macOS:

```bash
PYTHONPATH=.:services/reconciler python -m pytest -q services/reconciler/tests
PYTHONPATH=.:services/reconciler python -m scripts.repair_audit.run
```

Windows PowerShell:

```powershell
$env:PYTHONPATH = ".;services/reconciler"
python -m pytest -q services/reconciler/tests
python -m scripts.repair_audit.run
```

## 이번에 검증하지 않은 것 / 병합 전 확인할 것

- 이 작업 환경에는 Maven·Docker 및 외부 네트워크 실행 환경이 없다. 전체 JVM/Kafka/PostgreSQL/Render 회귀 검증을 했다고 말하지 않는다.
- 새 causation_id 규칙은 원인 **이벤트 ID**를 전제로 한다. 기존 producer가 명령 ID나 패킷 외 ID를 넣는 경우를 계약 검토하고 이행해야 한다.
- 인과관계 없는 서로 다른 시스템의 임의 시계 오차를 해결하지 않는다.
- 취소 요청 승인 권한, 실제 제조 시작과 취소의 단일 승인 경쟁, partial refund 및 다중 금융 posting의 금액별 귀속, 오래된 재생 결과의 version-fenced 저장은 이번 패치 범위 밖이다.
- FastAPI 생성 스키마는 새 필드를 노출하지만 수동 통합 OpenAPI 명세와 모든 소비자 호환성은 전체 저장소에서 다시 대조해야 한다.
- 기존 공개 데모와 main을 자동으로 변경하지 않는다. 검토용 Draft PR로 유지한다.

## 기준 구현 선택에 참고한 공식 문서

AWS Transactional outbox: 중복 전달을 전제로 한 소비자 멱등성·트랜잭션 경계.
https://docs.aws.amazon.com/en_en/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html

SQLite Atomic Commit: 로컬 트랜잭션 내구성 검증 범위.
https://www.sqlite.org/atomiccommit.html

## 후속 구현 — 복구 작업대

위 로컬 38개 검증과 검토용 브랜치 상태는 최초 감사 시점의 기록이다. 후속 작업은 [복구 작업대 문서](repair-workbench.md)와 해당 커밋의 CI artifact를 기준으로 확인한다. 전체 소스를 CI archive로 확보한 뒤 기존 고객·점주 데모와 통합했고, 원격 Chromium의 실제 HTTP 실행도 추가했다. 최초 로컬 환경 제약이나 테스트 수를 현재 최종 상태로 오해하지 않는다.

부분취소·복수 전표 등은 안전하게 차단하는 범위이며, 실제 금액 배분 정책을 구현했다는 뜻이 아니다. SQLite의 승인 버전 검사와 기존 PostgreSQL projection의 전역/오래된 결과 차단도 구분한다.
