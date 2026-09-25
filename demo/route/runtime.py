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
            while time.monotonic()<deadline and not ready.exists():
                if self.process.poll() is not None:raise RuntimeError('merchant process exited during startup')
                if self._stop.wait(.03):raise RuntimeError('merchant startup cancelled')
            if not ready.exists():raise RuntimeError('merchant process readiness timed out')
            self.url=json.loads(ready.read_text())['url']
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
    from . import api
    store=api.store
    previous_fleet=store.fleet
    # A bounded demo role is reached over real HTTP. The older JVM services
    # remain a separate topology, and the comparison harness stays local.
    with MerchantProcess(store.path+'.merchants') as fleet:
        store.fleet=fleet
        store.automatic_recovery_enabled=True
        try:
            with RecoveryWorker(store) as worker:
                app.state.route_recovery_worker=worker
                yield
        finally:
            store.automatic_recovery_enabled=False
            store.fleet=previous_fleet
