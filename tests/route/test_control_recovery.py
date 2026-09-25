"""Scenario controls must not orphan merchant capacity after a lost reply."""
import pytest
from demo.route.store import JourneyStore
from demo.route.recovery_worker import RecoveryWorker
from test_transfer_durability import ordered


@pytest.mark.parametrize('action',['occupy','clear'])
def test_guest_control_loss_is_journaled_and_worker_recovers(tmp_path,action):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st)
    def control(action,key):
        return st.transfer_control(s['id'],dict(action=action,store_id='oat',expected_version=s['version'],request_id=key))
    if action=='clear':s=control('occupy','first')
    before=s['order'],s['wallet'];once=[]
    def lose(shop,phase,key,result):
        if shop=='oat' and result['ok'] and not once:
            once.append(key);raise OSError('lost after merchant commit')
    st.fleet.after_commit=lose
    result=control(action,'lost-control')
    assert result['handoff_pending']
    assert (result['order'],result['wallet'])==before
    st.fleet.after_commit=None
    reopened=JourneyStore(st.path)
    RecoveryWorker(reopened).run_once(now=result['handoff']['recovery']['next_retry_at']+.01)
    done=reopened.get(s['id'])
    assert not done['handoff_pending']
    assert done['merchant_capacity']['oat']['used']==(1 if action=='occupy' else 0)
    assert len(done.get('capacity_guests',{}).get('oat',[]))==(1 if action=='occupy' else 0)
    assert (done['order'],done['wallet'])==before
