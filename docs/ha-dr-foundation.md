# HA·DB 유실 복구의 개발 통합 범위

이 브랜치는 기존 고객 흐름을 **실제 PostgreSQL 저장소와 독립 실행 역할**에 연결한다. 공개 서비스 전환이나 독립 호스트 HA 완료를 뜻하지 않는다. 기존 `main`과 Render 무료 서비스는 SQLite 실행을 유지한다.

## 저장소와 역할

주문, 가맹점, 가상 PG는 서로 다른 데이터베이스를 사용한다. 개발 시험의 세 데이터베이스는 한 PostgreSQL 물리 클러스터에 있다. 서비스 간 업무 처리는 기존 HTTP 명령과 영수증으로 진행하며 서로 다른 DB를 하나의 업무 트랜잭션으로 묶지 않는다.

- `PostgresJourneyStore`와 `PostgresCoordinator`: 주문·쿠폰·포인트·원래 요청키·단계 버전을 저장한다. 작업 임대가 만료된 뒤 도착한 응답은 이전 상태를 덮어쓰지 못한다.
- `PostgresMerchantFleet`: 자리 수, 제조 권한, 세대 번호, 명령 영수증을 PostgreSQL에서 함께 기록한다. 서로 다른 인스턴스의 동시 마지막 자리 요청도 같은 제약을 적용한다.
- `PostgresPaymentRepository`: 승인·확정·해제·알림 outbox를 원자적으로 기록한다. 동일 요청 반복, 주문당 한 번 청구, 지갑별 동시 한도를 보호한다.
- 앱·복구 작업자·가맹점·결제·알림 수신기를 독립 프로세스로 실행한다. 개발 토폴로지에는 각 역할을 두 개씩 둔다. 모든 프로세스는 같은 개발 호스트에 있으므로 이를 독립 호스트 두 대라고 부르지 않는다.
- 가맹점/결제 조회는 명시적으로 설정한 살아 있는 처리기를 찾는다. 결과가 불명확한 쓰기는 성공으로 대체하거나 새 거래키로 재시도하지 않는다. 저장된 작업이 원래 키로 결과를 재조회한다.

DB 트랜잭션은 작업 확보와 결과 반영에만 사용한다. 외부 HTTP가 지연되는 동안 주문 DB의 쓰기 잠금을 유지하지 않는다. 정상적인 다른 주문은 별도로 진행할 수 있어야 한다.

## 실행 설정과 기존 기능

새 모드는 `PICKUP_ROUTE_BACKEND=postgresql_development`로 선택한다. 모드를 선택했는데 DB 또는 내부 통신 설정이 없으면 시작을 거부한다. 로컬 SQLite나 가짜 성공 응답으로 대체하지 않는다.

역할별 DSN과 공통 내부 토큰은 환경변수로 공급한다. `ROUTE_MERCHANT_FAILOVER_URLS`와 `ROUTE_PAYMENT_FAILOVER_URLS`는 추가 처리기 origin의 JSON 배열이며 전체 origin 수는 최대 세 개다. 중복 주소를 거부하고, 현재 개발 모드는 loopback HTTP만 허용한다. 실제 원격 배포에 필요한 TLS·인증서·허용 호스트 검증은 별도 배포 단계다.

`/ready`는 DB, 가맹점, 결제, 복구 작업자의 준비 상태를 확인한다. `/api/route/runtime`에는 저장소 종류·노드 ID·검증 범위가 표시되며 비밀키와 DSN은 표시하지 않는다. 복구 작업자 상태는 최근 DB heartbeat 기준이며, 이 표시는 전체 호스트의 HA 인증이 아니다.

기존의 빠른 체험, 금액 동의, 주문·가맹점 단말·결제·혜택 원본 대조 화면을 유지한다. `/classic`, `/repair-lab`, 과거 비교 실험은 이 PostgreSQL 이식의 검증 범위와 구분한다.

## 물리 백업과 복원 시험

