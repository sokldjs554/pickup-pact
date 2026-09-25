# 페이타랩 Backend 공고와 구현 대조

확인일: 2026-09-25. 공식 공고: https://recruit.passorder.co.kr/c/XZ4WHRTjx8?back=true
지원 대상은 페이타랩(패스오더)의 Backend Developer 공고다. 기술 이름의 유무가 아니라 실제 실행 범위로 구분한다. 채용 목록의 모든 도구를 운영해 봤다는 뜻으로 읽히지 않도록 한다.

## 현재 소개할 제품

대표 화면은 `/`와 `/go`의 일정 중심 커피 주문이다. 목적지 마감·음료·예산으로 경로를 고르고, 제조 전 동의 아래 같은 주문을 다른 매장으로 옮긴다. 쿠폰 상실과 최종 차액을 표시하고, 포인트 보류·취소 복원·수령 확정을 같은 주문에 연결한다. 유지/이동 정책의 합성 비교도 제공한다.

`/classic`은 이전 고객·점주 화면이며 기술 화면은 `/classic?dev=1`이다. `/repair-lab`은 고정된 합성 증거를 사용하는 별도의 복구 작업대다. 두 화면과 새 제품은 같은 앱에 있지만 같은 주문 저장소나 하나의 분산 처리 흐름은 아니다.

## 공고 역량별 근거와 한계

| 공고의 요구 | 확인할 구현·증거 | 설명할 때 지킬 범위 |
|---|---|---|
| 고객 문제와 도메인 모델링 | `demo/route/planner.py`, `store.py`, `benefits.py`; 도착 마감, 제조 전 이동, 쿠폰 상실, 포인트 보류/복원/확정 | 고객 인터뷰나 실매출 개선을 측정한 사례가 아니라 개인 제품 제안 |
| REST·OpenAPI | `demo/route/api.py`, `contracts/route-benefits.openapi.json`, `tests/route/test_benefit_ui_contract.py`; 버전·견적 재검증 및 오류 상태 | 입력 스키마와 명세 스냅샷을 검증함. 모든 응답이 세분화된 타입 모델인 것은 아님 |
| 정합성·재시도 | `store.py`, `test_journey.py`, `test_benefits.py`; 동일 요청, 제조/이동 경합, 커밋 실패 롤백 | 주문 DB와 매장별 독립 DB, 별도 매장 HTTP 프로세스, 저장된 단계·자동 재확인·재시도 상한. 실제 가맹점·멀티 호스트 운영은 아님 |
| 원인 추적·검증 | `test_final_input_audit.py`, `RescheduleAdmissionTest.kt`; 미지원 유니코드 및 예약 변경 순서 반례 | 테스트를 먼저 실패시키고 수정한다. 무오류나 운영 SLA를 증명한 것은 아님 |
| AI 활용 및 반복 개선 | 실제 ChatGPT 협업, `docs/ai-iteration-log.md`, PR의 반례·수정·회귀 기록 | 요구/판단과 생성된 코드·검증 작업을 구분. 모든 코드를 수작업으로 작성했다거나 생산성 배수를 측정했다고 쓰지 않음 |
| Kotlin·Java·Spring·WebFlux | `services/commitment-service`, `merchant-fulfillment-service`, `ledger-service` | 별도 JVM/Docker 구성에서 실행. 공개 새 주문 UI가 이 서비스를 호출하지는 않음 |
| Python·FastAPI·Flask | 공개 제품, reconciler, ops-console | 각각의 테스트와 실행 기록을 구분 |
| DDD·EDA·MSA·Kafka·CQRS | commitment/merchant/ledger bounded contexts, outbox/inbox, PostgreSQL projection fence, `scripts/integration_smoke.py` | event 전달·취소 권위·원전표 잔액을 Docker에서 검증. 실물 제조/POS의 정확히 한 번 실행은 아님 |
| PostgreSQL·SQL 실행계획 | `scripts/capture_postgres_plan.py`, `sql/explain/`, release-gate 산출물 | 합성 DB의 EXPLAIN (ANALYZE, BUFFERS), 예상 인덱스와 강제 순차조회 비교. 실트래픽 성능 수치가 아님 |
| Redis·MongoDB·Elasticsearch·Celery | 예약 용량 Lua lease, 이벤트 전달 증거 보관, 검색 어댑터, 비동기 재생 작업 | 별도 topology/어댑터 범위. 새 공개 주문의 저장소로 사용했다고 설명하지 않음 |
| Docker·AWS·Kubernetes·Jenkins | Docker 빌드/전체 구성 실행, Terraform validation, K8s/Jenkins 파일 | Docker 실행과 인프라 문법 검증은 있음. AWS·EKS·Jenkins 운영 경험을 대신하지 않음 |
| Datadog·Elastic APM | 계측 설정·모니터 템플릿 | 외부 계정에서 장애를 운영·대응한 증거는 없음 |
| Claude Code·Cursor·Claude·Gemini·n8n·Make | 규칙/프롬프트/자동화 템플릿 | 파일 존재가 해당 도구의 실행·운영 경험을 입증하지 않음. 확인된 실제 도구 사용만 지원서에 작성 |
| Slack·Jira·Notion·스쿼드 협업 | handoff/sprint 문서와 선택적 자동화 예시 | 개인 프로젝트다. 실무 팀 협업이나 조직 프로세스 개선 경험으로 바꾸지 않음 |

