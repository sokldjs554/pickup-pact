# 암호화된 네트워크 백업과 복원

이 경로는 주문·가맹점·가상 결제의 PostgreSQL 물리 백업을 암호화 저장소에 보관하고, 별도로 신뢰한 복원 영수증으로 받아 검증한다. 기존 공개 서비스의 저장소나 실행 모드를 바꾸지 않는다.

## 데이터와 인증의 경계

`ResticArchive`는 HTTPS REST 저장소만 사용한다. 설정한 서버 주소, 서버 인증서, 암호화 암호, 서버 접근 자격증명을 각각 확인한다. 사용자 이름과 서버 암호를 URL에 넣지 않고 비공개 파일로 공급한다. 환경의 임의 비밀번호 명령이나 프록시 설정은 상속하지 않는다.

복원 영수증은 저장소 ID, 전체 스냅샷 ID, PostgreSQL 클러스터 ID, 별도로 보관한 백업 해시, 원본 코드와 목표 WAL 위치를 묶는다. `latest`나 짧은 스냅샷 접두어를 사용하지 않는다. 정상 업로드 응답만으로 영수증을 발행하지 않으며, 저장된 데이터를 실제로 다시 내려받아 원래 파일 목록·크기·해시와 대조한다.

백업 작성자는 append-only REST 서버를 이용한다. 이전 스냅샷 삭제·덮어쓰기는 서버에서 차단한다. 이것은 관리자나 저장소 자체를 잃는 상황까지 방지한다는 의미가 아니다. 운영 보존 정책과 별도 유지보수 권한은 배포 단계에서 정해야 한다.

## 복원 절차

1. 직접 만든 시험 데이터베이스 세 개에서 주문·매장·결제를 처리한다.
2. PostgreSQL 기본 백업과 필요한 WAL을 검증하고 암호화 HTTPS 저장소에 보관한다.
3. 복원 영수증을 별도로 보존한다. 암호화 암호와 서버 자격증명은 백업에 넣지 않는다.
4. 시험 원본 DB 컨테이너·데이터 볼륨·로컬 아카이브·복사본을 제거한다.
5. 지정된 스냅샷을 새 대상에 내려받고 PostgreSQL을 지정한 복원 지점까지 복구한다.
6. 주문·매장·결제·혜택의 원본과 미완료 작업을 대조한다. 미완료 청구는 원래 요청키로 재개한다.

복원 대상은 기존 디렉터리나 운영 DB가 아니라 새로 만든 디렉터리와 볼륨이어야 한다. 원본을 읽지 못하거나 검증이 실패한 결과를 정상 또는 금액 0으로 처리하지 않는다.

## 검증의 범위

`.github/workflows/ha-encrypted-backup-review.yml`은 별도 HTTPS 서버, 암호화 저장소, PostgreSQL 볼륨을 **한 GitHub Actions 호스트**에서 생성한다. HTTP 통신·암호화·업로드·재다운로드·물리 복원은 실제 실행이며, 서버 위치가 다른 장애 영역에 있다는 증거는 아니다.

테스트 대상은 잘못된 암호화 키, 서버 자격증명, 신뢰하지 않는 인증서, 스냅샷 삭제, 저장소 중단, 저장 공간 부족, 암호화 파일 변조다. 실제 복원은 서로 다른 새 시험 환경으로 3회 반복한다. 결과는 해당 코드 SHA의 `ha-encrypted-network-restore-evidence`에서 확인한다. 테스트 데이터, 암호와 암호화 저장소 자체는 아티팩트에 게시하지 않는다.

실제 독립 호스트의 HA·외부 백업 운영까지 완료하려면 서버 배치, 쿼럼과 주 서버 차단, 서비스 TLS와 접근 역할, 백업 주기·보존·암호화 키 복구, 복구 세대 전환을 별도로 연결하고 시험해야 한다. 이 개발 경로만으로 운영 무손실·무중단 또는 운영 복구 시간을 주장하지 않는다.

## 실행

```bash
# 격리된 개발 호스트에서만 실행한다. 기존 서버나 삭제할 볼륨을 받지 않는다.
PICKUP_HA_TEST=1 PICKUP_BACKUP_TRANSPORT=restic_https_development \
PYTHONPATH=.:services/reconciler python scripts/verify_ha_restore.py \
  --repeat 3 --output verification/ha-encrypted-restore/runs
```

의존성과 버전은 위 CI 파일에서 고정한다. 결과의 코드 SHA·도구 빌드 정보·이미지 digest·스냅샷 식별자·백업 해시를 함께 보존한다. 측정 시간은 작은 합성 데이터의 개발 복원 시간이며 운영 RTO/RPO가 아니다.

기술 기준: [Restic HTTPS 저장소](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html), [복원](https://restic.readthedocs.io/en/stable/050_restore.html), [PostgreSQL WAL 복원](https://www.postgresql.org/docs/17/continuous-archiving.html).
