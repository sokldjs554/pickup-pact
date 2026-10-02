"""Authenticated HTTP boundary for synthetic merchants with independent databases.

Separate process, real TCP, bounded requests/timeouts. No production merchant or
payment gateway integration. The test-only crash flag is a process argument,
never a public API parameter. Reply loss is scoped to a stable command key.
"""
from __future__ import annotations
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
from urllib.parse import urlsplit, unquote

from .merchant_fleet import MerchantFleet, CAPACITY

ACTIONS = {'ADMIT','HOLD','FREEZE','UNFREEZE','RELEASE_SOURCE','ACTIVATE','ABORT_TARGET','START','READY','CLAIM','CANCEL'}
KEY = re.compile(r'^[A-Za-z0-9:_-]{1,180}$')


class HttpMerchantFleet:
    handles_response_loss = True

    def __init__(self, base_url: str, token: str, timeout: float = .8, *, transport=None):
        parts = urlsplit(base_url)
        if parts.scheme not in {'http','https'} or not parts.hostname or parts.username or parts.query or parts.fragment:
            raise ValueError('merchant URL must be a configured HTTP(S) origin')
        if parts.path not in {'','/'} or len(token) < 24 or not 0 < timeout <= 5:
            raise ValueError('invalid merchant transport configuration')
        # transport=None keeps the existing development behaviour unchanged.
        self.transport_policy = transport
        self.url = base_url.rstrip('/') if transport is None else transport.origin(base_url)
        self.token, self.timeout = token, timeout

    def _client(self):
        import httpx
        if self.transport_policy is None:
            return httpx.Client(timeout=self.timeout, trust_env=False, follow_redirects=False)
        return self.transport_policy.http_client(self.timeout, server_role='merchant')

    def _generation(self) -> dict:
        return self.transport_policy.headers() if self.transport_policy is not None else {}

    def _request(self, path: str, body: dict | None = None) -> dict:
        import httpx
        try:
            with self._client() as client:
                response = client.request('GET' if body is None else 'POST', self.url+path,
                    json=body, headers={'Authorization':'Bearer '+self.token,**self._generation()})
                response.raise_for_status()
                if len(response.content) > 1_048_576:
                    raise ValueError('oversized merchant response')
                data=response.json()
                if not isinstance(data,dict):raise ValueError('invalid merchant response')
                return data
        except (httpx.HTTPError, ValueError, OSError) as exc:
            # Do not leak configured URL, token, or raw error bodies to customers.
            raise OSError('merchant response not confirmed') from exc

    def execute(self, shop: str, *, world: str, order_id: str, generation: int,
                operation_id: str, action: str, transfer_id: str = '', reject: bool = False,
                lose_reply: bool = False) -> dict:
        data=self._request('/v1/command',dict(shop=shop,world=world,order_id=order_id,generation=generation,
            operation_id=operation_id,action=action,transfer_id=transfer_id,reject=reject,lose_reply=lose_reply))
        result=data.get('result')
        if data.get('command_id') != operation_id or not isinstance(result,dict) or type(result.get('ok')) is not bool:
            raise OSError('merchant response not confirmed')
        if result['ok']:
            seat=result.get('seat',{})
            if seat.get('world') != world or seat.get('order_id') != order_id or seat.get('generation') != generation:
                raise OSError('merchant response not bound to command')
        return result

    def snapshot(self, world: str) -> dict:
        if not KEY.fullmatch(world):raise ValueError('invalid world')
        data=self._request('/v1/snapshot/'+world)
        if data.get('world') != world or set(data.get('merchants',{})) != set(CAPACITY):
            raise OSError('merchant snapshot not confirmed')
        return data['merchants']

    def evidence(self, world: str, order_id: str) -> dict:
        if not KEY.fullmatch(world) or not KEY.fullmatch(order_id):raise ValueError('invalid evidence identity')
        data=self._request('/v1/orders/'+world+'/'+order_id)
        if data.get('world_id')!=world or data.get('order_id')!=order_id or set(data.get('merchants',{}))!=set(CAPACITY):
            raise OSError('merchant evidence not bound to order')
        for shop,entry in data['merchants'].items():
            row=entry.get('reservation')
            if type(entry.get('revision')) is not int or entry['revision']<0:
                raise OSError('merchant evidence has no revision')
            if row and (row.get('world')!=world or row.get('order_id')!=order_id):
                raise OSError('merchant evidence includes another order')
        return data

    def set_accepting(self, shop: str, world: str, accepting: bool) -> None:
        self._request('/v1/policy',dict(shop=shop,world=world,accepting=accepting))


