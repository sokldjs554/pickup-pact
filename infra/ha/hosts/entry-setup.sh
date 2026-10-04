#!/usr/bin/env bash
# 진입점(HAProxy) 서버 준비: 패키지, 공개 TLS 인증서, 백엔드 mTLS 클라이언트 신원, 방화벽. 설정 파일과 시작은 러너가 한다.
# 사용: sudo bash -s -- <이 서버의 사설 IP> <사설망 CIDR>
set -euo pipefail
IP="$1"; CIDR="$2"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends haproxy openssl >/dev/null
T=/etc/pickup-pact/tls
# 공개 TLS: entry.pact.internal용 자체 서명 인증서. 개인키는 이 서버를 떠나지 않는다.
if [ ! -f "$T/public.pem" ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 5 -subj /CN=entry.pact.internal \
    -addext "subjectAltName=DNS:entry.pact.internal,IP:$IP" -keyout /run/entry-public.key -out "$T/public.crt" 2>/dev/null
  cat "$T/public.crt" /run/entry-public.key > "$T/public.pem"; rm -f /run/entry-public.key
fi
# 앱으로 가는 구간의 mTLS 클라이언트 신원: 시험 PKI는 배포 뒤 CA 키를 버렸으므로 etcd-client 잎 인증서를 재사용한다.
cat "$T/etcd-client.crt" "$T/etcd-client.key" > "$T/entry.pem"
chown root:haproxy "$T/public.pem" "$T/entry.pem"; chmod 0640 "$T/public.pem" "$T/entry.pem"
iptables -C INPUT -s "$CIDR" -p tcp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -s "$CIDR" -p tcp --dport 443 -j ACCEPT
netfilter-persistent save >/dev/null
systemctl disable --now haproxy >/dev/null 2>&1 || true
echo "haproxy=$(haproxy -v | head -1)"
