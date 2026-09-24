"""Public application composition; preserve the existing Render entrypoint.

The unchanged customer/merchant demo lives in customer_app.py. The repair
workbench operates only on fixed synthetic samples, never payment gateways.
"""
import os
from pathlib import Path
import tempfile

from demo.customer_app import app, run_scenario  # retain existing imports
from services.reconciler.app.workbench import create_router

_default_db = Path(tempfile.gettempdir())/'pickup-pact-repair-review.sqlite'
app.include_router(create_router(os.environ.get('REPAIR_REVIEW_DB', str(_default_db))))

# Customer-facing time-first ordering shares one persisted journey across views.
from demo.route.api import create_router as create_route_router
app.include_router(create_route_router())
