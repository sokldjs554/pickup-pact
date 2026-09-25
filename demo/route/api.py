from __future__ import annotations
import os
from pathlib import Path
import tempfile
from typing import Literal
from threading import BoundedSemaphore
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator
from .planner import catalogue
from .store import JourneyStore, Conflict
from .outcomes import run_experiment
from .runtime import route_lifespan

HERE=Path(__file__).parent
comparison_slots=BoundedSemaphore(2)  # Fixed 18 executions per request, bounded concurrency.
class Intent(BaseModel):
    model_config=ConfigDict(extra='forbid')
    destination: Literal['office','park']='office'
    deadline_minutes: StrictInt=Field(default=16,ge=3,le=90)
    drink: Literal['americano','latte']='latte'
    milk: Literal['regular','oat']='regular'
    decaf: StrictBool=False
    budget: StrictInt=Field(default=5500,ge=1000,le=30000)
    max_detour: StrictInt=Field(default=5,ge=0,le=20)
    priority: Literal['arrival','price','walk']='arrival'
    coupon_id: Literal['welcome500','wave1000','morning10']|None=None
    points: StrictInt=Field(default=0,ge=0,le=2000)
    @model_validator(mode='after')
    def meaningful_milk(self):
        if self.drink=='americano' and self.milk=='oat':
            raise ValueError('아메리카노에는 우유 옵션이 없어요.')
        return self

