"""Durable *synthetic* review records. No gateway, broker or money movement.

Evidence append, version checking, approved actions and resulting simulated
reversals are atomic. The caller never supplies authoritative amounts.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .engine import reconcile
from .financial_scope import accounting_amount
from .models import EventEnvelope, ReconcileRequest


class ReviewConflict(ValueError):
    pass


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sample_events(case: str) -> list[EventEnvelope]:
    base = datetime(2026, 9, 23, tzinfo=UTC)
    def ev(identity, kind, at, cause=None, payload=None, received=None):
        return EventEnvelope(event_id=identity, aggregate_id='sample-order', event_type=kind,
            occurred_at=base+timedelta(seconds=at), received_at=base+timedelta(seconds=at if received is None else received),
            causation_id=cause, payload=payload or {})
    root = [ev('hold','PickupSlotHeld',0), ev('pay','PaymentAuthorized',1,'hold'),
            ev('confirm','CommitmentConfirmed',2,'pay')]
    cancel = ev('cancel','CommitmentCancelled',3,'confirm',{'scope':'FULL'},20)
    post = ev('settle','SettlementPosted',4,'cancel',{'amount':'9000','currency':'KRW'})
    reward = ev('reward','RewardGranted',5,'cancel',{'amount':'90','currency':'PTS','source':'order_reward'})
    if case == 'normal': return root
    if case == 'delayed_cancel': return root+[cancel,post,reward]
    if case == 'conflicting_identity':
        return root+[cancel,post,post.model_copy(update={'payload':{'amount':'1','currency':'KRW'}})]
    if case == 'terminal_conflict': return root+[cancel,post,ev('claim','PickupClaimed',6,'confirm')]
    if case == 'missing_parent': return root+[cancel,post.model_copy(update={'causation_id':'late'})]
    if case == 'partial_cancel':
        partial = cancel.model_copy(update={'payload':{
            'scope':'PARTIAL',
            'amount':'3000',
            'allocations':[{'target_event_id':'settle','amount':'3000','unit':'KRW'}],
        }})
        return root+[partial,post]
    if case == 'multiple_postings':
        return root+[cancel,post,ev('settle2','SettlementPosted',6,'cancel',{'amount':'2000','currency':'KRW'})]
    raise ValueError('unknown sample case')


class ReviewStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS review_sessions (
                    id TEXT PRIMARY KEY, version INTEGER NOT NULL, sample_case TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS review_events (
                    session_id TEXT NOT NULL REFERENCES review_sessions(id),
                    event_id TEXT NOT NULL, semantic_hash TEXT NOT NULL, envelope TEXT NOT NULL,
                    PRIMARY KEY(session_id,event_id,semantic_hash));
                CREATE TABLE IF NOT EXISTS review_plans (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES review_sessions(id),
                    version INTEGER NOT NULL, evidence_hash TEXT NOT NULL,
                    actions TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS review_effects (
                    session_id TEXT NOT NULL REFERENCES review_sessions(id), action_id TEXT NOT NULL,
                    target_event_id TEXT NOT NULL, repair TEXT NOT NULL,
                    amount INTEGER NOT NULL CHECK(amount>0), unit TEXT NOT NULL CHECK(unit IN ('KRW','PTS')),
                    recorded_at TEXT NOT NULL, PRIMARY KEY(session_id,action_id),
                    UNIQUE(session_id,target_event_id,repair));
                CREATE TABLE IF NOT EXISTS review_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    action TEXT NOT NULL, detail TEXT NOT NULL, occurred_at TEXT NOT NULL);
            ''')
        finally:
            db.close()
        Path(self.path).chmod(0o600)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        return db

    @contextmanager
    def _tx(self, write=True):
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _audit(self, db, sid, action, detail):
        db.execute('INSERT INTO review_audit(session_id,action,detail,occurred_at) VALUES(?,?,?,?)',
            (sid,action,detail,datetime.now(UTC).isoformat()))

    def _row(self, db, sid):
        row = db.execute('SELECT * FROM review_sessions WHERE id=?',(sid,)).fetchone()
        if row is None: raise KeyError('session not found')
        return row

    def _events(self, db, sid):
        return [EventEnvelope.model_validate_json(row[0]) for row in db.execute(
            'SELECT envelope FROM review_events WHERE session_id=? ORDER BY event_id,semantic_hash',(sid,))]

    def _add_events(self, db, sid, events):
        added = 0
        for event in events:
            fingerprint = digest(event.model_dump(mode='json',exclude={'received_at'}))
            added += db.execute('INSERT OR IGNORE INTO review_events VALUES(?,?,?,?)',
                (sid,event.event_id,fingerprint,event.model_dump_json())).rowcount
        if added:
            db.execute('UPDATE review_sessions SET version=version+1 WHERE id=?',(sid,))
        return added

    def _view(self, db, sid):
        row = self._row(db,sid)
        events = self._events(db,sid)
        evidence_hash = digest(sorted(
            (e.model_dump(mode='json',exclude={'received_at'}) for e in events), key=canonical))
        return {'id':sid,'version':row['version'],'case':row['sample_case'],
            'evidence_hash':evidence_hash,'events':[e.model_dump(mode='json') for e in events],
            'evaluation':reconcile(ReconcileRequest(events=events)).model_dump(mode='json'),
            'effects':[dict(r) for r in db.execute('SELECT * FROM review_effects WHERE session_id=? ORDER BY action_id',(sid,))],
            'audit':[dict(r) for r in db.execute('SELECT sequence,action,detail,occurred_at FROM review_audit WHERE session_id=? ORDER BY sequence',(sid,))],
            'mode':'synthetic_only','external_money_movement':False}

    def create(self, case):
        events = sample_events(case)
        sid = str(uuid4())
        with self._tx() as db:
            if db.execute('SELECT count(*) FROM review_sessions').fetchone()[0] >= 1000:
                raise ReviewConflict('local session capacity reached; use a fresh local database')
            db.execute('INSERT INTO review_sessions VALUES(?,?,?,?)',(sid,0,case,datetime.now(UTC).isoformat()))
            self._add_events(db,sid,events)
            self._audit(db,sid,'SAMPLE_CREATED',case)
            return self._view(db,sid)

    def get(self, sid):
        with self._tx(False) as db: return self._view(db,sid)

    def simulate(self, sid, action):
        with self._tx() as db:
            view = self._view(db,sid)
            if len(view['audit']) >= 200: raise ReviewConflict('sample action limit reached')
            events = self._events(db,sid)
            posting = next((e for e in events if e.event_type=='SettlementPosted'),None)
            if posting is None: raise ReviewConflict('this sample has no settlement event')
            if action == 'redeliver':
                arriving = posting.model_copy(update={'received_at':datetime.now(UTC)})
            elif action == 'conflict':
                arriving = posting.model_copy(update={'payload':{'amount':'1','currency':'KRW'},'received_at':datetime.now(UTC)})
            elif action == 'deliver_missing':
                if view['case'] != 'missing_parent': raise ReviewConflict('no delayed sample evidence')
                arriving = EventEnvelope(event_id='late', aggregate_id='sample-order',
                    event_type='CapacityRevised', occurred_at=posting.occurred_at-timedelta(microseconds=1),
                    received_at=datetime.now(UTC),causation_id='cancel',payload={'revision':1})
            else: raise ReviewConflict('unsupported simulation')
            added = self._add_events(db,sid,[arriving])
            self._audit(db,sid,'EVIDENCE_RECEIVED',f'{action}; new_semantic_events={added}')
            return self._view(db,sid)

    def _actions(self, view):
        evaluation = view['evaluation']
        if evaluation['decision'] != 'AUTO' or 'MANUAL_REVIEW' in evaluation['repairs']:
            raise ReviewConflict('evidence is not executable')
        actions = []
        for action in evaluation.get('financial_actions', []):
            data = {
                'target_event_id': action['target_event_id'],
                'repair': action['repair'],
                'amount': accounting_amount(action['amount']),
                'unit': action['unit'],
            }
            actions.append({'action_id':digest({'session':view['id'],**data}),**data})
        if not actions: raise ReviewConflict('no financial correction is required')
        return actions

    def preview(self, sid, version):
        with self._tx() as db:
            view=self._view(db,sid)
            if version!=view['version']: raise ReviewConflict('stale evidence version; refresh first')
            if db.execute('SELECT count(*) FROM review_plans WHERE session_id=?',(sid,)).fetchone()[0]>=100:
                raise ReviewConflict('sample plan limit reached')
            actions=self._actions(view)
            pid=str(uuid4())
            db.execute('INSERT INTO review_plans VALUES(?,?,?,?,?,0)',(pid,sid,version,view['evidence_hash'],canonical(actions)))
            self._audit(db,sid,'PLAN_CREATED',pid)
            return {'id':pid,'version':version,'evidence_hash':view['evidence_hash'],'actions':actions,'mode':'synthetic_only'}

    def approve(self, sid, pid, version, evidence_hash, actions):
        with self._tx() as db:
            plan=db.execute('SELECT * FROM review_plans WHERE id=? AND session_id=?',(pid,sid)).fetchone()
            if plan is None: raise KeyError('plan not found')
            stored=json.loads(plan['actions'])
            if version!=plan['version'] or evidence_hash!=plan['evidence_hash'] or canonical(actions)!=canonical(stored):
                raise ReviewConflict('approval does not match the saved plan')
            if plan['applied']:
                return {'duplicate':True,'session':self._view(db,sid)}
            view=self._view(db,sid)
            if version!=view['version'] or evidence_hash!=view['evidence_hash']:
                raise ReviewConflict('stale plan; evidence has changed')
            if canonical(self._actions(view))!=canonical(stored):
                raise ReviewConflict('current evidence no longer authorizes the plan')
            now=datetime.now(UTC)
            last=max(e.occurred_at for e in self._events(db,sid))
            observed=max(now,last+timedelta(microseconds=1))
            reversals=[]
            for action in stored:
                db.execute('INSERT INTO review_effects VALUES(?,?,?,?,?,?,?)',(sid,action['action_id'],
                    action['target_event_id'],action['repair'],action['amount'],action['unit'],now.isoformat()))
                reversals.append(EventEnvelope(event_id='simulated-'+action['action_id'],aggregate_id='sample-order',
                    event_type='SettlementReversed' if action['repair']=='REVERSE_SETTLEMENT' else 'RewardReversed',
                    occurred_at=observed,received_at=now,causation_id=action['target_event_id'],
                    payload={'amount':str(action['amount']),'currency':action['unit'],'simulation':True}))
            self._add_events(db,sid,reversals)
            db.execute('UPDATE review_plans SET applied=1 WHERE id=?',(pid,))
            self._audit(db,sid,'SIMULATED_CORRECTION_RECORDED',pid)
            return {'duplicate':False,'session':self._view(db,sid)}
