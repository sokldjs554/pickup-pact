# 독립 가상 결제의 공개 반영과 검증 원본

## 공개 기능의 기준

2026-10-01에 확인한 기능 커밋은 `dbefc8348a7f21de5ee67e7868145c474cf46239`다. Render 배포 `dep-dav2jcdg1s2s73d8bhog`가 이 커밋으로 live였고, 공개 `/health`와 `/api/route/runtime`을 검사한 CI 원본에서도 커밋과 `payment_mode=simulator_http_v1`, `payment_transport=http`, `payment_storage_ready=true`, `merchant_transport=http`, 자동 복구 활성화를 대조했다.

새 기능은 PR #38에서 반영됐고, 중첩 summary 선택자 수정은 PR #39에서 반영됐다. 이 문서를 추가하는 변경은 문서·누락된 기존 로그·미디어 무결성 테스트뿐이다. 앱·가맹점·결제·복구 처리기를 다시 덮어쓰지 않는다. 이후 문서 커밋의 번호를 아래 기능 검증 커밋과 혼동하지 않는다.

[공개 데모](https://pickup-pact-demo.onrender.com/) · [공개 회귀 실행](https://github.com/sokldjs554/pickup-pact/actions/runs/36843864699)

## 고정한 원본 결과

| 구분 | 확인한 결과 | 원본 |
|---|---|---|
| main의 Python 앱 회귀 | 462개를 3회 통과: 87.36 / 84.67 / 83.57초 | `route-product-evidence` 11152263312 |
| 기존 공개 화면 회귀 | 가이드·매장 변경·주문·혜택·문구·동의, 6묶음 × 데스크톱/모바일 각각 3회 = 36회 통과 | `route-live-evidence` 11154061351 |
| 기능 커밋 main 작업 | 11개 워크플로 성공 확인 | 해당 커밋의 Actions 실행 기록 |

모든 공개 결과의 `release_commit`은 위 기능 커밋과 같았다. 로컬 CI 결과와 공개 결과를 구분했다. Python 실행에는 Starlette TestClient의 사용 중단 예정 경고가 회차별 1건 있었으며, 오류가 없는 출력이라고 바꿔 쓰지 않는다.

원본 ZIP의 SHA-256과 CRC를 다시 검사했다.

- 제품: `252f04ba5e7e3cc72eabfa7eb87f5c0bedd356a459ce93144addeabf8db34015`
- 공개: `1c87dfb9f95252a86de7dcb4d323c005b1e4a913b734b7c8e7df0ec8460fdebf`

독립 결제 장애와 다중 탭 경합은 [추가 공개 검증](https://github.com/sokldjs554/pickup-pact/actions/runs/36850776874)과 [공개 대기 예산을 명시한 탭 재검사](https://github.com/sokldjs554/pickup-pact/actions/runs/36851300464)에 따로 남긴다. 이 추가 실행은 위 36회에 포함하지 않는다. 실패한 실행을 지우거나 성공한 회차로 세지 않는다. 첫 다중 탭 실행은 최초 주문 HTTP 요청이 끝나기 전에 Playwright의 기본 5초 단언 기한에 걸렸다. 원본 trace의 아직 진행 중인 요청을 확인하고, 기존 동작 기한과 같은 20초의 공개 단언 예산을 명시했다. 주문·금액·200/409 경합 단언은 유지했으며, 이 조정을 서비스의 응답 시간 개선으로 주장하지 않는다.

## 제출된 로컬 ZIP과 원격을 구분한다

사용자가 제출한 `pickup-complete-20261001-164347-7e816d.zip`의 제품·브라우저·촬영·후보 회귀 단계는 통과했다. 마지막 중단은 `GitHub main changed; refusing to overlay: 48b2c5993dacc53f78d7e56366500b6b2af850cc`였다. 이는 오래된 기준 main으로 최신 변경을 덮지 않도록 한 보호 절차다. 해당 ZIP은 push나 공개 배포의 증거가 아니다.

그 ZIP의 최종 Python 454개, 기존 브라우저 36회, 실제 로컬 HTTP 녹화 결과를 확인했다. 그러나 소스가 현재 원격과 바이트 단위로 같지 않으므로, 그 성공을 위 main의 시험 결과로 대신하지 않았다. macOS 11의 Playwright 1.48/Chromium 130은 구형 로컬 호환성 범위다. 원격은 원격 소스와 현재 CI 브라우저 결과로 별도 확인했다.

## 영상과 로그의 범위

README의 42.4초 영상은 [배포 전 구현 기록](payment-release-verification-2026-09-30.md)의 실제 CI HTTP 녹화다. 공개 Render에서 새로 촬영한 영상이 아니며, 과거의 41.12초 가이드 영상과도 다르다. 영상과 GIF 전체 디코딩 및 게시 파일 해시를 검사했다.

`docs/media/payment/publication.json`에 명시된 세 테스트 로그가 저장소에서 빠진 것을 발견했다. 원본 아티팩트 `11094890834`에서 로그를 복원했고, 기존 manifest의 해시와 정확히 일치하는 원본 바이트를 사용했다. 원래 459개 회귀의 기록이며 현재 462개 결과로 바꾸지 않았다. 신규 테스트는 manifest의 모든 파일이 실제로 존재하고 해시가 맞는지 확인한다. 복원 전 missing-file 실패와 복원 후 통과를 확인했다.

## 검증의 한계

가상 매장·가상 카드·자체 승인 보류 규약이다. 실제 카드번호, 카드사/PG 운영 연동, 실가맹점 POS, 수령 후 환불·차지백, 멀티 호스트 고가용성, 임시 디스크 자체 유실 복구는 이 완료 범위가 아니다. 처음 보는 면접관 대상 이해도 실험이나 실제 매출 효과를 측정했다고 주장하지 않는다.
