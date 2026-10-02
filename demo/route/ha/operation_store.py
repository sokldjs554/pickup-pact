"""Persistent work ownership primitive; HTTP is performed between short transactions.

This module is not yet the existing journey coordinator's selected backend.
"""
from pathlib import Path
from ..payments.domain import canonical,fingerprint,key
from .database import PostgresDatabase,transaction_lock


class OperationConflict(ValueError):
    pass


class PostgresOperationStore:
    def __init__(self,dsn,*,schema='pact_operations',initialize=True):
        self.database=PostgresDatabase(dsn,schema=schema)
        try:
            if initialize:self.database.initialize((Path(__file__).parent/'sql/operations.sql').read_text())
        except Exception:
            self.close();raise

    def close(self):self.database.close()

    def enqueue(self,operation_id:str,order_id:str,payload:dict,*,request_key:str) -> dict:
        key(operation_id);key(order_id);key(request_key)
        if not isinstance(payload,dict):raise ValueError('operation payload must be an object')
        with self.database.transaction() as db:
            return self.enqueue_in(db,operation_id,order_id,payload,request_key=request_key)

    @staticmethod
    def enqueue_in(db,operation_id,order_id,payload,*,request_key):
        key(operation_id);key(order_id);key(request_key)
        if not isinstance(payload,dict):raise ValueError('operation payload must be an object')
        fp=fingerprint({'id':operation_id,'order_id':order_id,'payload':payload})
        from psycopg.types.json import Jsonb
        transaction_lock(db,'operation-order',order_id)
        prior=db.execute('SELECT * FROM ha_operations WHERE order_id=%s AND request_key=%s',(order_id,request_key)).fetchone()
        if prior:
            if prior['fingerprint']!=fp:raise OperationConflict('same request has different content')
            return prior
        if db.execute("SELECT 1 FROM ha_operations WHERE order_id=%s AND status='PENDING'",(order_id,)).fetchone():
            raise OperationConflict('an operation for this order is still pending')
        transaction_lock(db,'operation-id',operation_id)
        if db.execute('SELECT 1 FROM ha_operations WHERE id=%s',(operation_id,)).fetchone():
            raise OperationConflict('operation id belongs to another request')
        return db.execute('INSERT INTO ha_operations(id,order_id,request_key,fingerprint,payload) VALUES(%s,%s,%s,%s,%s) RETURNING *',
            (operation_id,order_id,request_key,fp,Jsonb(payload))).fetchone()

    def read(self,operation_id:str)->dict|None:
        with self.database.transaction(readonly=True) as db:
            return db.execute('SELECT * FROM ha_operations WHERE id=%s',(key(operation_id),)).fetchone()

    def claim_due(self,worker:str,*,limit:int=16,lease_seconds:int=30)->list[dict]:
        key(worker)
        if type(limit) is not int or not 1<=limit<=64 or type(lease_seconds) is not int or not 1<=lease_seconds<=120:
            raise ValueError('bounded operation lease required')
        with self.database.transaction() as db:
            return db.execute("""WITH due AS (
                SELECT id FROM ha_operations WHERE status='PENDING' AND next_at<=clock_timestamp()
                  AND (lease_until IS NULL OR lease_until<=clock_timestamp())
                ORDER BY next_at,id LIMIT %s FOR UPDATE SKIP LOCKED)
                UPDATE ha_operations o SET lease_owner=%s,lease_version=o.lease_version+1,
                    lease_until=clock_timestamp()+%s*interval '1 second'
                FROM due WHERE o.id=due.id RETURNING o.*""",(limit,worker,lease_seconds)).fetchall()

    def apply_result(self,claim:dict,payload:dict,*,finished:bool,retry_seconds:int=0)->bool:
        if not isinstance(payload,dict) or type(finished) is not bool or type(retry_seconds) is not int or not 0<=retry_seconds<=30:
            raise ValueError('explicit result and bounded retry required')
        with self.database.transaction() as db:
            return self.apply_in(db,claim,payload,finished=finished,retry_seconds=retry_seconds)

    @staticmethod
    def apply_in(db,claim,payload,*,finished,retry_seconds=0):
        if not isinstance(payload,dict) or type(finished) is not bool or type(retry_seconds) is not int or not 0<=retry_seconds<=30:
            raise ValueError('explicit result and bounded retry required')
        from psycopg.types.json import Jsonb
        row=db.execute("""UPDATE ha_operations SET payload=%s,status=%s,
                phase_version=phase_version+1,lease_owner=NULL,lease_until=NULL,
                next_at=clock_timestamp()+%s*interval '1 second'
            WHERE id=%s AND order_id=%s AND phase_version=%s AND lease_version=%s
                AND lease_owner=%s AND lease_until>clock_timestamp() AND status='PENDING'
            RETURNING id""",(Jsonb(payload),'COMPLETED' if finished else 'PENDING',retry_seconds,
                claim['id'],claim['order_id'],claim['phase_version'],claim['lease_version'],claim['lease_owner'])).fetchone()
        return bool(row)

    @staticmethod
    def claim_one_in(db,operation_id,worker,*,lease_seconds=30,force=False):
        key(operation_id);key(worker)
        if type(lease_seconds) is not int or not 1<=lease_seconds<=120 or type(force) is not bool:
            raise ValueError('bounded operation lease required')
        return db.execute("""UPDATE ha_operations SET lease_owner=%s,lease_version=lease_version+1,
            lease_until=clock_timestamp()+%s*interval '1 second'
            WHERE id=%s AND status='PENDING' AND (%s OR next_at<=clock_timestamp())
              AND (lease_until IS NULL OR lease_until<=clock_timestamp()) RETURNING *""",
            (worker,lease_seconds,operation_id,force)).fetchone()
