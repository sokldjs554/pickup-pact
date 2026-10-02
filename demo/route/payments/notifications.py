"""Authenticated outbox delivery and idempotent hints in the coordinator DB."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path
from .domain import PaymentError, key

MAX_BODY = 16384
EVENT_FIELDS = {'event_id','world_id','order_id','authorization_id','transaction_id',
                'revision','payment_revision','kind','amount_krw','currency','mode'}


def sign(secret: str, stamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), stamp.encode()+b'.'+body, hashlib.sha256).hexdigest()


def parse_event(secret: str, body: bytes, stamp: str, signature: str) -> dict:
    if not isinstance(body, bytes) or len(body)>MAX_BODY:
        raise PaymentError('INVALID_EVENT_SIZE',413)
    try:
        timestamp=int(stamp)
        if str(timestamp)!=stamp or abs(time.time()-timestamp)>300:
            raise ValueError()
        if len(signature)!=64 or not hmac.compare_digest(sign(secret,stamp,body).encode(),signature.encode('ascii')):
            raise ValueError()
    except (ValueError,TypeError,UnicodeError):
        raise PaymentError('INVALID_EVENT_SIGNATURE',403) from None
    try:
        event=json.loads(body)
        if not isinstance(event,dict) or set(event)!=EVENT_FIELDS:raise ValueError()
        for field in ('event_id','world_id','order_id','authorization_id','transaction_id'):key(event[field])
        for field in ('revision','payment_revision','amount_krw'):
            if type(event[field]) is not int or event[field]<1:raise ValueError()
        if event['kind'] not in {'AUTHORIZE','CAPTURE','VOID'} or event['currency']!='KRW' or event['mode']!='synthetic':
            raise ValueError()
    except (ValueError,TypeError,KeyError,UnicodeError):
        raise PaymentError('INVALID_EVENT',422) from None
    return event


class PaymentInbox:
    def __init__(self, path: str | Path, secret: str):
        if len(secret) < 24:
            raise ValueError('notification secret is too short')
        self.path, self.secret = str(path), secret
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS payment_authorization_refs(
                authorization_id TEXT PRIMARY KEY,world_id TEXT NOT NULL,order_id TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS payment_inbox(
                event_id TEXT PRIMARY KEY,body_sha256 TEXT NOT NULL,world_id TEXT NOT NULL,
                order_id TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL,received_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS payment_recheck_queue(
                world_id TEXT NOT NULL,order_id TEXT NOT NULL,revision INTEGER NOT NULL,
                PRIMARY KEY(world_id,order_id));''')

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self,world: str,order_id: str,authorization_id: str):
        with self.connection() as db:
            self.register_in(db, world, order_id, authorization_id)

    @staticmethod
    def register_in(db, world, order_id, authorization_id):
        key(world); key(order_id); key(authorization_id)
        row=db.execute('SELECT world_id,order_id FROM payment_authorization_refs WHERE authorization_id=?', (authorization_id,)).fetchone()
        if row and tuple(row)!=(world,order_id):
            raise PaymentError('AUTHORIZATION_BINDING_CONFLICT')
        db.execute('INSERT OR IGNORE INTO payment_authorization_refs VALUES(?,?,?)', (authorization_id,world,order_id))

    def accept(self,body: bytes,stamp: str,signature: str) -> dict:
        event=parse_event(self.secret,body,stamp,signature)
        sha=hashlib.sha256(body).hexdigest()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            binding=db.execute('SELECT world_id,order_id FROM payment_authorization_refs WHERE authorization_id=?', (event['authorization_id'],)).fetchone()
            if not binding or tuple(binding)!=(event['world_id'],event['order_id']):
                raise PaymentError('EVENT_BINDING_CONFLICT',403)
            prior=db.execute('SELECT body_sha256 FROM payment_inbox WHERE event_id=?',(event['event_id'],)).fetchone()
            if prior:
                if prior[0]!=sha:raise PaymentError('EVENT_BODY_CONFLICT')
                return dict(accepted=True,duplicate=True)
            db.execute('INSERT INTO payment_inbox VALUES(?,?,?,?,?,?,?)',
                       (event['event_id'],sha,event['world_id'],event['order_id'],event['revision'],body.decode(),time.time()))
            db.execute('INSERT INTO payment_recheck_queue VALUES(?,?,?) ON CONFLICT(world_id,order_id) DO UPDATE SET revision=MAX(revision,excluded.revision)',
                       (event['world_id'],event['order_id'],event['revision']))
        return dict(accepted=True,duplicate=False)


    def wake_operations(self, now: float, limit: int = 16) -> int:
        """Hints may shorten a long backoff, never reset REVIEW_REQUIRED or apply money."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            hints=db.execute('SELECT world_id,order_id FROM payment_recheck_queue LIMIT ?', (limit,)).fetchall()
            for world,order_id in hints:
                rows=db.execute("SELECT id,body FROM route_operations WHERE json_extract(body,'$.protocol_version')=2 AND json_extract(body,'$.world')=? AND json_extract(body,'$.order_id')=? AND json_extract(body,'$.status')='PENDING'", (world,order_id)).fetchall()
                for oid,body in rows:
                    op=json.loads(body)
                    if op['recovery']['state']!='REVIEW_REQUIRED':
                        op['recovery']['next_retry_at']=min(op['recovery']['next_retry_at'],now+3)
                        db.execute('UPDATE route_operations SET body=? WHERE id=?',(json.dumps(op,ensure_ascii=False),oid))
                db.execute('DELETE FROM payment_recheck_queue WHERE world_id=? AND order_id=?',(world,order_id))
        return len(hints)


