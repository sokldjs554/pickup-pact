"""Backup operations rules: retention never strands a kept base backup, alerts are explicit."""
import json
from pathlib import Path

import pytest

from demo.route.ha.backup_ops import (BaseBackup, Policy, WalSnapshot, assess, maintainer_commands, plan_retention,
                                     ship_wal, spool_status, wal_position)

POLICY = Policy(retain_base_backups=2, base_backup_interval_hours=24, max_base_backup_age_hours=30,
                max_wal_archive_delay_seconds=300, capacity_gib=100, alert_free_ratio=0.2)
DAY = 86400.0


def sid(n):
    return f'{n:064x}'


def seg(position, timeline=1):
    return f'{timeline:08X}{position // 256:08X}{position % 256:08X}'


def wal(first, last, timeline=1, snapshot=None):
    return WalSnapshot(snapshot or sid(1000+first), tuple(seg(p, timeline) for p in range(first, last+1)))


def backups():
    return [BaseBackup(sid(1), 1*DAY, seg(10), True), BaseBackup(sid(2), 2*DAY, seg(20), True),
            BaseBackup(sid(3), 3*DAY, seg(30), True)]


def test_wal_names_map_to_positions_and_reject_garbage():
    assert wal_position('000000010000000000000001') == (1, 1)
    assert wal_position('0000000200000001000000FF') == (2, 511)
    for name in ['00000001000000000000010', '000000010000000000000100', '000000000000000000000001', 'x'*24]:
        with pytest.raises(ValueError):
            wal_position(name)
    with pytest.raises(ValueError):
        wal_position('000000010000000000000001', segment_size=3)


def test_policy_comes_only_from_an_append_only_rendered_policy():
    rendered = dict(retain_base_backups=7, base_backup_interval_hours=24, max_base_backup_age_hours=30,
                    max_wal_archive_delay_seconds=300, capacity_gib=200, alert_free_ratio=0.2, writer='append_only')
    assert Policy.from_json(rendered).retain_base_backups == 7
    for change in [dict(writer='read_write'), dict(retain_base_backups=1), dict(alert_free_ratio=0.9),
                   dict(max_wal_archive_delay_seconds=3600), dict(capacity_gib='200')]:
        with pytest.raises(ValueError):
            Policy.from_json(rendered | change)


def test_retention_keeps_newest_verified_backups_and_every_wal_they_need():
    snapshots = [wal(5, 9), wal(10, 19), wal(20, 29), wal(30, 40)]
    plan = plan_retention(POLICY, backups(), snapshots)
    assert plan['problems'] == []
    assert plan['keep_base_backups'] == [sid(3), sid(2)] and plan['remove_base_backups'] == [sid(1)]
    # Oldest kept backup starts at segment 20: segments 5..19 may go, 20.. must stay.
    assert plan['remove_wal_snapshots'] == [sid(1005), sid(1010)]
    assert maintainer_commands(plan) == ['restic forget '+' '.join([sid(1), sid(1005), sid(1010)]), 'restic prune']


def test_a_wal_snapshot_spanning_the_boundary_is_kept_whole():
    plan = plan_retention(POLICY, backups(), [wal(5, 21), wal(22, 40)])
    assert plan['problems'] == [] and plan['remove_wal_snapshots'] == []


@pytest.mark.parametrize('snapshots,expected', [
    ([wal(10, 24), wal(26, 40)], 'WAL gap of 1 segments after base backup'),
    ([wal(10, 15)], 'no archived WAL after base backup'),
    ([wal(10, 29), wal(30, 40, timeline=2)], 'timeline 00000002 has no history file'),
])
def test_any_gap_or_missing_history_blocks_every_removal(snapshots, expected):
    plan = plan_retention(POLICY, backups(), snapshots)
    assert any(problem.startswith(expected) for problem in plan['problems']), plan['problems']
    assert plan['remove_base_backups'] == [] and plan['remove_wal_snapshots'] == []
    assert maintainer_commands(plan) == []


