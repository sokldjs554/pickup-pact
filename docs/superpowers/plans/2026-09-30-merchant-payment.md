# 가맹점·독립 가상 결제 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 주문 체험에 독립 결제 HTTP, 재개 가능한 결제 단계, 두 매장 단말과 독립 원장 대조를 연결한다.
**Architecture:** 새 journey만 protocol_version=2로 시작한다. 외부 I/O는 주문 DB 쓰기 트랜잭션 밖에서 수행하고 단계 revision으로 늦은 결과를 차단한다. 구버전 주문·비교 실험은 기존 규칙을 유지한다.
**Tech Stack:** Python, SQLite WAL/FULL, FastAPI/Pydantic, 표준 HTTP 서버, httpx, 기존 JavaScript, pytest/Playwright.
**Spec:** `docs/superpowers/specs/2026-09-30-merchant-payment-design.md`

## Global Constraints
- `DEMO_PLATFORM`, `KRW`, 양의 정수 금액, `demo-approved` / `demo-declined`; 실제 카드·PG·유료 서비스 없음.
- 0원은 `NO_CHARGE`; 결제 통신 오류를 구버전 성공으로 바꾸지 않는다.
- 재시도 3~30초, 실패 8회 한도, 결정 전 45초. 불명확한 금융 상태는 시간 경과만으로 실패 처리하지 않는다.
- 새 금액 승인 후에 기존 승인을 해제. 동일 금액은 기존 승인 재사용.
- 최초 승인/매장 접수 모두 확인, 수령은 청구/매장 수령 모두 확인, 취소는 매장 취소/승인 해제 모두 확인.
- 기존 main과 공개 앱은 검증 전 변경하지 않는다. BrewSlot 변경 없음.

## Review Focus
- bool/실수/누락/미등록 입력: 금융 원장 변경 없이 거절 (Task 1).
- 동일 요청키가 다른 세계·주문·금액에 재사용: 409; 다른 키도 이중 청구 불가 (Task 1).
- 승인 응답이 늦게 도착한 뒤 이미 ABORT: 승인 재확인·해제 후 복원, 신규 주문 안내 금지 (Tasks 2/3).
- 결제 원장 조회 실패 또는 순차 조회 중 상태 변경: UNKNOWN/PENDING, 0/일치로 위장 금지 (Task 4).
- 구버전 세션 복원·읽기 실패: mode 변환과 참조 삭제 금지 (Tasks 3/5).

## Task 1: 독립 결제 원장
**Files:** `demo/route/payments/domain.py`, `repository.py`; `tests/route/test_payment_repository.py`.
**Interfaces:** `PaymentRepository(path).execute(command:dict)->dict`, `.operation(key)->dict|None`, `.snapshot(world,order_id)->dict`.
- [ ] 승인/거절, 2,800→3,200원 재승인·해제·확정, 24개 동시 확정, 타세계 키 충돌, 0/음수/bool/실수, 해제 승인 재생 금지 테스트 작성.
- [ ] `pytest -q tests/route/test_payment_repository.py` 실패 확인.
- [ ] 명령·승인·거래·outbox를 같은 로컬 트랜잭션으로 저장; 승인별/주문별 확정 고유 제약 구현.
- [ ] 위 검사 통과 및 SQL 무결성/실행계획 확인; 로컬 커밋.

## Task 2: 실제 HTTP와 인증 알림
**Files:** `payments/http_server.py`, `http_client.py`, `runtime.py`, `notifications.py`; `tests/route/test_payment_http.py`, `test_payment_notifications.py`.
**Interfaces:** `PaymentClient.execute(command,fault='none')`, `.operation(command)`, `.snapshot(world,order)`; `PaymentProcess(path)` context; `PaymentInbox.accept(body,timestamp,signature)`.
- [ ] commit 후 연결 종료, 동일 키 조회, 인증·경로·크기·금액 결합, 재시작 후 거래 유지 테스트를 먼저 실행.
- [ ] loopback 전용 서버·제한된 전송·독립 DB·감독 구현. 실제 응답 누락은 DB commit 후 소켓 종료.
- [ ] outbox + HMAC 원문·시간·event_id 중복/충돌 검사; 과거 알림은 상태 전이 대신 재조회 신호.
- [ ] 실제 HTTP/프로세스 테스트와 서명·중복·역순·다른 세계 검사 통과; 커밋.

## Task 3: 새 주문 프로토콜
**Files:** `payment_steps.py`, `payment_operations.py`, `store.py`, `durable_operations.py`, `runtime.py`, `recovery_worker.py`; `tests/route/test_payment_operations.py`.
**Interfaces:** `PaymentOperations.command(sid,c)`, `.resume(sid,oid,duplicate=False,automatic=False,now=None)`, typed state metadata on `JourneyStore.view`.
- [ ] 최초 승인 실패/미응답/접수 거절, 변경 새 승인 거절·금액차·만료, capture/void 미응답, 24개 수령, 늦은 결과 fencing, I/O 중 타 주문 쓰기 테스트 작성·실패 확인.
- [ ] v2 candidate에는 미확인 금융 이벤트를 넣지 않음. 계획 저장→통신→revision 일치 시 적용. provider 거래 ID를 성공 이벤트에 결합.
- [ ] 구버전 분기와 고정 payment_mode, 8회 소진, GET 무부작용, 0원 경로 구현.
- [ ] T-01~T-16, T-19~T-22, T-25/T-26 관련 테스트 및 기존 회귀 실행; 커밋.

## Task 4: 독립 자료 대조 API
**Files:** `reconciliation.py`, `api.py`, `response_models.py`, 계약 JSON; `tests/route/test_payment_reconciliation.py`.
**Interfaces:** `reconcile(store,sid)->dict`: C0/M0/P0/M1/P1/C1 안정성, MATCH/PENDING/MISMATCH/UNAVAILABLE.
- [ ] 정상/원장 불일치/읽기실패/revision 변화/다른 방문자 격리 테스트 작성·실패 확인.
- [ ] 결제·매장 원본 직접 조회, revision과 시각 포함; GET 복구 명령 금지. 계약을 실제 OpenAPI로 생성.
- [ ] T-18/T-23/T-24/T-30 계약 검사 통과; 커밋.

## Task 5: 같은 제품의 단말·결제·결과 화면
**Files:** `guide-flow.js`, `index.html`, 신규 `payment-ui.js`/`payment.css`, `scripts/verify_payment_browser.py`, JS 단계 검사.
- [ ] order가 없어도 pending 먼저 안내, 접근성·가상 카드 폼·네 가지 결과·두 매장·오류 표시 계약 실패 확인.
- [ ] 주 시작 버튼 하나 유지; 상세 장애 선택, 원장 조회와 단말 상태, 단계 번호 중복 제거. UI는 실제 응답만 사용.
- [ ] 실제 HTTP/Chromium으로 데스크톱·모바일 기본/승인 누락/확정 누락/취소/키보드 확인; T-27~T-29.

## Task 6: 최종 검증과 제출 근거
**Files:** 별도 검증 workflow, README, 실행 결과 문서.
- [ ] 신규 금융/통합 테스트 3회 + 기존 전체 테스트 + Node 검증 + 실제 브라우저 각 3회.
- [ ] 강제 종료/별도 원장 조회 실패·위조 알림 반례 확인; 결과를 실행 범위별로 기록.
- [ ] 정확한 소스·로그·스크린샷·영상 보존. 새 영상 없이 기존 41초를 새 기능으로 부르지 않음.
- [ ] 최종 diff 자체 검토, 기능 브랜치/PR 보존. 독립 검토 또는 공개 배포를 못 했으면 미완료로 표시.
