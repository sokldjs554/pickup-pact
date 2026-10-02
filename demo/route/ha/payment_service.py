"""Independently runnable native PostgreSQL payment role for development.

No automatic SQLite fallback, no migration unless explicitly enabled. The HTTP
listener retains the existing loopback-only policy until deployment TLS exists.
"""
from __future__ import annotations
import argparse
import os
from ..payments.http_server import serve
from .payment_repository import PostgresPaymentRepository


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--ready-file')
    parser.add_argument('--callback-url')
    args=parser.parse_args()
    if not 0<=args.port<=65535:parser.error('invalid port')
    token=os.environ.get('ROUTE_PAYMENT_TOKEN','')
    if len(token)<24:parser.error('internal payment token is required')
    dsn=os.environ.get('PICKUP_PAYMENT_DSN','')
    if not dsn:parser.error('PostgreSQL connection configuration is required')
    repo=PostgresPaymentRepository(dsn,schema=os.environ.get('PICKUP_PAYMENT_SCHEMA','pact_payment'),
        initialize=os.environ.get('PICKUP_PAYMENT_INIT_SCHEMA')=='1')
    try:
        serve('',token,args.port,args.ready_file,args.callback_url,
              os.environ.get('ROUTE_PAYMENT_NOTIFY_SECRET'),repository=repo)
    finally:repo.close()


if __name__=='__main__':main()
