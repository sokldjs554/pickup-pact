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
from .response_models import JourneyView, RuntimeView, ReceiptView, ComparisonView, ReconciliationView

HERE=Path(__file__).parent
comparison_slots=BoundedSemaphore(2)  # Fixed 24 executions per request, bounded concurrency.
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

class JourneyInput(Intent):
    payment_card: Literal['demo-approved','demo-declined']='demo-approved'
    payment_scenario: Literal['none','authorize_reply_lost','capture_reply_lost','void_reply_lost',
                              'notification_duplicate','notification_late']='none'

class PaymentControl(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action: Literal['fault','card']
    expected_version: StrictInt=Field(ge=1)
    request_id: str=Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    fault: Literal['none','authorize_reply_lost','capture_reply_lost','void_reply_lost',
                   'notification_duplicate','notification_late']|None=None
    card_token: Literal['demo-approved','demo-declined']|None=None
    @model_validator(mode='after')
    def shape(self):
        if self.action=='fault' and (self.fault is None or self.card_token is not None):raise ValueError('문제 상황만 선택해 주세요.')
        if self.action=='card' and (self.card_token is None or self.fault is not None):raise ValueError('가상 카드만 선택해 주세요.')
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
_backend=os.environ.get('PICKUP_ROUTE_BACKEND','sqlite')
if _backend not in {'sqlite','postgresql_development'}:
    raise ValueError('unsupported route storage backend')
store=(None if _backend=='postgresql_development' else
       JourneyStore(os.environ.get('ROUTE_DB',str(Path(tempfile.gettempdir())/'pickup-pact-route.sqlite'))))

def create_router(*, store_provider=None, lifespan=route_lifespan):
    def selected_store():
        selected=store_provider() if store_provider is not None else store
        if selected is None:
            raise HTTPException(503,'주문 저장소를 연결하고 있어요.')
        return selected
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
    router=APIRouter(lifespan=lifespan, dependencies=[Depends(protect)], responses={
        400: {'description': 'Invalid Unicode in JSON rejected before mutation'},
    })
    @router.get('/api/route/runtime',response_model=RuntimeView,response_model_exclude_unset=True)
    def runtime_status() -> dict:
        payment_ready=False
        if selected_store().payment_gateway:
            try:payment_ready=selected_store().payment_gateway.health().get('storage_ready') is True
            except OSError:pass
        return dict(mode='synthetic', merchant_transport='http' if getattr(selected_store().fleet,'handles_response_loss',False) else 'local',
                    automatic_recovery=selected_store().automatic_recovery_enabled,
                    payment_mode='simulator_http_v1' if selected_store().payment_enabled else 'legacy_internal',
                    payment_transport=getattr(selected_store().payment_gateway,'transport','not_connected'),
                    payment_connection='available' if payment_ready else 'unavailable',
                    payment_storage_ready=payment_ready,
                    predecision_timeout_seconds=45, retry_limit=8,
                    scope=getattr(selected_store(),'evidence_scope','single_host_independent_process_and_store_databases'),
                    storage_backend=getattr(selected_store(),'backend','sqlite'),
                    node_id=getattr(selected_store(),'node_id','single-host'))
    @router.get('/ready',include_in_schema=False)
    def readiness():
        current=selected_store()
        try:
            if getattr(current,'backend','sqlite')=='postgresql':
                if not current.repository.storage_ready():raise OSError()
                merchant=current.fleet._request('/health')
                payment=current.payment_gateway.health()
                if not merchant.get('storage_ready') or not payment.get('storage_ready') or not current.automatic_recovery_enabled:
                    raise OSError()
            return dict(ready=True,storage_backend=getattr(current,'backend','sqlite'))
        except OSError:
            raise HTTPException(503,'주문·매장·결제·복구 작업자의 준비 상태를 확인하지 못했어요.') from None
    @router.get('/api/route/catalog')
    def catalog_api()->dict: return catalogue()
    @router.post('/api/route/journeys',status_code=201,response_model=JourneyView,response_model_exclude_unset=True)
    def create_journey(body:JourneyInput)->dict:
        return invoke(selected_store().create,body.model_dump(exclude={'payment_card','payment_scenario'}),
                            card_token=body.payment_card,payment_fault=body.payment_scenario)
    def invoke(fn,*args,**kwargs):
        try: return fn(*args,**kwargs)
        except KeyError as exc: raise HTTPException(404,'이 체험을 찾지 못했어요. 새 일정으로 시작해 주세요.') from exc
        except Conflict as exc: raise HTTPException(409,dict(code=exc.code,message=str(exc))) from exc
        except OSError: raise HTTPException(503, '저장된 처리 결과를 확인하지 못했어요. 같은 요청으로 다시 확인해 주세요.') from None
    @router.get('/api/route/journeys/{journey_id}',response_model=JourneyView,response_model_exclude_unset=True)
    def journey(journey_id:UUID)->dict: return invoke(selected_store().get,journey_id.hex)
    @router.post('/api/route/journeys/{journey_id}/commands',response_model=JourneyView,response_model_exclude_unset=True,responses={409:{'description':'Stale state, unsafe transfer or invalid lifecycle'}})
    def commands(journey_id:UUID,body:Command)->dict:
        return invoke(selected_store().command,journey_id.hex,body.model_dump())
    @router.post('/api/route/journeys/{journey_id}/transfer-controls', response_model=JourneyView,response_model_exclude_unset=True,responses={409:{'description':'Pending operation or changed state'}})
    def transfer_controls(journey_id:UUID,body:TransferControl)->dict:
        return invoke(selected_store().transfer_control,journey_id.hex,body.model_dump())
    @router.post('/api/route/journeys/{journey_id}/payment-controls',response_model=JourneyView,response_model_exclude_unset=True,
                 responses={409:{'description':'Fixed legacy mode, pending work or stale state'}})
    def payment_controls(journey_id:UUID,body:PaymentControl)->dict:
        return invoke(selected_store().payment_control,journey_id.hex,body.model_dump(exclude_none=True))
    @router.post('/api/route/transfer-comparison', response_model=ComparisonView,response_model_exclude_unset=True,responses={429:{'description':'Two comparisons are already running in this process'}})
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
        return invoke(selected_store().get,journey_id.hex)['comparison']
    @router.post('/api/route/experiments')
    def experiment(body:ExperimentRequest)->dict:
        return run_experiment(body.seed,body.cases)
    @router.get('/api/route/journeys/{journey_id}/reconciliation',response_model=ReconciliationView,response_model_exclude_unset=True)
    def reconciliation(journey_id:UUID)->dict:
        from .reconciliation import reconcile
        return invoke(reconcile,selected_store(),journey_id.hex)
    @router.get('/api/route/journeys/{journey_id}/receipt',response_model=ReceiptView,response_model_exclude_unset=True)
    def receipt(journey_id:UUID)->dict:
        s=invoke(selected_store().get,journey_id.hex)
        return dict(mode='synthetic',order=s['order'],receipt=s['receipt'],events=s['events'])
    @router.get('/',include_in_schema=False)
    @router.get('/go',include_in_schema=False)
    def product(): return FileResponse(HERE/'index.html',media_type='text/html',headers={'Cache-Control':'no-store'})
    @router.get('/route-assets/{asset}',include_in_schema=False)
    def asset_file(asset:str):
        if asset not in {'product.css','product.js','benefits.css','benefits-ui.js','handoff.css','handoff-ui.js','recovery-ui.js','agreement-ui.js','selection-state.js','guide-flow.js','guide.css','payment-ui.js','payment.css'}: raise HTTPException(404)
        return FileResponse(HERE/asset,headers={'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff'})
    return router
