"""Service-to-service transport policy for the native roles.

``loopback_development`` keeps the existing plaintext 127.0.0.1 contract.
``mtls_remote`` is the ``ha_postgres_v1`` transport: HTTPS only, an explicit
host allowlist, server identity checked against a private CA and hostname,
a client certificate required by every listener, and the calling role carried
in the certificate. Role tokens and the notification HMAC stay on top; TLS
does not replace them. Environment proxies and redirects are never followed.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import stat
from urllib.parse import urlsplit

ROLES = frozenset({'order', 'merchant', 'payment'})
ROLE_URI = 'urn:pickup-pact:role:'
GENERATION_HEADER = 'X-Pickup-Generation'
CALLBACK_PATH = '/internal/payments/events'
HOST = re.compile(r'^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$')
LOOPBACK_NAMES = frozenset({'localhost', 'localhost.localdomain', 'ip6-localhost'})


class PeerRejected(ssl.SSLError):
    """The TLS peer is authentic but holds a role this endpoint does not accept."""


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES or host.endswith('.localhost'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host(value) -> str:
    if not isinstance(value, str):
        raise ValueError('internal host names must be strings')
    host = value.strip().lower()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not HOST.fullmatch(host):
        raise ValueError('invalid internal host name')
    return host


def _private_file(path: str, *, secret: bool) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ValueError('TLS file path required')
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError('TLS file paths must be absolute')
    try:
        info = candidate.stat()
    except OSError:
        raise ValueError('configured TLS file is not readable') from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('TLS file must be a regular file')
    if secret and info.st_mode & 0o077:
        raise ValueError('private key must not be readable by group or others')
    return str(candidate)


def peer_roles(certificate: dict | None) -> set[str]:
    names = (certificate or {}).get('subjectAltName', ())
    return {value[len(ROLE_URI):] for kind, value in names
            if kind == 'URI' and isinstance(value, str) and value.startswith(ROLE_URI)}


def require_role(certificate: dict | None, allowed: frozenset[str] | set[str]) -> str:
    roles = peer_roles(certificate)
    # One certificate states exactly one role; a multi-role certificate is refused.
    if len(roles) != 1 or not roles <= ROLES or not roles & set(allowed):
        raise PeerRejected('peer certificate role is not accepted here')
    return next(iter(roles))


@dataclass(frozen=True)
class TransportPolicy:
    mode: str
    role: str = ''
    allowed_hosts: frozenset[str] = frozenset()
    ca_file: str = field(default='', repr=False)
    cert_file: str = field(default='', repr=False)
    key_file: str = field(default='', repr=False)
    generation: int = 0
    _contexts: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self):
        if self.mode == 'loopback_development':
            if self.role or self.allowed_hosts or self.ca_file or self.cert_file or self.key_file or self.generation:
                raise ValueError('loopback development transport takes no TLS settings')
            return
        if self.mode != 'mtls_remote':
            raise ValueError('unsupported internal transport mode')
        if self.role not in ROLES:
            raise ValueError('explicit service role required')
        if type(self.generation) is not int or not 1 <= self.generation <= 1_000_000:
            raise ValueError('explicit operating generation required')
        if not isinstance(self.allowed_hosts, frozenset) or not 1 <= len(self.allowed_hosts) <= 32:
            raise ValueError('bounded internal host allowlist required')
        for host in self.allowed_hosts:
            if _host(host) != host:
                raise ValueError('normalized internal host names required')
        _private_file(self.ca_file, secret=False)
        _private_file(self.cert_file, secret=False)
        _private_file(self.key_file, secret=True)

    @classmethod
    def loopback(cls) -> 'TransportPolicy':
        return cls('loopback_development')

    @classmethod
    def mtls(cls, *, role: str, allowed_hosts, ca_file: str, cert_file: str, key_file: str,
             generation: int = 1) -> 'TransportPolicy':
        if isinstance(allowed_hosts, (str, bytes)):
            raise ValueError('host allowlist must be a list')
        hosts = [_host(host) for host in allowed_hosts]
        if len(set(hosts)) != len(hosts):
            raise ValueError('duplicate internal host name')
        return cls('mtls_remote', role, frozenset(hosts), ca_file, cert_file, key_file, generation)

    @classmethod
    def from_env(cls, env, *, role: str) -> 'TransportPolicy':
        names = ['PICKUP_INTERNAL_TLS_CA', 'PICKUP_INTERNAL_TLS_CERT', 'PICKUP_INTERNAL_TLS_KEY',
                 'PICKUP_INTERNAL_HOSTS', 'PICKUP_OPERATING_GENERATION']
        if any(not isinstance(env.get(name), str) or not env[name].strip() for name in names):
            raise ValueError('internal TLS CA, certificate, key, host allowlist and operating generation are required')
        generation = env['PICKUP_OPERATING_GENERATION'].strip()
        if not generation.isdigit() or generation != str(int(generation)):
            raise ValueError('operating generation must be a positive integer')
        try:
            hosts = json.loads(env['PICKUP_INTERNAL_HOSTS'])
        except json.JSONDecodeError:
            raise ValueError('PICKUP_INTERNAL_HOSTS must be a JSON array') from None
        if not isinstance(hosts, list):
            raise ValueError('PICKUP_INTERNAL_HOSTS must be a JSON array')
        return cls.mtls(role=role, allowed_hosts=hosts, ca_file=env['PICKUP_INTERNAL_TLS_CA'],
                        cert_file=env['PICKUP_INTERNAL_TLS_CERT'], key_file=env['PICKUP_INTERNAL_TLS_KEY'],
                        generation=int(generation))

    @property
    def remote(self) -> bool:
        return self.mode == 'mtls_remote'

    @property
    def loopback_only(self) -> bool:
        return not self.remote or all(is_loopback(host) for host in self.allowed_hosts)

    @property
    def has_loopback_host(self) -> bool:
        return any(is_loopback(host) for host in self.allowed_hosts)

    def headers(self) -> dict:
        """Internal requests state the operating generation they belong to."""
        return {GENERATION_HEADER: str(self.generation)} if self.remote else {}

    def origin(self, url: str, *, callback: bool = False) -> str:
        if not self.remote:
            from ..payments.http_client import origin
            return origin(url, callback=callback)
        if not isinstance(url, str):
            raise ValueError('configured internal HTTPS endpoint required')
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = None
        host = (parts.hostname or '').lower()
        if (parts.scheme != 'https' or not host or host not in self.allowed_hosts or not port
                or parts.username or parts.password or parts.query or parts.fragment
                or (parts.path not in {'', '/'} if not callback else parts.path != CALLBACK_PATH)):
            raise ValueError('configured internal HTTPS endpoint required')
        return url.rstrip('/')

    def client_context(self) -> ssl.SSLContext:
        if not self.remote:
            raise ValueError('loopback development transport has no TLS context')
        context = self._contexts.get('client')
        if context is None:
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.ca_file)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_cert_chain(self.cert_file, self.key_file)
            self._contexts['client'] = context
        return context

    def server_context(self) -> ssl.SSLContext:
        if not self.remote:
            raise ValueError('loopback development transport has no TLS context')
        context = self._contexts.get('server')
        if context is None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_cert_chain(self.cert_file, self.key_file)
            context.load_verify_locations(cafile=self.ca_file)
            self._contexts['server'] = context
        return context

    def http_client(self, timeout: float, *, server_role: str):
        """One bounded client. In mTLS mode the server role is checked right
        after the handshake, before any request bytes or tokens are written."""
        import httpx
        if server_role not in ROLES:
            raise ValueError('expected server role required')
        if not self.remote:
            return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False)
        return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                            transport=_role_checked_transport(self.client_context(), server_role))


def _role_checked_transport(context: ssl.SSLContext, server_role: str):
    import httpcore
    import httpx

    class Stream(httpcore.NetworkStream):
        def __init__(self, inner):
            self._inner = inner

        def read(self, max_bytes, timeout=None):
            return self._inner.read(max_bytes, timeout)

        def write(self, buffer, timeout=None):
            return self._inner.write(buffer, timeout)

        def close(self):
            self._inner.close()

        def get_extra_info(self, info):
            return self._inner.get_extra_info(info)

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            stream = self._inner.start_tls(ssl_context, server_hostname, timeout)
            ssl_object = stream.get_extra_info('ssl_object')
            try:
                require_role(ssl_object.getpeercert() if ssl_object is not None else None, {server_role})
            except PeerRejected:
                stream.close()
                raise httpcore.ConnectError('internal server role rejected') from None
            return stream

    class Backend(httpcore.NetworkBackend):
        def __init__(self):
            self._inner = httpcore.SyncBackend()

        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            return Stream(self._inner.connect_tcp(host, port, timeout, local_address, socket_options))

        def connect_unix_socket(self, path, timeout=None, socket_options=None):
            raise httpcore.ConnectError('unix sockets are not an internal transport')

        def sleep(self, seconds):
            self._inner.sleep(seconds)

    transport = httpx.HTTPTransport(verify=context, trust_env=False, retries=0)
    if not isinstance(getattr(transport, '_pool', None), httpcore.ConnectionPool):
        raise RuntimeError('unsupported HTTP transport version; refusing unchecked TLS peers')
    transport._pool = httpcore.ConnectionPool(ssl_context=context, network_backend=Backend(),
                                              max_connections=4, http1=True, http2=False, retries=0)
    return transport


@dataclass(frozen=True)
class ServerTLS:
    """Listener side: bind address, advertised name and accepted caller roles."""
    policy: TransportPolicy
    accepted_roles: frozenset[str]
    bind_address: str
    advertise_host: str

    def __post_init__(self):
        if not self.policy.remote:
            raise ValueError('server TLS requires the mTLS transport')
        if not self.accepted_roles or not set(self.accepted_roles) <= ROLES:
            raise ValueError('accepted caller roles required')
        try:
            ipaddress.ip_address(self.bind_address)
        except ValueError:
            raise ValueError('bind address must be an IP literal') from None
        if self.advertise_host not in self.policy.allowed_hosts:
            raise ValueError('advertised host must be in the internal allowlist')

    @classmethod
    def from_env(cls, env, policy: TransportPolicy, *, accepted_roles) -> 'ServerTLS':
        bind = env.get('PICKUP_BIND_ADDRESS', '')
        advertise = env.get('PICKUP_ADVERTISE_HOST', '')
        if not bind or not advertise:
            raise ValueError('PICKUP_BIND_ADDRESS and PICKUP_ADVERTISE_HOST are required')
        return cls(policy, frozenset(accepted_roles), bind.strip(), _host(advertise))

    def wrap(self, server) -> None:
        # The handshake runs in the request thread with a timeout, so a slow
        # client cannot stall the accept loop.
        server.socket = self.policy.server_context().wrap_socket(
            server.socket, server_side=True, do_handshake_on_connect=False)
        server.handle_error = lambda *_: None  # never print peer data or tracebacks

    def accept(self, connection) -> str:
        connection.settimeout(3)
        connection.do_handshake()
        return require_role(connection.getpeercert(), self.accepted_roles)

    def current(self, headers) -> bool:
        """False for a request from another operating generation (e.g. a pre-restore
        process). Callers answer 503 so the sender keeps its original key pending."""
        return headers.get(GENERATION_HEADER, '') == str(self.policy.generation)

    def url(self, port: int, path: str = '') -> str:
        host = self.advertise_host
        if ':' in host:
            host = '[' + host + ']'
        return 'https://' + host + ':' + str(port) + path

