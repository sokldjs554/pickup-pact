#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

required = [
    "README.md",
    "Jenkinsfile",
    ".github/workflows/ci.yml",
    ".github/workflows/live-demo-smoke.yml",
    ".github/workflows/ui-e2e.yml",
    "demo/index.html",
    "demo/e2e/operator-flow.spec.js",
    "demo/VERIFICATION.md",
    "docker-compose.yml",
    "contracts/openapi.yaml",
    "contracts/asyncapi.yaml",
    "services/commitment-service/src/main/kotlin/io/pickuppact/commitment/domain/PickupCommitment.kt",
    "services/ledger-service/src/main/java/io/pickuppact/ledger/domain/LedgerBatch.java",
    "services/reconciler/app/main.py",
    "services/reconciler/app/engine.py",
    "services/ops-console/app.py",
    "infra/k8s/reconciler.yaml",
    "infra/aws/terraform/main.tf",
    "infra/observability/datadog-monitor.json",
    "automation/n8n/performance-regression-triage.json",
    "automation/make/README.md",
    "docs/ai-first-workflow.md",
    "docs/jd-traceability.md",
    "docs/jd-gap-audit.md",
    "docs/performance.md",
    "sql/explain/commitment_timeline.sql",
    "artifacts/consistency-benchmark.json",
    "artifacts/consistency-matrix.json",
    "artifacts/reconciler-http-summary.json",
    "scripts/integration_smoke.py",
    ".github/workflows/release-gate.yml",
]
missing = [path for path in required if not (ROOT / path).exists()]
if missing:
    print("missing required files:", *missing, sep="\n- ")
    sys.exit(1)

if any(p.name.upper().startswith("LICENSE") for p in ROOT.iterdir()):
    print("repository must not contain a license file")
    sys.exit(1)

all_text = "\n".join(
    p.read_text(encoding="utf-8", errors="ignore")
    for p in ROOT.rglob("*")
    if p.is_file() and ".git" not in p.parts and p.stat().st_size < 1_000_000
)
if re.search(r"\bMIT\s+License\b", all_text, flags=re.IGNORECASE):
    print("disallowed license wording found")
    sys.exit(1)

secret_patterns = [r"ghp_[A-Za-z0-9]{20,}", r"sk-[A-Za-z0-9_-]{20,}", r"AKIA[0-9A-Z]{16}"]
for pattern in secret_patterns:
    if re.search(pattern, all_text):
        print(f"possible secret found: {pattern}")
        sys.exit(1)

skills = [
    "Kotlin", "Python", "Java", "Spring", "WebFlux", "FastAPI", "Flask", "PostgreSQL",
    "MongoDB", "Redis", "Elasticsearch", "Kafka", "Celery", "AWS", "Kubernetes", "Docker",
    "Jenkins", "Datadog", "Elastic APM", "Claude Code", "Cursor", "Claude", "ChatGPT", "Gemini", "n8n", "Make",
    "Slack", "Jira", "Notion", "DDD", "EDA", "CQRS", "MSA", "OpenAPI", "REST",
]
missing_skills = [skill for skill in skills if skill.lower() not in all_text.lower()]
if missing_skills:
    print("missing skill evidence:", missing_skills)
    sys.exit(1)

# Cross-file contract checks catch documentation/code drift that ordinary unit tests miss.
openapi = (ROOT / "contracts/openapi.yaml").read_text()
asyncapi = (ROOT / "contracts/asyncapi.yaml").read_text()
schema = (ROOT / "sql/schema.sql").read_text()
persistence = (ROOT / "services/reconciler/app/persistence.py").read_text()
explain = (ROOT / "sql/explain/commitment_timeline.sql").read_text()
architecture = (ROOT / "docs/architecture.md").read_text()
domain_model = (ROOT / "docs/domain-model.md").read_text()
demo = (ROOT / "demo/main.py").read_text()
live_demo_workflow = (ROOT / ".github/workflows/live-demo-smoke.yml").read_text()
demo_dockerfile = (ROOT / "Dockerfile.demo").read_text()

