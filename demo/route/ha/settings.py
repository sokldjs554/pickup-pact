"""Explicit role settings for the native PostgreSQL modes.

``postgresql_development`` is the private loopback development topology.
``ha_postgres_v1`` is the multi-host configuration: mutual TLS between roles,
multi-host leader-only PostgreSQL DSNs with verified server identity, a
least-privilege runtime database user per data owner and an operating
generation. Loopback hosts are accepted in ``ha_postgres_v1`` only with
``PICKUP_HA_REHEARSAL=single_host_development`` and the runtime then labels
itself as a single-host rehearsal. Neither label certifies independent hosts.
"""
from dataclasses import dataclass, field
import re
import json
from ..payments.http_client import origin
from ..payments.domain import key

DEVELOPMENT = 'postgresql_development'
HA = 'ha_postgres_v1'
REHEARSAL = 'single_host_development'
REHEARSAL_SCOPE = 'ha_postgres_v1_single_host_rehearsal_not_host_ha'
MULTI_HOST_SCOPE = 'ha_postgres_v1_multi_host_unverified_by_runtime'


def backend(env) -> str:
    value = env.get('PICKUP_ROUTE_BACKEND', DEVELOPMENT)
    if value not in {DEVELOPMENT, HA}:
        raise ValueError('unsupported native storage backend')
    return value


@dataclass(frozen=True)
class HaContext:
    transport: object
    guard: object
    rehearsal: bool
    scope: str

    def dsn(self, env, name: str) -> str:
        from .dsn import validate_ha_dsn
        value = env.get(name, '')
        validate_ha_dsn(value, allow_loopback=self.rehearsal, minimum_hosts=1 if self.rehearsal else 2)
        return value

    def server(self, env, accepted_roles):
        from .transport import ServerTLS
        return ServerTLS.from_env(env, self.transport, accepted_roles=accepted_roles)


def ha_context(env, role: str) -> HaContext:
    """Every ha_postgres_v1 process: TLS identity, generation, rehearsal scope."""
    from .transport import TransportPolicy
    from .privileges import RuntimeGuard
    if backend(env) != HA:
        raise ValueError('ha_postgres_v1 must be selected explicitly')
    transport = TransportPolicy.from_env(env, role=role)
    marker = env.get('PICKUP_HA_REHEARSAL', '')
    if marker not in {'', REHEARSAL}:
        raise ValueError('unknown rehearsal scope')
    rehearsal = marker == REHEARSAL
    if transport.has_loopback_host and not rehearsal:
        raise ValueError('loopback internal hosts are only allowed in the single-host rehearsal')
    for name in ['ROUTE_MERCHANT_TOKEN', 'ROUTE_PAYMENT_TOKEN', 'ROUTE_PAYMENT_NOTIFY_SECRET']:
        if isinstance(env.get(name), str) and env[name] and len(env[name]) < 32:
            raise ValueError('ha_postgres_v1 internal secrets must have at least 32 characters')
    return HaContext(transport, RuntimeGuard(role, transport.generation), rehearsal,
                     REHEARSAL_SCOPE if rehearsal else MULTI_HOST_SCOPE)


def initialize_flag(env, name: str, context: HaContext | None) -> bool:
    value = env.get(name, '0')
    if value not in {'0', '1'}:
        raise ValueError('explicit initialization flag required')
    if context is not None and value == '1':
        raise ValueError('ha_postgres_v1 services never run DDL; use python -m demo.route.ha.migrate')
    return value == '1'


