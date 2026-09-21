# Demo verification contract

The public demo is not considered verified from a screenshot, one successful API call, or a previously green commit.

For a release candidate, all of the following automated paths must pass on the same commit:

- `demo-ci`: FastAPI demo unit/API tests and Docker image build.
- `ui-e2e`: local Chromium verification of the first-time guided flow, the full expert operator flow, capacity/conflict labs, and a narrow mobile viewport.
- `live-demo-smoke`: waits for the matching public Render release, verifies fixed scenarios and the complete API operator flow twice, then runs the same Chromium suite against the deployed public URL.
- `ci`: repository guardrails, Python/JVM tests, contracts, evidence reproduction, Docker builds, and Terraform validation.
- `release-gate`: three repeated test passes, deterministic evidence reproduction, full Docker topology integration twice, and Terraform validation.

Before calling a demo release complete, re-check implementation, tests, deployed execution, UI/API behavior, regression coverage, README/docs, and the final diff against the release commit.

The public Render demo is intentionally a session-isolated FastAPI sandbox for reviewer convenience. The full PostgreSQL/Redis/Kafka/MongoDB/Elasticsearch topology is verified separately by the release gate and is not claimed to run inside the public demo service.
