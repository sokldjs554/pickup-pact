#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

kotlinc \
  "$ROOT/services/commitment-service/src/main/kotlin/io/pickuppact/commitment/domain/PickupCommitment.kt" \
  "$ROOT/scripts/smoke/PickupDomainSmoke.kt" \
  -include-runtime \
  -d "$TMP/kotlin-domain-smoke.jar"
java -jar "$TMP/kotlin-domain-smoke.jar"

javac -d "$TMP/java" \
  "$ROOT/services/ledger-service/src/main/java/io/pickuppact/ledger/domain/LedgerDirection.java" \
  "$ROOT/services/ledger-service/src/main/java/io/pickuppact/ledger/domain/LedgerEntry.java" \
  "$ROOT/services/ledger-service/src/main/java/io/pickuppact/ledger/domain/LedgerBatch.java" \
  "$ROOT/services/ledger-service/src/main/java/io/pickuppact/ledger/domain/LedgerPostingPolicy.java" \
  "$ROOT/scripts/smoke/LedgerDomainSmoke.java"
java -cp "$TMP/java" LedgerDomainSmoke

echo "Kotlin and Java pure-domain smoke execution passed"
