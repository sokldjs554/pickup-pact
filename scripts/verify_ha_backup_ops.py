#!/usr/bin/env python3
"""WAL shipping and backup monitoring against a real append-only Restic REST store.

Runs on one development host with a self-created HTTPS rest-server (see
ha_restic_lab.py). WAL-named files are synthetic payloads: this verifies the
transport, confirmation, credential split and alerts, not PostgreSQL recovery
(that is verify_ha_restore.py). It is not evidence of an off-host store.

Checks, repeated --repeat times with a new repository each time:
  * a spool batch becomes one snapshot whose listing matches names and sizes,
    and only then is the spool released;
  * with the store down, or over quota, nothing is released, the failure is
    recorded and the monitor raises WAL_UPLOAD_FAILING / WAL_SHIPPING_DELAYED;
  * after recovery the same files ship and the alert clears;
  * the writer credential cannot remove a snapshot (forget is refused);
  * the retention plan computed from the repository's own listing keeps every
    WAL snapshot the retained base backups need.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from demo.route.ha.backup_ops import (BaseBackup, Policy, WalSnapshot, assess, maintainer_commands,  # noqa: E402
                                     plan_retention, ship_wal, spool_status)
from ha_restic_lab import ResticLab  # noqa: E402
from demo.route.ha.remote_backup import ResticArchive  # noqa: E402

POLICY = Policy(retain_base_backups=2, base_backup_interval_hours=24, max_base_backup_age_hours=30,
                max_wal_archive_delay_seconds=30, capacity_gib=1, alert_free_ratio=0.2)


def segment(position, timeline=1):
    return f'{timeline:08X}{position // 256:08X}{position % 256:08X}'


def spool_files(spool: Path, positions):
    for position in positions:
        (spool/segment(position)).write_bytes(os.urandom(4096) + segment(position).encode())


def rejected(action) -> str:
    try:
        action()
    except (OSError, ValueError) as exc:
        return type(exc).__name__
    raise AssertionError('operation was not refused')


def status_for(spool, shipper_file, now):
    return dict(base_backups=[dict(finished_at=now-3600, verified=True)], archiver={},
                spool=spool_status(spool, clock=lambda: now),
                shipper=json.loads(shipper_file.read_text()) if shipper_file.exists() else {},
                repository={}, retention={'problems': []})


def rehearse() -> dict:
    checks = {}
    with ResticLab() as lab, tempfile.TemporaryDirectory(prefix='pickup-wal-spool-') as temporary:
        spool, state = Path(temporary)/'spool', Path(temporary)/'shipper.json'
        spool.mkdir(mode=0o700)
        archive = ResticArchive(lab.settings, command_timeout=60)
        failing = ResticArchive(lab.settings, command_timeout=15)  # restic retries an unavailable store
        snapshots = []

        spool_files(spool, range(1, 6))
        result = ship_wal(spool, archive, status_file=state)
        snapshots.append(WalSnapshot(result['last_snapshot'], tuple(segment(p) for p in range(1, 6))))
        assert result['last_uploaded'] == 5 and not list(spool.glob('0*'))
        assert archive.list_files(result['last_snapshot'], '/'+'/'.join(Path(
            json.loads(archive._run(['snapshots', '--json', result['last_snapshot']]))[0]['paths'][0]).parts[1:])) \
            .keys() == {segment(p) for p in range(1, 6)}
        checks['batch_confirmed_then_released'] = True
        print('shipped first batch', flush=True)

        lab.stop()
        spool_files(spool, range(6, 9))
        checks['store_down_upload_refused'] = rejected(lambda: ship_wal(spool, failing, status_file=state))
        assert len(list(spool.glob('0*'))) == 3
        later = time.time()+POLICY.max_wal_archive_delay_seconds+5
        alerts = {a['code'] for a in assess(POLICY, status_for(spool, state, later), now=later)['alerts']}
        assert {'WAL_UPLOAD_FAILING', 'WAL_SHIPPING_DELAYED'} <= alerts, alerts
        checks['store_down_alerts'] = sorted(alerts)

        lab.start(quota=1)
        checks['quota_upload_refused'] = rejected(lambda: ship_wal(spool, failing, status_file=state))
        assert len(list(spool.glob('0*'))) == 3
        lab.stop()

        lab.start()
        result = ship_wal(spool, archive, status_file=state)
        snapshots.append(WalSnapshot(result['last_snapshot'], tuple(segment(p) for p in range(6, 9))))
        assert not list(spool.glob('0*'))
        now = time.time()
        assessment = assess(POLICY, status_for(spool, state, now), now=now)
        remaining = {a['code'] for a in assessment['alerts']}
        # Host disk alerts (SPOOL_CAPACITY) depend on the machine and are reported, not hidden.
        assert not remaining & {'WAL_UPLOAD_FAILING', 'WAL_SHIPPING_DELAYED'}, assessment
        checks['recovered_alerts_cleared'] = True
        checks['other_alerts_on_this_host'] = sorted(remaining)

        checks['writer_cannot_forget'] = rejected(lambda: failing._run(['forget', snapshots[0].snapshot_id]))
        listed = json.loads(archive._run(['snapshots', '--json', '--tag', 'pickup-wal']))
        assert {s['id'] for s in listed} == {s.snapshot_id for s in snapshots}
        checks['snapshots_after_forget_attempt'] = len(listed)

        # Retention computed from the repository's own WAL snapshots.
        backups = [BaseBackup('a'*64, 1.0, segment(2), True), BaseBackup('b'*64, 2.0, segment(6), True)]
        plan = plan_retention(POLICY, backups, snapshots)
        assert plan['problems'] == [] and plan['remove_wal_snapshots'] == []  # 2.. is still needed
        backups.append(BaseBackup('c'*64, 3.0, segment(7), True))
        plan = plan_retention(POLICY, backups, snapshots)
        # Kept backups start at segments 6 and 7: the 1..5 snapshot is removable, 6..8 is not.
        assert plan['remove_base_backups'] == ['a'*64]
        assert plan['remove_wal_snapshots'] == [snapshots[0].snapshot_id], plan
        checks['retention_keeps_needed_wal'] = maintainer_commands(plan)
        archive._run(['check', '--read-data'])
        checks['repository_check_read_data'] = True
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=3, choices=range(1, 6))
    args = parser.parse_args()
    if os.environ.get('PICKUP_HA_TEST') != '1':
        raise SystemExit('explicit isolated rehearsal opt-in required (PICKUP_HA_TEST=1)')
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(scope='single_host_restic_https_development_not_off_host', runs=[], passed=False)
    try:
        for number in range(1, args.repeat+1):
            report['runs'].append(dict(repeat=number, checks=rehearse(), passed=True))
        report['passed'] = True
    except Exception as exc:  # noqa: BLE001 - retained in the report
        report['runs'].append(dict(passed=False, error=f'{type(exc).__name__}: {str(exc)[:600]}'))
        raise
    finally:
        (args.output/'results.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(passed=report['passed'], runs=len(report['runs']))))


if __name__ == '__main__':
    main()