`verify_ha_restore.py`는 직접 만든 UUID·소유 레이블의 시험 자원만 사용한다. 임의 DSN이나 운영 볼륨을 삭제 대상으로 받지 않는다. PostgreSQL의 `pg_basebackup`과 `pg_verifybackup`을 실행하고 기본 백업 이후의 변경을 WAL에 보관한다.

원본 시험 컨테이너와 데이터 볼륨을 제거한 뒤, 복제본 없이 보존된 기본 백업·WAL로 새 볼륨에 복원한다. 주문·가맹점·결제 데이터베이스를 같은 지정 지점으로 복원하고, 그 이후 추가한 시험 데이터가 나타나지 않아야 한다.

복구 대조에는 완료 주문과 청구 응답이 유실된 미완료 주문이 포함된다. 기존 거래 ID와 요청키, 제조·수령 매장, 청구 금액, 승인 보류, 쿠폰·포인트를 대조한다. 원래 요청으로 미완료 작업을 마친 뒤 청구 3,200원 1건, 승인 보류 0원, 1,000P 사용·32P 적립을 확인한다. 이 복원 대조는 native 저장소 호출이며 HTTP 장애 시험과 구분한다.

백업 파일의 해시, 클러스터 ID, 필요한 WAL 구간과 봉인 manifest를 검사한다. 변조된 기본 백업·빠진 WAL·미완료 자료는 정상 백업으로 인정하지 않는다. 비밀번호와 원본 DB 파일은 CI 아티팩트에 게시하지 않는다.

**개발 시험의 백업은 같은 CI 호스트의 다른 Docker 볼륨이다.** 이 결과로 호스트 전체 상실, 외부 저장소 가용성, 외부 백업 암호화·보존 정책 또는 운영 RTO/RPO를 주장하지 않는다. 기록한 복원 시간은 작은 합성 데이터의 개발 시험 값이다.

## 실행과 결과 확인

`.github/workflows/ha-foundation-review.yml`은 세 job을 별도로 실행한다.

1. 실제 PostgreSQL 저장소·경합 계약과 기존 Python 전체 3회, JavaScript 상태/저장소 계약.
2. 서로 다른 새 시험 클러스터에서 원본 볼륨 제거·세 DB 복원·전체 원장 대조 3회.
3. 앱·작업자 교체, 가맹점/PG 처리기 중단, 실제 HTTP와 기존 Chromium 데스크톱·모바일·키보드·다중 탭 검사.

브라우저 묶음 하나가 실패해도 가능한 다른 묶음의 진단을 모으지만, 실패를 통과로 바꾸지는 않는다. 제출된 소스 SHA와 아티팩트 내부 SHA가 같아야 한다. 실행하지 않은 검사는 성공 횟수에 포함하지 않는다.

```bash
python -m pip install -r requirements-ha.txt -r services/ops-console/requirements.txt
# 별도의 loopback 개발 DB를 사용한다. 운영 DSN을 넣지 않는다.
PICKUP_HA_TEST=1 PICKUP_PG_TEST_DSN='<isolated development DSN>' PYTHONPATH=.:services/reconciler python -m pytest -q
# Docker에서 실행기가 직접 만든 자원만 복원 시험에 사용한다.
PICKUP_HA_TEST=1 PYTHONPATH=.:services/reconciler python scripts/verify_ha_restore.py --repeat 3 --output verification/ha-restore/runs
```

## 실제 HA·외부 복구의 남은 배포 단계

독립 호스트 3대의 승인된 인벤토리, PostgreSQL/Patroni 동기 복제와 합의 저장소, 이전 주 서버 쓰기 차단, 이중 진입점, 내부 TLS, 암호화·별도 권한·보존 정책이 있는 외부 백업 저장소를 연결해야 한다. 독립 호스트 상실과 네트워크 분리, 외부 저장소만 이용한 복원도 실제로 반복 검증해야 한다.

이 브랜치의 실행기는 유료 서버·외부 저장소·실제 카드 거래를 만들지 않는다. 승인된 호스트·저장소·비용·장애 시험 범위 없이 운영 배포 또는 파괴 시험을 진행하지 않는다.
