"""Service-to-service mTLS for ha_postgres_v1: server identity, caller role, no downgrade.

Every case runs over real TCP with a disposable private CA. All listeners are on
one development host, so this is transport evidence, never multi-host HA evidence.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time

import httpx
import pytest

from demo.route.ha.transport import ServerTLS, TransportPolicy, require_role, PeerRejected
from demo.route.payments.http_client import PaymentClient, PaymentUnavailable
from demo.route.payments.runtime import _wait_ready_json
from tls_helpers import PrivateAuthority, tls_env

ROOT = Path(__file__).resolve().parents[2]
TOKEN = secrets.token_hex(32)
MERCHANT_TOKEN = secrets.token_hex(32)
SECRET = secrets.token_hex(32)


@pytest.fixture(scope='module')
def pki(tmp_path_factory):
    root = tmp_path_factory.mktemp('pki')
    ca = PrivateAuthority(root/'ca')
    certs = {role: ca.issue(role, role=role) for role in ['order', 'merchant', 'payment']}
    certs['payment-elsewhere'] = ca.issue('payment-elsewhere', role='payment', dns=('other.internal',), ips=())
    certs['two-roles'] = ca.issue('two-roles', role=None, roles=['order', 'payment'])
    certs['no-role'] = ca.issue('no-role', role=None)
    rogue = PrivateAuthority(root/'rogue', 'rogue-ca')
    certs['rogue-order'] = rogue.issue('rogue-order', role='order')
    return ca, certs


def policy(pki, name, role=None, hosts=('localhost',), generation=1):
    ca, certs = pki
    cert, key = certs[name]
    return TransportPolicy.mtls(role=role or name, allowed_hosts=list(hosts), ca_file=str(ca.cert),
                                cert_file=cert, key_file=key, generation=generation)


@contextmanager
def role_process(tmp_path, pki, kind, cert_name, accept, extra_args=(), generation=1, directory=None):
    ca, certs = pki
    if directory is None:
        directory = tmp_path/(kind+'-'+secrets.token_hex(4))
        directory.mkdir()
    marker = directory/('ready-'+secrets.token_hex(4)+'.json')
    env = {**os.environ, **tls_env(ca, *certs[cert_name], generation=generation), 'PYTHONPATH': str(ROOT),
           'ROUTE_PAYMENT_TOKEN': TOKEN, 'ROUTE_MERCHANT_TOKEN': MERCHANT_TOKEN,
           'ROUTE_PAYMENT_NOTIFY_SECRET': SECRET}
    process = subprocess.Popen([sys.executable, str(ROOT/'tests/ha/tls_launch.py'), kind, '--role', cert_name,
                                '--accept', accept, '--directory', str(directory), '--ready-file', str(marker),
                                *extra_args], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        ready = _wait_ready_json(marker, process, threading.Event(), time.monotonic()+15)
        assert ready['pid'] == process.pid
        yield ready['url'], directory
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
        process.stderr.close()


def approval(order='PCT-mtls', key='mtls-approve', auth='AUTH-mtls'):
    return dict(action='AUTHORIZE', world_id='world', order_id=order, operation_key=key,
                authorization_id=auth, amount_krw=3200, currency='KRW', payment_revision=1,
                quote_fingerprint='a'*64, card_token='demo-approved')


@contextmanager
def raw_tls_server(pki, cert_name):
    """Records every byte received after the handshake (to prove tokens are withheld)."""
    ca, certs = pki
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*certs[cert_name])
    context.load_verify_locations(cafile=str(ca.cert))
    context.verify_mode = ssl.CERT_REQUIRED
    listener = socket.create_server(('127.0.0.1', 0))
    received = bytearray()
    done = threading.Event()

    def run():
        try:
            listener.settimeout(10)
            conn, _ = listener.accept()
            with context.wrap_socket(conn, server_side=True) as tls:
                tls.settimeout(2)
                while True:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    received.extend(chunk)
        except OSError:
            pass
        finally:
            done.set()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield 'https://localhost:'+str(listener.getsockname()[1]), received, done
    finally:
        listener.close()
        thread.join(5)


def test_loopback_policy_is_the_existing_development_contract():
    loopback = TransportPolicy.loopback()
    assert loopback.origin('http://127.0.0.1:19002/') == 'http://127.0.0.1:19002'
    for url in ['https://localhost:19002', 'http://localhost:19002', 'http://10.0.0.2:5']:
        with pytest.raises(ValueError):
            loopback.origin(url)
    assert not loopback.remote and loopback.loopback_only
    with pytest.raises(ValueError):
        TransportPolicy('loopback_development', role='order')
    # Without a policy the clients keep their original loopback-only validation.
    with pytest.raises(ValueError):
        PaymentClient('https://pay-a.pact.internal:8443', TOKEN)


def test_mtls_origin_requires_allowlisted_https_with_explicit_port(pki):
    remote = policy(pki, 'order', hosts=('pay-a.pact.internal', 'localhost'))
    assert remote.origin('https://PAY-A.pact.internal:8443/') == 'https://PAY-A.pact.internal:8443'
    assert remote.origin('https://pay-a.pact.internal:8443/internal/payments/events', callback=True)
    for url in ['http://pay-a.pact.internal:8443', 'https://pay-b.pact.internal:8443',
                'https://pay-a.pact.internal', 'https://u:p@pay-a.pact.internal:8443',
                'https://pay-a.pact.internal:8443/v1', 'https://pay-a.pact.internal:8443/?x=1',
                'https://pay-a.pact.internal:8443#f', 'https://pay-a.pact.internal:99999']:
        with pytest.raises(ValueError):
            remote.origin(url)
    with pytest.raises(ValueError):
        remote.origin('https://pay-a.pact.internal:8443', callback=True)
    assert remote.has_loopback_host and not remote.loopback_only


def test_mtls_settings_require_private_absolute_files_and_explicit_roles(pki, tmp_path):
    ca, certs = pki
    cert, key = certs['order']
    loose = tmp_path/'loose.key'
    loose.write_bytes(Path(key).read_bytes())
    os.chmod(loose, 0o644)
    good = dict(role='order', allowed_hosts=['localhost'], ca_file=str(ca.cert), cert_file=cert, key_file=key)
    assert TransportPolicy.mtls(**good).remote
    for change in [dict(key_file=str(loose)), dict(key_file='relative.key'), dict(ca_file=str(tmp_path/'missing')),
                   dict(allowed_hosts=[]), dict(allowed_hosts='localhost'), dict(allowed_hosts=['a..b']),
                   dict(allowed_hosts=['localhost', 'LOCALHOST']), dict(role='admin'), dict(role='')]:
        with pytest.raises(ValueError):
            TransportPolicy.mtls(**(good | change))
    env = tls_env(ca, cert, key)
    assert TransportPolicy.from_env(env, role='order').allowed_hosts == frozenset({'localhost'})
    for name in env:
        with pytest.raises(ValueError):
            TransportPolicy.from_env(env | {name: ''}, role='order')
    for hosts in ['localhost', '{"a":1}', '[']:
        with pytest.raises(ValueError):
            TransportPolicy.from_env(env | {'PICKUP_INTERNAL_HOSTS': hosts}, role='order')
    for generation in ['0', '-1', '01', '1.5', 'x', '1000001']:
        with pytest.raises(ValueError):
            TransportPolicy.from_env(env | {'PICKUP_OPERATING_GENERATION': generation}, role='order')
    assert TransportPolicy.from_env(env | {'PICKUP_OPERATING_GENERATION': '7'}, role='order').headers() == \
        {'X-Pickup-Generation': '7'}
    assert TransportPolicy.loopback().headers() == {}
    remote = TransportPolicy.from_env(env, role='order')
    assert str(ca.cert) not in repr(remote) and key not in repr(remote)
    for bind, advertise in [('localhost', 'localhost'), ('127.0.0.1', 'other.internal')]:
        with pytest.raises(ValueError):
            ServerTLS(remote, frozenset({'payment'}), bind, advertise)
    with pytest.raises(ValueError):
        ServerTLS(remote, frozenset({'auditor'}), '127.0.0.1', 'localhost')
    with pytest.raises(ValueError):
        ServerTLS(TransportPolicy.loopback(), frozenset({'order'}), '127.0.0.1', 'localhost')


def test_certificate_role_must_be_single_known_and_accepted():
    uri = 'urn:pickup-pact:role:'
    assert require_role({'subjectAltName': (('DNS', 'x'), ('URI', uri+'order'))}, {'order'}) == 'order'
    for names in [(), (('URI', uri+'order'), ('URI', uri+'payment')), (('URI', uri+'admin'),),
                  (('DNS', uri+'order'),), (('URI', 'spiffe://x/order'),)]:
        with pytest.raises(PeerRejected):
            require_role({'subjectAltName': names}, {'order', 'payment', 'admin'})
    with pytest.raises(PeerRejected):
        require_role({'subjectAltName': (('URI', uri+'merchant'),)}, {'order'})
    with pytest.raises(PeerRejected):
        require_role(None, {'order'})


def test_payment_round_trip_over_mtls_and_only_the_order_role_is_served(tmp_path, pki):
    with role_process(tmp_path, pki, 'payment', 'payment', 'order') as (url, _):
        assert url.startswith('https://localhost:')
        client = PaymentClient(url, TOKEN, timeout=2, transport=policy(pki, 'order'))
        assert client.health()['storage_ready'] is True
        request = approval()
        approved = client.execute(request)
        assert approved['ok'] and approved['outcome'] == 'AUTHORIZED'
        assert client.execute(request) == approved
        assert client.operation(request) == approved
        for name, role in [('merchant', 'merchant'), ('two-roles', 'order'), ('no-role', 'order'),
                           ('rogue-order', 'order')]:
            with pytest.raises(PaymentUnavailable):
                PaymentClient(url, TOKEN, timeout=2, transport=policy(pki, name, role=role)).health()
        # TLS alone is not authorization: the role token is still required.
        with pytest.raises(PaymentUnavailable):
            PaymentClient(url, 'x'*64, timeout=2, transport=policy(pki, 'order')).health()
        ca, _ = pki
        with httpx.Client(verify=ssl.create_default_context(cafile=str(ca.cert)), trust_env=False) as anonymous:
            with pytest.raises(httpx.HTTPError):
                anonymous.get(url+'/health', headers={'Authorization': 'Bearer '+TOKEN})
        with httpx.Client(trust_env=False) as plaintext:
            with pytest.raises(httpx.HTTPError):
                plaintext.get(url.replace('https://', 'http://')+'/health', headers={'Authorization': 'Bearer '+TOKEN})
        proof = client.snapshot('world', 'PCT-mtls')
        assert len(proof['authorizations']) == 1 and proof['held_krw'] == 3200 and proof['capture_count'] == 0


def test_client_withholds_token_from_a_server_with_another_role(pki):
    with raw_tls_server(pki, 'merchant') as (url, received, done):
        with pytest.raises(PaymentUnavailable):
            PaymentClient(url, TOKEN, timeout=2, transport=policy(pki, 'order')).health()
        assert done.wait(5)
    assert bytes(received) == b''


def test_client_rejects_a_valid_role_certificate_for_another_hostname(pki):
    with raw_tls_server(pki, 'payment-elsewhere') as (url, received, done):
        with pytest.raises(PaymentUnavailable):
            PaymentClient(url, TOKEN, timeout=2, transport=policy(pki, 'order')).health()
        assert done.wait(5)
    assert bytes(received) == b''


def test_merchant_over_mtls_serves_only_the_order_role(tmp_path, pki):
    from demo.route.ha.replica_clients import ReplicaMerchantFleet
    with role_process(tmp_path, pki, 'merchant', 'merchant', 'order') as (url, _):
        fleet = ReplicaMerchantFleet([url], MERCHANT_TOKEN, timeout=2, transport=policy(pki, 'order'))
        assert fleet.health()['storage_ready'] is True
        from demo.route.merchant_fleet import CAPACITY
        assert set(fleet.snapshot('world')) == set(CAPACITY)
        wrong = ReplicaMerchantFleet([url], MERCHANT_TOKEN, timeout=2, transport=policy(pki, 'payment'))
        with pytest.raises(OSError):
            wrong.health()
        # A client expecting the payment role refuses the merchant listener before sending a token.
        with pytest.raises(PaymentUnavailable):
            PaymentClient(url, TOKEN, timeout=2, transport=policy(pki, 'order')).health()


def test_notification_receiver_accepts_only_the_payment_role(tmp_path, pki):
    with role_process(tmp_path, pki, 'notification', 'order', 'payment') as (url, _):
        assert url.endswith('/internal/payments/events')
        body = b'{}'
        headers = {'Content-Type': 'application/json', 'X-Payment-Timestamp': str(int(time.time())),
                   'X-Payment-Signature': '0'*64, **policy(pki, 'payment').headers()}
        with policy(pki, 'payment').http_client(2, server_role='order') as client:
            response = client.post(url, content=body, headers=headers)
        # The request reached the inbox: TLS role and generation passed, the HMAC did not.
        assert response.status_code == 403 and response.json() == {'code': 'INVALID_EVENT_SIGNATURE'}
        with policy(pki, 'payment').http_client(2, server_role='order') as client:
            unlabeled = client.post(url, content=body, headers={k: v for k, v in headers.items()
                                                                  if k != 'X-Pickup-Generation'})
        assert unlabeled.status_code == 503
        for name in ['order', 'merchant']:
            with policy(pki, name).http_client(2, server_role='order') as client:
                with pytest.raises(httpx.HTTPError):
                    client.post(url, content=body, headers=headers)


def test_payment_notifications_are_delivered_over_mtls(tmp_path, pki):
    from demo.route.payments.notifications import PaymentInbox
    with role_process(tmp_path, pki, 'notification', 'order', 'payment') as (callback, directory):
        PaymentInbox(directory/'inbox.sqlite', SECRET).register('world', 'PCT-notify', 'AUTH-notify')
        with role_process(tmp_path, pki, 'payment', 'payment', 'order', ['--callback-url', callback]) as (url, _):
            client = PaymentClient(url, TOKEN, timeout=2, transport=policy(pki, 'order'))
            client.execute(approval('PCT-notify', 'notify-approve', 'AUTH-notify'))
            deadline = time.monotonic()+10
            rows = []
            while time.monotonic() < deadline and not rows:
                with sqlite3.connect(directory/'inbox.sqlite') as db:
                    rows = db.execute('SELECT world_id,order_id FROM payment_inbox').fetchall()
                time.sleep(.1)
            assert rows == [('world', 'PCT-notify')]


def test_ready_url_and_policy_never_carry_secrets(tmp_path, pki):
    with role_process(tmp_path, pki, 'payment', 'payment', 'order') as (url, directory):
        text = json.dumps(url)
        assert TOKEN not in text and 'BEGIN' not in text
        assert not any(TOKEN in p.read_text(errors='replace') for p in directory.glob('*') if p.is_file()
                       and p.suffix in {'.json', '.log'})


def test_previous_generation_is_refused_as_unavailable_while_records_stay_readable(tmp_path, pki):
    """DR-06: after restore + generation bump, a pre-restore process gets 503 (its
    operation stays pending under the ORIGINAL key); the current generation reads
    the restored record by that same key."""
    from demo.route.ha.replica_clients import ReplicaMerchantFleet
    shared = tmp_path/'restored'
    shared.mkdir()
    request = approval('PCT-generation', 'generation-approve', 'AUTH-generation')
    with role_process(tmp_path, pki, 'payment', 'payment', 'order', generation=1, directory=shared) as (url, _):
        before = PaymentClient(url, TOKEN, timeout=2, transport=policy(pki, 'order', generation=1)).execute(request)
    with role_process(tmp_path, pki, 'payment', 'payment', 'order', generation=2, directory=shared) as (url, _):
        stale = PaymentClient(url, TOKEN, timeout=2, transport=policy(pki, 'order', generation=1))
        for call in [stale.health, lambda: stale.operation(request), lambda: stale.execute(request)]:
            with pytest.raises(PaymentUnavailable):
                call()
        current = PaymentClient(url, TOKEN, timeout=2, transport=policy(pki, 'order', generation=2))
        assert current.operation(request) == before
        assert current.execute(request) == before
        proof = current.snapshot('world', 'PCT-generation')
        assert len(proof['authorizations']) == 1 and proof['held_krw'] == 3200
    with role_process(tmp_path, pki, 'merchant', 'merchant', 'order', generation=2) as (url, _):
        with pytest.raises(OSError):
            ReplicaMerchantFleet([url], MERCHANT_TOKEN, timeout=2, transport=policy(pki, 'order', generation=1)).snapshot('world')
        assert ReplicaMerchantFleet([url], MERCHANT_TOKEN, timeout=2,
                                    transport=policy(pki, 'order', generation=2)).snapshot('world')
    with role_process(tmp_path, pki, 'notification', 'order', 'payment', generation=2) as (url, _):
        headers = {'Content-Type': 'application/json', 'X-Payment-Timestamp': str(int(time.time())),
                   'X-Payment-Signature': '0'*64}
        with policy(pki, 'payment', generation=1).http_client(2, server_role='order') as client:
            response = client.post(url, content=b'{}', headers={**headers, 'X-Pickup-Generation': '1'})
        assert response.status_code == 503 and response.json() == {'code': 'STALE_GENERATION'}


def test_notifications_fail_over_to_another_order_receiver(tmp_path, pki):
    from demo.route.payments.notifications import PaymentInbox
    shared = tmp_path/'order-inbox'
    shared.mkdir()
    PaymentInbox(shared/'inbox.sqlite', SECRET).register('world', 'PCT-failover', 'AUTH-failover')
    with role_process(tmp_path, pki, 'notification', 'order', 'payment', directory=shared) as (first, _):
        with role_process(tmp_path, pki, 'notification', 'order', 'payment', directory=shared) as (second, _):
            pass  # stopped: the first configured receiver is now unreachable
        with role_process(tmp_path, pki, 'notification', 'order', 'payment', directory=shared) as (third, _):
            args = ['--callback-url', second, '--callback-url', third]
            with role_process(tmp_path, pki, 'payment', 'payment', 'order', args) as (url, _):
                client = PaymentClient(url, TOKEN, timeout=2, transport=policy(pki, 'order'))
                client.execute(approval('PCT-failover', 'failover-approve', 'AUTH-failover'))
                deadline = time.monotonic()+10
                rows = []
                while time.monotonic() < deadline and not rows:
                    with sqlite3.connect(shared/'inbox.sqlite') as db:
                        rows = db.execute('SELECT order_id FROM payment_inbox').fetchall()
                    time.sleep(.1)
                assert rows == [('PCT-failover',)]
    assert first != third


def test_payment_refuses_ambiguous_notification_targets(pki, tmp_path):
    from demo.route.payments.http_server import serve
    ca, certs = pki
    tls = ServerTLS(policy(pki, 'payment'), frozenset({'order'}), '127.0.0.1', 'localhost')
    target = 'https://localhost:1/internal/payments/events'
    for callbacks in [[target, target], [target.replace('1/', str(n)+'/') for n in range(2, 6)],
                      ['http://127.0.0.1:1/internal/payments/events'], ['https://localhost:1/other']]:
        with pytest.raises(ValueError):
            serve(str(tmp_path), TOKEN, 0, None, callbacks, SECRET, tls=tls)
    with pytest.raises(ValueError):
        serve(str(tmp_path), TOKEN, 0, None, ['http://127.0.0.1:1/internal/payments/events']*2, SECRET)
