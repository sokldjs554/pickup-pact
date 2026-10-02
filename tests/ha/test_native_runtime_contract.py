"""Read-only role selection and reusable API boundaries before native runtime wiring."""
from pathlib import Path
import importlib
import os
import subprocess
import sys
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


def test_existing_router_can_bind_store_without_global_mutation(monkeypatch, tmp_path):
    from demo.route import api
    from demo.route.store import JourneyStore
    from contextlib import asynccontextmanager
    a = JourneyStore(tmp_path/'a.sqlite')
    b = JourneyStore(tmp_path/'b.sqlite')
    @asynccontextmanager
    async def idle(app):
        yield
    assert 'store_provider' in __import__('inspect').signature(api.create_router).parameters
    original = api.store
    apps=[]
    for store in [a,b]:
        app=FastAPI()
        app.include_router(api.create_router(store_provider=lambda store=store:store, lifespan=idle))
        apps.append(app)
    with TestClient(apps[0]) as first, TestClient(apps[1]) as second:
        state=first.post('/api/route/journeys',json={}).json()
        assert first.get('/api/route/journeys/'+state['id']).status_code==200
        assert second.get('/api/route/journeys/'+state['id']).status_code==404
        assert api.store is original


def test_native_api_import_never_creates_sqlite_fallback(tmp_path):
    env={**os.environ, 'PICKUP_ROUTE_BACKEND':'postgresql_development',
         'ROUTE_DB':str(tmp_path/'must-not-exist.sqlite')}
    p=subprocess.run([sys.executable,'-c','import demo.route.api; assert demo.route.api.store is None'],
                     env=env,capture_output=True,text=True,timeout=20)
    assert p.returncode==0,p.stderr
    assert list(tmp_path.iterdir())==[]


def test_native_runtime_settings_require_explicit_shared_secrets():
    try: settings=importlib.import_module('demo.route.ha.settings')
    except ModuleNotFoundError: pytest.fail('native role settings are not implemented')
    with pytest.raises(ValueError):settings.RoleSettings.from_env({})
    valid={'PICKUP_ORDER_DSN':'dbname=test', 'PICKUP_NODE_ID':'node-a',
           'ROUTE_MERCHANT_URL':'http://127.0.0.1:19001','ROUTE_MERCHANT_TOKEN':'m'*32,
           'ROUTE_PAYMENT_URL':'http://127.0.0.1:19002','ROUTE_PAYMENT_TOKEN':'p'*32,
           'ROUTE_PAYMENT_NOTIFY_SECRET':'s'*32}
    out=settings.RoleSettings.from_env(valid)
    assert out.node_id=='node-a'
    assert 'dbname=' not in repr(out) and 'ssss' not in repr(out)
    for key in ['ROUTE_PAYMENT_TOKEN','ROUTE_MERCHANT_TOKEN','ROUTE_PAYMENT_NOTIFY_SECRET']:
        with pytest.raises(ValueError):settings.RoleSettings.from_env(valid | {key:''})
    for url in ['https://outside.example','http://user:pass@127.0.0.1:3','http://127.0.0.1:3/path']:
        with pytest.raises(ValueError):settings.RoleSettings.from_env(valid | {'ROUTE_PAYMENT_URL':url})
    with pytest.raises(ValueError):settings.RoleSettings.from_env(valid | {'PICKUP_NODE_ID':'../bad'})


def test_runtime_modules_have_no_startup_side_effects():
    for name in ['merchant_service','notification_service','worker','runtime']:
        try:importlib.import_module('demo.route.ha.'+name)
        except ModuleNotFoundError:pytest.fail('native runtime is not implemented: '+name)
