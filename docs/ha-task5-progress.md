# Task 5 진행 기록 — 멀티 호스트 실행 구성과 외부 복구 게이트

이 파일은 중단 후 재개를 위한 작업 기록이다. 체크 표시는 실제 커밋·테스트 결과가 있을 때만 바꾼다. 긴 로그는 저장소에 넣지 않고, 아래 명령을 다시 실행해 재현한다.

## 출발점

- 작업 브랜치: `feat/ha-dr-foundation-20261002`
- 인계 시 HEAD: `2b4d8759e9fdb2f6ac10e76f66bac6ba3051138e`, main: `9cfd70562a85e18dcb842a4a71b333af60ea0a44`
- 인계 시 CI: `ha-foundation-review` 36972107990, `ha-encrypted-backup-review` 36972108021 성공(2b4d875).
- Task 1~4와 백업 경로 수정은 다시 만들지 않는다. Task 5만 이어서 진행한다.
- 출발점 재확인(개발 호스트, Python 3.12, PostgreSQL 17.11 컨테이너): HA 묶음 111 통과, 전체 Python 574 통과, 실패·건너뜀 0.

## 범위 구분

| 구분 | 의미 |
|---|---|
| 구현 완료 | 코드·설정·문서가 커밋됨 |
| 개발 검증 완료 | 한 개발 호스트에서 실제 프로세스·TCP·PostgreSQL로 통과. 독립 호스트 증거 아님 |
| 실제 멀티 호스트 검증 완료 | 승인된 서로 다른 호스트에서 HA-01~10, DR-01~08 3회 반복 |
| 공개 배포 완료 | 승인 후 공개 주소 전환 |

## 단계별 상태

### 1. 실행·접속·인증 구성 (`ha_postgres_v1`)

- [x] 내부 통신 mTLS 정책 `demo/route/ha/transport.py`
  - HTTPS만, 허용 호스트 목록, 명시 포트, 사설 CA 서버 신원·호스트명 확인, 환경 프록시·리다이렉트 미사용
  - 모든 수신기가 클라이언트 인증서를 요구하고 인증서 SAN URI `urn:pickup-pact:role:<역할>`로 호출 역할을 제한
    - 가맹점·가상 PG는 `order`만, 결제 알림 수신기는 `payment`만 받는다.
  - 클라이언트도 TLS 직후 서버 역할을 확인한다. 역할이 다르면 요청 바이트와 토큰을 보내기 전에 끊는다.
  - 기존 역할 토큰과 알림 HMAC은 그대로 유지한다. TLS가 이를 대신하지 않는다.
  - 개인키 파일은 0600 수준, 절대 경로만 받는다.
- [x] 기존 개발 모드(`postgresql_development`, loopback HTTP)는 변경 없음
- [x] DB 접속 `demo/route/ha/dsn.py`
  - Patroni 구성원 다중 호스트 목록과 `target_session_attrs=read-write`로 쓰기 리더만 접속한다. 단일 프록시에 의존하지 않는다.
  - `sslmode=verify-full`과 명시 CA가 필요하다. DSN에 비밀번호를 넣을 수 없고, 0600 passfile이나 클라이언트 인증서만 받는다.
  - 관리·복제 사용자로는 접속할 수 없다. `options`·`service`·`hostaddr`도 거부한다.
- [x] 역할별 최소 권한 `demo/route/ha/privileges.py`, 소유자 전용 마이그레이션 `demo/route/ha/migrate.py`
  - 런타임 사용자는 DDL을 실행하지 않는다.
  - 결제 거래·결제 명령·매장 영수증·요청 기록·알림 수신 기록은 추가만 할 수 있다(UPDATE/DELETE/TRUNCATE 없음).
  - 계약에 없는 테이블이 생기면 마이그레이션과 기동을 멈춘다.
  - 기동 시 관리 속성, 스키마 생성 권한, 다른 역할 DB 접속 권한, 테이블 권한 차이를 거부한다.
- [x] 운영 세대(복구 세대)
  - 각 DB에 1행을 둔다. 소유자만 compare-and-set으로 올리고, 같은 복원 사유로 다시 실행해도 결과가 같다.
  - 연결을 꺼낼 때마다 세대를 확인한다. 이전 세대 프로세스는 복원된 DB에 쓰지 못한다.
  - 내부 HTTP와 알림은 `X-Pickup-Generation`을 보낸다. 세대가 다르면 503이며 거절(decline)로 처리하지 않는다. 원래 작업키는 보류 상태로 남는다.
  - 기존 주문·승인·청구 멱등 키는 새로 만들지 않는다. 새 세대에서 같은 키로 조회·재개한다.
- [x] HA 모드 설정
  - 역할별 토큰·비밀은 서로 달라야 하고 32자 이상이다.
  - 가맹점·결제 origin은 역할마다 2개 이상이어야 한다.
  - `PICKUP_HA_REHEARSAL=single_host_development`일 때만 loopback을 허용하고, 그때 범위 표시는 `..._single_host_rehearsal_not_host_ha`이다.
- [ ] Patroni·etcd·진입점 구성 렌더링과 인벤토리 승인 검증
- [ ] 단일 호스트 리허설 스크립트: TLS PostgreSQL, 최소 권한 사용자, 역할별 2개 프로세스, mTLS, 실제 고객 흐름

### 2. 외부 백업 운영

- [ ] 보존·WAL 계획, 주기·지연·업로드 실패·용량 감시, 작성자/보존 관리자 권한 분리, 키·영수증 보관 위치 검증

### 3. 실제 독립 호스트 장애·복원 시험

- [ ] 승인 대기. 비용·호스트·파괴 범위가 승인되기 전에는 실행하지 않는다.

## 개발 검증 기록

| 시각(UTC) | 커밋 | 명령 | 결과 |
|---|---|---|---|
| 2026-10-02 | 2b4d875 | `pytest -q tests/ha` | 111 통과 |
| 2026-10-02 | 2b4d875 | `pytest -q` | 574 통과 |
| 2026-10-02 | 작업 중 | `pytest -q tests/ha` (mTLS·설정·권한·세대 추가) | 189 통과 |

## 재개 방법

```bash
git switch feat/ha-dr-foundation-20261002 && git log --oneline -5
python -m pip install -r requirements-ha.txt -r services/ops-console/requirements.txt
# 격리된 개발 PostgreSQL 17만 사용한다.
PICKUP_HA_TEST=1 PICKUP_PG_TEST_DSN='<isolated development DSN>' PYTHONPATH=.:services/reconciler python -m pytest -q tests/ha
```
