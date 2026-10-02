"""Native PostgreSQL provider: the same synthetic contract, no SQL translation."""
from __future__ import annotations
from pathlib import Path
import time
from uuid import uuid4
from ..payments.domain import PaymentError,bound,canonical,fingerprint,key,validate
from ..payments.repository import PaymentRepository
from .database import PostgresDatabase,transaction_lock


class PostgresPaymentRepository:
    backend='postgresql'
    transport='local_test'
    _response=staticmethod(PaymentRepository._response)

    def __init__(self,dsn: str,*,schema='pact_payment',limit_krw=100000,initialize=True):
        if type(limit_krw) is not int or limit_krw<1:
            raise ValueError('positive synthetic credit limit required')
        self.database=PostgresDatabase(dsn,schema=schema)
        try:
            if initialize:
                self.database.initialize((Path(__file__).parent/'sql/payments.sql').read_text())
                with self.database.transaction() as db:
                    db.execute('INSERT INTO payment_settings VALUES(%s,%s) ON CONFLICT DO NOTHING',('limit_krw',limit_krw))
            with self.database.transaction(readonly=True) as db:
                settings=db.execute("SELECT value FROM payment_settings WHERE name='limit_krw'").fetchone()
                if not settings: raise ValueError('payment schema is not initialized')
                self.limit_krw=settings['value']
        except Exception:
            self.close();raise

    def close(self):
        self.database.close()

    def storage_ready(self):
        with self.database.transaction(readonly=True) as db:
            row=db.execute('SELECT version FROM pact_storage_meta WHERE singleton=1').fetchone()
            return bool(row and row['version']==1)

    def execute(self,command: dict,*,notification_copies=1,notification_delay=0):
        c=validate(command)
        if (type(notification_copies) is not int or notification_copies not in {1,2}
                or type(notification_delay) not in {int,float} or not 0<=notification_delay<=3):
            raise ValueError('bounded notification options required')
        from psycopg.types.json import Jsonb
        fp=fingerprint(c)
        with self.database.transaction() as db:
            # Consistent hierarchy: request -> authorization -> wallet. All
            # commands for one wallet observe the same credit usage, while
            # independent visitors do not acquire a global database lock.
            transaction_lock(db,'payment-command',c['operation_key'])
            old=db.execute('SELECT fingerprint,result FROM payment_commands WHERE operation_key=%s',(c['operation_key'],)).fetchone()
            if old:
                if old['fingerprint']!=fp: raise PaymentError('IDEMPOTENCY_CONFLICT')
                return old['result']
            transaction_lock(db,'payment-authorization',c['authorization_id'])
            transaction_lock(db,'payment-wallet',c['world_id'])
            result=self._apply(db,c,(notification_copies,notification_delay))
            db.execute('INSERT INTO payment_commands VALUES(%s,%s,%s)',(c['operation_key'],fp,Jsonb(result)))
            return result

    def _apply(self,db,c,delivery):
        row=db.execute('SELECT * FROM payment_authorizations WHERE authorization_id=%s',(c['authorization_id'],)).fetchone()
        if row: bound(row,c)
        action=c['action']
        if action=='AUTHORIZE':
            captured=db.execute("SELECT 1 FROM payment_transactions WHERE world_id=%s AND order_id=%s AND kind='CAPTURE'",(c['world_id'],c['order_id'])).fetchone()
            if captured: raise PaymentError('ORDER_ALREADY_CAPTURED')
            if row:
                if row['status']!='AUTHORIZED' or row['card_token']!=c['card_token']:
                    raise PaymentError('AUTHORIZATION_NOT_ACTIVE')
                return self._existing(db,c,'AUTHORIZED','AUTHORIZE')
            if c['card_token']=='demo-declined':
                return self._response(c,'DECLINED',None,ok=False,code='DECLINED')
            used=db.execute("SELECT COALESCE(SUM(amount_krw),0) AS amount FROM payment_authorizations WHERE world_id=%s AND card_token=%s AND status IN ('AUTHORIZED','CAPTURED')",(c['world_id'],c['card_token'])).fetchone()['amount']
            if used+c['amount_krw']>self.limit_krw:
                return self._response(c,'DECLINED',None,ok=False,code='LIMIT_EXCEEDED')
            now=time.time()
            db.execute('INSERT INTO payment_authorizations VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (c['authorization_id'],c['world_id'],c['order_id'],c['amount_krw'],c['currency'],c['payment_revision'],c['quote_fingerprint'],c['card_token'],'AUTHORIZED',now,now))
            return self._record(db,c,'AUTHORIZED',delivery)
        if not row: raise PaymentError('AUTHORIZATION_NOT_FOUND')
        outcome='CAPTURED' if action=='CAPTURE' else 'VOIDED'
        if row['status']==outcome: return self._existing(db,c,outcome,action)
        if row['status']!='AUTHORIZED': raise PaymentError('AUTHORIZATION_NOT_ACTIVE')
        if action=='CAPTURE' and db.execute("SELECT 1 FROM payment_transactions WHERE world_id=%s AND order_id=%s AND kind='CAPTURE'",(c['world_id'],c['order_id'])).fetchone():
            raise PaymentError('ORDER_ALREADY_CAPTURED')
        db.execute('UPDATE payment_authorizations SET status=%s,updated_at=%s WHERE authorization_id=%s',(outcome,time.time(),c['authorization_id']))
        return self._record(db,c,outcome,delivery)

    def _existing(self,db,c,outcome,kind):
        row=db.execute('SELECT id,seq FROM payment_transactions WHERE authorization_id=%s AND kind=%s',(c['authorization_id'],kind)).fetchone()
        if not row: raise PaymentError('LEDGER_INCONSISTENT')
        return self._response(c,outcome,row['id'],revision=row['seq'])

    def _record(self,db,c,outcome,delivery):
        txid='TX-'+uuid4().hex
        revision=db.execute('INSERT INTO payment_transactions(id,authorization_id,world_id,order_id,kind,amount_krw,at) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING seq',
            (txid,c['authorization_id'],c['world_id'],c['order_id'],c['action'],c['amount_krw'],time.time())).fetchone()['seq']
        event_id='EV-'+uuid4().hex
        event=dict(event_id=event_id,world_id=c['world_id'],order_id=c['order_id'],authorization_id=c['authorization_id'],transaction_id=txid,
            revision=revision,kind=c['action'],payment_revision=c['payment_revision'],amount_krw=c['amount_krw'],currency='KRW',mode='synthetic')
        db.execute("INSERT INTO payment_outbox(event_id,world_id,order_id,revision,body,next_at,copies) VALUES(%s,%s,%s,%s,%s,clock_timestamp()+%s*interval '1 second',%s)",
            (event_id,c['world_id'],c['order_id'],revision,canonical(event),delivery[1],delivery[0]))
        return self._response(c,outcome,txid,revision=revision)

    def operation(self,operation_key):
        with self.database.transaction(readonly=True) as db:
            row=db.execute('SELECT result FROM payment_commands WHERE operation_key=%s',(key(operation_key),)).fetchone()
            return row['result'] if row else None

    def snapshot(self,world,order_id):
        key(world);key(order_id)
        with self.database.transaction(readonly=True) as db:
            auths=db.execute('SELECT * FROM payment_authorizations WHERE world_id=%s AND order_id=%s ORDER BY payment_revision,authorization_id',(world,order_id)).fetchall()
            txs=db.execute('SELECT * FROM payment_transactions WHERE world_id=%s AND order_id=%s ORDER BY seq',(world,order_id)).fetchall()
        for a in auths: a.pop('card_token')
        return dict(mode='synthetic',provider='DEMO_PLATFORM',world_id=world,order_id=order_id,currency='KRW',
            revision=max((t['seq'] for t in txs),default=0),authorizations=auths,transactions=txs,
            held_krw=sum(a['amount_krw'] for a in auths if a['status']=='AUTHORIZED'),
            captured_krw=sum(t['amount_krw'] for t in txs if t['kind']=='CAPTURE'),capture_count=sum(t['kind']=='CAPTURE' for t in txs))

    def consume_fault(self,world,operation_key,mode):
        if mode not in {'drop_reply','duplicate_notification','late_notification'}: return False
        with self.database.transaction() as db:
            return bool(db.execute('INSERT INTO payment_faults VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING operation_key',
                (key(world),key(operation_key),mode)).fetchone())

    def claim_notifications(self,worker: str,*,limit=16,lease_seconds=30):
        key(worker)
        if type(limit) is not int or not 1<=limit<=64 or type(lease_seconds) is not int or not 1<=lease_seconds<=120:
            raise ValueError('bounded outbox lease required')
        with self.database.transaction() as db:
            rows=db.execute("""WITH due AS (
                SELECT event_id FROM payment_outbox
                WHERE NOT delivered AND attempts<8 AND next_at<=clock_timestamp()
                AND (lease_until IS NULL OR lease_until<=clock_timestamp())
                ORDER BY revision LIMIT %s FOR UPDATE SKIP LOCKED)
                UPDATE payment_outbox p SET lease_owner=%s,lease_version=p.lease_version+1,
                    lease_until=clock_timestamp()+%s*interval '1 second'
                FROM due WHERE p.event_id=due.event_id RETURNING p.*""",(limit,worker,lease_seconds)).fetchall()
            return sorted(rows,key=lambda r:r['revision'])

    def finish_notification(self,claim: dict,ok: bool):
        if type(ok) is not bool: raise ValueError('explicit delivery outcome required')
        with self.database.transaction() as db:
            row=db.execute("""UPDATE payment_outbox SET delivered=%s,attempts=attempts+1,
                next_at=clock_timestamp()+LEAST(3*power(2,LEAST(attempts,4)),30)*interval '1 second',
                lease_owner=NULL,lease_until=NULL
                WHERE event_id=%s AND lease_owner=%s AND lease_version=%s
                AND NOT delivered AND lease_until>clock_timestamp() RETURNING event_id""",
                (ok,claim['event_id'],claim['lease_owner'],claim['lease_version'])).fetchone()
            return bool(row)
