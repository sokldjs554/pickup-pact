"""Configured same-storage replicas; reads find a peer, uncertain writes stay pending.

All origins remain loopback-only in this development mode. This is not a remote
TLS deployment or proof that two processes occupy independent hosts.
"""
from threading import local
from ..payments.http_client import PaymentClient, origin
from ..merchant_http import HttpMerchantFleet


def replica_origins(urls):
    if not isinstance(urls,(tuple,list)) or not 1<=len(urls)<=3:
        raise ValueError('one to three explicit replica origins required')
    if any(not isinstance(url,str) for url in urls):
        raise ValueError('replica origins must be strings')
    values=tuple(origin(url) for url in urls)
    if len(set(values))!=len(values):
        raise ValueError('duplicate replica origin')
    return values


class _ReplicaReads:
    def __init__(self,clients):
        self._clients=tuple(clients)
        self._selected=local()

    def _read(self,method,*args,**kwargs):
        for index,client in enumerate(self._clients):
            try:
                result=getattr(client,method)(*args,**kwargs)
            except OSError:
                continue
            self._selected.index=index
            return result
        self._selected.index=None
        raise OSError('no configured replica returned a confirmed result')

    def _write(self,method,*args,**kwargs):
        index=getattr(self._selected,'index',None)
        if index is None:
            self.health()
            index=self._selected.index
        try:
            return getattr(self._clients[index],method)(*args,**kwargs)
        except OSError:
            # Do not hide an ambiguous write by manufacturing a fresh request.
            # The durable coordinator will query the ORIGINAL operation key.
            self._selected.index=None
            raise


class ReplicaPaymentClient(_ReplicaReads):
    transport='http'

    def __init__(self,urls,token,timeout=2):
        super().__init__([PaymentClient(url,token,timeout=timeout) for url in replica_origins(urls)])

    def health(self):
        return self._read('health')

    def operation(self,command):
        return self._read('operation',command)

    def snapshot(self,world,order_id):
        return self._read('snapshot',world,order_id)

    def execute(self,command,*,fault='none'):
        return self._write('execute',command,fault=fault)


class ReplicaMerchantFleet(_ReplicaReads):
    handles_response_loss=True

    def __init__(self,urls,token,timeout=2):
        super().__init__([HttpMerchantFleet(url,token,timeout=timeout) for url in replica_origins(urls)])

    def health(self):
        return self._read('_request','/health')

    def _request(self,path):
        if path!='/health':
            raise ValueError('only the explicit health query is exposed')
        return self.health()

    def snapshot(self,world):
        return self._read('snapshot',world)

    def evidence(self,world,order_id):
        return self._read('evidence',world,order_id)

    def execute(self,shop,**command):
        self.health()
        return self._write('execute',shop,**command)

    def set_accepting(self,shop,world,accepting):
        self.health()
        return self._write('set_accepting',shop,world,accepting)