## 지원서에서 앞에 둘 이야기

“커피 주문을 복제하기보다 고객의 도착 마감을 기준으로 선택과 주문 변경을 연결했다. 이동하면 빨라질 수 있지만 전용 쿠폰을 잃거나 예산을 넘을 수 있어, 서버가 변경 내용을 다시 계산하고 고객이 동의하도록 했다.”

그다음 같은 주문의 제조/이동 경합, 포인트 보류·복원, 근거가 부족한 정산 복구 차단을 설명한다. 기술 스택 전체를 첫 문단에 나열하지 않는다. 비교 실험은 합성 모델의 유지/이동 정책 비교이며 다른 지원자나 패스오더의 실제 성능을 측정한 결과가 아니다.

공식 공고는 이력서와 함께 AI 활용 경험, 깊이 파고든 기술 문제 또는 팀 프로세스 개선 경험에 대한 답변을 요청한다. 이 개인 프로젝트에서는 AI 협업 및 실제 재현한 기술 문제를 선택한다. 운영/팀 경험을 만들어 넣지 않는다.

## 검증 자료를 읽는 기준

최신 결과는 해당 commit의 workflow와 artifact를 기준으로 한다. PR의 성공을 main 성공으로, Docker의 성공을 공개 UI 성공으로 바꾸어 설명하지 않는다. 공개 health의 SHA, 고객/점주/복구 UI의 실제 HTTP 브라우저 검증을 따로 확인한다. 로컬에 없는 Maven/Docker나 정책상 차단된 Chromium을 통과로 세지 않는다.

과거 기록: [2026-09-22 공고 대조](jd-audit-2026-09-22.md). 현재 제품 범위: [쿠폰·포인트와 효과 비교](benefits-and-outcomes.md), [복구 작업대](repair-workbench.md). 이번 제출 전 점검: [최종 감사](final-application-audit-2026-09-24.md).

## 매장 변경 복구 수정본

`merchant_fleet.py` / `durable_operations.py`는 거절·독립 DB 커밋 이후 응답 손실·마지막 자리 경쟁을 다룬다. `test_transfer_durability.py`에서 별도 프로세스 강제 종료와 재시도를 시험하고, 취소 후 재주문도 동일 명령 처리기로 비교한다. [상세 범위](transfer-recovery.md). 이 변경의 원격 CI·공개 배포·실제 브라우저 검증은 아직 완료하지 않았으므로 기존 main의 통과 기록을 재사용해 지원서 성과로 쓰지 않는다.

## 이번 자동 복구·HTTP 보강

`recovery_worker.py`는 생성/재시도/기한을 저장하고, 변경 결정 전 만료와 결정 후 인계를 구분한다. `merchant_http.py`와 `runtime.py`는 실제 소켓 통신과 감독 프로세스 재시작을 담당한다. `recovery-ui.js`는 사용자 재주문 없이 상태를 조회한다. 이 경로의 공개 배포 여부는 `/api/route/runtime` 및 exact-SHA 브라우저 산출물로 확인한다. 쿠폰·포인트는 여전히 일정별 모의 지갑이다.

공고의 근본 원인 분석과 데이터 정합성에는 독립 저장 뒤 응답 누락, 오래된 작업 스케줄 이관, 공개 상태 버전 회귀를 재현한 시험을 연결한다. 실제 직원과의 협업·AWS/APM 운영 경력이나 경쟁 서비스 전체의 우위를 대체하지 않는다.
