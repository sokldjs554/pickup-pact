# Recovery Release Audit Implementation Plan

> Use executing-plans inline. The user approved continuous whole-project review and correction, not repeated confirmation requests.

**Goal:** Ship the existing recoverable transfer with unattended bounded recovery, real merchant HTTP, verified UI and aligned release evidence.
**Architecture:** JourneyStore owns order/benefit decisions. DurableOperations journals each phase. RecoveryWorker scans durable due work. HttpMerchantFleet calls an authenticated, separately supervised merchant process with per-shop databases. UI only reads progress.
**Tech Stack:** Existing Python/FastAPI/SQLite/httpx, standard-library HTTP server/subprocess, JS, pytest, Playwright and existing JVM/Docker verification.
**Spec:** docs/superpowers/specs/2026-09-25-recovery-release-audit.md

## Global constraints
No real-money/GPS/production-HA claim. Keep classic/repair routes, all money semantics and unchanged policy experiment. No license changes. Never abandon a durable COMMIT by age. No app-source writes to main without exact-HEAD branch verification.

## Review focus
Old pending records without recovery metadata must not be postponed forever. Paused responses must advance the public state version. HTTP protocol rejection must not crash on arrays/maps instead of strings. A crash after independent merchant commit cannot become duplicate manufacturing. Losing the merchant process cannot erase the customer's saved order.

## Execution ledger
- [x] Restore source archive with exact modes and tree; rerun 295-test baseline.
- [x] RED/GREEN `test_recovery_worker.py`: due scheduling, concurrent workers, persisted retry budget, predecision expiry, postdecision completion, upgrade.
- [x] RED/GREEN `test_merchant_http.py`: real sockets/process, request auth/types, response loss, shared last seat, process death after three commits and reopening.
- [x] RED/GREEN `test_runtime_recovery.py`: shipped ASGI lifespan starts merchant HTTP and server worker; no recover POST required.
- [x] Add GET-only, version-fenced automatic UI refresh; preserve manual review and existing error messages.
- [ ] Repeat full existing/new regressions, inspect actual browser output; fix every discovered issue relevant to current behavior.
- [ ] Publish exact reviewed changes to feature branch; verify full CI, merge and verify new main public SHA.
- [ ] Capture the verified deployed recovery flow; align README and application documents; retain old versions.

Each checkbox refers to executed evidence, not code presence. Final release results live in the verification artifact, not a guessed completion marker.
