"""Independently runnable native PostgreSQL payment role for development.

No automatic SQLite fallback, no migration unless explicitly enabled. The
development listener is loopback-only; ha_postgres_v1 serves only the order
role over mTLS and delivers notifications to the order role over mTLS.
"""
from __future__ import annotations
import argparse
import os
from ..payments.http_server import serve
from .payment_repository import PostgresPaymentRepository
from .settings import HA, backend, ha_context, initialize_flag


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--ready-file')
    parser.add_argument('--callback-url',action='append',default=[],
                        help='order notification receiver; repeat for up to three in ha_postgres_v1')
    args=parser.parse_args()
    if not 0<=args.port<=65535:parser.error('invalid port')
    token=os.environ.get('ROUTE_PAYMENT_TOKEN','')
    if len(token)<24:parser.error('internal payment token is required')
    env=os.environ
    dsn=env.get('PICKUP_PAYMENT_DSN','')
    if not dsn:parser.error('PostgreSQL connection configuration is required')
    context=ha_context(env,'payment') if backend(env)==HA else None
    tls=None
    if context is not None:
        context.dsn(env,'PICKUP_PAYMENT_DSN')
        tls=context.server(env,{'order'})
        if not args.callback_url:parser.error('ha_postgres_v1 requires the order notification endpoint')
    repo=PostgresPaymentRepository(dsn,schema=env.get('PICKUP_PAYMENT_SCHEMA','pact_payment'),
        initialize=initialize_flag(env,'PICKUP_PAYMENT_INIT_SCHEMA',context),
        runtime_guard=None if context is None else context.guard)
    try:
        serve('',token,args.port,args.ready_file,args.callback_url or None,
              env.get('ROUTE_PAYMENT_NOTIFY_SECRET'),repository=repo,tls=tls)
    finally:repo.close()


if __name__=='__main__':main()
