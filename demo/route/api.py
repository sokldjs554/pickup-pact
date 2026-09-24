from __future__ import annotations
import os
from pathlib import Path
import tempfile
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator
from .planner import catalogue
from .store import JourneyStore, Conflict
from .outcomes import run_experiment

HERE=Path(__file__).parent
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
    action: Literal['reserve','transfer','disrupt','delay','advance','start','ready','claim','cancel']
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
                 'start':set(),'ready':set(),'cancel':set()}[self.action]
        for name in {'quote_id','store_id','pickup_code'}:
            if getattr(self,name) is not None and name not in allowed: raise ValueError('명령에 맞지 않는 필드예요.')
        if self.minutes and 'minutes' not in allowed: raise ValueError('시간을 받지 않는 명령이에요.')
        if self.action in {'reserve','transfer'} and not self.quote_id: raise ValueError('quote_id is required')
        if self.action=='disrupt' and (not self.store_id or self.minutes<1): raise ValueError('매장과 지연 시간이 필요해요.')
        if self.action=='claim' and not self.pickup_code: raise ValueError('수령 코드가 필요해요.')
        return self

class ExperimentRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    seed: StrictInt=Field(default=7,ge=0,le=1000000)
    cases: StrictInt=Field(default=120,ge=6,le=200)

# Demo sessions use random capability IDs, never personal or live payment data.
store=JourneyStore(os.environ.get('ROUTE_DB',str(Path(tempfile.gettempdir())/'pickup-pact-route.sqlite')))

def create_router():
    def protect(request: Request,response: Response):
        response.headers['Cache-Control']='no-store'
        if request.method=='POST':
            if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':
                raise HTTPException(415,'application/json is required')
            origin=request.headers.get('origin')
            if origin and origin!=str(request.base_url).rstrip('/'):
                raise HTTPException(403,'cross-origin request rejected')
    router=APIRouter(dependencies=[Depends(protect)])
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
        if asset not in {'product.css','product.js','benefits.css','benefits-ui.js'}: raise HTTPException(404)
        return FileResponse(HERE/asset,headers={'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff'})
    return router
