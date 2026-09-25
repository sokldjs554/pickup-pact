# Recoverable merchant transfer implementation plan

> Execute inline using executing-plans. User requested direct implementation of the proposed transfer-focused revision.

**Goal:** Preserve an order through merchant rejection, shared last-slot competition and uncertain replies; compare against cancel/reorder.
**Architecture:** Existing JourneyStore remains owner of pricing/wallet. MerchantFleet has one independent DB per modeled shop. DurableOperations stores candidate changes before calling merchants and retries phases with stable IDs. No global DB transaction crosses merchant files.
**Tech Stack:** Existing Python/FastAPI/SQLite/JS/pytest/Playwright, no new production dependency.
**Spec:** docs/superpowers/specs/2026-09-25-recoverable-transfer.md

## Global constraints
Korean conversational UI. No real-money or real-GPS claims. Keep /classic and /repair-lab intact. Do not add a license. Do not change previous experiment outcomes. Never say a pending transfer succeeded. Source 993e8f8 restored with identical Git tree 98d84d92a400e66f79e1895992ffd2617fedf722.

## Review focus
Crash after external commit before coordinator acknowledgement; stale merchant generation; shared target capacity; repeated idempotency keys; rollback retains exact original order/wallet. Each must have an executed regression.

## Tasks
- [x] Add tests asserting current journey has independent merchant capacity and recoverable operation state; run RED against original code.
- [x] Implement `merchant_fleet.py`: execute(store_id, world, order_id, generation, operation_id, action, transfer_id), snapshot(world). Transactions and receipts scoped to one merchant DB.
- [x] Implement `durable_operations.py`: command(sid, command), recover(sid), control(sid, control). Persist candidate before calls; drive each step idempotently; expose pending/rejected/completed status. Wrap existing pure `_apply` rather than replace benefits rules.
- [x] Integrate JourneyStore and typed route control/recovery/reorder endpoints. Test failure boundaries and unchanged legacy results. Update OpenAPI snapshot from runtime, not by hand.
- [x] Implement comparison using three real command policies and a disclosed schedule of adversarial events. Save full inputs/results/traces.
- [ ] Add UI stage card and controls in existing flow, plus comparison table. Run JS/HTTP/UI and repeated regression; inspect screenshots.
- [ ] Review complete diff, create reproducible patch/source/evidence. Publish only if an authorized write path is available; current GitHub connector exposes read functions only, direct Git CLI DNS fails. Do not claim a new public deployment.

## 실제 실행 기록

코어·API·공정한 기준 비교 구현 완료. 10개 커밋 경계에서 자식 프로세스를 종료한 후 재시작 시험을 추가했다. 거절 후 새 시도에 이전 세대를 다시 쓰던 오류와, 취소 후 재주문 거절을 원래 주문 보존으로 잘못 안내하던 오류를 반례로 수정했다.

현재 로컬 회귀 295개 통과. 실제 HTTP 3회 흐름, 수령 요청 24개 동시 재전송 및 6가지 상황×3정책 비교의 의미 해시 일치를 확인했다. JSX가 아닌 기존 JS 구문·표시 함수 검증도 구분한다.

미완료: 실제 HTTP Chromium 탐색이 정책으로 차단됨. 원격 쓰기/CLI 네트워크 부재로 신규 PR·CI·배포 미수행. 오프라인 DOM 레이아웃 확인은 브라우저 사용 흐름 성공을 뜻하지 않는다. 마지막 두 체크박스는 이 제한 때문에 완료로 표시하지 않는다. 최종 소스·패치·로그는 별도 묶음으로 제공한다.
