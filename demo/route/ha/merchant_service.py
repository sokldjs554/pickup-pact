"""Independent PostgreSQL merchant role.

postgresql_development: loopback listener. ha_postgres_v1: mTLS listener that
serves only the order role, least-privilege runtime DB user, no DDL.
"""
import argparse
import os
from ..merchant_http import serve
from .merchant_repository import PostgresMerchantFleet
from .settings import HA, backend, ha_context, initialize_flag


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--ready-file')
    args=parser.parse_args()
    token=os.environ.get('ROUTE_MERCHANT_TOKEN','')
    if not 0<=args.port<=65535 or len(token)<24:parser.error('valid port and internal token required')
    env=os.environ
    context=ha_context(env,'merchant') if backend(env)==HA else None
    tls=None
    if context is not None:
        context.dsn(env,'PICKUP_MERCHANT_DSN')
        tls=context.server(env,{'order'})
    repo=PostgresMerchantFleet(env.get('PICKUP_MERCHANT_DSN',''),
        schema=env.get('PICKUP_MERCHANT_SCHEMA','pact_merchants'),
        initialize=initialize_flag(env,'PICKUP_MERCHANT_INIT_SCHEMA',context),
        runtime_guard=None if context is None else context.guard)
    try:serve('',token,port=args.port,ready_file=args.ready_file,repository=repo,tls=tls)
    finally:repo.close()


if __name__=='__main__':main()
