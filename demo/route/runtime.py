"""One public app process plus a supervised synthetic merchant HTTP process.

This is process/DB isolation on a single host, not multi-host high availability.
The internal bearer token is generated at startup and never sent to a browser.
"""
from __future__ import annotations
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from uuid import uuid4
from .merchant_http import HttpMerchantFleet
from .payments.runtime import _wait_ready_json
from .recovery_worker import RecoveryWorker

log=logging.getLogger(__name__)


class MerchantProcess:
    def __init__(self, directory: str):
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.token=secrets.token_hex(32)
        self.port=0
        self.process=None
        self._stop=threading.Event()
        self._thread=None
        self._log=None
        self.url=None

    def _launch(self):
        ready=self.directory/('ready-'+uuid4().hex+'.json')
        args=[sys.executable,'-m','demo.route.merchant_http','--directory',str(self.directory),
              '--port',str(self.port),'--ready-file',str(ready)]
        self.process=subprocess.Popen(args,env={**os.environ,'ROUTE_MERCHANT_TOKEN':self.token},
                                      stdout=self._log,stderr=self._log)
        deadline=time.monotonic()+8
        try:
            data=_wait_ready_json(ready,self.process,self._stop,deadline)
            self.url=data['url']
            self.port=int(self.url.rsplit(':',1)[1])
        except Exception:
            if self.process.poll() is None:self.process.terminate();self.process.wait(5)
            raise
        finally:
            ready.unlink(missing_ok=True)

    def _supervise(self):
        while not self._stop.wait(.5):
            if self.process.poll() is not None:
                try:
                    log.warning('restarting synthetic merchant HTTP process')
                    self._launch()
                except Exception:
                    log.exception('merchant restart failed; operations remain pending')
                    self._stop.wait(1)

    def __enter__(self):
        self._log=(self.directory/'runtime.log').open('a')
        try:self._launch()
        except Exception:self._log.close();raise
        self._thread=threading.Thread(target=self._supervise,name='merchant-supervisor',daemon=True)
        self._thread.start()
        return HttpMerchantFleet(self.url,self.token)

    def __exit__(self,*args):
        self._stop.set()
        if self._thread:self._thread.join(10)
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(5)
        if self._log:self._log.close()


@asynccontextmanager
async def route_lifespan(app):
    if os.environ.get('PICKUP_ROUTE_BACKEND') in {'postgresql_development','ha_postgres_v1'}:
        from .ha.runtime import native_lifespan
        async with native_lifespan(app):
            yield
        return
    from contextlib import ExitStack
    from . import api
    from .payments.runtime import PaymentProcess
    from .payments.notifications import NotificationServer
    store=api.store
    previous_fleet,previous_pg,previous_enabled=store.fleet,store.payment_gateway,store.payment_enabled
    mode=os.environ.get('ROUTE_PAYMENT_MODE','simulator_http_v1')
    if mode not in {'simulator_http_v1','legacy'}:raise ValueError('unsupported synthetic payment mode')
    try:
        with ExitStack() as stack:
            store.fleet=stack.enter_context(MerchantProcess(store.path+'.merchants'))
            if mode=='simulator_http_v1':
                receiver=stack.enter_context(NotificationServer(store.payment_inbox))
                process=stack.enter_context(PaymentProcess(store.path+'.payments',receiver.url,store.payment_inbox.secret))
                store.payment_gateway=process.client
                store.payment_enabled=True
            store.automatic_recovery_enabled=True
            worker=stack.enter_context(RecoveryWorker(store))
            app.state.route_recovery_worker=worker
            yield
    finally:
        store.automatic_recovery_enabled=False
        store.fleet,store.payment_gateway,store.payment_enabled=previous_fleet,previous_pg,previous_enabled
