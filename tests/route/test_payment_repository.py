"""Real SQLite financial effects; independent from the order event list."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import sqlite3
import pytest


def cmd(action='AUTHORIZE', **changes):
    result = dict(action=action, world_id='world-a', order_id='PCT-ONE',
        operation_key='op:'+action, authorization_id='auth-a', amount_krw=2800,
        currency='KRW', payment_revision=1, quote_fingerprint='a'*64)
    if action == 'AUTHORIZE': result['card_token'] = 'demo-approved'
    return result | changes


def repo(tmp_path, **kwargs):
    from demo.route.payments.repository import PaymentRepository
    return PaymentRepository(tmp_path/'payments.sqlite', **kwargs)


def test_approval_survives_reopen_and_has_independent_transaction(tmp_path):
    r=repo(tmp_path); a=r.execute(cmd())
    assert a['ok'] and a['outcome']=='AUTHORIZED'
    assert repo(tmp_path).execute(cmd()) == a
    s=repo(tmp_path).snapshot('world-a','PCT-ONE')
    assert s['held_krw']==2800 and s['captured_krw']==0
    assert s['revision']==1 and len(s['transactions'])==1
    assert s['transactions'][0]['id']==a['transaction_id']
    assert r.operation('op:AUTHORIZE')==a


def test_replacement_preserves_old_until_confirmed_and_captures_final_amount(tmp_path):
    r=repo(tmp_path);r.execute(cmd())
    newer=dict(authorization_id='auth-b', payment_revision=2, quote_fingerprint='b'*64,amount_krw=3200)
    r.execute(cmd(**newer,operation_key='op:REAUTHORIZE'))
    assert r.snapshot('world-a','PCT-ONE')['held_krw']==6000
    r.execute(cmd('VOID'))
    result=r.execute(cmd('CAPTURE',**newer))
    s=r.snapshot('world-a','PCT-ONE')
    assert result['ok'] and s['held_krw']==0 and s['captured_krw']==3200
    assert s['capture_count']==1 and s['revision']==4
    assert [t['kind'] for t in s['transactions']]==['AUTHORIZE','AUTHORIZE','VOID','CAPTURE']


def test_decline_is_durable_command_without_approval(tmp_path):
    r=repo(tmp_path); c=cmd(card_token='demo-declined')
    answer=r.execute(c)
    assert not answer['ok'] and answer['code']=='DECLINED'
    assert r.execute(c)==answer
    assert not r.snapshot('world-a','PCT-ONE')['authorizations']
    assert r.snapshot('world-a','PCT-ONE')['revision']==0


def test_replacement_limit_failure_does_not_void_existing_hold(tmp_path):
    r=repo(tmp_path,limit_krw=5900);r.execute(cmd())
    c=cmd(authorization_id='auth-b',payment_revision=2,amount_krw=3200,operation_key='new')
    assert r.execute(c)['code']=='LIMIT_EXCEEDED'
    s=r.snapshot('world-a','PCT-ONE');assert s['held_krw']==2800
    assert len(s['transactions'])==1


@pytest.mark.parametrize('change',[{'amount_krw':3200},{'world_id':'other'}, {'order_id':'OTHER'}, {'payment_revision':2}])
def test_same_command_key_different_content_is_conflict_without_effect(tmp_path,change):
    from demo.route.payments.domain import PaymentError
    r=repo(tmp_path);r.execute(cmd());before=r.snapshot('world-a','PCT-ONE')
    with pytest.raises(PaymentError) as e:r.execute(cmd(**change))
    assert e.value.status==409 and e.value.code=='IDEMPOTENCY_CONFLICT'
    assert r.snapshot('world-a','PCT-ONE')==before


def test_twenty_four_different_capture_keys_only_one_effect(tmp_path):
    r=repo(tmp_path);r.execute(cmd())
    with ThreadPoolExecutor(max_workers=12) as pool:
        rows=list(pool.map(lambda i:r.execute(cmd('CAPTURE',operation_key=f'capture-{i}')),range(24)))
    assert len({x['transaction_id'] for x in rows})==1
    assert all(x['ok'] for x in rows)
    assert r.snapshot('world-a','PCT-ONE')['captured_krw']==2800
    assert r.snapshot('world-a','PCT-ONE')['capture_count']==1


def test_second_authorization_cannot_capture_same_order(tmp_path):
    from demo.route.payments.domain import PaymentError
    r=repo(tmp_path);r.execute(cmd());r.execute(cmd('CAPTURE'))
    newer=dict(authorization_id='auth-b',payment_revision=2,operation_key='new')
    # Once captured, even a fresh approval is rejected, not a second credit hold.
    with pytest.raises(PaymentError):r.execute(cmd(**newer))
    assert r.snapshot('world-a','PCT-ONE')['capture_count']==1


def test_voided_authorization_cannot_be_revived_by_new_key(tmp_path):
    from demo.route.payments.domain import PaymentError
    r=repo(tmp_path);original=r.execute(cmd());r.execute(cmd('VOID'))
    assert r.execute(cmd())==original  # original receipt != current financial state
    with pytest.raises(PaymentError):r.execute(cmd(operation_key='new-approval-key'))
    with pytest.raises(PaymentError):r.execute(cmd('CAPTURE'))
    s=r.snapshot('world-a','PCT-ONE');assert s['held_krw']==s['captured_krw']==0
    assert s['authorizations'][0]['status']=='VOIDED'


def test_authorization_binding_cannot_change_order_or_amount(tmp_path):
    from demo.route.payments.domain import PaymentError
    r=repo(tmp_path);r.execute(cmd())
    for field,value in [('world_id','world-b'),('order_id','PCT-TWO'),('amount_krw',1),('quote_fingerprint','b'*64),('payment_revision',2)]:
        with pytest.raises(PaymentError):r.execute(cmd('CAPTURE',operation_key=uuid4().hex,**{field:value}))
    assert r.snapshot('world-a','PCT-ONE')['capture_count']==0
    assert r.snapshot('world-b','PCT-ONE')['authorizations']==[]


@pytest.mark.parametrize('amount',[0,-1,True,False,2.8,'2800',None,30001])
def test_bad_money_never_enters_ledger(tmp_path,amount):
    from demo.route.payments.domain import PaymentError
    r=repo(tmp_path)
    with pytest.raises(PaymentError) as e:r.execute(cmd(amount_krw=amount))
    assert e.value.status==422
    assert r.snapshot('world-a','PCT-ONE')['transactions']==[]


@pytest.mark.parametrize('change',[{'currency':'USD'},{'payment_revision':True},{'payment_revision':0},
 {'card_token':'live-card'},{'card_number':'1234'}, {'authorization_id':'../secret'}, {'world_id':'\ud800'}])
def test_bad_protocol_input_has_no_effect(tmp_path,change):
    from demo.route.payments.domain import PaymentError
    r=repo(tmp_path)
    with pytest.raises(PaymentError):r.execute(cmd(**change))
    assert r.snapshot('world-a','PCT-ONE')['revision']==0


def test_outbox_and_financial_effect_commit_together(tmp_path):
    r=repo(tmp_path);r.execute(cmd());r.execute(cmd('VOID'))
    with sqlite3.connect(r.path) as db:
        assert db.execute('select count(*) from payment_outbox').fetchone()[0]==2
        assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
        assert db.execute('pragma journal_mode').fetchone()[0]=='wal'
    assert r.snapshot('world-a','PCT-ONE')['revision']==2
