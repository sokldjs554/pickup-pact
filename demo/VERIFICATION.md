# Demo verification contract

The interviewer demo is not considered verified from a screenshot or one successful API call.

For a release candidate, all of the following automated paths must pass on the same commit:

- `demo-ci`: FastAPI demo unit/API tests and Docker image build.
- `ui-e2e`: Chromium user flow covering order creation, payment authorization, confirmation, fault injection, reconciliation, sandbox repair, ledger inspection, capacity drift, and conflicting event IDs.
- `live-demo-smoke`: public Render smoke tests, including the complete operator flow twice.
- `ci`: repository guardrails, Python/JVM tests, contracts, evidence reproduction, Docker builds, and Terraform validation.
- `release-gate`: three repeated test passes, deterministic evidence reproduction, full Docker topology integration twice, and Terraform validation.

The public Render demo is intentionally a session-isolated FastAPI sandbox for reviewer convenience. The full PostgreSQL/Redis/Kafka/MongoDB/Elasticsearch topology is verified separately by the release gate and is not claimed to run inside the public demo service.
