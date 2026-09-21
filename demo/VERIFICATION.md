# Demo verification contract

The public demo is not considered verified from a screenshot, one successful API call, or a previously green commit.

For a release candidate, all of the following automated paths must pass on the same commit:

- `demo-ci`: FastAPI demo unit/API tests and Docker image build.
- `ui-e2e`: local Chromium verification of the first-time guided flow, the full expert operator flow, capacity/conflict labs, and a narrow mobile viewport.
- `live-demo-smoke`: waits until the public `/health` reports the exact `RENDER_GIT_COMMIT` equal to the GitHub Actions SHA, verifies fixed scenarios and the complete API operator flow twice, then runs the same Chromium suite against that deployed public URL.
- `ci`: repository guardrails, Python/JVM tests, contracts, evidence reproduction, Docker builds, and Terraform validation.
- `release-gate`: three repeated test passes, deterministic evidence reproduction, full Docker topology integration twice, and Terraform validation.

Before calling a demo release complete, re-check the current release commit in this exact order:

1. implementation — read the current UI/backend code instead of trusting an earlier result;
2. tests — execute all five automated paths on the same commit;
3. deployed execution — confirm the public Render service reports that exact commit;
4. UI/API behavior — exercise both the first-time guided journey and the expert operator flow;
5. regression — repeat core tests and full topology checks, including the two-pass integration smoke;
6. README/docs — verify that public claims match the implementation and measured evidence;
7. final diff — compare the release commit with its pre-change base and review every changed file.

A release is not complete if any current-commit check is skipped, still running, failing, or only inferred from a previous commit.

The public Render demo is intentionally a session-isolated FastAPI sandbox for reviewer convenience. The full PostgreSQL/Redis/Kafka/MongoDB/Elasticsearch topology is verified separately by the release gate and is not claimed to run inside the public demo service.
