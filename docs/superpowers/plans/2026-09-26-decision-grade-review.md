# Decision-grade review Implementation Plan

> Inline execution: superpowers:executing-plans. 각 작업은 실패 재현→구현→동일 반례/회귀 확인으로 진행한다.

**Goal:** 상품·참여·비용 규칙과 공정한 대안 비교를 실제 매장 변경 흐름에 연결한다.
**Architecture:** 기존 저장된 작업 coordinator를 유지하고 순수 agreement 모듈·typed response·UI presentation을 분리한다.
**Tech Stack:** Python/FastAPI/Pydantic/SQLite/JavaScript/Playwright. 추가 외부 서비스나 비용 없음.
**Spec:** docs/superpowers/specs/2026-09-26-decision-grade-review.md

## Global Constraints
- 원본/기존 테스트 보존. 기존 실험은 별도 legacy 모델임을 유지.
- 테스트 double을 공개 브라우저 성공으로 세지 않는다.
- 앱 코드가 바뀌었으면 이전 영상으로 새 검증을 대신하지 않는다.
- 자소서/포트폴리오는 사용자에게 변경점을 제시한 다음.

## Review Focus
- 참가/규격 정보 누락, 변경된 견적, 중복 수령, 미확정 이관 중 통제 요청, 기존 체험 재개.

## Tasks
- [ ] A. tests/route/test_transfer_agreement.py: 미참여/다른 그룹/규격 불일치/금액 균형/견적 변화 실패 재현; agreement.py 및 planner/store에 통합.
- [ ] B. tests/route/test_protected_reorder.py: 기존 주문 보존·새 ID·혜택 재검증·실패 후 원복·중간 종료; durable_operations/store에 비교용 protected_reorder 추가.
- [ ] C. 비교4종/6상황과 저장소 재사용 격리·동일 결과·성능 측정; transfer_comparison.py 및 테스트/UI/스크립트 갱신.
- [ ] D. 첫 화면/조건 동의/모의 정산 표시, 선택 보존; UI 반례→수정→브라우저 증거.
- [ ] E. 핵심 응답 모델·OpenAPI·검증 스크립트 및 사업 가정 문서 일치.
- [ ] F. 전체 반복 검증·최종 diff·PR CI·같은 main 배포/브라우저 확인. 실패나 제약은 그대로 기록.
