"""Connection settings accepted by ``ha_postgres_v1``.

The service connects to whichever Patroni member is the writable leader through
libpq's multi-host list (``target_session_attrs=read-write``), so neither a proxy
process nor one host is a single point of entry. Server identity is verified
(``sslmode=verify-full`` with an explicit CA) and passwords never appear in the
DSN; they come from a private passfile or a client certificate.
"""
from __future__ import annotations
import ipaddress
from pathlib import Path
import re
import stat

HOST = re.compile(r'^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$')
IDENT = re.compile(r'^[a-z][a-z0-9_]{0,62}$')
ALLOWED = {'host', 'port', 'dbname', 'user', 'sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'passfile',
           'target_session_attrs', 'load_balance_hosts', 'connect_timeout', 'application_name',
           'channel_binding', 'ssl_min_protocol_version', 'sslsni'}


def _loopback(host: str) -> bool:
    if host == 'localhost' or host.endswith('.localhost'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _file(path: str, *, secret: bool) -> None:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError('database TLS and passfile paths must be absolute')
    try:
        info = candidate.stat()
    except OSError:
        raise ValueError('configured database credential file is not readable') from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('database credential path must be a regular file')
    if secret and info.st_mode & 0o077:
        raise ValueError('database secret files must not be readable by group or others')


def validate_ha_dsn(dsn: str, *, allow_loopback: bool = False, minimum_hosts: int = 2) -> dict:
    """Return the parsed, non-secret connection settings or raise ValueError.

    Errors never echo the DSN, because a rejected value may contain a secret.
    """
    from psycopg.conninfo import conninfo_to_dict
    import psycopg
    if not isinstance(dsn, str) or not dsn.strip():
        raise ValueError('PostgreSQL connection settings required')
    try:
        info = conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:
        raise ValueError('unreadable PostgreSQL connection settings') from None
    unknown = set(info) - ALLOWED
    if 'password' in info:
        raise ValueError('passwords must come from a passfile or client certificate, not the DSN')
    if unknown:
        raise ValueError('unsupported PostgreSQL connection settings: '+', '.join(sorted(k for k in unknown if k != 'password')))
    hosts = [part.strip().lower() for part in str(info.get('host', '')).split(',')]
    if not hosts or any(not part for part in hosts):
        raise ValueError('explicit PostgreSQL host list required')
    if not 1 <= minimum_hosts <= len(hosts) <= 5:
        raise ValueError('a bounded list of Patroni member hosts is required')
    for host in hosts:
        if host.startswith('/'):
            raise ValueError('local socket connections are not an HA endpoint')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not HOST.fullmatch(host):
                raise ValueError('invalid PostgreSQL host name') from None
        if _loopback(host) and not allow_loopback:
            raise ValueError('loopback PostgreSQL hosts are only allowed in the single-host rehearsal')
    ports = [part.strip() for part in str(info.get('port', '5432')).split(',')]
    if len(ports) not in {1, len(hosts)} or any(not p.isdigit() or not 0 < int(p) < 65536 for p in ports):
        raise ValueError('one port or one port per host required')
    endpoints = list(zip(hosts, ports if len(ports) == len(hosts) else ports*len(hosts)))
    if len(set(endpoints)) != len(endpoints):
        raise ValueError('duplicate PostgreSQL member endpoint')
    if not IDENT.fullmatch(str(info.get('dbname', ''))) or not IDENT.fullmatch(str(info.get('user', ''))):
        raise ValueError('explicit database and runtime user required')
    if info.get('user') in {'postgres', 'replicator', 'rewind_user'}:
        raise ValueError('services must not connect as an administrative or replication user')
    if info.get('sslmode') != 'verify-full':
        raise ValueError('sslmode=verify-full is required')
    if not info.get('sslrootcert') or info['sslrootcert'] == 'system':
        raise ValueError('an explicit private CA file is required for the database')
    _file(info['sslrootcert'], secret=False)
    if info.get('target_session_attrs', 'read-write') != 'read-write':
        raise ValueError('services must target the writable leader')
    if info.get('load_balance_hosts', 'disable') not in {'disable', 'random'}:
        raise ValueError('invalid host selection policy')
    if info.get('ssl_min_protocol_version', 'TLSv1.2') not in {'TLSv1.2', 'TLSv1.3'}:
        raise ValueError('TLS 1.2 or newer required')
    if info.get('channel_binding', 'prefer') not in {'prefer', 'require'}:
        raise ValueError('channel binding cannot be disabled')
    if bool(info.get('sslcert')) != bool(info.get('sslkey')):
        raise ValueError('client certificate and key must be configured together')
    if info.get('sslcert'):
        _file(info['sslcert'], secret=False)
        _file(info['sslkey'], secret=True)
    if info.get('passfile'):
        _file(info['passfile'], secret=True)
    if not info.get('passfile') and not info.get('sslcert'):
        raise ValueError('a private passfile or client certificate is required')
    if 'connect_timeout' in info and (not str(info['connect_timeout']).isdigit() or not 1 <= int(info['connect_timeout']) <= 10):
        raise ValueError('bounded connect timeout required')
    return dict(hosts=tuple(hosts), ports=tuple(ports), dbname=info['dbname'], user=info['user'],
                loopback=any(_loopback(host) for host in hosts))
