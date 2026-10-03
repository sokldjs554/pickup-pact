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
- [x] Patroni·etcd·진입점 구성 렌더링과 인벤토리 승인 검증(`inventory.py`, `scripts/ha_inventory.py`, `infra/ha/`)
- [x] 단일 호스트 리허설(`scripts/ha_cluster_rehearsal.py`, `verify_ha_cluster.py`): 렌더링한 설정 그대로 Patroni 3·etcd 3·역할 15개 프로세스

### 2. 외부 백업 운영

- [x] 보존·WAL 계획, 주기·지연·업로드 실패·용량 감시, 작성자/보존 관리자 권한 분리, 키·영수증 보관 위치 검증(`backup_ops.py`, `verify_ha_backup_ops.py`)
- [x] 전체 클러스터 유실 → 외부 저장소 복원 → 비밀번호·토큰 교체·세대 갱신 → API·브라우저 확인(`verify_ha_cluster_restore.py`)

### 3. 실제 독립 호스트 장애·복원 시험

- [x] 승인 기록: sokldjs, 2026-10-03, 비용 상한 0원, 파괴 시험 2026-10-03~05, `infra/ha/inventory.oracle-osaka.yaml` (검토·배포·파괴 3단계 검사 통과)
- [x] 서버 4대(Oracle Osaka AD-1, 무료 체험 크레딧): 접속·사양·FD 분리 점검 통과. pact-a=FD-1, pact-b=FD-2, pact-c=FD-3, pact-backup=FD-1(승인 예외 `backup_failure_domain`, 범위 `oracle_single_ad_fault_domains`)
  - 첫 점검에서 pact-a와 pact-b가 같은 FD로 잡혀 pact-a를 FD-1에 다시 만들었다.
- [x] 설치 단계: PostgreSQL 17.11, Patroni 4.0.4, etcd 3.5.17, restic 0.19.1(+백업 서버 rest-server 0.14.0), 서버 방화벽(사설망만), softdog 권한 — 4대 모두 통과
- [x] 배포 단계(2026-10-03, 135초): 임시 CA(5일) 인증서, etcd 3노드 정족수, Patroni 3노드 수렴(strict 동기), 역할·스키마 마이그레이션 3종, 서비스 5종 ×3대 기동, 앱 3대 `/ready`=200·`storage_backend=postgresql`
  - 첫 시도는 Oracle VCN 보안 규칙이 서브넷에 적용되지 않아 서버 간 etcd/DB/앱 포트가 차단되어 멈췄다. 규칙(10.0.0.0/16 TCP 전체)을 서브넷의 Security List에 넣은 뒤 재시작했다.
  - 데이터가 없는 부분 배포만 정리 후 재시작하며, PostgreSQL 데이터가 있으면 거부한다.
- [ ] HA-01~10, DR-01~08 ×3 (파괴 시험은 별도 확인 후)

## 개발 검증 기록

| 시각(UTC) | 커밋 | 명령 | 결과 |
|---|---|---|---|
| 2026-10-02 | 2b4d875 | `pytest -q tests/ha` | 111 통과 |
| 2026-10-02 | 2b4d875 | `pytest -q` | 574 통과 |
| 2026-10-02 | 작업 중 | `pytest -q tests/ha` (mTLS·설정·권한·세대 추가) | 189 통과 |

## 최종 검증 (커밋 b712f51, 깨끗한 작업 트리, 단일 개발 호스트)

| 항목 | 반복 | 결과 |
|---|---|---|
| 전체 Python `pytest -q` (PostgreSQL 17.11 포함) | 3 | 각 733 통과, 실패·건너뜀 0 |
| JavaScript 상태 계약 `scripts/verify_*.cjs`, `verify_repo.py` | 3 | 모두 통과 |
| SQLite 공개 모드 실제 HTTP·브라우저(guided, handoff HTTP/브라우저, route, benefits, decision, payment, tabs) | 각 3 | 모두 통과. tabs는 처음에 로컬 Chromium 버전 불일치(playwright 1.63↔빌드 1194)로 실행 전 실패 → playwright 1.56으로 맞춘 뒤 3회 통과 |
| native PostgreSQL 개발 토폴로지 `verify_ha_journey.py --browsers` | 3 | 24 시나리오·브라우저 8묶음 통과 |
| 기존 물리 복원 `verify_ha_restore.py` (local / restic HTTPS) | 각 3 | 통과 |
| WAL 전송·경보 `verify_ha_backup_ops.py` | 3 | 통과 |
| 클러스터 장애 `verify_ha_cluster.py`(WAL, HA-01~06, 세대 차단) | 3(새 클러스터) | 24/24 통과 |
| 전체 유실·복원 `verify_ha_cluster_restore.py --browsers` | 3(새 클러스터) | 3/3 통과, 브라우저 4묶음 매회 통과 |

리허설 측정값(운영 RTO/RPO 아님):
- HA-04 리더 호스트 강제 종료: 최장 쓰기 공백 48.6/32.8/32.8초, 응답한 쓰기 110/112/100건 중 유실 0, 불명확 9/6/6건
- HA-05 대기 노드 전부 중단 중 쓰기 응답 0건(strict 유지), 대기 노드 복귀 후 재개
- HA-06 리더 망 분리: 최장 쓰기 공백 40.4/33.7/43.4초, 유실 0, 쓰기 가능 노드 동시 2개 관측 0
- 전체 유실 후 앱 준비까지 57.6/59.4/58.3초(작은 합성 데이터, 같은 컴퓨터)

SQL 변경: 연결마다 확인하는 세대 조회는 1행 표, `EXPLAIN (ANALYZE, BUFFERS)` 결과 shared hit 1, 실행 0.019 ms.

## 재개 방법

```bash
git switch feat/ha-dr-foundation-20261002 && git log --oneline -5
python -m pip install -r requirements-ha.txt -r services/ops-console/requirements.txt
# 격리된 개발 PostgreSQL 17만 사용한다.
PICKUP_HA_TEST=1 PICKUP_PG_TEST_DSN='<isolated development DSN>' PYTHONPATH=.:services/reconciler python -m pytest -q tests/ha
```
