# 페이타랩 Backend 공고와 구현 대조

확인일: 2026-09-26. 공식 공고: https://recruit.passorder.co.kr/c/XZ4WHRTjx8?back=true
지원 대상은 페이타랩(패스오더)의 Backend Developer 공고다. 기술 이름의 유무가 아니라 실제 실행 범위로 구분한다. 개발환경 목록 전체를 실제로 운영했다는 뜻으로 읽히지 않도록 한다.

## 현재 소개할 제품

대표 화면은 `/`와 `/go`의 일정 중심 커피 주문이다. 목적지 마감·음료·예산으로 매장을 고르고, 제조 전 동의 아래 같은 주문을 다른 매장으로 이어간다. 핵심은 새 매장이 거절하면 원래 주문·혜택을 유지하고, 수락 뒤 응답이 끊겨도 서버가 저장된 단계와 매장 처리 기록을 다시 확인한다는 점이다. 쿠폰·포인트는 보류·복원·확정을 같은 주문에 연결한다.

`/classic`은 이전 고객·매장 화면이며 기술 화면은 `/classic?dev=1`이다. `/repair-lab`은 고정 합성 증거를 사용하는 별도의 취소·정산 확인 화면이다. 모두 같은 앱에 있지만 같은 주문 저장소나 하나의 Kafka 처리 흐름은 아니다.

## 공고 역량별 근거와 한계

| 공고의 요구 | 확인할 구현·증거 | 설명할 때 지킬 범위 |
|---|---|---|
| 고객 문제와 도메인 모델링 | `demo/route/planner.py`, `durable_operations.py`, `benefits.py`; 도착 마감, 변경 거절 시 주문 보존, 혜택 보류/복원/확정 | 고객 인터뷰나 실매출 개선이 아니라 개인 제품 제안 |
| REST·OpenAPI | `demo/route/api.py`, `contracts/route-benefits.openapi.json`; 상태 버전·견적·요청키·입력과 오류 검증 | 일부 응답은 범용 객체이며 모든 응답이 세분화된 타입 모델은 아님 |
| 정합성·재시도 | `merchant_fleet.py`, `durable_operations.py`, `recovery_worker.py`, `tests/route/test_transfer_durability.py` | 주문 DB와 매장별 독립 DB, 저장된 단계와 자동 재확인. 글로벌 원자적 트랜잭션을 주장하지 않음 |
| 실제 HTTP·복구 | `merchant_http.py`, `runtime.py`, `test_merchant_http.py`, `test_runtime_recovery.py` | 한 호스트의 별도 프로세스, 실제 소켓 통신·매장 재시작. 실가맹점·멀티 호스트 고가용성 운영은 아님 |
| 원인 추적·검증 | `test_final_input_audit.py`, `RescheduleAdmissionTest.kt`, 공개 비교 응답·DOM 추적 | 유니코드 오류, 예약 변경 순서, 공개 7.8초 응답과 화면 잠금의 원인을 나누어 재현·수정 |
| AI 활용 및 반복 개선 | 실제 ChatGPT 협업, PR의 반례·수정·회귀 기록, `docs/ai-iteration-log.md` | 본인의 요구·판단과 AI의 구현·검증 도움을 구분. 모든 코드를 수작업으로 썼거나 생산성 배수를 측정했다고 쓰지 않음 |
| Kotlin·Java·Spring·WebFlux | `services/commitment-service`, `merchant-fulfillment-service`, `ledger-service` | 별도 JVM/Docker 구성. 공개 새 주문 UI가 이 전체 서비스를 호출하지 않음 |
| Python·FastAPI·Flask | 공개 제품, reconciler, ops-console | 각각의 테스트와 실행 기록을 구분 |
| DDD·EDA·MSA·Kafka·CQRS | 기존 commitment/merchant/ledger 경계, outbox/inbox, PostgreSQL projection fence, `scripts/integration_smoke.py` | 이벤트 전달·취소 권위·원전표 잔액을 Docker에서 검증. 새 공개 매장 이동은 Kafka 경로가 아님 |
| PostgreSQL·SQL 실행계획 | `scripts/capture_postgres_plan.py`, `sql/explain/`, release-gate 산출물 | 합성 DB의 EXPLAIN (ANALYZE, BUFFERS), 인덱스와 강제 순차조회 비교. 실트래픽 성능 수치가 아님 |
| Redis·MongoDB·Elasticsearch·Celery | 예약 용량 lease, 전달 증거 보관, 검색 어댑터, 비동기 재생 | 별도 topology/어댑터 범위이며 새 공개 주문 저장소가 아님 |
| Docker·AWS·Kubernetes·Jenkins | Docker 빌드/전체 실행, Terraform validation, K8s/Jenkins 파일 | Docker 실행과 정적 검증을 AWS·EKS·Jenkins 운영 경력으로 바꾸지 않음 |
| Datadog·Elastic APM | 계측 설정·모니터 템플릿 | 외부 계정에서 장애를 운영·대응한 근거 없음 |
| Claude Code·Cursor·Claude·Gemini·n8n·Make | 규칙·프롬프트·자동화 템플릿 | 파일 존재가 실제 도구 사용·운영을 증명하지 않음 |
| Slack·Jira·Notion·스쿼드 협업 | handoff/sprint 문서와 선택적 자동화 예시 | 개인 프로젝트이며 실무 팀 협업이나 조직 프로세스 개선 경험을 만들어 넣지 않음 |

