"""Independent PostgreSQL merchant role, bound to loopback until TLS deployment."""
import argparse
import os
from ..merchant_http import serve
from .merchant_repository import PostgresMerchantFleet


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--ready-file')
    args=parser.parse_args()
    token=os.environ.get('ROUTE_MERCHANT_TOKEN','')
    if not 0<=args.port<=65535 or len(token)<24:parser.error('valid port and internal token required')
    repo=PostgresMerchantFleet(os.environ.get('PICKUP_MERCHANT_DSN',''),
        schema=os.environ.get('PICKUP_MERCHANT_SCHEMA','pact_merchants'),
        initialize=os.environ.get('PICKUP_MERCHANT_INIT_SCHEMA')=='1')
    try:serve('',token,port=args.port,ready_file=args.ready_file,repository=repo)
    finally:repo.close()


if __name__=='__main__':main()
