"""Recovery must not depend on another customer request or release committed work."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import pytest
from demo.route.store import JourneyStore
from test_transfer_durability import ordered, configure, command, plan


def pending(st, fault='after_target_hold'):
    s=ordered(st);before=deepcopy(s);s=configure(st,s,fault)
    return command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id']), before


def worker(st):
    from demo.route.recovery_worker import RecoveryWorker
    return RecoveryWorker(st)


def test_saved_operation_recovers_without_customer_recover_request(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s,_=pending(st)
    assert s['handoff_pending']
    assert worker(st).run_once(now=10**12)['completed']==1
    done=st.get(s['id'])
    # Long expired pre-decision work must restore, not invent a new commitment.
    assert not done['handoff_pending'] and done['order']['store_id']=='wave'


def test_fresh_pending_work_auto_recovers_once_across_workers(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s,_=pending(st)
    from demo.route.recovery_worker import RecoveryWorker
    due=s['handoff']['recovery']['next_retry_at']
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(lambda _:RecoveryWorker(JourneyStore(st.path)).run_once(now=due+.01),range(8)))
    done=st.get(s['id']);assert not done['handoff_pending']
    assert done['order']['store_id']=='oat'
    assert len(done['receipt']['transfers'])==1 and done['receipt']['authorization_count']==1
    assert done['wallet']['held_points']==1000


@pytest.mark.parametrize('fault',['after_source_release','after_target_activation'])
def test_age_does_not_undo_durable_commit(tmp_path,fault):
    st=JourneyStore(tmp_path/'orders.sqlite');s,_=pending(st,fault)
    assert s['handoff']['decision']=='COMMIT'
    worker(st).run_once(now=10**12)
    done=st.get(s['id']);assert not done['handoff_pending']
    assert done['order']['store_id']=='oat'
    assert done['merchant_capacity']['wave']['used']==0
    assert done['merchant_capacity']['oat']['used']==1


def test_expired_undecided_hold_returns_original_benefits_and_cleans_target(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s,before=pending(st)
    worker(st).run_once(now=10**12)
    done=st.get(s['id'])
    assert done['handoff']['decision']=='ABORT' and done['handoff']['status']=='REJECTED'
    assert done['handoff']['failure']=='CONFIRMATION_EXPIRED'
    assert done['order']==before['order'] and done['wallet']==before['wallet']
    assert done['merchant_capacity']['wave']['used']==1 and done['merchant_capacity']['oat']['used']==0


def test_no_busy_retry_and_failed_work_escalates_without_releasing_seats(tmp_path,monkeypatch):
    st=JourneyStore(tmp_path/'orders.sqlite');s,_=pending(st,'after_source_release')
    def unavailable(*a,**k):raise OSError('connection lost')
    monkeypatch.setattr(st.fleet,'execute',unavailable)
    w=worker(st)
    assert w.run_once(now=s['handoff']['recovery']['next_retry_at']-1)['attempted']==0
    for _ in range(8):
        current=st.get(s['id']);w.run_once(now=current['handoff']['recovery']['next_retry_at']+.01)
        if st.get(s['id'])['handoff']['recovery']['state']=='REVIEW_REQUIRED':break
    current=st.get(s['id'])
    assert current['handoff_pending'] and current['handoff']['decision']=='COMMIT'
    assert current['handoff']['recovery']['state']=='REVIEW_REQUIRED'
    assert w.run_once(now=10**12)['attempted']==0
    assert current['merchant_capacity']['oat']['used']==1


def test_worker_reopen_does_not_reset_due_time_or_budget(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s,_=pending(st)
    saved=s['handoff']['recovery']
    reopened=JourneyStore(st.path)
    assert reopened.get(s['id'])['handoff']['recovery']==saved
    assert worker(reopened).run_once(now=saved['next_retry_at']-1)['attempted']==0


def test_periodic_worker_closes_cleanly_and_recovers_without_frontend(tmp_path):
    import time
    st=JourneyStore(tmp_path/'orders.sqlite');s,_=pending(st)
    from demo.route.recovery_worker import RecoveryWorker
    w=RecoveryWorker(st,interval=.02)
    with w:
        deadline=time.monotonic()+6
        while time.monotonic()<deadline and st.get(s['id'])['handoff_pending']:time.sleep(.03)
        assert not st.get(s['id'])['handoff_pending']
    assert not w.is_alive


@pytest.mark.parametrize('phase',['FREEZE','HOLD','RELEASE_SOURCE','ACTIVATE','ABORT_TARGET','UNFREEZE'])
def test_worker_resumes_process_crash_without_original_customer_payload(tmp_path,phase):
    import multiprocessing
    from test_transfer_durability import _crash_worker
    path=tmp_path/'orders.sqlite';st=JourneyStore(path);s=ordered(st)
    if phase in {'ABORT_TARGET','UNFREEZE'}:s=configure(st,s,'target_reject')
    before=deepcopy(s)
    request=dict(action='transfer',expected_version=s['version'],request_id='worker-crash',quote_id=plan(s,'oat')['quote_id'])
    child=multiprocessing.get_context('spawn').Process(target=_crash_worker,args=(str(path),s['id'],request,phase))
    child.start();child.join(12)
    if child.is_alive():child.kill();child.join();pytest.fail('crash boundary not reached')
    assert child.exitcode==91
    restarted=JourneyStore(path)
    # Old predecision work compensates, COMMIT work completes, compensation continues.
    worker(restarted).run_once(now=10**12)
    done=restarted.get(s['id']);assert not done['handoff_pending']
    if phase in {'RELEASE_SOURCE','ACTIVATE'}:
        assert done['order']['store_id']=='oat'
        assert done['merchant_capacity']['wave']['used']==0 and done['merchant_capacity']['oat']['used']==1
    else:
        assert done['order']==before['order'] and done['wallet']==before['wallet']
        assert done['merchant_capacity']['wave']['used']==1 and done['merchant_capacity']['oat']['used']==0


def test_old_pending_rows_without_schedule_are_recovered_not_dropped(tmp_path):
    import json
    st=JourneyStore(tmp_path/'orders.sqlite');s,_=pending(st)
    with st.connection() as db:
        body=json.loads(db.execute('SELECT body FROM route_operations WHERE id=?',(s['active_operation'],)).fetchone()[0])
        del body['recovery']
        db.execute('UPDATE route_operations SET body=? WHERE id=?',(json.dumps(body),body['id']))
    worker(st).run_once(now=10**12)
    done=st.get(s['id'])
    assert not done['handoff_pending']


def test_pending_recovery_metadata_change_advances_public_version(tmp_path,monkeypatch):
    st=JourneyStore(tmp_path/'orders.sqlite');s,_=pending(st)
    def unavailable(*a,**k):raise OSError('still unavailable')
    monkeypatch.setattr(st.fleet,'execute',unavailable)
    worker(st).run_once(now=s['handoff']['recovery']['next_retry_at']+.01)
    current=st.get(s['id'])
    assert current['handoff_pending']
    assert current['handoff']['recovery']['retry_count']>s['handoff']['recovery']['retry_count']
    assert current['version']>s['version']
