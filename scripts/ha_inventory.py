"""Validate or render a ha_postgres_v1 deployment inventory. Never contacts a host.

    python scripts/ha_inventory.py validate infra/ha/inventory.example.yaml --stage review
    python scripts/ha_inventory.py render inventory.yaml --out build/ha-config

Exit status is 1 when any problem is found; all problems are listed at once.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.route.ha import inventory  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('validate')
    check.add_argument('inventory')
    check.add_argument('--stage', choices=inventory.STAGES, default='review')
    build = commands.add_parser('render')
    build.add_argument('inventory')
    build.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        data = inventory.load(args.inventory)
    except inventory.InventoryError as exc:
        print(json.dumps(dict(ok=False, problems=exc.problems), ensure_ascii=False, indent=2))
        return 1
    if args.command == 'validate':
        problems = inventory.validate(data, args.stage)
        print(json.dumps(dict(ok=not problems, stage=args.stage, inventory_sha256=inventory.digest(data),
                              problems=problems), ensure_ascii=False, indent=2))
        return 1 if problems else 0
    try:
        manifest = inventory.render(data, Path(args.out))
    except inventory.InventoryError as exc:
        print(json.dumps(dict(ok=False, stage='review', problems=exc.problems), ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(dict(ok=True, **{k: manifest[k] for k in ('inventory_sha256', 'kind', 'evidence_scope', 'stages')},
                          files=len(manifest['files'])), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