def serve(directory: str, token: str, host: str = '127.0.0.1', port: int = 0,
          ready_file: str | None = None, crash_phase: str | None = None, *, repository=None, tls=None):
    if len(token) < 24:raise ValueError('ROUTE_MERCHANT_TOKEN must have at least 24 characters')
    if tls is not None:
        if crash_phase:raise ValueError('test crash hooks are not part of the TLS deployment')
        host=tls.bind_address
    fleet=repository if repository is not None else MerchantFleet(directory)
    transport_db=None
    if repository is None:
        transport_db=Path(directory)/'transport.sqlite'
        with sqlite3.connect(transport_db) as db:
            db.execute('CREATE TABLE IF NOT EXISTS lost_replies(world TEXT,command_id TEXT,PRIMARY KEY(world,command_id))')
    elif crash_phase:
        raise ValueError('legacy crash hook cannot be attached to a shared repository')
    if crash_phase:
        def crash(shop,action,key,result):
            if action == crash_phase and result['ok']:os._exit(91)
        fleet.after_commit=crash

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            if tls is not None:
                tls.accept(self.request)  # handshake and caller role before any request byte
            super().setup()

        def log_message(self, *_):
            pass  # Do not log tokens, order IDs, or request payloads.

        def send(self,status,body):
            raw=json.dumps(body,ensure_ascii=True).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            self.wfile.write(raw)

        def authenticated(self):
            candidate=self.headers.get('Authorization','').encode('utf-8')
            return hmac.compare_digest(candidate,('Bearer '+token).encode('utf-8'))

        def do_GET(self):
            if self.path == '/health':
                try:ready=repository.storage_ready() if repository is not None else True
                except OSError:ready=False
                return self.send(200 if ready else 503,dict(status='ok' if ready else 'unavailable',
                    mode='synthetic-http',pid=os.getpid(),storage_ready=ready,
                    storage_backend=getattr(fleet,'backend','sqlite')))
            if not self.authenticated():return self.send(403,dict(error='forbidden'))
            if tls is not None and not tls.current(self.headers):return self.send(503,dict(error='stale_generation'))
            if self.path.startswith('/v1/orders/'):
                parts=self.path[len('/v1/orders/'):].split('/')
                if len(parts)==2 and all(KEY.fullmatch(p) for p in parts):
                    try:return self.send(200,fleet.evidence(*parts))
                    except (sqlite3.Error,OSError):return self.send(503,dict(error='storage_unavailable'))
                return self.send(422,dict(error='invalid_identity'))
            prefix='/v1/snapshot/'
            if self.path.startswith(prefix) and KEY.fullmatch(self.path[len(prefix):]):
                world=self.path[len(prefix):]
                try:return self.send(200,dict(world=world,merchants=fleet.snapshot(world)))
                except (sqlite3.Error,OSError):return self.send(503,dict(error='storage_unavailable'))
            return self.send(404,dict(error='not_found'))

        def do_POST(self):
            if not self.authenticated():return self.send(403,dict(error='forbidden'))
            if tls is not None and not tls.current(self.headers):return self.send(503,dict(error='stale_generation'))
            if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                return self.send(415,dict(error='json_required'))
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 16384:return self.send(413,dict(error='body_size'))
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict):raise ValueError('object required')
                pending=[body]
                while pending:
                    value=pending.pop()
                    if isinstance(value,str) and any(0xD800<=ord(c)<=0xDFFF for c in value):raise ValueError('Unicode')
                    if isinstance(value,dict):pending.extend(value.keys());pending.extend(value.values())
                    elif isinstance(value,list):pending.extend(value)
            except (ValueError,UnicodeError,RecursionError):return self.send(400,dict(error='invalid_json'))
            if self.path not in {'/v1/command','/v1/policy'}:return self.send(404,dict(error='not_found'))
            if not isinstance(body.get('shop'), str) or body.get('shop') not in CAPACITY or not isinstance(body.get('world'),str) or not KEY.fullmatch(body['world']):
                return self.send(422,dict(error='invalid_merchant_or_world'))
            if self.path == '/v1/policy':
                if set(body)!={'shop','world','accepting'} or type(body.get('accepting')) is not bool:
                    return self.send(422,dict(error='invalid_policy'))
                try:fleet.set_accepting(body['shop'],body['world'],body['accepting'])
                except (sqlite3.Error,OSError):return self.send(503,dict(error='storage_unavailable'))
                return self.send(200,dict(ok=True))
            required={'shop','world','order_id','generation','operation_id','action'}
            if (not required <= body.keys() or not body.keys() <= required|{'transfer_id','reject','lose_reply'}
                or not isinstance(body['action'], str) or body['action'] not in ACTIONS or type(body['generation']) is not int
                or not 0 <= body['generation'] <= 1_000_000_000
                or not all(isinstance(body[k],str) and KEY.fullmatch(body[k]) for k in ('order_id','operation_id'))
                or not isinstance(body.get('transfer_id',''),str)
                or (body.get('transfer_id') and not KEY.fullmatch(body['transfer_id']))
                or any(type(body.get(k,False)) is not bool for k in ('reject','lose_reply'))):
                return self.send(422,dict(error='invalid_command'))
            shop=body.pop('shop');lose_reply=body.pop('lose_reply',False)
            try:result=fleet.execute(shop,**body)
            except (sqlite3.OperationalError,OSError):return self.send(503,dict(error='storage_unavailable'))
            if lose_reply and result['ok']:
                try:
                    if repository is not None:
                        first=repository.consume_reply_loss(body['world'],body['operation_id'])
                    else:
                        with sqlite3.connect(transport_db,timeout=3) as db:
                            cursor=db.execute('INSERT OR IGNORE INTO lost_replies VALUES(?,?)',(body['world'],body['operation_id']))
                            first=cursor.rowcount==1
                except (sqlite3.Error,OSError):return self.send(503,dict(error='storage_unavailable'))
                if first:
                    self.close_connection=True
                    try:self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    self.connection.close()
                    return
            self.send(200,dict(command_id=body['operation_id'],result=result))

    class Server(ThreadingHTTPServer):
        daemon_threads=True
        allow_reuse_address=True
        def get_request(self):
            conn,addr=super().get_request();conn.settimeout(3);return conn,addr
    server=Server((host,port),Handler)
    if tls is not None:tls.wrap(server)
    if ready_file:
        url=tls.url(server.server_port) if tls is not None else f'http://{host}:{server.server_port}'
        p=Path(ready_file);p.write_text(json.dumps(dict(url=url,pid=os.getpid())))
    server.serve_forever(poll_interval=.1)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',required=True);p.add_argument('--host',default='127.0.0.1')
    p.add_argument('--port',type=int,default=0);p.add_argument('--ready-file')
    p.add_argument('--crash-phase',choices=sorted(ACTIONS))
    a=p.parse_args()
    serve(a.directory,os.environ.get('ROUTE_MERCHANT_TOKEN',''),a.host,a.port,a.ready_file,a.crash_phase)
