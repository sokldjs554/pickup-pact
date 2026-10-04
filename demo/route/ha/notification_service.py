"""Independent signature-checking listener sharing the order inbox, not money."""
import argparse
import json
import os
from pathlib import Path
import signal
import threading
from ..payments.notifications import NotificationServer
from .journey_repository import PostgresJourneyRepository
from .inbox import PostgresPaymentInbox
from .settings import HA, backend, ha_context, initialize_flag


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--ready-file')
    args=parser.parse_args()
    if not 0<=args.port<=65535:parser.error('valid port required')
    secret=os.environ.get('ROUTE_PAYMENT_NOTIFY_SECRET','')
    if len(secret)<24:parser.error('shared notification secret required')
    env=os.environ
    context=ha_context(env,'order') if backend(env)==HA else None
    tls=None
    if context is not None:
        context.dsn(env,'PICKUP_ORDER_DSN')
        tls=context.server(env,{'payment'})  # only the payment role may deliver events
    repo=PostgresJourneyRepository(env.get('PICKUP_ORDER_DSN',''),
        schema=env.get('PICKUP_ORDER_SCHEMA','pact_orders'),
        initialize=initialize_flag(env,'PICKUP_ORDER_INIT_SCHEMA',context),
        runtime_guard=None if context is None else context.guard)
    stop=threading.Event()
    for sig in [signal.SIGTERM,signal.SIGINT]:signal.signal(sig,lambda *_:stop.set())
    try:
        with NotificationServer(PostgresPaymentInbox(repo,secret),port=args.port,tls=tls) as server:
            if args.ready_file:Path(args.ready_file).write_text(json.dumps({'url':server.url,'pid':os.getpid()}))
            stop.wait()
    finally:repo.close()


if __name__=='__main__':main()
