"""Select a shared native store without starting private merchant/PG databases."""
from contextlib import asynccontextmanager
import os
from .settings import RoleSettings


@asynccontextmanager
async def native_lifespan(app):
    from .. import api
    settings=RoleSettings.from_env(os.environ)
    previous=api.store
    store=settings.store()
    api.store=store
    app.state.native_store=store
    try:
        yield
    finally:
        api.store=previous
        store.close()
