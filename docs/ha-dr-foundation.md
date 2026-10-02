# HA·DB 유실 복구의 개발 검증 경계

이 브랜치는 HA 전체 구현이나 공개 배포 완료가 아니라 **네이티브 PostgreSQL 저장소와 복원 검증의 첫 단계**다. 기존 공개 앱은 SQLite 기반 실행을 유지한다. `main`과 Render 설정은 이 작업으로 변경하지 않는다.

## 구현된 구성

- `PostgresPaymentRepository`: 실제 PostgreSQL SQL과 고유 제약, 동일 요청 응답, 주문당 한 번 청구, 체험 지갑별 동시 승인 제한.
- `PostgresOperationStore`: 저장된 작업 ID·요청 내용·단계 버전·임대 버전. 이전 작업자의 응답은 임대가 만료되거나 다음 단계로 넘어갔다면 반영하지 않는다. 아직 기존 주문 조정기의 전체 저장소를 대체하지 않았다.
- 독립 결제 실행 역할: `python -m demo.route.ha.payment_service`. 연결 실패를 SQLite 성공으로 대체하지 않는다. `PICKUP_PAYMENT_INIT_SCHEMA=1`을 명시해야 초기 스키마를 생성한다. 내부 HTTP는 기존 loopback 정책을 유지한다.
- 백업 검증: 기본 백업의 원본 파일·클러스터 식별자·필수 WAL 구간·해시와 manifest의 완전성을 검사한다.
- 물리 복원 시험: 직접 만든 시험 DB만 백업하고, 원본 컨테이너와 데이터 볼륨을 제거한 뒤 새 볼륨으로 복원한다. 복제본 없이 기본 백업과 아카이브 WAL만 사용한다.

## 실제 복원에서 확인하는 것

PostgreSQL `pg_basebackup`과 `pg_verifybackup`을 실행한다. 기본 백업 이후에 결제 확정과 미완료 작업을 각각 다른 데이터베이스에 저장하고 공통 복원 지점을 남긴다. 그 뒤에 추가한 시험용 행은 복원 결과에 나타나면 안 된다.

복원 전 완료한 결제의 원래 거래 ID·금액·멱등 응답이 보존되어야 한다. 미완료 작업과 3,200원 승인 보류도 보존되어야 하며, 복원 후 같은 외부 명령 키로 작업을 마쳐 청구 한 건·보류 0원이 되어야 한다. 손상된 기본 백업, 누락되거나 덜 복사된 WAL, 다른 클러스터의 자료는 복원 대상으로 인정하지 않는다.

대상 자원은 실행기가 만든 UUID 이름과 소유 레이블을 삭제 직전에 다시 확인한다. 임의 DSN·기존 볼륨·운영 서버를 삭제 대상으로 입력받지 않는다. 원본 DB나 비밀번호는 검증 아티팩트에 게시하지 않는다.

## 재현과 근거

`.github/workflows/ha-foundation-review.yml`에서 PostgreSQL 17.11의 고정 이미지 digest를 사용한다. 저장소·동시성·실제 HTTP 재시작 검사와 전체 Python 테스트 3회를 실행한다. 별도 job에서는 서로 다른 시험 클러스터를 만들어 물리 복원을 3회 실행한다.

원본 로그, 시험한 커밋, 의존성, 이미지 digest는 `ha-foundation-evidence`에 남긴다. 복원 결과·클러스터 ID·백업 해시·원본 PostgreSQL 복구 로그는 `ha-restore-development-evidence`에 남긴다. 보고할 때 반드시 같은 커밋의 아티팩트를 사용한다. 건너뛴 PostgreSQL 검사를 통과 수에 합산하지 않는다.

```bash
# 별도로 준비한 개발용 PostgreSQL에만 연결한다.
python -m pip install -r requirements-ha.txt -r services/ops-console/requirements.txt
PICKUP_HA_TEST=1 PICKUP_PG_TEST_DSN='<isolated test DSN>' PYTHONPATH=.:services/reconciler python -m pytest -q
# Docker가 있는 개발 호스트에서 새 시험 자원만 만든다.
PICKUP_HA_TEST=1 PYTHONPATH=.:services/reconciler python scripts/verify_ha_restore.py --repeat 3 --output verification/ha-restore/runs
```

## 아직 완료되지 않은 것

전체 주문·가맹점 저장소의 PostgreSQL 이식과 기존 고객 흐름 통합, 여러 앱/작업자/가맹점 실행 연결, Patroni·쿼럼·이전 주 서버 차단, 독립된 실제 호스트 장애 시험, 암호화된 외부 백업 저장소와 업로드 영수증, 외부 저장소에서의 전체 서비스 복원은 남아 있다.

현재 복원 시험의 백업은 **같은 CI 호스트의 별도 Docker 볼륨**이다. 실제 호스트 상실, 외부 저장소 가용성, HA 운영, 데이터 손실 0 또는 운영 RTO/RPO를 입증하지 않는다. 측정한 시간은 작은 개발 데이터로 진행한 복원 시험의 시간일 뿐이다. 가상 결제 원장·작업 기록의 복원을 전체 주문·매장·포인트 복원 성공으로 확대하지 않는다.

실제 다중 호스트와 외부 저장소는 접근 권한·비용·파괴 시험 범위가 승인된 뒤 별도 게이트에서 검증한다. 유료 클라우드 자원이나 실제 금전 거래는 이 브랜치의 실행기가 생성하지 않는다.