class Command(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action: Literal['reserve','transfer','disrupt','delay','advance','start','ready','claim','cancel','recover','reorder']
    expected_version: StrictInt=Field(ge=1)
    request_id: str=Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    quote_id: str|None=Field(default=None,max_length=64)
    store_id: Literal['corner','wave','oat','garden','express']|None=None
    minutes: StrictInt=Field(default=0,ge=0,le=30)
    pickup_code: str|None=Field(default=None,max_length=12)
    @model_validator(mode='after')
    def command_shape(self):
        allowed={'reserve':{'quote_id'},'transfer':{'quote_id'},'disrupt':{'store_id','minutes'},
                 'delay':{'minutes'},'advance':{'minutes'},'claim':{'pickup_code'},
                 'start':set(),'ready':set(),'cancel':set(),'recover':set(),'reorder':{'quote_id'}}[self.action]
        for name in {'quote_id','store_id','pickup_code'}:
            if getattr(self,name) is not None and name not in allowed: raise ValueError('명령에 맞지 않는 필드예요.')
        if self.minutes and 'minutes' not in allowed: raise ValueError('시간을 받지 않는 명령이에요.')
        if self.action in {'reserve','transfer','reorder'} and not self.quote_id: raise ValueError('quote_id is required')
        if self.action=='disrupt' and (not self.store_id or self.minutes<1): raise ValueError('매장과 지연 시간이 필요해요.')
        if self.action=='claim' and not self.pickup_code: raise ValueError('수령 코드가 필요해요.')
        return self

class TransferControl(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action: Literal['fault','occupy','clear']
    expected_version: StrictInt=Field(ge=1)
    request_id: str=Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    fault: Literal['none','target_reject','after_target_hold','after_source_release','after_target_activation']|None=None
    store_id: Literal['corner','wave','oat','garden','express']|None=None
    @model_validator(mode='after')
    def shape(self):
        if self.action=='fault' and (self.fault is None or self.store_id is not None):
            raise ValueError('문제 상황만 선택해 주세요.')
        if self.action in {'occupy','clear'} and (self.store_id is None or self.fault is not None):
            raise ValueError('다른 손님이 이용할 매장을 선택해 주세요.')
        return self

class TransferComparisonRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    intent: Intent=Field(default_factory=Intent)

class ExperimentRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    seed: StrictInt=Field(default=7,ge=0,le=1000000)
    cases: StrictInt=Field(default=120,ge=6,le=200)

# Demo sessions use random capability IDs, never personal or live payment data.
store=JourneyStore(os.environ.get('ROUTE_DB',str(Path(tempfile.gettempdir())/'pickup-pact-route.sqlite')))

def create_router():
    async def protect(request: Request,response: Response):
        response.headers['Cache-Control']='no-store'
        if request.method=='POST':
            if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':
                raise HTTPException(415,'application/json is required')
            origin=request.headers.get('origin')
            if origin and origin!=str(request.base_url).rstrip('/'):
                raise HTTPException(403,'cross-origin request rejected')
            # JSON may decode a lone UTF-16 surrogate. It cannot be encoded as
            # UTF-8 for command fingerprints or validation-error responses.
            # Reject it before domain mutation, including in unknown/nested keys.
            pending = [await request.json()] if await request.body() else []
            while pending:
                value = pending.pop()
                if isinstance(value, str):
                    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                        raise HTTPException(400, 'invalid Unicode in JSON')
                elif isinstance(value, dict):
                    pending.extend(value.keys())
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
    router=APIRouter(lifespan=route_lifespan, dependencies=[Depends(protect)], responses={
        400: {'description': 'Invalid Unicode in JSON rejected before mutation'},
    })
    @router.get('/api/route/runtime')
    def runtime_status() -> dict:
        return dict(mode='synthetic', merchant_transport='http' if getattr(store.fleet,'handles_response_loss',False) else 'local',
                    automatic_recovery=store.automatic_recovery_enabled,
                    predecision_timeout_seconds=45, retry_limit=8,
                    scope='single_host_independent_process_and_store_databases')
    @router.get('/api/route/catalog')
    def catalog_api()->dict: return catalogue()
    @router.post('/api/route/journeys',status_code=201)
    def create_journey(body:Intent)->dict: return store.create(body.model_dump())
    def invoke(fn,*args):
        try: return fn(*args)
        except KeyError as exc: raise HTTPException(404,'이 체험을 찾지 못했어요. 새 일정으로 시작해 주세요.') from exc
        except Conflict as exc: raise HTTPException(409,dict(code=exc.code,message=str(exc))) from exc
    @router.get('/api/route/journeys/{journey_id}')
    def journey(journey_id:UUID)->dict: return invoke(store.get,journey_id.hex)
    @router.post('/api/route/journeys/{journey_id}/commands',responses={409:{'description':'Stale state, unsafe transfer or invalid lifecycle'}})
    def commands(journey_id:UUID,body:Command)->dict:
        return invoke(store.command,journey_id.hex,body.model_dump())
    @router.post('/api/route/journeys/{journey_id}/transfer-controls', responses={409:{'description':'Pending operation or changed state'}})
    def transfer_controls(journey_id:UUID,body:TransferControl)->dict:
        return invoke(store.transfer_control,journey_id.hex,body.model_dump())
    @router.post('/api/route/transfer-comparison', responses={429:{'description':'Two comparisons are already running in this process'}})
    def transfer_comparison(body:TransferComparisonRequest)->dict:
        from .transfer_comparison import run_transfer_comparison
        if not comparison_slots.acquire(blocking=False):
            raise HTTPException(429, '다른 비교가 실행 중이에요. 잠시 후 다시 눌러 주세요.', headers={'Retry-After':'2'})
        try:
            return run_transfer_comparison(body.intent.model_dump())
        finally:
            comparison_slots.release()
    @router.get('/api/route/journeys/{journey_id}/comparison')
    def comparison(journey_id:UUID)->dict:
        return invoke(store.get,journey_id.hex)['comparison']
    @router.post('/api/route/experiments')
    def experiment(body:ExperimentRequest)->dict:
        return run_experiment(body.seed,body.cases)
    @router.get('/api/route/journeys/{journey_id}/receipt')
    def receipt(journey_id:UUID)->dict:
        s=invoke(store.get,journey_id.hex)
        return dict(mode='synthetic',order=s['order'],receipt=s['receipt'],events=s['events'])
    @router.get('/',include_in_schema=False)
    @router.get('/go',include_in_schema=False)
    def product(): return FileResponse(HERE/'index.html',media_type='text/html',headers={'Cache-Control':'no-store'})
    @router.get('/route-assets/{asset}',include_in_schema=False)
    def asset_file(asset:str):
        if asset not in {'product.css','product.js','benefits.css','benefits-ui.js','handoff.css','handoff-ui.js','recovery-ui.js'}: raise HTTPException(404)
        return FileResponse(HERE/asset,headers={'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff'})
    return router
