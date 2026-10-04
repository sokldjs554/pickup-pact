#!/usr/bin/env bash
# 올려 둔 파일의 소유자·권한을 정하고 앱 실행 환경(venv)을 만든다. 서비스는 시작하지 않는다.
# 사용: sudo bash -s -- <db>
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
id pickup >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin pickup
T=/etc/pickup-pact/tls; S=/etc/pickup-pact/secrets
chown root:root /etc/pickup-pact "$T" "$S"; chmod 0755 /etc/pickup-pact "$T" "$S"
chmod 0644 "$T"/*.crt
for k in postgres patroni etcd-client; do chown postgres:postgres "$T/$k.key"; done
chown etcd:etcd "$T/etcd.key"
for k in order merchant payment; do chown pickup:pickup "$T/$k.key"; done
chmod 0600 "$T"/*.key
chown root:root "$S"/*.env; chmod 0600 "$S"/*.env
chown pickup:pickup "$S"/*.pgpass; chmod 0600 "$S"/*.pgpass
chown root:root /etc/pickup-pact/*.yml /etc/pickup-pact/*.env; chmod 0644 /etc/pickup-pact/*.yml /etc/pickup-pact/*.env
install -d -o postgres -g postgres -m 0700 /var/lib/postgresql/17 /var/lib/pickup-pact/wal-spool
install -d -o etcd -g etcd -m 0700 /var/lib/etcd
chown root:root /usr/local/bin/pickup-wal-archive /usr/local/bin/pickup-restore-bootstrap
chmod 0755 /usr/local/bin/pickup-wal-archive /usr/local/bin/pickup-restore-bootstrap
chown -R root:root /opt/pickup-pact
if [ ! -x /opt/pickup-pact/.venv/bin/python ]; then python3 -m venv /opt/pickup-pact/.venv; fi
/opt/pickup-pact/.venv/bin/pip install -q --disable-pip-version-check -r /opt/pickup-pact/requirements-ha.txt
systemctl daemon-reload
echo "finalized=yes"
echo "python=$(/opt/pickup-pact/.venv/bin/python --version)"
