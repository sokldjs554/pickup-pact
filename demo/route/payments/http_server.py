"""Synthetic PG. Response loss happens after a durable commit, not in the UI."""
from __future__ import annotations
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import socket
import sqlite3
import threading
from urllib.parse import unquote, urlsplit
import httpx
from .domain import PaymentError, canonical
from .repository import PaymentRepository
from .storage import PaymentStore
from .notifications import deliver_due
from .http_client import origin


def serve(directory: str,token: str,port: int=0,ready_file: str|None=None,
          callback_url: str|None=None,notify_secret: str|None=None,crash_action: str|None=None,
          repository: PaymentStore|None=None):
    if len(token)<24:raise ValueError('strong internal payment token required')
    if callback_url:
        origin(callback_url,callback=True)
        if not notify_secret or len(notify_secret)<24:raise ValueError('notification secret missing')
    repo=repository if repository is not None else PaymentRepository(Path(directory)/'payments.sqlite')
    slots=threading.BoundedSemaphore(16)
    stop=threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def setup(self):
            super().setup();self.connection.settimeout(3)
        def send(self,status,body):
            data=canonical(body).encode();self.send_response(status)
            self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(data)));self.end_headers()
            try:self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError):pass
        def authenticated(self):
            return hmac.compare_digest(self.headers.get('Authorization','').encode(),('Bearer '+token).encode())
        def do_GET(self):self.handle_api(False)
        def do_POST(self):self.handle_api(True)
        def handle_api(self,post):
            if not self.authenticated():self.send(403,{'code':'UNAUTHORIZED'});return
            if not slots.acquire(blocking=False):self.send(503,{'code':'BUSY'});return
            try:
                parts=urlsplit(self.path)
                if parts.query or parts.fragment:raise PaymentError('INVALID_PATH',422)
                path=parts.path
                if not post:
                    if path=='/health':
                        if not repo.storage_ready():raise OSError('payment storage is unavailable')
                        self.send(200,dict(service='pickup-payment',mode='synthetic',storage_ready=True));return
                    if path.startswith('/v1/operations/'):
                        result=repo.operation(unquote(path[len('/v1/operations/'):]))
                        self.send(200 if result else 404,result or {'code':'NOT_FOUND'});return
                    fields=path.split('/')
                    if len(fields)==5 and fields[1:3]==['v1','orders']:
                        self.send(200,repo.snapshot(unquote(fields[3]),unquote(fields[4])));return
                    self.send(404,{'code':'NOT_FOUND'});return
                if self.headers.get('Transfer-Encoding'):raise PaymentError('TRANSFER_ENCODING_REJECTED',400)
                length=int(self.headers.get('Content-Length','-1'))
                if length<1:raise PaymentError('INVALID_LENGTH',400)
                if length>16384:raise PaymentError('BODY_TOO_LARGE',413)
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise PaymentError('JSON_REQUIRED',415)
                raw=self.rfile.read(length)
                if len(raw)!=length:raise PaymentError('INCOMPLETE_BODY',400)
                c=json.loads(raw)
                if not isinstance(c,dict):raise PaymentError('INVALID_BODY',422)
                fault=c.pop('fault','none')
                if fault not in {'none','drop_reply','duplicate_notification','late_notification'}:raise PaymentError('INVALID_FAULT',422)
                expected='/v1/authorizations' if c.get('action')=='AUTHORIZE' else '/v1/authorizations/'+str(c.get('authorization_id',''))+'/'+str(c.get('action','')).lower()
                if unquote(path)!=expected:raise PaymentError('ACTION_PATH_MISMATCH',422)
                result=repo.execute(c,notification_copies=2 if fault=='duplicate_notification' else 1,
                                    notification_delay=2 if fault=='late_notification' else 0)
                if crash_action==c['action'] and result['ok']:
                    os._exit(92)  # Test-only process argument, never an HTTP field.
                if result['ok'] and fault=='drop_reply' and repo.consume_fault(c['world_id'],c['operation_key'],fault):
                    self.close_connection=True
                    try:self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    self.connection.close();return
                self.send(200,result)
            except PaymentError as exc:self.send(exc.status,{'code':exc.code})
            except (ValueError,UnicodeError):self.send(400,{'code':'INVALID_JSON'})
            except (OSError,sqlite3.Error):self.send(503,{'code':'PAYMENT_UNAVAILABLE'})
            finally:slots.release()

    def delivery():
        def send(body,stamp,signature):
            with httpx.Client(trust_env=False,timeout=1,follow_redirects=False) as client:
                response=client.post(callback_url,content=body,headers={'Content-Type':'application/json',
                    'X-Payment-Timestamp':stamp,'X-Payment-Signature':signature})
                return response.status_code==200 and response.json().get('accepted') is True
        while not stop.wait(.25):
            try:deliver_due(repo,notify_secret,send)
            except Exception:stop.wait(1)  # persisted outbox stays unacknowledged

    server=ThreadingHTTPServer(('127.0.0.1',port),Handler);server.daemon_threads=True
    if callback_url:threading.Thread(target=delivery,daemon=True,name='payment-outbox').start()
    if ready_file:
        target=Path(ready_file);target.write_text(json.dumps({'url':f'http://127.0.0.1:{server.server_port}','pid':os.getpid()}))
    try:server.serve_forever(poll_interval=.2)
    finally:stop.set();server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--directory',required=True)
    parser.add_argument('--port',type=int,default=0);parser.add_argument('--ready-file')
    parser.add_argument('--callback-url');parser.add_argument('--crash-action',choices=['AUTHORIZE','CAPTURE','VOID'])
    args=parser.parse_args()
    serve(args.directory,os.environ['ROUTE_PAYMENT_TOKEN'],args.port,args.ready_file,args.callback_url,
          os.environ.get('ROUTE_PAYMENT_NOTIFY_SECRET'),args.crash_action)