class NotificationServer:
    """A dedicated loopback listener; the public customer's Origin rules stay intact."""
    def __init__(self, inbox: PaymentInbox):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def setup(self):super().setup();self.connection.settimeout(2)
            def do_POST(self):
                status=200
                try:
                    if self.path!='/internal/payments/events':raise PaymentError('NOT_FOUND',404)
                    if self.headers.get('Origin') or self.headers.get('Transfer-Encoding'):raise PaymentError('INVALID_NOTIFICATION',403)
                    length=int(self.headers.get('Content-Length','-1'))
                    if length<1:raise PaymentError('INVALID_LENGTH',400)
                    if length>MAX_BODY:raise PaymentError('INVALID_EVENT_SIZE',413)
                    if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise PaymentError('JSON_REQUIRED',415)
                    body=self.rfile.read(length)
                    if len(body)!=length:raise PaymentError('INCOMPLETE_BODY',400)
                    result=inbox.accept(body,self.headers.get('X-Payment-Timestamp',''),self.headers.get('X-Payment-Signature',''))
                except PaymentError as exc:status=exc.status;result={'code':exc.code}
                except (ValueError,OSError):status=400;result={'code':'INVALID_NOTIFICATION'}
                raw=json.dumps(result).encode();self.send_response(status)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                try:self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
        self.url=f'http://127.0.0.1:{self.server.server_port}/internal/payments/events'
        self.thread=None

    def __enter__(self):
        import threading
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.1},daemon=True)
        self.thread.start();return self

    def __exit__(self,*_):
        self.server.shutdown();self.server.server_close()
        if self.thread:self.thread.join(3)


def deliver_due(repo,secret: str,send,*,duplicate: bool=False,limit: int=16) -> int:
    """At-least-once delivery outside the DB transaction, with fenced ACKs."""
    from uuid import uuid4
    if not 1<=limit<=64:raise ValueError('delivery batch limit')
    rows=repo.claim_notifications('sender-'+uuid4().hex,limit=limit,lease_seconds=30)
    delivered=0
    for row in rows:
        ok=True
        for _ in range(2 if duplicate else row.get('copies',1)):
            body=row['body'].encode();stamp=str(int(time.time()))
            try:ok=bool(send(body,stamp,sign(secret,stamp,body))) and ok
            except (OSError,ValueError):ok=False
        if repo.finish_notification(row,ok) and ok:
            delivered+=1
    return delivered
