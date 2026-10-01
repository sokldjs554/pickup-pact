"""Bounded authenticated loopback transport; uncertainty never means decline."""
from __future__ import annotations
import json
from urllib.parse import urlsplit, quote
import httpx
from .domain import PaymentError, key, validate


class PaymentUnavailable(OSError):
    pass


def origin(url: str, *, callback: bool=False) -> str:
    parts=urlsplit(url)
    if (parts.scheme!='http' or parts.hostname!='127.0.0.1' or not parts.port
            or parts.username or parts.password or parts.query or parts.fragment
            or (parts.path not in {'','/'} if not callback else parts.path!='/internal/payments/events')):
        raise ValueError('configured loopback payment endpoint required')
    return url.rstrip('/')


class PaymentClient:
    transport='http'

    def __init__(self,url: str,token: str,timeout: float=.8):
        self.url=origin(url)
        if len(token)<24 or not 0<timeout<=5:raise ValueError('invalid payment transport config')
        self.token,self.timeout=token,timeout

    def _request(self,path: str,body: dict|None=None,*,optional=False):
        try:
            with httpx.Client(timeout=self.timeout,trust_env=False,follow_redirects=False) as client:
                with client.stream('GET' if body is None else 'POST',self.url+path,json=body,
                                   headers={'Authorization':'Bearer '+self.token}) as response:
                    raw=bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw)>1048576:raise ValueError('response too large')
                    if optional and response.status_code==404:return None
                    data=json.loads(raw)
                    if not isinstance(data,dict):raise ValueError('object required')
                    if response.status_code in {409,422}:
                        code=data.get('code')
                        if not isinstance(code,str) or len(code)>100:raise ValueError('invalid code')
                        raise PaymentError(code,response.status_code)
                    response.raise_for_status()
                    return data
        except (httpx.HTTPError,ValueError,UnicodeError) as exc:
            if isinstance(exc,PaymentError):raise
            raise PaymentUnavailable('payment response not confirmed') from exc

    @staticmethod
    def _bound(result: dict, c: dict) -> dict:
        if any(result.get(field)!=value for field,value in c.items() if field!='card_token'):
            raise PaymentUnavailable('payment response not bound to command')
        if type(result.get('ok')) is not bool or result.get('provider')!='DEMO_PLATFORM' or result.get('mode')!='synthetic':
            raise PaymentUnavailable('invalid payment response')
        if result['ok']:
            expected={'AUTHORIZE':'AUTHORIZED','CAPTURE':'CAPTURED','VOID':'VOIDED'}[c['action']]
            if result.get('outcome')!=expected or not isinstance(result.get('transaction_id'),str) or type(result.get('revision')) is not int:
                raise PaymentUnavailable('invalid confirmed payment evidence')
        elif result.get('outcome')!='DECLINED' or result.get('transaction_id') is not None or result.get('code') not in {'DECLINED','LIMIT_EXCEEDED'}:
            raise PaymentUnavailable('invalid decline evidence')
        return result

    def execute(self,command: dict,*,fault: str='none') -> dict:
        c=validate(command)
        if fault not in {'none','drop_reply','duplicate_notification','late_notification'}:
            raise ValueError('unsupported payment fault')
        path='/v1/authorizations' if c['action']=='AUTHORIZE' else '/v1/authorizations/'+quote(c['authorization_id'],safe='')+'/'+c['action'].lower()
        return self._bound(self._request(path,c|{'fault':fault}),c)

    def operation(self,command: dict) -> dict|None:
        c=validate(command)
        result=self._request('/v1/operations/'+quote(c['operation_key'],safe=''),optional=True)
        return None if result is None else self._bound(result,c)

    def snapshot(self,world: str,order_id: str) -> dict:
        key(world);key(order_id)
        data=self._request('/v1/orders/'+quote(world,safe='')+'/'+quote(order_id,safe=''))
        if data.get('world_id')!=world or data.get('order_id')!=order_id or data.get('provider')!='DEMO_PLATFORM':
            raise PaymentUnavailable('payment snapshot binding mismatch')
        for field in ('revision','held_krw','captured_krw','capture_count'):
            if type(data.get(field)) is not int or data[field]<0:
                raise PaymentUnavailable('invalid payment totals')
        for field in ('transactions','authorizations'):
            if not isinstance(data.get(field),list):raise PaymentUnavailable('missing payment records')
            for row in data[field]:
                if (not isinstance(row,dict) or row.get('world_id')!=world or row.get('order_id')!=order_id
                        or type(row.get('amount_krw')) is not int or row['amount_krw']<=0):
                    raise PaymentUnavailable('invalid payment record')
        if (sum(t['amount_krw'] for t in data['transactions'] if t.get('kind')=='CAPTURE')!=data['captured_krw']
                or sum(t.get('kind')=='CAPTURE' for t in data['transactions'])!=data['capture_count']
                or sum(a['amount_krw'] for a in data['authorizations'] if a.get('status')=='AUTHORIZED')!=data['held_krw']):
            raise PaymentUnavailable('payment summary does not match records')
        return data

    def health(self):
        data=self._request('/health')
        if data.get('service')!='pickup-payment' or data.get('storage_ready') is not True:
            raise PaymentUnavailable('payment storage not ready')
        return data
