#!/usr/bin/env bash
# 시험 서버에 패키지·사용자·방화벽·watchdog 권한을 설치한다. 인증서·비밀번호·설정은 다루지 않는다(deploy 단계).
# 사용: sudo bash -s -- <db|backup> <사설망 CIDR> <"ip=name,ip=name,...">
# 같은 서버에 다시 실행해도 결과가 같다. 끝에 key=value 줄로 설치된 버전을 출력한다.
set -euo pipefail
ROLE="$1"; CIDR="$2"; HOSTS="$3"
export DEBIAN_FRONTEND=noninteractive
ETCD_VERSION=3.5.17;       ETCD_SHA256=eff6ac621d41711085d0f38fab17d8fa3705f6326c3ff11301a1f5a71fc94edd
RESTIC_VERSION=0.19.1;     RESTIC_SHA256=f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c
REST_SERVER_VERSION=0.14.0; REST_SERVER_SHA256=4c9c95bc079a0334e81fad379b19dc5c3353c71c2c88d652cafce2081c2b1c66
PATRONI_VERSION=4.0.4;     PSYCOPG_VERSION=3.2.3
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

fetch() {  # url sha256 out
  curl -fsSL --retry 3 --max-time 300 -o "$3" "$1"
  echo "$2  $3" | sha256sum -c --quiet -
}

# --- 이름 해석: 인증서 호스트 이름 확인(verify-full)이 쓰는 내부 DNS를 /etc/hosts로 고정
sed -i '/# pickup-pact-begin/,/# pickup-pact-end/d' /etc/hosts
{
  echo '# pickup-pact-begin'
  IFS=, read -ra pairs <<< "$HOSTS"
  for pair in "${pairs[@]}"; do echo "${pair%%=*} ${pair##*=}.pact.internal ${pair##*=}"; done
  echo '# pickup-pact-end'
} >> /etc/hosts

# --- 서버 방화벽: Oracle Ubuntu 이미지는 22번만 연다. 필요한 포트만 사설망에서 연다.
if [ "$ROLE" = db ]; then PORTS=2379,2380,5432,8008,8000,8443,8444,8445; else PORTS=8000; fi
iptables -C INPUT -s "$CIDR" -p tcp -m multiport --dports "$PORTS" -j ACCEPT 2>/dev/null \
  || iptables -I INPUT 1 -s "$CIDR" -p tcp -m multiport --dports "$PORTS" -j ACCEPT
apt-get update -qq
apt-get install -y -qq --no-install-recommends curl ca-certificates bzip2 iptables-persistent chrony >/dev/null
netfilter-persistent save >/dev/null

# --- restic (DB 서버는 WAL 업로드용, 백업 서버는 확인용)
fetch "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_amd64.bz2" "$RESTIC_SHA256" "$WORK/restic.bz2"
bzip2 -dc "$WORK/restic.bz2" > "$WORK/restic" && install -m 0755 "$WORK/restic" /usr/local/bin/restic

if [ "$ROLE" = backup ]; then
  fetch "https://github.com/restic/rest-server/releases/download/v${REST_SERVER_VERSION}/rest-server_${REST_SERVER_VERSION}_linux_amd64.tar.gz" "$REST_SERVER_SHA256" "$WORK/rs.tgz"
  tar -xzf "$WORK/rs.tgz" -C "$WORK" && install -m 0755 "$(find "$WORK" -name rest-server -type f | head -1)" /usr/local/bin/rest-server
  id restic >/dev/null 2>&1 || useradd --system --home /var/lib/restic-repo --shell /usr/sbin/nologin restic
  install -d -o restic -g restic -m 0700 /var/lib/restic-repo
  apt-get install -y -qq --no-install-recommends apache2-utils openssl >/dev/null
