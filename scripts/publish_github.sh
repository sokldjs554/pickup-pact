#!/usr/bin/env bash
set -euo pipefail

visibility="${1:-private}"
case "$visibility" in
  private|public) ;;
  *) echo "usage: $0 [private|public]" >&2; exit 2 ;;
esac

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) is required." >&2
  exit 1
fi

gh auth status

if git remote get-url origin >/dev/null 2>&1; then
  echo "origin already exists: $(git remote get-url origin)" >&2
  exit 1
fi

gh repo create sokldjs554/pickup-pact --"$visibility" --source=. --remote=origin --push