## 지원서에서 앞에 둘 이야기

“매장을 바꾸다 실패했다고 원래 주문까지 사라져서는 안 된다고 생각했다. 새 매장의 거절과 응답 누락을 구분하고, 이미 저장된 변경 결정을 따라 같은 작업을 이어가도록 만들었다. 확인 중에는 제조를 잠시 멈출 수 있지만 중복 제조나 새 결제로 해결하지 않았다.”

그다음 쿠폰·포인트와 금액 변경 동의, 마지막 자리 경쟁, 강제 종료 뒤 재개를 설명한다. 기술 스택 전체를 첫 문단에 나열하지 않는다. 유지·취소 후 재주문·같은 주문 변경의 6개 상황 실행 비교와 기존 360건 합성 도착 시간 실험도 구분한다. 정상·응답 재시도에서 같았던 결과를 포함하며, 경쟁 앱의 비공개 구현이나 다른 지원자 전체를 직접 평가한 것은 아니다.

공식 공고는 AI 활용 경험과 깊이 파고든 기술 문제 또는 팀 프로세스 개선 경험에 대한 답변을 요청한다. 여기서는 개인 프로젝트에서 확인된 AI 협업과 기술 문제 해결을 선택한다. 운영·팀 경험을 만들어 넣지 않는다.

## 공개 반영과 검증 기준

앱 `000a106cc5fda6a3be595a83f938087096c3ad63`에서 9개 main workflow와 같은 SHA의 공개 브라우저 검증을 확인했다. `route-product` 실행 `36158962973`의 복구·주문·혜택·문구 각 6회, 합계 24회는 자동 복구·실제 HTTP 매장 실행이 켜진 배포를 대상으로 했다. 이후 미디어 전용 변경은 앱 파일이 같은지 대조하고 새 main의 검증은 따로 확인한다.

[앱·촬영 원본과 실패 기록](recovery-release-verification-2026-09-26.md). PR의 성공을 main 성공으로, Docker의 성공을 공개 UI 성공으로 대신하지 않는다. 로컬에서 실행하지 못한 Maven/Docker/Chromium은 로컬 통과로 세지 않는다.

`recovery_worker.py`는 예정 시각·기한·재시도 횟수를 저장한다. 결정 전 기한 경과와 결정 후 인계를 나누며, 한도 초과 작업은 추가 확인 대상으로 남긴다. 무기한 장애·호스트 DB 유실까지 자동 해결하지 않는다. 쿠폰·포인트는 일정별 모의 지갑이다.

과거 기록: [기존 공고 대조](jd-audit-2026-09-22.md), [제출 전 오류 감사](final-application-audit-2026-09-24.md). 상세 범위: [매장 변경과 복구](transfer-recovery.md), [혜택과 실험](benefits-and-outcomes.md), [취소·정산 작업대](repair-workbench.md).
