"""Test-only launcher: existing SQLite-backed synthetic roles behind the mTLS listener.

Used by the transport contract tests so certificate/role checks are exercised
over real TCP without a PostgreSQL dependency. Never part of a deployment.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from demo.route.ha.transport import ServerTLS, TransportPolicy  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('kind', choices=['merchant', 'payment', 'notification'])
    parser.add_argument('--role', required=True)
    parser.add_argument('--accept', required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--ready-file', required=True)
    parser.add_argument('--callback-url', action='append', default=[])
    args = parser.parse_args()
    policy = TransportPolicy.from_env(os.environ, role=args.role)
    tls = ServerTLS(policy, frozenset(args.accept.split(',')), '127.0.0.1', 'localhost')
    if args.kind == 'merchant':
        from demo.route.merchant_http import serve
        serve(args.directory, os.environ['ROUTE_MERCHANT_TOKEN'], port=0, ready_file=args.ready_file, tls=tls)
    elif args.kind == 'payment':
        from demo.route.payments.http_server import serve
        serve(args.directory, os.environ['ROUTE_PAYMENT_TOKEN'], 0, args.ready_file, args.callback_url or None,
              os.environ.get('ROUTE_PAYMENT_NOTIFY_SECRET'), tls=tls)
    else:
        from demo.route.payments.notifications import NotificationServer, PaymentInbox
        inbox = PaymentInbox(Path(args.directory)/'inbox.sqlite', os.environ['ROUTE_PAYMENT_NOTIFY_SECRET'])
        stop = threading.Event()
        for sig in [signal.SIGTERM, signal.SIGINT]:
            signal.signal(sig, lambda *_: stop.set())
        with NotificationServer(inbox, tls=tls) as server:
            Path(args.ready_file).write_text(json.dumps({'url': server.url, 'pid': os.getpid()}))
            stop.wait()


if __name__ == '__main__':
    main()
