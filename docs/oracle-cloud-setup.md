# Oracle Cloud 시험 환경 준비 (A안: PAYG + 예산 경보)

Task 5의 실제 독립 호스트 시험(HA-01~10, DR-01~08)을 위한 준비 절차다. 계정·결제·서버 생성은 소유자가 직접 한다. 자동화 쪽에는 클라우드 계정 권한을 주지 않는다. 개인키·비밀번호는 채팅이나 GitHub 저장소에 올리지 않는다.

## 0. 예상 비용과 원칙

ARM(A1) 1 OCPU·6GB 서버 3대와 소형 x86 백업 서버 1대를 쓴다.

| 경우 | 3일 시험 | 한 달 방치 |
|---|---|---|
| 유료 계정 무료분이 월 3,000 OCPU시간·18,000GB시간으로 유지된 경우 | 0원 | 0원 |
| 무료분이 2 OCPU·12GB로 줄어든 경우 | 약 2,000원 | 약 19,000원 |
| 무료분이 전혀 적용되지 않는 경우 | 약 6,000원 | 약 57,000원 |

A1 요금은 OCPU 1개당 시간 $0.01, 메모리 1GB당 시간 $0.0015이며 1달러는 1,400원으로 가정했다. 실제 청구액은 Oracle 청구 화면을 기준으로 한다.

- 시험이 끝나면 서버·디스크·네트워크를 모두 삭제한다(8장).
- 아래 사양 외의 서버는 만들지 않는다.

## 1. 가입과 홈 리전

1. https://www.oracle.com/cloud/free/ 에서 가입한다. 카드 인증용 소액 승인이 잠깐 잡혔다가 취소될 수 있다.
2. 홈 리전은 가입 후 바꿀 수 없다. 한국 리전은 쓰지 않는다.
   - 서울은 가입 화면에서 홈 리전으로 고를 수 없다.
   - 춘천은 A1 서버를 만들 수 없다.
   - 가까운 **Japan Central (Osaka)** 또는 **Japan East (Tokyo)**를 고른다.
     - 시험 트래픽은 대부분 같은 리전 서버끼리 오가므로 거리는 결과에 거의 영향이 없다.
     - SSH·브라우저 접속만 조금 느려진다.
   - 일본 리전도 가용 영역은 1개이고, 그 안의 fault domain 3개로 서버를 나눈다.
   - A1 서버가 "Out of capacity"로 만들어지지 않는 경우가 잦다. 그때는 시간을 두고 다시 시도하거나, 다른 fault domain을 먼저 만든다.

## 2. PAYG 전환과 예산 경보 (서버를 만들기 전에)

1. 콘솔 오른쪽 위 프로필 → **Upgrade and Manage Payment** → **Pay As You Go**로 전환한다.
2. 메뉴 **Billing & Cost Management → Budgets → Create Budget**에서 예산을 만든다.
   - Target: Compartment(root)
   - Budget amount: **1** (USD, 약 1,400원)
   - Alert rule: Actual spend 50%와 100%에서 본인 이메일로 알림
3. 시험 기간 동안 **Cost Analysis** 화면을 하루 한 번 확인한다.

## 3. 네트워크 (VCN)

1. **Networking → Virtual Cloud Networks → Start VCN Wizard → Create VCN with Internet Connectivity**를 선택한다.
   - 이름: `pact-ha-vcn`
   - VCN CIDR: `10.0.0.0/16`
   - Public subnet: `10.0.1.0/24`
   - Private subnet: `10.0.2.0/24` (사용하지 않아도 됨)
2. Public subnet의 **Security List**에서 Ingress 규칙을 아래만 남긴다.

| 출발지 | 프로토콜/포트 | 용도 |
|---|---|---|
| 내 공인 IP/32 (또는 0.0.0.0/0, 키 인증만 사용) | TCP 22 | SSH |
| 10.0.0.0/16 | TCP 2379-2380 | etcd |
| 10.0.0.0/16 | TCP 5432 | PostgreSQL (hostssl+SCRAM만 허용) |
| 10.0.0.0/16 | TCP 8008 | Patroni REST (클라이언트 인증서 필수) |
| 10.0.0.0/16 | TCP 8000, 8443-8445 | 주문 앱, 가맹점·가상 PG·알림 수신기 (mTLS) |
| 10.0.0.0/16 | TCP 8000 (백업 서버) | Restic REST 저장소 (HTTPS) |
| 0.0.0.0/0 | TCP 443 | 공개 진입점 (pact-a, pact-b만) |

