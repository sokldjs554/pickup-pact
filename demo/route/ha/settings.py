"""Explicit private development topology; real remote TLS/HA is a separate gate."""
from dataclasses import dataclass, field
import re
from ..payments.http_client import origin
from ..payments.domain import key


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

    @classmethod
    def from_env(cls, env):
        required=['PICKUP_ORDER_DSN','PICKUP_NODE_ID','ROUTE_MERCHANT_URL','ROUTE_MERCHANT_TOKEN',
                  'ROUTE_PAYMENT_URL','ROUTE_PAYMENT_TOKEN','ROUTE_PAYMENT_NOTIFY_SECRET']
        if any(not isinstance(env.get(k),str) or not env[k].strip() for k in required):
            raise ValueError('explicit PostgreSQL role and shared connection settings required')
        if any(len(env[k])<24 for k in ['ROUTE_MERCHANT_TOKEN','ROUTE_PAYMENT_TOKEN','ROUTE_PAYMENT_NOTIFY_SECRET']):
            raise ValueError('shared internal secrets must have at least 24 characters')
        key(env['PICKUP_NODE_ID'])
        if len(env['PICKUP_NODE_ID'])>80:raise ValueError('bounded node identity required')
        schema=env.get('PICKUP_ORDER_SCHEMA','pact_orders')
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,47}',schema) or schema=='public' or schema.startswith('pg_'):
            raise ValueError('explicit repository schema required')
        if env.get('PICKUP_ORDER_INIT_SCHEMA','0') not in {'0','1'}:
            raise ValueError('explicit initialization flag required')
        return cls(env['PICKUP_ORDER_DSN'],env['PICKUP_NODE_ID'],origin(env['ROUTE_MERCHANT_URL']),
                   env['ROUTE_MERCHANT_TOKEN'],origin(env['ROUTE_PAYMENT_URL']),env['ROUTE_PAYMENT_TOKEN'],
                   env['ROUTE_PAYMENT_NOTIFY_SECRET'],schema,env.get('PICKUP_ORDER_INIT_SCHEMA')=='1')

    def store(self):
        from ..merchant_http import HttpMerchantFleet
        from ..payments.http_client import PaymentClient
        from .journey_store import PostgresJourneyStore
        return PostgresJourneyStore(self.order_dsn, schema=self.schema, initialize=self.initialize,
            worker_id=self.node_id, notification_secret=self.notification_secret,
            fleet=HttpMerchantFleet(self.merchant_url,self.merchant_token,timeout=2),
            payment_gateway=PaymentClient(self.payment_url,self.payment_token,timeout=2))
