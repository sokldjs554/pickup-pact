"""Disposable single-host process topology. Never a multi-host HA certificate."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.request import Request,urlopen
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]


def request(base,path,body=None):
    data=None if body is None else json.dumps(body).encode()
    req=Request(base+path,data=data,headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=20) as response:return json.load(response)


class DevelopmentStack:
    def __init__(self,root):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=False)
        self.processes={};self.logs={};self.created=[];self.launches={}
        self.run_id=uuid4().hex
        self.secrets=[secrets.token_hex(32) for _ in range(3)]
        self.base_env={**os.environ,'PYTHONPATH':str(ROOT)+':'+str(ROOT/'services/reconciler'),
            'PICKUP_ROUTE_BACKEND':'postgresql_development',
            'ROUTE_MERCHANT_TOKEN':self.secrets[0],'ROUTE_PAYMENT_TOKEN':self.secrets[1],
            'ROUTE_PAYMENT_NOTIFY_SECRET':self.secrets[2]}
        self.urls=[];self.apps=[]

    def start(self,name,args,extra=None,ready=True):
        if name in self.processes and self.processes[name].poll() is None:
            raise ValueError('role is already running')
        self.launches[name]=(list(args),dict(extra or {}),ready)
        log=self.root/(name+'.log');handle=log.open('a');self.logs[name]=handle
        marker=self.root/(name+'-'+uuid4().hex+'.json')
        command=[sys.executable,*args]
        if ready:command+=['--ready-file',str(marker)]
        process=subprocess.Popen(command,cwd=ROOT,env={**self.base_env,**(extra or {})},stdout=handle,stderr=handle)
        self.processes[name]=process
        if not ready:return process
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if process.poll() is not None:raise AssertionError(name+' stopped during startup; see retained log')
            if marker.exists():
                try:
                    data=json.loads(marker.read_text())
                    if data['pid']!=process.pid:raise AssertionError('ready marker belongs to another process')
                    return data['url']
                except (json.JSONDecodeError,KeyError):pass
            time.sleep(.05)
        raise AssertionError(name+' readiness deadline exceeded')

    def start_role(self,name,args):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        return self.start(name,[*args,'--port',str(port)])

    def restart_role(self,name):
        if name not in self.launches:raise ValueError('role was not created by this stack')
        args,extra,ready=self.launches[name]
        return self.start(name,args,extra,ready=ready)

    def stop(self,name):
        process=self.processes[name]
        if process.poll() is None:
            process.terminate()
            try:process.wait(25)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(5)
        self.logs[name].close()

    def start_app(self,index):
        if index==len(self.urls):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            self.urls.append('http://127.0.0.1:'+str(port));self.apps.append('app-'+str(index))
        else:port=int(self.urls[index].rsplit(':',1)[1])
        name=self.apps[index]
        self.start(name,['-m','uvicorn','demo.main:app','--host','127.0.0.1','--port',str(port)],
                   {'PICKUP_NODE_ID':name,'ROUTE_DB':str(self.root/(name+'-forbidden-order.sqlite')),
                    'REPAIR_REVIEW_DB':str(self.root/(name+'-legacy-repair.sqlite'))},ready=False)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if self.processes[name].poll() is not None:raise AssertionError(name+' stopped during startup')
            try:
                runtime=request(self.urls[index],'/api/route/runtime')
                if runtime.get('node_id')==name and runtime.get('storage_backend')=='postgresql':
                    assert not (self.root/(name+'-forbidden-order.sqlite')).exists()
                    return
            except (OSError,ValueError):pass
            time.sleep(.1)
        raise AssertionError(name+' runtime is not native PostgreSQL')

    def start_worker(self,index):
        name='worker-'+str(index)
        self.start(name,['-m','demo.route.ha.worker'],{'PICKUP_NODE_ID':name},ready=False)

    def setup(self):
        if os.environ.get('PICKUP_HA_TEST')!='1':raise ValueError('explicit development opt-in required')
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict,make_conninfo
        dsn=os.environ.get('PICKUP_PG_TEST_DSN','')
        info=conninfo_to_dict(dsn)
        if info.get('host')!='127.0.0.1' or info.get('dbname')!='pickup_ha_test':
            raise ValueError('only the explicit loopback CI development database is accepted')
        self.admin=dsn
        with psycopg.connect(dsn,autocommit=True) as db:
            for role in ['order','merchant','payment']:
                name='pickup_int_'+self.run_id+'_'+role
                db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
                self.created.append(name)
                db.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(sql.Identifier(name),sql.Literal(self.run_id)))
                self.base_env['PICKUP_'+role.upper()+'_DSN']=make_conninfo(dsn,dbname=name)
                self.base_env['PICKUP_'+role.upper()+'_INIT_SCHEMA']='1'
        merchants=[self.start_role('merchant-'+str(i),['-m','demo.route.ha.merchant_service']) for i in range(2)]
        callbacks=[self.start_role('notification-'+str(i),['-m','demo.route.ha.notification_service']) for i in range(2)]
        payments=[self.start_role('payment-'+str(i),['-m','demo.route.ha.payment_service','--callback-url',callbacks[i]]) for i in range(2)]
        self.base_env.update(ROUTE_MERCHANT_URL=merchants[0],ROUTE_PAYMENT_URL=payments[0],
            ROUTE_MERCHANT_FAILOVER_URLS=json.dumps(merchants[1:]),ROUTE_PAYMENT_FAILOVER_URLS=json.dumps(payments[1:]))
        self.start_app(0);self.start_app(1)
        self.start_worker(0);self.start_worker(1)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if request(self.urls[0],'/api/route/runtime')['automatic_recovery']:return
            time.sleep(.1)
        raise AssertionError('independent worker heartbeat was not observed')

    def close(self):
        errors=[]
        for name in list(reversed(self.processes)):
            try:self.stop(name)
            except Exception as exc:errors.append(name+':'+type(exc).__name__)
        for handle in self.logs.values():
            if not handle.closed:handle.close()
        for path in self.root.glob('*.log'):
            text=path.read_text(errors='replace')
            for secret in self.secrets:text=text.replace(secret,'[redacted]')
            path.write_text(text)
        if self.created:
            import psycopg
            from psycopg import sql
            with psycopg.connect(self.admin,autocommit=True) as db:
                for name in reversed(self.created):
                    marker=db.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s",(name,)).fetchone()
                    if marker!=(self.run_id,):
                        errors.append('database ownership mismatch');continue
                    try:db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
                    except psycopg.Error:errors.append('database cleanup failed')
        if errors:raise RuntimeError('; '.join(errors))


@contextmanager
def stack(root):
    topology=DevelopmentStack(root)
    try:
        topology.setup();yield topology
    finally:topology.close()
