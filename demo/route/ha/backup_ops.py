"""External backup operations for ``ha_postgres_v1``: WAL shipping, retention, monitoring.

Credentials are split by role. Database hosts hold only the append-only writer
credential of the REST store: they can add snapshots but cannot remove any.
Removing snapshots (``forget``/``prune``) needs the maintainer credential, which
never lives on a database host. This module therefore only *plans* removals and
prints the explicit maintainer commands; it never runs them.

The retention plan never removes WAL that a retained base backup needs: a WAL
snapshot is removable only when every segment in it precedes the oldest WAL
position required by the kept base backups, and nothing is removed while a
retained backup has a WAL gap, a timeline lacks its history file, or fewer
verified backups exist than the policy keeps.

Monitoring turns collected facts into alerts and reports the current exposure
(time since the newest WAL confirmed off-host). That figure is a measurement of
the present state, never a promised RPO.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
from uuid import uuid4

WAL = re.compile(r'^([0-9A-F]{8})([0-9A-F]{8})([0-9A-F]{8})$')
HISTORY = re.compile(r'^([0-9A-F]{8})\.history$')
# Also archived by PostgreSQL: the last partial segment of a timeline at promotion,
# and base backup history files. Neither counts as a complete segment.
PARTIAL = re.compile(r'^([0-9A-F]{24})\.partial$')
BACKUP_LABEL = re.compile(r'^([0-9A-F]{24})\.[0-9A-F]{8}\.backup$')


def archived_name(name: str) -> bool:
    return bool(WAL.fullmatch(name) or HISTORY.fullmatch(name) or PARTIAL.fullmatch(name)
                or BACKUP_LABEL.fullmatch(name))


def segment_of(name: str) -> str | None:
    """The segment a WAL, partial or backup-label file belongs to (None for history files)."""
    if WAL.fullmatch(name):
        return name
    match = PARTIAL.fullmatch(name) or BACKUP_LABEL.fullmatch(name)
    return match.group(1) if match else None
SEGMENT = 16*1024*1024
WAL_TAG = 'pickup-wal'


@dataclass(frozen=True)
class Policy:
    retain_base_backups: int
    base_backup_interval_hours: int
    max_base_backup_age_hours: int
    max_wal_archive_delay_seconds: int
    capacity_gib: int
    alert_free_ratio: float

    @classmethod
    def from_json(cls, data: dict) -> 'Policy':
        values = {}
        for name, low, high in [('retain_base_backups', 2, 90), ('base_backup_interval_hours', 1, 168),
                                ('max_base_backup_age_hours', 1, 336), ('max_wal_archive_delay_seconds', 30, 900),
                                ('capacity_gib', 1, 1_000_000)]:
            value = data.get(name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'backup policy {name} out of range')
            values[name] = value
        ratio = data.get('alert_free_ratio')
        if type(ratio) not in {int, float} or not 0.05 <= ratio <= 0.5:
            raise ValueError('backup policy alert_free_ratio out of range')
        if data.get('writer') != 'append_only':
            raise ValueError('database hosts may only hold an append-only backup credential')
        return cls(alert_free_ratio=float(ratio), **values)


def wal_position(name: str, segment_size: int = SEGMENT) -> tuple[int, int]:
    """(timeline, absolute segment number) of a WAL file name."""
    match = WAL.fullmatch(name)
    if not match:
        raise ValueError('not a WAL segment name')
    if segment_size <= 0 or segment_size & (segment_size-1):
        raise ValueError('WAL segment size must be a power of two')
    per_id = 0x100000000 // segment_size
    timeline, log, seg = (int(part, 16) for part in match.groups())
    if timeline == 0 or seg >= per_id:
        raise ValueError('invalid WAL segment name')
    return timeline, log*per_id+seg


@dataclass(frozen=True)
class BaseBackup:
    snapshot_id: str
    finished_at: float
    start_wal: str
    verified: bool


@dataclass(frozen=True)
class WalSnapshot:
    snapshot_id: str
    files: tuple[str, ...]


def plan_retention(policy: Policy, backups: list[BaseBackup], wal_snapshots: list[WalSnapshot], *,
                   segment_size: int = SEGMENT) -> dict:
    problems = []
    verified = sorted((b for b in backups if b.verified), key=lambda b: b.finished_at, reverse=True)
    keep = verified[:policy.retain_base_backups]
    unverified = [b.snapshot_id for b in backups if not b.verified]
    if len(verified) < policy.retain_base_backups:
        problems.append(f'only {len(verified)} verified base backups; policy keeps {policy.retain_base_backups}')
    names = [name for snapshot in wal_snapshots for name in snapshot.files]
    positions, timelines = set(), set()
    for name in names:
        if WAL.fullmatch(name):
            timeline, position = wal_position(name, segment_size)
            positions.add(position)
            timelines.add(timeline)
        elif not archived_name(name):
            problems.append('unexpected file in a WAL snapshot')
    histories = {int(HISTORY.fullmatch(name).group(1), 16) for name in names if HISTORY.fullmatch(name)}
    for timeline in sorted(t for t in timelines if t > 1):
        if timeline not in histories:
            problems.append(f'timeline {timeline:08X} has no history file')
    latest = max(positions) if positions else None
    boundary = None
    for backup in keep:
        _, start = wal_position(backup.start_wal, segment_size)
        boundary = start if boundary is None else min(boundary, start)
        if latest is None or latest < start:
            problems.append(f'no archived WAL after base backup {backup.snapshot_id[:12]}')
            continue
        missing = [p for p in range(start, latest+1) if p not in positions]
        if missing:
            problems.append(f'WAL gap of {len(missing)} segments after base backup {backup.snapshot_id[:12]}')
    remove_backups, remove_wal = [], []
    if not problems and boundary is not None:
        remove_backups = [b.snapshot_id for b in verified[policy.retain_base_backups:]]
        for snapshot in wal_snapshots:
            segments = [segment_of(name) for name in snapshot.files]
            if snapshot.files and all(item is not None and wal_position(item, segment_size)[1] < boundary
                                      for item in segments):
                remove_wal.append(snapshot.snapshot_id)
    return dict(keep_base_backups=[b.snapshot_id for b in keep], remove_base_backups=remove_backups,
                remove_wal_snapshots=remove_wal, unverified_left_for_review=unverified,
                oldest_required_wal_position=boundary, problems=problems)


def maintainer_commands(plan: dict) -> list[str]:
    """Explicit IDs for the maintainer host; never executed by this module."""
    if plan['problems']:
        return []
    ids = plan['remove_base_backups'] + plan['remove_wal_snapshots']
    for snapshot in ids:
        if not re.fullmatch(r'[0-9a-f]{64}', snapshot):
            raise ValueError('full snapshot IDs required')
    return [] if not ids else ['restic forget ' + ' '.join(ids), 'restic prune']


# ---------------------------------------------------------------- shipping

def _spooled(spool: Path) -> list[Path]:
    files = []
    for path in sorted(spool.iterdir()):
        if path.name.startswith('.'):
            continue  # partial copies and batch directories
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('unexpected non-regular entry in the WAL spool')
        if not archived_name(path.name):
            raise ValueError('unexpected file in the WAL spool')
        files.append(path)
    return files


def ship_wal(spool: Path, archive, *, status_file: Path, max_files: int = 256, clock=time.time) -> dict:
    """Upload spooled WAL as one snapshot, confirm names and sizes, then release the spool.

    A spooled file is removed only after it is listed in the new snapshot with
    the same size. Any failure leaves every file in place and records the error.
    """
    spool = Path(spool)
    status = _read_status(status_file)
    batch = None
    try:
        files = _spooled(spool)[:max_files]
        if not files:
            status.update(last_check_at=clock(), pending_files=0)
            return status
        batch = spool/('.batch-'+uuid4().hex)
        batch.mkdir(mode=0o700)
        sizes = {}
        for path in files:
            os.link(path, batch/path.name)  # same filesystem; content cannot change underneath
            sizes[path.name] = path.stat().st_size
        snapshot = archive.archive_directory(batch, tag=WAL_TAG)
        listed = archive.list_files(snapshot, str(batch))
        if any(listed.get(name) != size for name, size in sizes.items()):
            raise ValueError('uploaded WAL snapshot does not list every spooled file')
        for path in files:
            path.unlink()
        segments = [name for name in sizes if WAL.fullmatch(name)]
        status.update(last_success_at=clock(), last_snapshot=snapshot, last_uploaded=len(files),
                      newest_uploaded=max(segments, key=lambda n: wal_position(n)[::-1]) if segments else None,
                      pending_files=len(_spooled(spool)), last_check_at=clock())
        return status
    except (OSError, ValueError) as exc:
        status.update(last_error_at=clock(), last_error=type(exc).__name__, last_check_at=clock(),
                      pending_files=len([p for p in spool.iterdir() if not p.name.startswith('.')]))
        raise
    finally:
        if batch is not None:
            shutil.rmtree(batch, ignore_errors=True)
        _write_status(status_file, status)


def _read_status(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_status(path: Path, status: dict) -> None:
    path = Path(path)
    temporary = path.with_name('.'+path.name+'.tmp')
    temporary.write_text(json.dumps(status, sort_keys=True))
    os.replace(temporary, path)


def spool_status(spool: Path, *, clock=time.time) -> dict:
    files = _spooled(Path(spool))
    usage = shutil.disk_usage(spool)
    oldest = min((p.stat().st_mtime for p in files), default=None)
    return dict(files=len(files), oldest_age_s=None if oldest is None else max(0.0, clock()-oldest),
                free_ratio=usage.free/usage.total if usage.total else 0.0)


def archiver_status(db) -> dict:
    """pg_stat_archiver through a monitoring connection (pg_monitor role)."""
    row = db.execute('''SELECT archived_count, failed_count,
                               extract(epoch FROM last_archived_time) AS last_archived_at,
                               extract(epoch FROM last_failed_time) AS last_failed_at
                        FROM pg_stat_archiver''').fetchone()
    if row is None:
        return {}
    return {key: None if value is None else float(value) for key, value in dict(row).items()}


# -------------------------------------------------------------- monitoring

def assess(policy: Policy, status: dict, *, now: float) -> dict:
    alerts = []

    def alert(code, severity, detail):
        alerts.append(dict(code=code, severity=severity, detail=detail))

    backups = status.get('base_backups') or []
    verified = [b for b in backups if b.get('verified')]
    newest = max((b['finished_at'] for b in verified), default=None)
    if newest is None:
        alert('BASE_BACKUP_MISSING', 'critical', 'no verified base backup exists')
    elif now-newest > policy.max_base_backup_age_hours*3600:
        alert('BASE_BACKUP_STALE', 'critical', f'newest verified base backup is {round((now-newest)/3600, 1)} h old')
    if backups and not max(backups, key=lambda b: b['finished_at']).get('verified'):
        alert('BASE_BACKUP_UNVERIFIED', 'warning', 'the newest base backup has not passed restore verification')
    archiver = status.get('archiver') or {}
    if (archiver.get('last_failed_at') or 0) > (archiver.get('last_archived_at') or 0):
        alert('WAL_ARCHIVE_FAILING', 'critical', 'PostgreSQL archive_command is failing; WAL accumulates in pg_wal')
    spool = status.get('spool') or {}
    if spool.get('oldest_age_s') is not None and spool['oldest_age_s'] > policy.max_wal_archive_delay_seconds:
        alert('WAL_SHIPPING_DELAYED', 'critical', f'oldest spooled WAL waited {int(spool["oldest_age_s"])} s')
    if spool.get('free_ratio') is not None and spool['free_ratio'] < 0.1:
        alert('SPOOL_CAPACITY', 'critical', 'WAL spool disk below 10% free')
    shipper = status.get('shipper') or {}
    if (shipper.get('last_error_at') or 0) > (shipper.get('last_success_at') or 0):
        alert('WAL_UPLOAD_FAILING', 'critical', 'the last WAL upload failed: '+str(shipper.get('last_error', 'unknown')))
    repository = status.get('repository') or {}
    if isinstance(repository.get('used_gib'), (int, float)):
        free = 1-repository['used_gib']/policy.capacity_gib
        if free < policy.alert_free_ratio/2:
            alert('REPOSITORY_CAPACITY', 'critical', f'backup store {round(free*100, 1)}% free')
        elif free < policy.alert_free_ratio:
            alert('REPOSITORY_CAPACITY', 'warning', f'backup store {round(free*100, 1)}% free')
    for problem in (status.get('retention') or {}).get('problems', []):
        alert('RETENTION_BLOCKED', 'critical', problem)
    if spool.get('oldest_age_s') is not None:
        exposure = spool['oldest_age_s']
    elif shipper.get('last_success_at'):
        exposure = max(0.0, now-shipper['last_success_at'])
    else:
        exposure = None
    severity = 'critical' if any(a['severity'] == 'critical' for a in alerts) else \
        'warning' if alerts else 'ok'
    return dict(status=severity, alerts=alerts, measured_exposure_s=exposure,
                note='measured exposure since the newest WAL confirmed off-host, not a promised RPO')


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['assess', 'plan'])
    parser.add_argument('--policy', required=True, type=Path)
    parser.add_argument('--status', required=True, type=Path)
    args = parser.parse_args(argv)
    policy = Policy.from_json(json.loads(args.policy.read_text()))
    status = json.loads(args.status.read_text())
    if args.command == 'plan':
        plan = plan_retention(policy, [BaseBackup(**b) for b in status.get('base_backups', [])],
                              [WalSnapshot(s['snapshot_id'], tuple(s['files'])) for s in status.get('wal_snapshots', [])])
        print(json.dumps(dict(plan=plan, maintainer_commands=maintainer_commands(plan)), indent=2))
        return 2 if plan['problems'] else 0
    result = assess(policy, status, now=time.time())
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return {'ok': 0, 'warning': 1, 'critical': 2}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())
