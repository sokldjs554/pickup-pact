# Pickup Pact HA/DR Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans in this session. Track each measured result, not an inferred success.

**Goal:** Preserve the existing order, merchant and synthetic payment invariants while introducing native PostgreSQL persistence, separately runnable roles, and guarded recovery rehearsals.

**Architecture:** Keep the current SQLite public mode unchanged. Add explicit PostgreSQL repositories, never SQL-string translation. Promote development, multi-host fault injection, and public deployment through separate gates.

**Tech Stack:** Python 3.12+, PostgreSQL 17.11, psycopg 3.3.6, psycopg_pool 3.3.1, existing HTTP and pytest contracts.

**Spec:** `docs/superpowers/specs/2026-10-02-ha-disaster-recovery-design.md` at upstream design commit `1fd542ae146662f4d8ff3629f96ceed1719f094a`.

## Global Constraints

- Existing main and the free public Render service stay unchanged until an explicitly approved rollout.
- No live PG/card/POS credentials; synthetic money only. No paid resource creation.
- No operating database deletion. Destructive rehearsal accepts only a newly created, labelled test cluster.
- One-host containers are development evidence, never independent-host HA evidence.
- No SQLite success fallback in PostgreSQL mode; unknown commit outcomes reuse the original operation key.
- No open-source licence or author attribution added.

## Review Focus

- Different idempotency keys targeting the same order must still produce one capture (Task 1).
- Credit-limit approval races across multiple authorizations must serialize by wallet, not globally (Task 1).
- Expired leases and delayed responses must not update a later operation phase (Task 2).
- Partial/altered backups and an unavailable WAL segment must not be published as recoverable (Task 3).
- Different deployment hosts and a genuinely external backup store must be verified before an HA/DR-complete claim (Task 5).

## Task 1 — Native PostgreSQL payment boundary

**Files:** `demo/route/ha/database.py`, `payment_repository.py`, `sql/payments.sql`; `demo/route/payments/storage.py`; existing HTTP/notification adapter; `tests/ha/test_payment_postgres.py`.

**Interfaces:** `PaymentStore.execute(command, notification_copies=1, notification_delay=0) -> dict`; `operation(key)`, `snapshot(world,order)`, `consume_fault(world,key,mode)`, `storage_ready()`, `claim_notifications(worker,limit,lease_seconds)`, `finish_notification(claim,ok)`.

- [ ] Write real PostgreSQL tests for reopen, request replay/conflict, cross-world binding, same-order capture and credit races, unknown result replay and durable outbox.
- [ ] Run tests before implementation; verify missing-interface failure.
- [ ] Implement exact PostgreSQL SQL/constraints with transaction-scoped wallet and identity locking; keep network I/O outside the transaction.
- [ ] Wire the existing synthetic HTTP provider to the repository interface; retain SQLite behavior.
- [ ] Run native PostgreSQL and complete existing Python/JS regression; retain environment, commands, source identity and logs.

## Task 2 — Order/merchant storage and fenced work ownership

**Files:** `demo/route/ha/operation_store.py`, `merchant_repository.py`, `sql/orders.sql`, `sql/merchants.sql`; `tests/ha/test_operation_store.py`, `test_merchant_postgres.py`.

**Interfaces:** transactional journey and immutable request records; persisted operation payload plus phase-version and monotonically increasing lease token; `claim_due(worker,limit,lease_seconds)`; `apply_result(claim,...) -> applied/rejected`.

- [ ] Pin lease takeover, simultaneous workers, expired response rejection and unrelated-order progress using separate database sessions.
- [ ] Implement per-order state and leased operations with `FOR UPDATE SKIP LOCKED`; do not keep a DB connection during HTTP.
- [ ] Implement merchant capacity/phase/generation/receipt operations in its own database.
- [ ] Reuse domain transitions; wire new coordinator and independent role runners only after parity tests pass.

## Task 3 — Verified backup and isolated restore

**Files:** `demo/route/ha/backup.py`, `scripts/verify_ha_restore.py`; `tests/ha/test_backup_guards.py`.

**Interfaces:** verify manifest, cluster identity, required files/checksums, sealed upload receipt, allowed fresh restore destination and test identity. `verify_ha_restore.py` creates its own disposable PostgreSQL instance and never accepts a production DSN as a destruction target.

- [ ] Write red tests for traversal/symlink, partial upload, changed file, missing WAL, same-source restore, unlabelled target and wrong cluster.
- [ ] Implement guards and base-backup/WAL rehearsal with exact input paths and transaction evidence.
- [ ] Restore into a fresh target, prove known transaction set and pending work; retain any lost interval rather than inventing data.
- [ ] Measure rehearsal elapsed time without relabelling it as multi-host production RTO/RPO.

## Task 4 — Integrate independent roles and full user flow

- [ ] Native order/merchant/PG role boot, shared internal authentication, readiness separated from liveness.
- [ ] Same order across two app instances, two workers and each independent data owner; same existing UI and API contracts.
- [ ] Repeat browser, HTTP, crash and legacy regression; no assertion weakening.

## Task 5 — Multi-host rollout and external recovery gate

Status and evidence: `docs/ha-multi-host-rollout.md`, `docs/ha-task5-progress.md`.

- [ ] Validate three distinct approved hosts, quorum, sync standby, fencing, redundant entry points and external backup identity.
  - Validator and approval gates implemented (`demo/route/ha/inventory.py`, review/deploy/destructive). No approved inventory exists yet, so this item stays open.
- [x] Configure Patroni/PostgreSQL with strict sync policy and separate role DB access.
  - Rendered Patroni/etcd/pg_hba/systemd configuration, least-privilege runtime users, owner migration, mTLS between roles, operating generation.
  - Exercised on a single-host Docker rehearsal only.
- [ ] Run HA-01..HA-10 and DR-01..DR-08 three times; retain failed iterations.
  - Single-host rehearsal, 3 fresh clusters each: HA-01..HA-06, WAL archive, generation fence, whole-cluster loss and restore from the external store with API and browser checks.
  - Independent hosts: not run. HA-10: not rehearsed. Development timings are not RTO/RPO.
- [x] Do not provision or damage any host without approved inventory/cost/destruction scope.
  - No resource was created. The deploy and destructive stages refuse inventories without approval.

## Task 6 — Review and release

- [ ] Independently review final diff (self-review explicitly labelled if no separate reviewer is available).
- [ ] Exact-source CI, candidate browser evidence, backup hashes and measured recovery report.
- [ ] Public release only after all applicable gates and authorization; no old source overlay.
