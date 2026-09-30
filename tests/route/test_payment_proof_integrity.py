"""Every green proof requires independent source and loyalty evidence."""
import pytest
from test_payment_operations import system, ordered, ready, command
from test_payment_reconciliation import check


def test_declined_order_cannot_hide_an_orphan_merchant_reservation(system):
    st,pg=system;s=ordered(st,card_token='demo-declined')
    receipt=st.fleet.execute('wave',world=s['world_id'],order_id=s['payment_order_id'],generation=0,
        operation_id='orphan-seat-check',action='ADMIT')
    assert receipt['ok']
    out=check(st,s)
    assert out['status']=='MISMATCH'
    assert out['checks']['merchant']['status']=='MISMATCH'
    assert out['checks']['payment']['status']=='MATCH'


def test_declined_order_cannot_hide_unreleased_points_or_coupon(system):
    st,pg=system;s=ordered(st,card_token='demo-declined')
    with st.connection() as db:
        raw=st._load(db,s['id']);raw['wallet'].update(held_points=500,held_coupon='welcome500');st._save(db,raw)
    out=check(st,s)
    assert out['status']=='MISMATCH' and out['checks']['benefits']['status']=='MISMATCH'


@pytest.mark.parametrize('corruption',['coupon','balance'])
def test_final_benefit_proof_checks_coupon_and_remaining_balance(system,corruption):
    st,pg=system;s=ready(st,ordered(st));s=command(st,s,'claim',pickup_code=s['order']['pickup_code'])
    with st.connection() as db:
        raw=st._load(db,s['id'])
        if corruption=='coupon':raw['wallet']['used_coupons']=[]
        else:raw['wallet']['balance']+=1
        st._save(db,raw)
    out=check(st,s)
    assert out['status']=='MISMATCH' and out['checks']['benefits']['status']=='MISMATCH'
