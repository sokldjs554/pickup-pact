"""Replaceable workers over shared queue, with durable lease ownership per step."""
import logging
import os
import signal
import threading
import time
from ..recovery_worker import RecoveryWorker

log=logging.getLogger(__name__)


class PostgresRecoveryWorker(RecoveryWorker):
    def __init__(self,store,interval=0.5):
        if not 0<interval<=5:raise ValueError('bounded heartbeat interval required')
        self.store,self.interval=store,interval
        self._stop=threading.Event()
        self._thread=None

    def run_once(self,now=None,limit=16):
        if now is not None:raise ValueError('native worker uses the database clock')
        self.store.repository.heartbeat(self.store.node_id)
        self.store.payment_inbox.wake_operations(time.time(),limit)
        rows=self.store.repository.due(limit)
        result=dict(attempted=0,completed=0,pending=0,errors=0)
        for row in rows:
            if self._stop.is_set():break
            try:
                view=self.store.operations.resume(row['sid'],row['id'],automatic=True)
                result['attempted']+=1
                result['pending' if view['handoff_pending'] else 'completed']+=1
            except (OSError,ValueError,KeyError):
                result['errors']+=1
                log.warning('native recovery remains pending')
        return result

    def __exit__(self,*args):
        try:super().__exit__(*args)
        finally:
            try:self.store.repository.heartbeat(self.store.node_id,remove=True)
            except OSError:pass


def main():
    from .settings import RoleSettings
    settings=RoleSettings.from_env(os.environ)
    store=settings.store()
    stop=threading.Event()
    for sig in [signal.SIGTERM,signal.SIGINT]:signal.signal(sig,lambda *_:stop.set())
    try:
        with PostgresRecoveryWorker(store):stop.wait()
    finally:store.close()


if __name__=='__main__':main()
