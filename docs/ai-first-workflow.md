# AI-first development workflow

The target backend role explicitly expects AI-assisted development across requirement analysis, design, implementation, review, documentation, and repetitive-work automation. This repository keeps that workflow inspectable instead of claiming that “AI was used” without boundaries.

## 1. PRD / requirement analysis

Input: a short product requirement or incident scenario.

Prompt artifact: `ai/prompts/prd_to_domain.md`

Expected output:

- actors and business invariants;
- bounded contexts;
- commands and domain events;
- consistency requirements;
- idempotency boundaries;
- open questions that must be resolved by a human.

Human gate: no generated acceptance criterion is treated as fact unless it exists in the PRD or is explicitly approved.

## 2. API design review

Prompt artifact: `ai/prompts/openapi_review.md`

The review checks retry semantics, status codes, compatibility, timestamps, optimistic concurrency, authorization boundaries, and accidental replay of financial mutations.

The checked-in OpenAPI remains the contract; generated review text is advisory.

## 3. Invariant-driven test generation

Prompt artifact: `ai/prompts/test_generation.md`

AI can suggest missing negative tests for duplicate delivery, out-of-order delivery, capacity races, late cancellation, conflicting event IDs, and projection rebuilds. A test is accepted only when its setup and deterministic assertion are understandable without the model.

## 4. SQL / performance analysis

Prompt artifact: `ai/prompts/sql_explain_review.md`

The model must separate facts visible in `EXPLAIN (ANALYZE, BUFFERS)` from hypotheses and may not invent latency improvements. This is why the repository keeps measured synthetic correctness and local HTTP baselines separate from production claims.

## 5. Incident triage

Prompt artifact: `ai/prompts/incident_triage.md`

The optional provider-neutral reviewer supports Claude, OpenAI/ChatGPT, and Gemini adapters. In CI and the public demo the provider is `offline`, making the result deterministic.

Hard boundary: AI cannot create, add, or execute financial repair commands. `services/reconciler/app/engine.py` is authoritative for repair policy.

## 6. Repetitive-work automation

- `automation/n8n/performance-regression-triage.json` demonstrates a regression-triage workflow.
- `automation/make/README.md` documents an equivalent Make workflow.
- Slack/Jira/Notion are documented as optional destinations; this repository does not claim a live SaaS connection.

## What is actually verified

GitHub Actions verifies repository guardrails, Python reconciliation tests, Flask console tests, contract/config parsing, Kotlin/Java Maven tests, deterministic consistency benchmarks, Docker builds, and Terraform validation. A separate live-demo smoke test verifies the public Render page, fixed failure scenarios, and the complete session-based operator flow. The release gate repeats unit/integration verification and boots the full Docker topology for runtime smoke.

This document describes a reproducible workflow and guardrails, not a claim that every provider was called on every commit.
