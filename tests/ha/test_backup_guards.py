"""Backup guards inspect original bytes; they do not claim a PostgreSQL restore."""
import importlib
import json
from pathlib import Path
import pytest


def api():
    try:return importlib.import_module('demo.route.ha.backup')
    except ModuleNotFoundError:pytest.fail('verified backup boundary is missing')


def bundle(tmp_path):
    root=tmp_path/'bundle';(root/'base/global').mkdir(parents=True);(root/'wal').mkdir()
    (root/'base/PG_VERSION').write_text('17\n');(root/'base/backup_manifest').write_text('{}')
    (root/'base/global/pg_control').write_bytes(b'unit-test-not-a-live-database')
    wal='000000010000000000000001';(root/'wal'/wal).write_bytes(bytes(1024*1024))
    metadata=dict(cluster_id='123456789',source_commit='a'*40,timeline=1,wal_segment_size=1024*1024,
                  start_lsn='0/100000',target_lsn='0/100010',scope='development_single_host')
    return root,metadata


def test_sealed_bytes_validate_only_against_trusted_digest(tmp_path):
    b=api();root,meta=bundle(tmp_path);digest=b.seal_bundle(root,meta)
    report=b.verify_bundle(root,expected_sha256=digest,expected_cluster_id='123456789')
    assert report['cluster_id']=='123456789' and report['version']==1
    assert report['required_wal']==['000000010000000000000001']
    with pytest.raises(ValueError):b.verify_bundle(root,expected_sha256='f'*64,expected_cluster_id='123456789')
    with pytest.raises(ValueError):b.verify_bundle(root,expected_sha256=digest,expected_cluster_id='other')


def test_changed_file_is_rejected_before_restore(tmp_path):
    b=api();root,meta=bundle(tmp_path);sha=b.seal_bundle(root,meta)
    (root/'base/PG_VERSION').write_text('18\n')
    with pytest.raises(ValueError):b.verify_bundle(root,expected_sha256=sha,expected_cluster_id=meta['cluster_id'])


def test_missing_wal_and_truncated_wal_are_rejected(tmp_path):
    b=api();root,meta=bundle(tmp_path);sha=b.seal_bundle(root,meta)
    path=next((root/'wal').iterdir());path.unlink()
    with pytest.raises(ValueError):b.verify_bundle(root,expected_sha256=sha,expected_cluster_id=meta['cluster_id'])
    path.write_bytes(b'incomplete upload')
    with pytest.raises(ValueError):b.seal_bundle(root,meta)


def test_symlink_is_not_a_backup_file(tmp_path):
    b=api();root,meta=bundle(tmp_path)
    (root/'base/link').symlink_to(tmp_path/'outside')
    with pytest.raises(ValueError):b.seal_bundle(root,meta)


def test_directory_symlink_is_not_followed(tmp_path):
    b=api();root,meta=bundle(tmp_path)
    (root/'base/linked-dir').symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(ValueError):b.seal_bundle(root,meta)


def test_added_file_after_sealing_is_rejected(tmp_path):
    b=api();root,meta=bundle(tmp_path);sha=b.seal_bundle(root,meta)
    (root/'base/unknown').write_text('not in original backup')
    with pytest.raises(ValueError):b.verify_bundle(root,expected_sha256=sha,expected_cluster_id=meta['cluster_id'])


def test_unsealed_or_wrong_version_is_not_restorable(tmp_path):
    b=api();root,meta=bundle(tmp_path)
    with pytest.raises(ValueError):b.verify_bundle(root,expected_sha256='a'*64,expected_cluster_id=meta['cluster_id'])
    (root/'base/PG_VERSION').write_text('16')
    with pytest.raises(ValueError):b.seal_bundle(root,meta)


def test_wal_range_crosses_log_boundary_and_is_bounded():
    b=api();names=b.required_wal('0/FFF00000','1/100001',timeline=2,segment_bytes=1024*1024)
    assert names==['000000020000000000000FFF','000000020000000100000000','000000020000000100000001']
    for start,end,segment in [('x','0/1',1024*1024),('0/2','0/1',1024*1024),('0/1','F/0',16),('0/1','FFFFFFFF/FFFFFFFF',1024*1024)]:
        with pytest.raises(ValueError):b.required_wal(start,end,timeline=1,segment_bytes=segment)


def test_delete_guard_never_accepts_non_test_or_changed_resource():
    b=api();run='1234567890abcdef'
    labels={'pickup.rehearsal':run,'pickup.role':'source'}
    assert b.assert_test_resource('pickup-dr-'+run+'-source',labels,run,'source') is None
    for name,lab,r,role in [('main',labels,run,'source'),('pickup-dr-'+run+'-source',{},run,'source'),
                         ('pickup-dr-'+run+'-source',labels,'wrong','source')]:
        with pytest.raises(ValueError):b.assert_test_resource(name,lab,r,role)


def test_bundle_scope_must_be_a_known_label():
    import pytest
    from demo.route.ha import backup
    meta = dict(cluster_id='1', source_commit='a'*40, timeline=1, wal_segment_size=16*1024*1024,
                start_lsn='0/100000', target_lsn='0/100010', scope='development_single_host')
    assert backup._metadata(meta)
    assert backup._metadata(dict(meta, scope='oracle_single_ad_fault_domains'))
    with pytest.raises(ValueError):
        backup._metadata(dict(meta, scope='production'))
