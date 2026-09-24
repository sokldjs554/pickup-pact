# Benefits and Outcome Evidence Implementation Plan

> Execute inline using superpowers:executing-plans.

**Goal:** Add coupon/points lifecycle and auditable paired outcome evidence to the existing customer product.
**Architecture:** Pure benefits pricing and paired evaluation modules; existing SQLite journey transaction owns benefit holds, consumption and refunds. UI reads the same server quote and event history.
**Tech Stack:** Existing FastAPI/Pydantic/SQLite/Python, browser JavaScript, pytest/Playwright.
**Spec:** docs/superpowers/specs/2026-09-24-benefits-evidence.md

## Global Constraints
No live financial claims, no market superiority claim, no new open-source licensing. Korean UI. Real HTTP browser tests. Final budget is cash after selected benefits. Immutable financial event history. Session-isolated demo wallet, not shared production account.

## Review Focus
Store-specific coupon loss; expiry while held versus released; points clipping and no negative balance; duplicate pickup/cancel; paired results must count failures and not read future noise at selection.

## Task 1 — Benefits lifecycle
Files: create demo/route/benefits.py and tests/route/test_benefits.py; modify planner.py/store.py/api.py.
Interfaces: benefits.initial_wallet(), price_quote(state, store, gross)->dict, hold/consume/release(state, order); plans returns pricing plus price=net cash.
Test request includes coupon_id='welcome500', points=1000; quote for wave latte must equal gross4300-discount500-points1000=2800. Reserve then cancel restores 2000 available points; pickup twice consumes once and earns28 once. Wave-only coupon loses1000 on Oat transfer; new net budget enforced. RED: endpoint rejects new fields before implementation. GREEN: tests and existing suite.

## Task 2 — Paired evidence
Files: create demo/route/outcomes.py, scripts/route_outcomes.py and tests/route/test_outcomes.py; add API for current-order comparison and fixed-seed paired trials.
Interfaces: compare_order(state) uses identical snapshot; run_experiment(seed, cases) returns rows/summary/digest. Repeated seeds match; each case includes both strategy outcomes, losses and unserviceable cases; no future delay supplied to policy. CLI saves JSON/CSV. API input bounded.

## Task 3 — Customer UX
Files: modify demo/route/index.html/product.js/product.css; add scripts/verify_benefits_browser.py.
Inputs: coupon and points on intent; pricing per card, wallet holds, transfer confirmation, receipt and restoration; outcome comparison on same order plus reproducible scenario report. Browser journey must cover real HTTP benefits->transfer loss confirmation->cancel restore and separate claim->earned points->replay.

## Task 4 — Release audit
Extend route-product workflow without weakening legacy assertions. Save result JSON, test logs, screenshots, source SHA. Repeat Python x3, experiments x3 and browser desktop/mobile x3 where available. Push feature PR using exact base tree; no main overwrite or force push. Verify same-SHA CI/deployment before completion report.
