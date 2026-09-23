# Pickup Pact

**주문 취소·정산 증거가 늦게 오거나 서로 모순될 때, 자동 복구를 허용할지·기다릴지·차단할지 판단하고 승인된 전표만 모의 원장에 기록하는 백엔드 검증 프로젝트입니다.**

[취소·정산 복구 작업대](https://pickup-pact-demo.onrender.com/repair-lab) · [기존 주문·매장 데모](https://pickup-pact-demo.onrender.com) · [실행·검증 방법](docs/repair-workbench.md)

공개 주소의 현재 배포 커밋은 `/health`의 `release_commit`으로 확인합니다. PR 브랜치 변경은 병합·배포 전까지 공개 화면에 반영되지 않습니다.

## 먼저 확인할 범위

**실제 결제·환불을 실행하지 않습니다.** 공개 화면은 고정된 합성 주문을 사용하는 검증 환경입니다. 패스오더 운영 데이터·비공개 아키텍처·실제 지연 보상 정책을 재현했다고 주장하지 않습니다.

예약 픽업·제조 시점 관리·적립 기능 자체는 이미 상용 서비스에 있습니다. 이 저장소는 해당 기능의 시장 독창성이나 다른 지원자보다 우수함을 주장하지 않습니다. 무엇을 재현했고 어디서 잘못 판단했으며 어떤 조건에서 자동 처리를 거부하는지를 코드와 반복 실행 결과로 보여줍니다.

## 실제로 눌러보는 흐름

복구 작업대에서 **취소 전달 지연 → 계획 조회 → 전표·금액·단위 확인 → 승인 후 모의 기록 → 동일 승인 재전송**을 실행합니다.

정산 9,000 KRW와 주문 적립 90 PTS는 서로 다른 전표입니다. 승인 시점의 증거 버전·해시와 서버에 저장한 계획이 맞을 때만 두 모의 조치를 기록합니다. 승인 뒤 응답을 잃어 같은 요청을 보내도 기존 결과를 반환합니다.

| 샘플 상황 | 처리 |
|---|---|
| 확정 취소 뒤 정산·주문 적립 | 지원 범위와 증거가 맞으면 복구 계획 생성 |
| 원인 이벤트가 아직 미도착 | `WAIT_FOR_EVIDENCE`, 필요한 ID를 보여주고 조치 금지 |
| 같은 정산 ID에 다른 금액 | `MANUAL_REVIEW`, 충돌한 증거를 보존하고 조치 금지 |
| 취소 확정과 수령 완료가 모두 존재 | 시각으로 승자를 정하지 않고 검토 |
| 일부 취소·복수 정산 전표 | 금액 귀속을 추정하지 않고 자동 처리 금지 |
| 계획 조회 후 새 충돌 증거 도착 | 과거 승인 요청을 HTTP 409로 거절 |
| 동일 승인 동시·반복 요청 | 같은 전표의 모의 조치는 한 번만 기록 |

현재 복구 정책은 **전체 취소·단일 정산/주문 적립 전표**에 한정됩니다. 부분환불과 여러 전표의 배분을 구현한 척하지 않고 명시적으로 차단합니다. 보상 성격의 포인트도 주문 적립과 같다고 가정해 자동 회수하지 않습니다.

## 구현의 경계

- **증거 판정:** 기존 `services/reconciler/app/engine.py`를 사용합니다. 명시적인 원인 이벤트 ID를 우선하며, 누락·순환·내용 충돌은 자동 조치를 막습니다. 서로 무관한 생산자의 임의 시계 오차까지 해결하지는 않습니다.
- **금액·단위:** 부정확한 금액, KRW/PTS 혼동, 부분 역분개, 잘못된 원본 전표 참조를 차단합니다.
- **승인 계획:** 서버가 전표 ID·금액·단위·증거 해시·버전을 저장합니다. 클라이언트의 금액 수정이나 오래된 계획은 승인되지 않습니다.
- **내구성:** SQLite 트랜잭션 안에서 계획 적용·모의 조치·모의 역분개 증거를 함께 커밋합니다. 같은 저장소 안의 버전 검사이며 분산 시스템 전체의 전역 버전 제어는 아닙니다.
- **조회용 모델:** 미해결 증거는 기존 projection 재생성 API와 저장 함수 모두에서 거절됩니다. 기존 PostgreSQL projection의 오래된 성공 결과 덮어쓰기 방지까지 구현한 것은 아닙니다.

## 기존 시스템과의 관계

기존 코드도 유지합니다. Kotlin/Spring WebFlux의 signed quote·capacity HOLD, Java/Spring의 merchant fulfillment와 ledger, PostgreSQL/Redis/Kafka, Python/FastAPI reconciler, Flask 운영 콘솔, CQRS 재생, SQL 실행계획 검증이 있습니다.

공개 Render는 이 전체 Docker 구성을 실행하지 않습니다. 기존 주문·점주 화면은 세션별 메모리 모의 환경이고, 새 복구 작업대는 SQLite의 고정 샘플·모의 기록을 사용합니다. **`POS_PRINT`·알림은 중복 없는 의도 기록을 검증한 것이며 실제 프린터 출력·알림 발송의 정확히 한 번 실행을 증명한 것이 아닙니다.**

`demo/main.py`는 공개 앱을 조합합니다. 기존 고객·점주 구현은 내용 변경 없이 `demo/customer_app.py`로 분리했습니다. 기존 실행 명령 `uvicorn demo.main:app`도 새 `/repair-lab`을 제공합니다.

## 같은 조건의 비교

원본 `801755bb`, 별도 보수적 기준 구현, 수정한 실제 엔진에 **13개 합성 상황 × 입력 순서 5가지**를 적용했습니다.

| 지표 | 원본 | 보수적 기준 | 수정 엔진 |
|---|---:|---:|---:|
| 기대 판정·조치 집합 일치 | 25/65 | 65/65 | 65/65 |
| 허용되지 않은 금융 조치 제안 | 30/65 | 0/65 | 0/65 |
| 필요한 조치 누락 | 5/65 | 0/65 | 0/65 |

**기준과 수정본은 동률입니다.** 65종 운영 사고나 실제 환불 피해율이 아니라 13종의 입력 순서를 바꾼 판정 실험입니다. 신규 금액·승인·API 회귀 테스트는 이 표와 별도입니다. 비교 실행기는 원본 파일의 Git blob hash와 기대 판정도 검사합니다.

## 실행

Python 3.12 이상을 사용합니다. 실제 API 키가 필요하지 않습니다.

```bash
python -m pip install -r requirements-workbench.txt
PYTHONPATH=.:services/reconciler python -m app.workbench --db .repair-review/review.sqlite --port 8765
```

브라우저에서 `http://127.0.0.1:8765/repair-lab`을 엽니다. Windows PowerShell은 `./run-workbench.ps1`, Linux는 `./run-workbench.sh`도 사용할 수 있습니다.

기존 주문·매장 화면까지 함께 실행하려면:

```bash
python -m pip install -r demo/requirements.txt
python -m uvicorn demo.main:app --host 127.0.0.1 --port 10000
```

로컬에서 샘플 DB를 유지하려면 `REPAIR_REVIEW_DB`를 지정합니다. 공개 무료 Render의 `/tmp` DB는 재배포·인스턴스 교체 후 사라질 수 있습니다. 영구 저장 서비스라고 설명하지 않습니다.

## 검증

```bash
PYTHONPATH=.:services/reconciler python -m pytest -q services/reconciler/tests demo/test_demo.py demo/test_repair_integration.py
PYTHONPATH=.:services/reconciler python -m scripts.repair_audit.run --output comparison.json
python -m pip install playwright
python -m playwright install chromium
PYTHONPATH=.:services/reconciler python -m scripts.repair_audit.verify_workbench_browser --app-entry demo.main:app --repeat 3
mvn -B -DskipTests=false test
```

`repair-workbench` CI는 전체 Python 테스트와 비교를 세 번 반복하고, 독립 실행과 통합 실행 각각 데스크톱·모바일을 실제 HTTP로 검증합니다. 코드 원본 ZIP, 커밋 ID, 로그·결과 JSON·캡처를 같은 실행의 artifact로 보관합니다. `ci`와 `release-gate`는 JVM·Docker topology·SQL·설정 검증을 별도로 수행합니다. 합성 승인 기록의 강제 종료 시험을 Kafka/실제 금융 장애 검증으로 대체해 설명하지 않습니다.

## 공고와 연결되는 증거

| 공고 역량 | 확인할 코드·문서 |
|---|---|
| 복잡한 비즈니스 모델링 | 취소 요청과 확정 사실 구분, 금액·단위·전표 귀속 제한, `financial_scope.py` |
| REST/OpenAPI | 구체적인 요청·응답 모델, 403/404/409/415/422, `test_contract_consistency.py` |
| 데이터 정합성·재시도 | `repair_review_store.py`, 동시 승인·응답 손실·강제 종료 시험 |
| Kafka/DDD/CQRS | 기존 commitment/merchant/ledger 서비스와 전체 Docker 검증 |
| SQL 실행계획 | `scripts/capture_postgres_plan.py`, 합성 PostgreSQL 실행계획과 인덱스 비교 |
| AI 결과의 반복 개선 | 기존 통과 테스트에 반례 추가, 실패 관찰, 수정·회귀 기록, `docs/ai-iteration-log.md` |

실제 AWS·Datadog·ElasticAPM·n8n/Make 운영, PM/디자이너와의 팀 협업, 실물 POS·PG 실행은 문서·설정만으로 실무 경험이라고 주장하지 않습니다.

자세한 범위: [복구 작업대](docs/repair-workbench.md) · [최초 반례와 비교](docs/repair-evidence-audit.md) · [아키텍처](docs/architecture.md) · [공고 대조](docs/jd-traceability.md)
