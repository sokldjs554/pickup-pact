"""Shared authenticated notification hints; never apply money from an event alone."""
import hashlib
from ..payments.notifications import parse_event
from ..payments.domain import PaymentError
from .database import transaction_lock


class PostgresPaymentInbox:
    def __init__(self, repository, secret):
        if not isinstance(secret, str) or len(secret) < 24:
            raise ValueError('shared notification secret is required')
        self.repository, self.secret = repository, secret

    def accept(self, body, stamp, signature):
        event = parse_event(self.secret, body, stamp, signature)
        sha = hashlib.sha256(body).hexdigest()
        from psycopg.types.json import Jsonb
        with self.repository.database.transaction() as db:
            transaction_lock(db, 'notification-event', event['event_id'])
            binding = db.execute('SELECT world_id,order_id FROM authorization_refs WHERE authorization_id=%s',
                                 (event['authorization_id'],)).fetchone()
            if binding != dict(world_id=event['world_id'], order_id=event['order_id']):
                raise PaymentError('EVENT_BINDING_CONFLICT', 403)
            old = db.execute('SELECT body_sha256 FROM notification_inbox WHERE event_id=%s', (event['event_id'],)).fetchone()
            if old:
                if old['body_sha256'] != sha:
                    raise PaymentError('EVENT_BODY_CONFLICT')
                return dict(accepted=True, duplicate=True)
            db.execute('''INSERT INTO notification_inbox(event_id,body_sha256,world_id,order_id,revision,body)
                VALUES(%s,%s,%s,%s,%s,%s)''',
                       (event['event_id'], sha, event['world_id'], event['order_id'], event['revision'], Jsonb(event)))
            db.execute('''INSERT INTO notification_hints VALUES(%s,%s,%s) ON CONFLICT(world_id,order_id)
                DO UPDATE SET revision=GREATEST(notification_hints.revision,excluded.revision)''',
                       (event['world_id'], event['order_id'], event['revision']))
        return dict(accepted=True, duplicate=False)

    def wake_operations(self, now, limit=16):
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError('bounded notification batch required')
        with self.repository.database.transaction() as db:
            rows = db.execute('SELECT * FROM notification_hints ORDER BY world_id,order_id LIMIT %s FOR UPDATE SKIP LOCKED',
                              (limit,)).fetchall()
            for row in rows:
                db.execute("""UPDATE ha_operations SET next_at=LEAST(next_at,to_timestamp(%s))
                    WHERE payload->>'world'=%s AND payload->>'order_id'=%s AND status='PENDING'
                    AND payload->'recovery'->>'state'<>'REVIEW_REQUIRED'""", (now+3, row['world_id'], row['order_id']))
                db.execute('DELETE FROM notification_hints WHERE world_id=%s AND order_id=%s', (row['world_id'], row['order_id']))
        return len(rows)