@dataclass(frozen=True)
class RoleSettings:
    order_dsn: str = field(repr=False)
    node_id: str
    merchant_url: str = field(repr=False)
    merchant_token: str = field(repr=False)
    payment_url: str = field(repr=False)
    payment_token: str = field(repr=False)
    notification_secret: str = field(repr=False)
    schema: str = 'pact_orders'
    initialize: bool = False
    merchant_failovers: tuple[str,...] = field(default=(),repr=False)
    payment_failovers: tuple[str,...] = field(default=(),repr=False)
    ha: HaContext | None = field(default=None, repr=False)

    @property
    def backend(self) -> str:
        return HA if self.ha is not None else DEVELOPMENT

    @classmethod
    def from_env(cls, env):
        mode = backend(env)
        required=['PICKUP_ORDER_DSN','PICKUP_NODE_ID','ROUTE_MERCHANT_URL','ROUTE_MERCHANT_TOKEN',
                  'ROUTE_PAYMENT_URL','ROUTE_PAYMENT_TOKEN','ROUTE_PAYMENT_NOTIFY_SECRET']
        if any(not isinstance(env.get(k),str) or not env[k].strip() for k in required):
            raise ValueError('explicit PostgreSQL role and shared connection settings required')
        shared=[env[k] for k in ['ROUTE_MERCHANT_TOKEN','ROUTE_PAYMENT_TOKEN','ROUTE_PAYMENT_NOTIFY_SECRET']]
        if any(len(value)<24 for value in shared):
            raise ValueError('shared internal secrets must have at least 24 characters')
        key(env['PICKUP_NODE_ID'])
        if len(env['PICKUP_NODE_ID'])>80:raise ValueError('bounded node identity required')
        schema=env.get('PICKUP_ORDER_SCHEMA','pact_orders')
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,47}',schema) or schema=='public' or schema.startswith('pg_'):
            raise ValueError('explicit repository schema required')
        context=None
        if mode == HA:
            context=ha_context(env,'order')
            if len(set(shared))!=len(shared):
                raise ValueError('each internal role needs its own token or secret')
            context.dsn(env,'PICKUP_ORDER_DSN')
        initialize=initialize_flag(env,'PICKUP_ORDER_INIT_SCHEMA',context)
        validate=origin if context is None else context.transport.origin
        from .replica_clients import replica_origins
        def failovers(name, primary):
            try:extra=json.loads(env.get(name,'[]'))
            except json.JSONDecodeError:raise ValueError('failover origins must be a JSON array') from None
            if not isinstance(extra,list):raise ValueError('failover origins must be a JSON array')
            return replica_origins([primary,*extra],None if context is None else context.transport)[1:]
        merchant_failovers=failovers('ROUTE_MERCHANT_FAILOVER_URLS',env['ROUTE_MERCHANT_URL'])
        payment_failovers=failovers('ROUTE_PAYMENT_FAILOVER_URLS',env['ROUTE_PAYMENT_URL'])
        if context is not None and (not merchant_failovers or not payment_failovers):
            raise ValueError('ha_postgres_v1 needs at least two merchant and two payment origins')
        return cls(env['PICKUP_ORDER_DSN'],env['PICKUP_NODE_ID'],validate(env['ROUTE_MERCHANT_URL']),
                   env['ROUTE_MERCHANT_TOKEN'],validate(env['ROUTE_PAYMENT_URL']),env['ROUTE_PAYMENT_TOKEN'],
                   env['ROUTE_PAYMENT_NOTIFY_SECRET'],schema,initialize,
                   merchant_failovers=merchant_failovers,payment_failovers=payment_failovers,ha=context)

    def store(self):
        from .replica_clients import ReplicaMerchantFleet, ReplicaPaymentClient
        from .journey_store import PostgresJourneyStore
        transport=None if self.ha is None else self.ha.transport
        return PostgresJourneyStore(self.order_dsn, schema=self.schema, initialize=self.initialize,
            worker_id=self.node_id, notification_secret=self.notification_secret,
            fleet=ReplicaMerchantFleet((self.merchant_url,*self.merchant_failovers),self.merchant_token,timeout=2,
                                       transport=transport),
            payment_gateway=ReplicaPaymentClient((self.payment_url,*self.payment_failovers),self.payment_token,
                                                 timeout=2,transport=transport),
            runtime_guard=None if self.ha is None else self.ha.guard,
            evidence_scope=None if self.ha is None else self.ha.scope)