assert "paymentAuthorized" not in openapi, "hold contract must not let a caller self-authorize payment"
assert "/api/v1/commitments/{id}/authorize-payment" in openapi
assert "/api/v1/ledger/postings" in openapi
assert "/api/v1/commitments/{id}:" in openapi
assert "/api/v1/ledger/orders/{aggregateId}" in openapi
assert "/api/v1/ledger/conflicts" in openapi
assert "/api/v1/projections/rebuild" in openapi
assert "/api/v1/projections/{aggregateId}" in openapi
assert "commitment_projection" in schema and "commitment_projection" in persistence
assert "ledger_conflicts" in schema
assert "required: [eventId, aggregateId, type, amount]" in asyncapi
assert "PickupClaimed" in openapi and "PickupClaimed" in asyncapi
assert "reconciliation_run" in schema and "reconciliation_run" in persistence
assert "outbox_events" in explain and "outbox_event\n" not in explain
assert "financial_event_receipt" not in architecture
assert "financial_event_receipt" not in domain_model
assert "OutboxPublisher" not in architecture
assert "services.reconciler.app.engine" in demo
assert "RENDER_GIT_COMMIT" in demo and "release_commit" in demo
assert "EXPECTED_COMMIT" in live_demo_workflow and 'health["release_commit"] == EXPECTED_COMMIT' in live_demo_workflow
assert "COPY services /app/services" in demo_dockerfile
assert (ROOT / "scripts/integration_smoke.py").read_text().count("full topology integration pass") == 1
env_example = (ROOT / ".env.example").read_text()
assert "MONGODB_URL=" in env_example
assert "REDIS_URL=" in env_example
assert "MONGO_URL=" not in env_example
assert "CELERY_BROKER_URL=" not in env_example
demo_doc = (ROOT / "docs/demo.md").read_text()
demo_index = (ROOT / "demo/index.html").read_text()
demo_verification = (ROOT / "demo/VERIFICATION.md").read_text()
assert "session-isolated smart-order customer demo" in demo_doc
assert "Customer smart-order experience" in demo_doc
assert "Technical detail layer" in demo_doc
assert "REVERSE_SETTLEMENT" in demo_doc and "REVERSE_REWARD" in demo_doc
performance_doc = (ROOT / "docs/performance.md").read_text()
release_gate = (ROOT / ".github/workflows/release-gate.yml").read_text()
jd_gap = (ROOT / "docs/jd-gap-audit.md").read_text()
assert "idx_outbox_aggregate_timeline" in performance_doc
assert "commitment-timeline-explain.json" in release_gate
assert "Query execution-plan analysis" in jd_gap
assert "Trust Receipt" in jd_gap and "Pickup Code" in jd_gap
for marker in [
    "오늘 뭐 드실래요?",
    "근처 매장",
    "장바구니 보기",
    "내 주문",
    "체험 손님",
    "주문 취소",
    "픽업 시간이 바뀌면 먼저 알려드려요.",
    "괜찮아요",
    "주문 내역",
    "모바일 영수증",
    "1회용 픽업 코드",
    "TRUST RECEIPT",
    "제품 화면으로 돌아가기",
]:
    assert marker in demo_index, f"customer demo marker missing: {marker}"
for marker in [
    "ui-e2e",
    "live-demo-smoke",
    "release-gate",
    "same commit",
    "deployed public URL",
]:
    assert marker in demo_verification, f"verification contract marker missing: {marker}"

# Check relative Markdown links so README/docs do not point to files that are absent.
link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
broken_links: list[str] = []
for markdown in ROOT.rglob("*.md"):
    text = markdown.read_text(encoding="utf-8", errors="ignore")
    for raw in link_pattern.findall(text):
        target = raw.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        resolved = (markdown.parent / target).resolve()
        try:
            resolved.relative_to(ROOT.resolve())
        except ValueError:
            broken_links.append(f"{markdown.relative_to(ROOT)} -> {raw} (outside repository)")
            continue
        if not resolved.exists():
            broken_links.append(f"{markdown.relative_to(ROOT)} -> {raw}")
if broken_links:
    print("broken relative markdown links:", *broken_links, sep="\n- ")
    sys.exit(1)

print(
    f"repository verification passed "
    f"({len(required)} required artifacts, {len(skills)} skill markers, contract/link consistency checks)"
)
