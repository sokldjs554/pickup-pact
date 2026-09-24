# Time-first Coffee Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. Execute within this session.

**Goal:** Build a customer-facing deadline-aware coffee route with one-order merchant transfer.
**Architecture:** FastAPI command API + deterministic graph planner + transactional SQLite journey aggregate; HTML/CSS/JS shared customer, merchant, receipt views. Preserve the old backend and UI at /classic.
**Tech Stack:** Python, FastAPI, Pydantic, SQLite, browser JavaScript, pytest, Chromium/Playwright.
**Spec:** docs/superpowers/specs/2026-09-24-time-first-coffee.md

## Global Constraints
No real payment, live GPS, provider keys, external assets or third-party merchant simulation claims. No MIT addition. Keep existing API behavior. Only known factory inputs accepted. Test each state transition. Customer copy in Korean. Never silently replace drink constraints or exceed budget.

## Review Focus
Stale quote after busy update; simultaneous start vs transfer; repeated financial command; no feasible cafe; page reload across view changes. Independent sessions must not mutate one another.

## Tasks
- [ ] 1. Baseline and endpoint RED: run existing suites, test new POST /api/route/journeys (must fail 404), document baseline SHA.
- [ ] 2. Planner and store: demo/route/planner.py provides catalogue(), plans(state); demo/route/store.py provides create(intent), get(id), command(id, request); write state and invariant tests, run RED then GREEN. Typed API in demo/route/api.py.
- [ ] 3. Product: demo/route/index.html, product.css, product.js. Server-derived routes, quotes, rescue, merchant controls, receipt, settings, refresh. Existing home /classic. New home test and Chromium journeys.
- [ ] 4. Regression & release: repeat pytest full existing/new suites three times, same comparison digest, real HTTP Chromium desktop/mobile. Update OpenAPI via runtime export and docs; add CI new tests plus browser. Verify diff excludes unrelated code. Publish feature branch/PR; merge only exact passing HEAD; verify matching deployed SHA and live UI when available.

## Working ledger
Baseline: restored exact CI source for main 240e830c10a6d8bbd418d1beaf224ab23ca889bd; tested-source SHA256 d27bf9366d184fc0457458db16a760629ffc016035f2d224d8b3c878007fd0e7.
Baseline tests: 175 passed; local Flask/Maven/Docker not yet available.
Ruling: user asked to rebuild directly and not repeat planning discussions. Execute design in-session, retain rollback via branch and old UI.