기본으로 들어 있는 ICMP 규칙은 그대로 둔다. 3306 같은 다른 포트는 열지 않는다.

## 4. SSH 키

본인 PC에서 만든다. **개인키는 절대 공유하지 않는다.**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/pact-ha -C pact-ha
cat ~/.ssh/pact-ha.pub   # 서버를 만들 때 이 공개키를 붙여 넣는다
```

## 5. 서버 4대

**Compute → Instances → Create instance**에서 아래처럼 만든다. 각 서버의 **Show advanced options → Networking**에서 Private IP를 직접 지정한다.

| 이름 | Shape | 이미지 | Fault domain | Private IP | 역할 |
|---|---|---|---|---|---|
| pact-a | VM.Standard.A1.Flex, **1 OCPU · 6 GB** | Ubuntu 24.04 (aarch64) | FAULT-DOMAIN-1 | 10.0.1.11 | PostgreSQL·etcd·주문·가맹점·결제·진입점 |
| pact-b | VM.Standard.A1.Flex, **1 OCPU · 6 GB** | Ubuntu 24.04 (aarch64) | FAULT-DOMAIN-2 | 10.0.1.12 | 위와 같음 |
| pact-c | VM.Standard.A1.Flex, **1 OCPU · 6 GB** | Ubuntu 24.04 (aarch64) | FAULT-DOMAIN-3 | 10.0.1.13 | PostgreSQL·etcd·주문·가맹점·결제 |
| pact-backup | VM.Standard.E2.1.Micro (Always Free) | Ubuntu 24.04 | 아무 곳 | 10.0.1.20 | Restic append-only 저장소 |

- Boot volume은 기본값(약 47GB)을 쓴다. 4대 합계가 무료 200GB 안에 들어간다.
- Subnet은 `pact-ha-vcn`의 public subnet이고, **Assign a public IPv4 address**를 켠다.
- SSH keys에는 4장의 공개키를 붙여 넣는다.
- 만든 뒤 각 서버의 **공인 IP**와 **OCID**를 메모한다.

## 6. 이 환경의 한계 (결과 문서에 그대로 적는다)

- 네 서버가 같은 리전·같은 가용 영역 안의 서로 다른 fault domain에 있다. 데이터센터 전체 장애는 시험 범위가 아니다.
- 백업 서버는 DB 서버 중 하나와 같은 fault domain을 공유한다. "다른 장애 영역의 외부 저장소"보다 약한 조건이다.
  - 인벤토리 검사기의 `deploy` 단계는 이 조건을 거부한다. 승인 기록에 예외 사유를 남기고, 증거의 범위 이름을 `oracle_single_ad_fault_domains`로 구분한다.
- watchdog는 Ubuntu `softdog` 모듈로 쓴다. 실제 하드웨어 watchdog가 아니라 커널 소프트웨어 타이머다.

## 7. 준비가 끝나면 알려줄 정보

공개해도 되는 정보만 보낸다.

- 홈 리전 이름
- 서버 4대의 공인 IP (private IP가 위 표와 다르면 그것도)
- 공개 진입점 도메인이 있으면 그 이름. 없으면 공인 IP로 시험한다.
- 승인 기록용: 승인자 이름, 승인일, 월 비용 상한(예: 1달러), 파괴 시험 기간

**보내지 않는 것:** 개인키, Oracle 비밀번호, API 키.

배포와 시험은 GitHub Actions 워크플로가 SSH로 실행한다. 개인키는 GitHub 저장소의 **Settings → Secrets and variables → Actions**에 `PACT_HA_SSH_KEY`로 직접 넣는다. 이 작업 공간에서는 SSH 접속이 차단되어 있어 서버에 직접 접속하지 않는다.

## 8. 시험 후 삭제 (필수)

1. **Compute → Instances**: 4대 모두 **Terminate**, "Permanently delete the attached boot volume" 체크
2. **Block Storage → Boot Volumes / Block Volumes**: 남은 디스크가 없는지 확인
3. **Networking → Virtual Cloud Networks**: `pact-ha-vcn` 삭제
4. **Networking → Reserved Public IPs**: 남은 IP가 없는지 확인
5. 다음 날 **Cost Analysis**에서 비용이 더 늘지 않았는지 확인한다.
6. GitHub Secret `PACT_HA_SSH_KEY`를 삭제하고, 본인 PC의 `~/.ssh/pact-ha` 키도 폐기한다.
