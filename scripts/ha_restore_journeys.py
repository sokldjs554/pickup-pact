"""Actual application-ledger recovery probe; local repository calls are explicit."""
from copy import deepcopy
from uuid import uuid4
from demo.route.ha.journey_store import PostgresJourneyStore
from demo.route.ha.merchant_repository import PostgresMerchantFleet
from demo.route.payments.http_client import PaymentClient
from demo.route.payments.domain import validate
from demo.route.reconciliation import reconcile


class RehearsalGateway:
    """Not a deployed transport; preserve provider binding during restore rehearsal."""
    transport='local_restore_rehearsal'
    def __init__(self,repo):self.repo=repo
    def operation(self,command):
        c=validate(command);row=self.repo.operation(c['operation_key'])
        return PaymentClient._bound(row,c) if row is not None else None
    def execute(self,command,*,fault='none'):
        c=validate(command);row=self.repo.execute(c)
        if row['ok'] and fault=='drop_reply' and self.repo.consume_fault(c['world_id'],c['operation_key'],fault):
            raise OSError('reply omitted after actual provider commit')
        return PaymentClient._bound(row,c)
    def snapshot(self,*args):return self.repo.snapshot(*args)
    def health(self):return {'storage_ready':self.repo.storage_ready()}


class JourneyRecoveryProbe:
    def __init__(self,order_dsn,merchant_dsn,pg,*,initialize):
        self.merchants=PostgresMerchantFleet(merchant_dsn,schema='pact_restore_merchants',initialize=initialize)
        try:
            self.store=PostgresJourneyStore(order_dsn,schema='pact_restore_journeys',initialize=initialize,
                fleet=self.merchants,payment_gateway=RehearsalGateway(pg),
                notification_secret='isolated-restore-shared-signing-key',worker_id='restore-probe')
        except Exception:self.merchants.close();raise
        self.pg=pg

    def close(self):
        try:self.store.close()
        finally:self.merchants.close()

    def command(self,s,action,**extra):
        c=dict(action=action,request_id=uuid4().hex,expected_version=s['version'],**extra)
        return self.store.command(s['id'],c),c

    def new(self,fault='none'):
        from demo.route.api import Intent
        s=self.store.create(Intent(points=1000,coupon_id='welcome500').model_dump(),payment_fault=fault)
        wave=next(p for p in s['all_plans'] if p['store_id']=='wave')
        return self.command(s,'reserve',quote_id=wave['quote_id'])[0]

    def ready(self,s):
        oat=next(p for p in s['all_plans'] if p['store_id']=='oat')
        s,_=self.command(s,'transfer',quote_id=oat['quote_id'])
        delta=max(0,s['current_plan']['start_at']-s['clock'])
        if delta:s,_=self.command(s,'advance',minutes=delta)
        s,_=self.command(s,'start')
        s,_=self.command(s,'advance',minutes=s['order']['ready_at']-s['clock'])
        return self.command(s,'ready')[0]

    def seed_before_backup(self):
        self.before=self.new()
        assert self.before['order'] and self.before['order']['price']==2800

    def prepare_target(self):
        final=self.ready(self.before)
        final,_=self.command(final,'claim',pickup_code=final['order']['pickup_code'])
        final_proof=reconcile(self.store,final['id'])
        assert final_proof['status']=='MATCH'
        pending=self.ready(self.new('capture_reply_lost'))
        pending,original_command=self.command(pending,'claim',pickup_code=pending['order']['pickup_code'])
        assert pending['handoff_pending'] and pending['wallet']['spent']==0
        expected={'final':self.store.read_state(final['id']), 'pending':self.store.read_state(pending['id']),
                  'original_claim':original_command,'payment':{},'merchants':{}}
        for name in ['final','pending']:
            s=expected[name]
            expected['payment'][name]=self.pg.snapshot(s['world_id'],s['order']['id'])
            expected['merchants'][name]=self.merchants.evidence(s['world_id'],s['order']['id'])
        return expected

    def verify_restored(self,expected):
        for name in ['final','pending']:
            s=expected[name]
            assert self.store.read_state(s['id'])==s
            assert self.pg.snapshot(s['world_id'],s['order']['id'])==expected['payment'][name]
            assert self.merchants.evidence(s['world_id'],s['order']['id'])==expected['merchants'][name]
        pending=expected['pending']
        recovered=self.store.command(pending['id'],expected['original_claim'])
        assert recovered['duplicate'] and not recovered['handoff_pending']
        assert recovered['order']['id']==pending['order']['id']
        proofs=[]
        for name in ['final','pending']:
            s=expected[name];proof=reconcile(self.store,s['id'])
            assert proof['status']=='MATCH' and proof['terminal']
            assert proof['payment']['captured_krw']==3200 and proof['payment']['capture_count']==1 and proof['payment']['held_krw']==0
            assert proof['benefits']['spent']==1000 and proof['benefits']['earned']==32
            assert proof['merchants']['wave']['reservation']['phase']=='RELEASED'
            assert proof['merchants']['oat']['reservation']['phase']=='CLAIMED'
            proofs.append(proof)
        return {'status':'MATCH','completed_and_pending_preserved':True,'original_claim_key_reused':True,
                'database_count':3,'proofs':proofs}
