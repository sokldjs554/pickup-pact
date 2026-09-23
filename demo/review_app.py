"""Explicit public entrypoint, compatible with legacy demo.main:app deployments.

Both entrypoints expose customer/merchant flows and /repair-lab. The repair
sample database is configured by REPAIR_REVIEW_DB in demo.main.
"""
from demo.main import app
