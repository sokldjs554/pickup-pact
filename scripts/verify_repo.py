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
    "docker-compose.yml",
    "contracts/openapi.yaml",
    "contracts/asyncapi.yaml",
    "services/commitment-service/src/main/kotlin/io/pickuppact/commitment/domain/PickupCommitment.kt",
    "services/ledger-service/src/main/java/io/pickuppact/ledger/domain/LedgerBatch.java",
    "services/reconciler/app/main.py",
    "services/ops-console/app.py",
    "infra/k8s/reconciler.yaml",
    "infra/aws/terraform/main.tf",
    "infra/observability/datadog-monitor.json",
    "automation/n8n/performance-regression-triage.json",
    "automation/make/README.md",
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

print(f"repository verification passed ({len(required)} required artifacts, {len(skills)} skill markers)")
