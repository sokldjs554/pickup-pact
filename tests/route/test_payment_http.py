"""Separate process and real loopback sockets, not mocked response JSON."""
import os
import time
import httpx
import pytest
from test_payment_repository import cmd


def test_committed_approval_with_lost_socket_recovered_by_original_key(tmp_path):
    from demo.route.payments.runtime import PaymentProcess
    from demo.route.payments.http_client import PaymentUnavailable
    with PaymentProcess(tmp_path/'pg') as process:
        assert process.process.pid != os.getpid()
        with pytest.raises(PaymentUnavailable):process.client.execute(cmd(),fault='drop_reply')
        result=process.client.operation(cmd())
        assert result['ok'] and result['outcome']=='AUTHORIZED'
        assert process.client.execute(cmd(),fault='drop_reply')==result
        assert process.client.snapshot('world-a','PCT-ONE')['held_krw']==2800
        assert process.client.health()['storage_ready'] is True


def test_capture_and_void_drop_reply_once_after_commit(tmp_path):
    from demo.route.payments.runtime import PaymentProcess
    from demo.route.payments.http_client import PaymentUnavailable
    with PaymentProcess(tmp_path/'pg') as process:
        process.client.execute(cmd())
        with pytest.raises(PaymentUnavailable):process.client.execute(cmd('VOID'),fault='drop_reply')
        assert process.client.snapshot('world-a','PCT-ONE')['held_krw']==0
        assert process.client.operation(cmd('VOID'))['outcome']=='VOIDED'
        newer=dict(authorization_id='b',payment_revision=2,operation_key='approve-b')
        process.client.execute(cmd(**newer))
        capture=cmd('CAPTURE',**(newer|{'operation_key':'capture-b'}))
        with pytest.raises(PaymentUnavailable):process.client.execute(capture,fault='drop_reply')
        assert process.client.operation(capture)['outcome']=='CAPTURED'
        assert process.client.snapshot('world-a','PCT-ONE')['capture_count']==1


def test_restart_reuses_disk_without_recreating_approval(tmp_path):
    from demo.route.payments.runtime import PaymentProcess
    with PaymentProcess(tmp_path/'pg') as process:
        original=process.client.execute(cmd())
        pid=process.process.pid;process.process.kill();process.process.wait(3)
        until=time.monotonic()+8
        while time.monotonic()<until:
            if process.process.pid!=pid and process.process.poll() is None:
                try:
                    if process.client.operation(cmd())==original:break
                except OSError:pass
            time.sleep(.05)
        else:pytest.fail('supervised PG restart did not preserve original receipt')
        assert process.client.snapshot('world-a','PCT-ONE')['revision']==1
    with PaymentProcess(tmp_path/'pg') as restarted:
        assert restarted.client.operation(cmd())==original


def test_http_auth_bad_shape_and_wrong_binding_do_not_mutate(tmp_path):
    from demo.route.payments.runtime import PaymentProcess
    from demo.route.payments.domain import PaymentError
    with PaymentProcess(tmp_path/'pg') as process, httpx.Client(timeout=2,trust_env=False) as raw:
        url=process.client.url
        assert raw.post(url+'/v1/authorizations',json=cmd()).status_code==403
        headers={'Authorization':'Bearer '+process.token}
        assert raw.post(url+'/v1/authorizations',json=cmd(amount_krw=True),headers=headers).status_code==422
        assert raw.post(url+'/v1/authorizations',json=cmd(callback_url='https://bad.example'),headers=headers).status_code==422
        assert raw.post(url+'/v1/authorizations',content='{' ,headers=headers|{'Content-Type':'application/json'}).status_code==400
        assert raw.post(url+'/v1/authorizations',content='x'*40000,headers=headers|{'Content-Type':'application/json'}).status_code==413
        process.client.execute(cmd())
        with pytest.raises(PaymentError):process.client.execute(cmd('CAPTURE',world_id='other-world'))
        assert process.client.snapshot('world-a','PCT-ONE')['capture_count']==0


@pytest.mark.parametrize('url',['https://example.org','http://127.0.0.1.evil.test','http://localhost','http://127.0.0.1:22/path','file:///tmp/x'])
def test_client_cannot_be_pointed_at_external_or_arbitrary_path(url):
    from demo.route.payments.http_client import PaymentClient
    with pytest.raises(ValueError):PaymentClient(url,'x'*64)


def test_ready_file_reader_waits_for_complete_json(tmp_path):
    from demo.route.payments import runtime as rt
    from threading import Event, Thread
    import json
    ready=tmp_path/'ready.json'
    ready.write_text('')
    stop=Event()
    class Process:
        @staticmethod
        def poll(): return None
    def finish():
        time.sleep(.05)
        ready.write_text(json.dumps({'url':'http://127.0.0.1:43210'}))
    writer=Thread(target=finish);writer.start()
    try:
        assert rt._wait_ready_json(ready,Process(),stop,time.monotonic()+1)['url']=='http://127.0.0.1:43210'
    finally:
        writer.join()
