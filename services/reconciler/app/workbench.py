"""Bounded sample recovery workbench. Run locally; no real financial execution."""
from pathlib import Path
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from .repair_review_store import ReviewConflict, ReviewStore

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')

class NewSession(Input):
    case: Literal['normal','delayed_cancel','conflicting_identity','terminal_conflict','missing_parent','partial_cancel','multiple_postings']

class Simulate(Input):
    action: Literal['redeliver','conflict','deliver_missing']

class PlanRequest(Input):
    version: StrictInt = Field(ge=1)

class ApprovedAction(Input):
    action_id: str = Field(pattern='^[a-f0-9]{64}$')
    target_event_id: str = Field(min_length=1,max_length=160)
    repair: Literal['REVERSE_SETTLEMENT','REVERSE_REWARD']
    amount: StrictInt = Field(gt=0,le=1_000_000_000_000)
    unit: Literal['KRW','PTS']

class ApproveRequest(PlanRequest):
    evidence_hash: str = Field(pattern='^[a-f0-9]{64}$')
    actions: list[ApprovedAction] = Field(min_length=1,max_length=2)


def create_router(database_path):
    store = ReviewStore(database_path)
    router = APIRouter()
    def perform(fn,*args):
        try: return fn(*args)
        except KeyError: return JSONResponse({'detail':'sample session or plan not found'},status_code=404)
        except ReviewConflict as exc: return JSONResponse({'detail':str(exc)},status_code=409)

    @router.get('/repair-lab',response_class=HTMLResponse,include_in_schema=False)
    def page():
        return HTMLResponse(Path(__file__).with_name('repair_workbench.html').read_text(encoding='utf-8'),headers={
            'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"})

    @router.post('/api/repair-lab/sessions',status_code=201)
    def new_session(body: NewSession): return perform(store.create,body.case)

    @router.get('/api/repair-lab/sessions/{session_id}')
    def session(session_id: UUID): return perform(store.get,str(session_id))

    @router.post('/api/repair-lab/sessions/{session_id}/simulate')
    def simulate(session_id: UUID,body: Simulate): return perform(store.simulate,str(session_id),body.action)

    @router.post('/api/repair-lab/sessions/{session_id}/plans',status_code=201)
    def preview(session_id: UUID,body: PlanRequest): return perform(store.preview,str(session_id),body.version)

    @router.post('/api/repair-lab/sessions/{session_id}/plans/{plan_id}/approve')
    def approve(session_id: UUID,plan_id: UUID,body: ApproveRequest):
        return perform(store.approve,str(session_id),str(plan_id),body.version,body.evidence_hash,
                       [a.model_dump() for a in body.actions])
    return router


def create_app(database_path):
    app=FastAPI(title='Pickup Pact 복구 작업대',version='0.1.0',
        description='고정된 합성 주문의 취소·정산 증거 검토. 실제 금융 실행 없음.')
    @app.middleware('http')
    async def local_security(request: Request,call_next):
        # JSON requests only; cross-origin HTML forms cannot cause a mutation.
        if request.method=='POST' and request.headers.get('content-type','').split(';')[0]!='application/json':
            return JSONResponse({'detail':'application/json is required'},status_code=415)
        origin=request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail':'cross-origin request rejected'},status_code=403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        return response
    app.include_router(create_router(database_path))
    @app.get('/',include_in_schema=False)
    def home(): return RedirectResponse('/repair-lab')
    @app.get('/health')
    def health(): return {'status':'ok','mode':'synthetic_only'}
    return app

if __name__=='__main__':
    import argparse
    import uvicorn
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default='.repair-review/review.sqlite')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    uvicorn.run(create_app(args.db),host='127.0.0.1',port=args.port)