def test_timeline_switch_with_history_is_continuous_and_history_is_never_removed():
    history = WalSnapshot(sid(77), ('00000002.history',))
    plan = plan_retention(POLICY, backups(), [wal(5, 25), wal(26, 40, timeline=2), history])
    assert plan['problems'] == [] and sid(77) not in plan['remove_wal_snapshots']


def test_unverified_backups_neither_count_nor_disappear():
    unverified = [BaseBackup(sid(9), 4*DAY, seg(35), False)]
    plan = plan_retention(POLICY, backups()[:1]+unverified, [wal(5, 40)])
    assert plan['problems'] == ['only 1 verified base backups; policy keeps 2']
    assert plan['remove_base_backups'] == [] and plan['unverified_left_for_review'] == [sid(9)]
    plan = plan_retention(POLICY, backups()+unverified, [wal(5, 40)])
    assert sid(9) not in plan['remove_base_backups'] + plan['keep_base_backups']


def test_maintainer_plan_needs_full_snapshot_ids():
    with pytest.raises(ValueError):
        maintainer_commands(dict(problems=[], remove_base_backups=['abc'], remove_wal_snapshots=[]))


def healthy(now):
    return dict(base_backups=[dict(finished_at=now-3600, verified=True)],
                archiver=dict(last_archived_at=now-30, last_failed_at=now-7200),
                spool=dict(files=0, oldest_age_s=None, free_ratio=.8),
                shipper=dict(last_success_at=now-40, last_error_at=now-9000), repository=dict(used_gib=40),
                retention=dict(problems=[]))


def test_healthy_status_has_no_alert_and_reports_measured_exposure():
    result = assess(POLICY, healthy(1e6), now=1e6)
    assert result['status'] == 'ok' and result['alerts'] == [] and result['measured_exposure_s'] == 40
    assert 'not a promised RPO' in result['note']


@pytest.mark.parametrize('change,code,severity', [
    (lambda s, n: s.update(base_backups=[]), 'BASE_BACKUP_MISSING', 'critical'),
    (lambda s, n: s['base_backups'][0].update(finished_at=n-31*3600), 'BASE_BACKUP_STALE', 'critical'),
    (lambda s, n: s['base_backups'].append(dict(finished_at=n, verified=False)), 'BASE_BACKUP_UNVERIFIED', 'warning'),
    (lambda s, n: s['archiver'].update(last_failed_at=n-1), 'WAL_ARCHIVE_FAILING', 'critical'),
    (lambda s, n: s['spool'].update(files=3, oldest_age_s=301), 'WAL_SHIPPING_DELAYED', 'critical'),
    (lambda s, n: s['spool'].update(free_ratio=.05), 'SPOOL_CAPACITY', 'critical'),
    (lambda s, n: s['shipper'].update(last_error_at=n-1, last_error='OSError'), 'WAL_UPLOAD_FAILING', 'critical'),
    (lambda s, n: s['repository'].update(used_gib=85), 'REPOSITORY_CAPACITY', 'warning'),
    (lambda s, n: s['repository'].update(used_gib=95), 'REPOSITORY_CAPACITY', 'critical'),
    (lambda s, n: s['retention'].update(problems=['WAL gap']), 'RETENTION_BLOCKED', 'critical'),
])
def test_each_failure_raises_a_named_alert(change, code, severity):
    now = 1e6
    status = healthy(now)
    change(status, now)
    result = assess(POLICY, status, now=now)
    assert {(a['code'], a['severity']) for a in result['alerts']} == {(code, severity)}
    assert result['status'] == severity


def test_spooled_wal_sets_the_exposure_to_its_age():
    status = healthy(1e6)
    status['spool'].update(files=2, oldest_age_s=12.5)
    assert assess(POLICY, status, now=1e6)['measured_exposure_s'] == 12.5


