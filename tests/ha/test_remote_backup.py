"""External backup boundary tests; real Restic/TLS roundtrip is a separate CI job."""
import importlib
from pathlib import Path
import pytest
from test_backup_guards import bundle
from demo.route.ha.backup import seal_bundle


def api():
    try:
        return importlib.import_module('demo.route.ha.remote_backup')
    except ModuleNotFoundError:
        pytest.fail('verified encrypted remote backup adapter is not implemented')


def settings(tmp_path, **overrides):
    files = {}
    for name, value in [('password_file', 'encryption-secret-' + 'p'*32),
                        ('username_file', 'backup'), ('credential_file', 'server-secret-' + 'c'*32),
                        ('ca_file', '-----BEGIN CERTIFICATE-----\ntest-only\n')]:
        path = tmp_path/name
        path.write_text(value)
        path.chmod(0o600)
        files[name] = path
    return api().ResticSettings(repository='rest:https://backup.example:8443/backup/pickup/',
        allowed_authority='backup.example:8443', **(files | overrides))


@pytest.mark.parametrize('url', ['http://backup.example:8443/backup/',
    'rest:http://backup.example:8443/backup/', 'rest:https://other.example/backup/',
    'rest:https://user:password@backup.example:8443/backup/',
    'rest:https://backup.example:8443/../etc/',
    'rest:https://backup.example:8443/%2e%2e/',
    'rest:https://backup.example:8443/backup/?tls=false',
    'rest:https://backup.example:8443/backup/#secret', 's3:https://backup.example/bucket'])
def test_only_explicit_https_repository_is_accepted(tmp_path, url):
    config = settings(tmp_path)
    from dataclasses import replace
    with pytest.raises(ValueError): replace(config, repository=url)


def test_secrets_do_not_appear_in_settings_repr_and_environment_is_clean(tmp_path, monkeypatch):
    config = settings(tmp_path)
    assert 'secret' not in repr(config) and 'backup.example' not in repr(config)
    monkeypatch.setenv('RESTIC_PASSWORD_COMMAND', 'do-not-run')
    monkeypatch.setenv('RESTIC_REPOSITORY', '/unrelated')
    monkeypatch.setenv('HTTPS_PROXY', 'http://unrelated')
    env = config.environment()
    assert 'RESTIC_PASSWORD_COMMAND' not in env and 'HTTPS_PROXY' not in env
    assert env['RESTIC_REPOSITORY'] == config.repository
    assert env['RESTIC_PASSWORD_FILE'] == str(config.password_file)
    assert env['RESTIC_REST_PASSWORD'].startswith('server-secret-')


@pytest.mark.parametrize('field', ['password_file', 'credential_file', 'username_file'])
def test_secret_files_must_be_private_and_not_symlinks(tmp_path, field):
    config = settings(tmp_path)
    path = getattr(config, field)
    path.chmod(0o644)
    with pytest.raises(ValueError): config.environment()
    path.chmod(0o600)
    replacement = tmp_path/'replacement'
    path.rename(replacement)
    path.symlink_to(replacement)
    with pytest.raises(ValueError): config.environment()


def test_unsealed_or_changed_backup_does_not_launch_restic(tmp_path):
    config = settings(tmp_path)
    root, meta = bundle(tmp_path)
    client = api().ResticArchive(config, executable='/missing-restic-must-not-run')
    with pytest.raises(ValueError): client.upload(root, expected_sha256='a'*64, expected_cluster_id=meta['cluster_id'])
    digest = seal_bundle(root, meta)
    (root/'base/PG_VERSION').write_text('broken')
    with pytest.raises(ValueError): client.upload(root, expected_sha256=digest, expected_cluster_id=meta['cluster_id'])


def test_credentials_inside_backup_tree_are_never_uploaded(tmp_path):
    root, meta = bundle(tmp_path)
    config = settings(root)
    digest = seal_bundle(root, meta)
    with pytest.raises(ValueError, match='secret'):
        api().ResticArchive(config, executable='/missing').upload(root,
            expected_sha256=digest, expected_cluster_id=meta['cluster_id'])


def receipt():
    return dict(format=1, repository_id='1'*64, snapshot_id='2'*64,
        bundle_sha256='3'*64, cluster_id='123456789', snapshot_path='/temporary/bundle',
        source_commit='4'*40, target_lsn='0/100010', scope='encrypted_https_transfer_not_host_ha')


@pytest.mark.parametrize('bad', [dict(snapshot_id='latest'), dict(snapshot_id='abc'),
    dict(snapshot_path='/tmp/../outside'), dict(snapshot_path='relative'),
    dict(snapshot_path='/tmp/bundle:other'), dict(bundle_sha256='0'*64),
    dict(cluster_id='999'), dict(format=True), dict(extra='unexpected')])
def test_restore_requires_exact_trusted_receipt_before_network(tmp_path, bad):
    config = settings(tmp_path)
    archive = api().ResticArchive(config, executable='/missing')
    target = tmp_path/'fresh'
    with pytest.raises(ValueError):
        archive.restore(receipt() | bad, target, expected_sha256='3'*64, expected_cluster_id='123456789')
    assert not target.exists()


def test_restore_never_targets_an_existing_directory(tmp_path):
    config = settings(tmp_path)
    target = tmp_path/'existing'; target.mkdir()
    sentinel = target/'keep'; sentinel.write_text('original')
    with pytest.raises(ValueError):
        api().ResticArchive(config, executable='/missing').restore(receipt(), target,
            expected_sha256='3'*64, expected_cluster_id='123456789')
    assert sentinel.read_text() == 'original'


def test_restore_rejects_symlinked_parent_before_network(tmp_path):
    config = settings(tmp_path)
    real = tmp_path/'real'; real.mkdir()
    alias = tmp_path/'alias'; alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        api().ResticArchive(config, executable='/missing').restore(receipt(), alias/'fresh',
            expected_sha256='3'*64, expected_cluster_id='123456789')
    assert list(real.iterdir()) == []


def test_failed_command_exposes_stage_but_not_secrets_or_endpoint(tmp_path):
    import sys
    config = settings(tmp_path)
    program = tmp_path/'failed-restic'
    program.write_text('#!' + sys.executable + '''
import os, sys, base64
credential = os.environ['RESTIC_REST_PASSWORD']
password = open(os.environ['RESTIC_PASSWORD_FILE']).read().strip()
auth = base64.b64encode((os.environ['RESTIC_REST_USERNAME']+':'+credential).encode()).decode()
print('RESTORE_FAILURE: unsupported metadata\\n'+os.environ['RESTIC_REPOSITORY']+' '+credential+' '+password+' '+auth, file=sys.stderr)
sys.exit(7)
''')
    program.chmod(0o700)
    with pytest.raises(OSError) as captured:
        api().ResticArchive(config, executable=str(program))._run(['restore','snapshot:/bundle','--target','/fresh'])
    message = str(captured.value)
    assert 'restore' in message and 'exit 7' in message
    assert 'RESTORE_FAILURE: unsupported metadata' in message
    assert 'secret-' not in message and 'backup.example' not in message
    assert 'rest:https' not in message and len(message) < 2400
