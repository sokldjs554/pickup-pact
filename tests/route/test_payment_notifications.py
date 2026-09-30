"""Signed immutable events are hints, never a substitute for provider state."""
import json
import sqlite3
import time
import pytest


def event(**kw):
    return dict(event_id='event-1',world_id='world-a',order_id='PCT-ONE',authorization_id='auth-a',
                transaction_id='tx-1',revision=1,payment_revision=1,kind='AUTHORIZE',
                amount_krw=2800,currency='KRW',mode='synthetic')|kw


def test_duplicate_and_reverse_events_do_not_become_two_financial_effects(tmp_path):
    from demo.route.payments.notifications import PaymentInbox, sign
    inbox=PaymentInbox(tmp_path/'orders.sqlite','x'*64)
    inbox.register('world-a','PCT-ONE','auth-a')
    newer=json.dumps(event(event_id='event-2',revision=2,kind='VOID',transaction_id='tx-2')).encode()
    ts=str(int(time.time()))
    assert not inbox.accept(newer,ts,sign('x'*64,ts,newer))['duplicate']
    old=json.dumps(event()).encode()
    assert not inbox.accept(old,ts,sign('x'*64,ts,old))['duplicate']
    assert inbox.accept(old,ts,sign('x'*64,ts,old))['duplicate']
    with sqlite3.connect(inbox.path) as db:
        assert db.execute('select count(*) from payment_inbox').fetchone()[0]==2
        assert db.execute('select revision from payment_recheck_queue').fetchone()[0]==2
        assert not db.execute("select name from sqlite_master where name='payment_authorizations'").fetchone()


def test_conflicting_event_id_forged_stale_and_wrong_world_rejected(tmp_path):
    from demo.route.payments.notifications import PaymentInbox, sign
    from demo.route.payments.domain import PaymentError
    inbox=PaymentInbox(tmp_path/'orders.sqlite','x'*64);inbox.register('world-a','PCT-ONE','auth-a')
    body=json.dumps(event()).encode();ts=str(int(time.time()))
    inbox.accept(body,ts,sign('x'*64,ts,body))
    with pytest.raises(PaymentError):inbox.accept(body,ts,'f'*64)
    for age in [-600,600]:
        stamp=str(int(time.time())+age)
        with pytest.raises(PaymentError):inbox.accept(body,stamp,sign('x'*64,stamp,body))
    for change in [{'amount_krw':1},{'world_id':'other','event_id':'other'},{'authorization_id':'unregistered','event_id':'other'}]:
        bad=json.dumps(event(**change)).encode()
        with pytest.raises(PaymentError):inbox.accept(bad,ts,sign('x'*64,ts,bad))
    with sqlite3.connect(inbox.path) as db:
        assert db.execute('select count(*) from payment_inbox').fetchone()[0]==1


def test_pending_outbox_is_resent_with_new_secret_and_duplicate_ack(tmp_path):
    from demo.route.payments.notifications import PaymentInbox, deliver_due
    from demo.route.payments.repository import PaymentRepository
    from test_payment_repository import cmd
    inbox=PaymentInbox(tmp_path/'orders.sqlite','new-secret-'+'x'*32)
    inbox.register('world-a','PCT-ONE','auth-a')
    repo=PaymentRepository(tmp_path/'pg.sqlite');repo.execute(cmd())
    seen=[]
    # Sender callback captures genuine serialized body and HMAC; no financial result is mocked.
    def send(body,stamp,signature):
        seen.append(inbox.accept(body,stamp,signature));return True
    assert deliver_due(repo,'new-secret-'+'x'*32,send,duplicate=True)==1
    assert [x['duplicate'] for x in seen]==[False,True]
    assert deliver_due(repo,'new-secret-'+'x'*32,send)==0


def test_actual_pg_outbox_delivers_duplicate_http_to_internal_inbox(tmp_path):
    from demo.route.payments.notifications import PaymentInbox, NotificationServer
    from demo.route.payments.runtime import PaymentProcess
    from test_payment_repository import cmd
    inbox=PaymentInbox(tmp_path/'orders.sqlite','x'*64);inbox.register('world-a','PCT-ONE','auth-a')
    observed=[];real_accept=inbox.accept
    def capture(*args):
        result=real_accept(*args);observed.append(result);return result
    inbox.accept=capture
    with NotificationServer(inbox) as receiver, PaymentProcess(tmp_path/'pg',receiver.url,inbox.secret) as pg:
        pg.client.execute(cmd(),fault='duplicate_notification')
        end=time.monotonic()+3
        while len(observed)<2 and time.monotonic()<end:time.sleep(.03)
        assert [r['duplicate'] for r in observed]==[False,True]
        with sqlite3.connect(inbox.path) as db:
            assert db.execute('select count(*) from payment_inbox').fetchone()[0]==1


def test_restart_rotated_signing_key_sends_preserved_outbox(tmp_path):
    from demo.route.payments.notifications import PaymentInbox, NotificationServer
    from demo.route.payments.runtime import PaymentProcess
    from test_payment_repository import cmd
    with PaymentProcess(tmp_path/'pg') as pg:pg.client.execute(cmd())
    inbox=PaymentInbox(tmp_path/'orders.sqlite','rotated-'+'x'*32);inbox.register('world-a','PCT-ONE','auth-a')
    with NotificationServer(inbox) as receiver, PaymentProcess(tmp_path/'pg',receiver.url,inbox.secret):
        end=time.monotonic()+3;count=0
        while time.monotonic()<end:
            with sqlite3.connect(inbox.path) as db:count=db.execute('select count(*) from payment_inbox').fetchone()[0]
            if count:break
            time.sleep(.05)
        assert count==1