class FakeArchive:
    def __init__(self, *, fail=False, drop=None):
        self.fail, self.drop, self.uploaded = fail, drop, []

    def archive_directory(self, directory, *, tag):
        if self.fail:
            raise OSError('store unavailable')
        self.listing = {p.name: p.stat().st_size for p in Path(directory).iterdir()}
        if self.drop:
            self.listing.pop(self.drop)
        self.uploaded.append((tag, sorted(self.listing)))
        return sid(5)

    def list_files(self, snapshot, directory):
        return dict(self.listing)


def spooled(tmp_path):
    spool = tmp_path/'spool'
    spool.mkdir()
    for position in (1, 2):
        (spool/seg(position)).write_bytes(b'x'*(10+position))
    (spool/'.partial.000000010000000000000003.9').write_bytes(b'in progress')
    return spool


def test_shipping_releases_spool_only_after_the_snapshot_lists_every_file(tmp_path):
    spool = spooled(tmp_path)
    archive = FakeArchive()
    status = ship_wal(spool, archive, status_file=tmp_path/'status.json', clock=lambda: 50.0)
    assert archive.uploaded == [('pickup-wal', [seg(1), seg(2)])]
    assert sorted(p.name for p in spool.iterdir()) == ['.partial.000000010000000000000003.9']
    assert status['last_success_at'] == 50.0 and status['newest_uploaded'] == seg(2)
    assert json.loads((tmp_path/'status.json').read_text())['last_snapshot'] == sid(5)


@pytest.mark.parametrize('archive', [FakeArchive(fail=True), FakeArchive(drop=seg(2))])
def test_failed_or_partial_upload_keeps_every_spooled_file(tmp_path, archive):
    spool = spooled(tmp_path)
    with pytest.raises((OSError, ValueError)):
        ship_wal(spool, archive, status_file=tmp_path/'status.json', clock=lambda: 60.0)
    assert {seg(1), seg(2)} <= {p.name for p in spool.iterdir()}
    assert not [p for p in spool.iterdir() if p.name.startswith('.batch-')]
    status = json.loads((tmp_path/'status.json').read_text())
    assert status['last_error_at'] == 60.0 and status['pending_files'] == 2


def test_spool_rejects_unexpected_entries(tmp_path):
    spool = spooled(tmp_path)
    (spool/'notes.txt').write_text('x')
    with pytest.raises(ValueError):
        ship_wal(spool, FakeArchive(), status_file=tmp_path/'status.json')
    (spool/'notes.txt').unlink()
    (spool/seg(9)).symlink_to(spool/seg(1))
    with pytest.raises(ValueError):
        spool_status(spool)


def test_partial_segments_and_backup_labels_are_shipped_but_never_count_as_complete(tmp_path):
    spool = tmp_path/'spool'
    spool.mkdir()
    names = [seg(1), seg(2)+'.partial', seg(2)+'.00000028.backup', '00000002.history', seg(2, timeline=2)]
    for name in names:
        (spool/name).write_bytes(b'x')
    archive = FakeArchive()
    ship_wal(spool, archive, status_file=tmp_path/'status.json')
    assert archive.uploaded == [('pickup-wal', sorted(names))]
    # A partial segment does not close a gap.
    plan = plan_retention(POLICY, backups(), [wal(10, 24), WalSnapshot(sid(5), (seg(25)+'.partial',)), wal(26, 40)])
    assert any(problem.startswith('WAL gap') for problem in plan['problems'])
    # Labels and partials before the boundary are removable with their segment; history never is.
    old = WalSnapshot(sid(6), (seg(5), seg(6)+'.partial', seg(6)+'.00000028.backup'))
    plan = plan_retention(POLICY, backups(), [old, wal(7, 40), WalSnapshot(sid(7), ('00000001.history',))])
    assert sid(6) in plan['remove_wal_snapshots'] and sid(7) not in plan['remove_wal_snapshots']
