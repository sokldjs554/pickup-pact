"""One supervised loopback payment process; durable files, not host-loss HA."""
from __future__ import annotations
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from uuid import uuid4
from .http_client import PaymentClient, origin


class PaymentProcess:
    def __init__(self,directory: str|Path,callback_url: str|None=None,notify_secret: str|None=None):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.callback_url=origin(callback_url,callback=True) if callback_url else None
        self.notify_secret=notify_secret or secrets.token_hex(32)
        self.token=secrets.token_hex(32)
        self.port=0;self.process=None;self.client=None;self._thread=None;self._log=None
        self._stop=threading.Event()

    def _launch(self):
        ready=self.directory/('ready-'+uuid4().hex+'.json')
        args=[sys.executable,'-m','demo.route.payments.http_server','--directory',str(self.directory),
              '--port',str(self.port),'--ready-file',str(ready)]
        if self.callback_url:args+=['--callback-url',self.callback_url]
        self.process=subprocess.Popen(args,env={**os.environ,'ROUTE_PAYMENT_TOKEN':self.token,
                                       'ROUTE_PAYMENT_NOTIFY_SECRET':self.notify_secret},stdout=self._log,stderr=self._log)
        try:
            deadline=time.monotonic()+8
            while time.monotonic()<deadline and not ready.exists():
                if self.process.poll() is not None:raise RuntimeError('payment process startup failed')
                if self._stop.wait(.025):raise RuntimeError('payment startup cancelled')
            if not ready.exists():raise RuntimeError('payment readiness timed out')
            url=json.loads(ready.read_text())['url'];self.port=int(url.rsplit(':',1)[1])
            self.client=PaymentClient(url,self.token)
            self.client.health()
        except BaseException:
            if self.process.poll() is None:self.process.terminate();self.process.wait(5)
            raise
        finally:ready.unlink(missing_ok=True)

    def _supervise(self):
        while not self._stop.wait(.25):
            if self.process.poll() is not None:
                try:self._launch()
                except Exception:self._stop.wait(1)

    def __enter__(self):
        self._log=(self.directory/'runtime.log').open('a')
        try:self._launch()
        except BaseException:self._log.close();raise
        self._thread=threading.Thread(target=self._supervise,daemon=True,name='payment-supervisor')
        self._thread.start();return self

    def __exit__(self,*_):
        self._stop.set()
        if self._thread:self._thread.join(10)
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(5)
        if self._log:self._log.close()