else
  # --- PostgreSQL 17 (PGDG). 지정 마이너가 있으면 고정하고, 없으면 설치된 값을 증거에 남긴다.
  apt-get install -y -qq --no-install-recommends postgresql-common gnupg >/dev/null
  /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y >/dev/null 2>&1
  apt-get update -qq
  if apt-cache madison postgresql-17 | grep -q ' 17\.11-'; then
    apt-get install -y -qq --no-install-recommends postgresql-17=17.11-* postgresql-client-17=17.11-* >/dev/null
  else
    apt-get install -y -qq --no-install-recommends postgresql-17 postgresql-client-17 >/dev/null
  fi
  # apt가 만든 기본 클러스터는 우리가 방금 만든 빈 자원이다. Patroni가 데이터 디렉터리를 소유하므로 지운다.
  systemctl disable --now postgresql >/dev/null 2>&1 || true
  if pg_lsclusters -h 2>/dev/null | grep -q '^17 main'; then pg_dropcluster 17 main --stop; fi
  apt-get install -y -qq --no-install-recommends python3-venv python3-dev >/dev/null
  python3 -m venv /opt/patroni
  /opt/patroni/bin/pip install -q --disable-pip-version-check "patroni[etcd3]==${PATRONI_VERSION}" "psycopg[binary]==${PSYCOPG_VERSION}"
  fetch "https://github.com/etcd-io/etcd/releases/download/v${ETCD_VERSION}/etcd-v${ETCD_VERSION}-linux-amd64.tar.gz" "$ETCD_SHA256" "$WORK/etcd.tgz"
  tar -xzf "$WORK/etcd.tgz" -C "$WORK"
  install -m 0755 "$WORK/etcd-v${ETCD_VERSION}-linux-amd64/etcd" "$WORK/etcd-v${ETCD_VERSION}-linux-amd64/etcdctl" /usr/local/bin/
  id etcd >/dev/null 2>&1 || useradd --system --home /var/lib/etcd --shell /usr/sbin/nologin etcd
  install -d -o etcd -g etcd -m 0700 /var/lib/etcd
  install -d -m 0755 /etc/pickup-pact
  install -d -o root -g root -m 0750 /etc/pickup-pact/tls /etc/pickup-pact/secrets
  install -d -o postgres -g postgres -m 0700 /var/lib/pickup-pact/patroni /var/lib/pickup-pact/wal-spool 2>/dev/null \
    || { install -d -m 0755 /var/lib/pickup-pact; install -d -o postgres -g postgres -m 0700 /var/lib/pickup-pact/patroni /var/lib/pickup-pact/wal-spool; }
  # --- watchdog: softdog를 부팅 때마다 불러오고 Patroni(postgres 사용자)만 열게 한다.
  echo softdog > /etc/modules-load.d/softdog.conf
  modprobe softdog
  echo 'KERNEL=="watchdog", OWNER="postgres", GROUP="postgres", MODE="0600"' > /etc/udev/rules.d/60-patroni-watchdog.rules
  udevadm control --reload-rules && udevadm trigger /dev/watchdog || true
  chown postgres:postgres /dev/watchdog; chmod 0600 /dev/watchdog
fi

systemctl enable --now chrony >/dev/null 2>&1 || true
echo "role=$ROLE"
echo "restic=$(/usr/local/bin/restic version | cut -d' ' -f2)"
if [ "$ROLE" = backup ]; then
  echo "rest_server=$(/usr/local/bin/rest-server --version 2>&1 | head -1 | tr -d '\n')"
else
  echo "postgresql=$(/usr/lib/postgresql/17/bin/postgres --version)"
  echo "patroni=$(/opt/patroni/bin/patroni --version)"
  echo "etcd=$(/usr/local/bin/etcd --version | head -1)"
  echo "watchdog=$(stat -c '%U:%a' /dev/watchdog)"
  echo "default_cluster_removed=$(pg_lsclusters -h | wc -l)"
fi
echo "firewall=$(iptables -S INPUT | grep -c "$CIDR")"
echo "time_synced=$(timedatectl show -p NTPSynchronized --value)"
