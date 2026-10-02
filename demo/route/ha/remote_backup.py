"""Pinned, encrypted Restic snapshots over explicitly trusted HTTPS.

This adapter does not initialize repositories, delete snapshots, relax TLS,
provision hosts or claim that a transfer is a completed database recovery.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4
from .backup import verify_bundle, _lsn

HEX = re.compile(r'^[0-9a-f]{64}$')
SCOPE = 'encrypted_https_transfer_not_host_ha'
RECEIPT_FIELDS = {'format','repository_id','snapshot_id','bundle_sha256','cluster_id',
                  'snapshot_path','source_commit','target_lsn','scope'}


def _private_file(path: Path) -> str:
    path = Path(path)
    if path.is_symlink():
        raise ValueError('secret file must not be a symlink')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError('secret file must be a small private regular file')
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as source:
            text = source.read(4097).rstrip('\r\n')
        if not text or '\n' in text or '\r' in text or '\0' in text:
            raise ValueError('secret must be one nonempty line')
        return text
    finally:
        os.close(fd)


def _fresh_target(path: Path) -> Path:
    path = Path(path).absolute()
    if path.exists() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('restore needs a new destination without symlinked parents')
    if not path.parent.is_dir() or '..' in path.parts:
        raise ValueError('restore parent must already exist')
    return path


def validate_receipt(receipt: dict, *, expected_sha256: str, expected_cluster_id: str) -> None:
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS:
        raise ValueError('unsupported backup receipt')
    if type(receipt['format']) is not int or receipt['format'] != 1 or receipt['scope'] != SCOPE:
        raise ValueError('unsupported backup receipt format')
    for name in ['repository_id','snapshot_id','bundle_sha256']:
        if not isinstance(receipt[name], str) or not HEX.fullmatch(receipt[name]):
            raise ValueError('full immutable snapshot and repository identities required')
    if receipt['bundle_sha256'] != expected_sha256 or receipt['cluster_id'] != expected_cluster_id:
        raise ValueError('receipt differs from independently trusted backup identity')
    if not isinstance(expected_cluster_id, str) or not re.fullmatch(r'[0-9]{1,20}', expected_cluster_id):
        raise ValueError('invalid cluster identity')
    if not isinstance(receipt['source_commit'], str) or not re.fullmatch('[0-9a-f]{40}', receipt['source_commit']):
        raise ValueError('source identity required')
    path = receipt['snapshot_path']
    if (not isinstance(path, str) or not path.startswith('/') or path == '/'
            or str(PurePosixPath(path)) != path or '..' in PurePosixPath(path).parts
            or any(c in path for c in ':\n\r\0\\')):
        raise ValueError('one canonical snapshot root required')
    _lsn(receipt['target_lsn'])


@dataclass(frozen=True)
class ResticSettings:
    repository: str = field(repr=False)
    allowed_authority: str = field(repr=False)
    password_file: Path = field(repr=False)
    username_file: Path = field(repr=False)
    credential_file: Path = field(repr=False)
    ca_file: Path = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.repository, str) or not self.repository.startswith('rest:https://'):
            raise ValueError('only explicitly configured HTTPS REST storage is accepted')
        parsed = urlsplit(self.repository[5:])
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.netloc != self.allowed_authority
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or not re.fullmatch(r'/[A-Za-z0-9_/-]+/', parsed.path)
                or '//' in parsed.path or '..' in parsed.path):
            raise ValueError('unexpected backup endpoint or repository path')
        try:
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError('invalid backup port')
        except ValueError:
            raise ValueError('invalid backup endpoint') from None
        for name in ['password_file','username_file','credential_file','ca_file']:
            object.__setattr__(self, name, Path(getattr(self, name)).absolute())

    def environment(self) -> dict[str, str]:
        password = _private_file(self.password_file)
        username = _private_file(self.username_file)
        credential = _private_file(self.credential_file)
        if len(password) < 32 or len(credential) < 24 or not re.fullmatch('[A-Za-z0-9_-]{1,80}', username):
            raise ValueError('separate strong encryption and server credentials required')
        if self.ca_file.is_symlink() or not self.ca_file.is_file():
            raise ValueError('explicit trusted CA file required')
        # Never inherit password commands, another repository, proxy settings or
        # command-line options from the caller's Restic environment.
        env = {k: os.environ[k] for k in ['PATH','HOME','TMPDIR','SYSTEMROOT'] if k in os.environ}
        env.update(RESTIC_REPOSITORY=self.repository, RESTIC_PASSWORD_FILE=str(self.password_file),
                   RESTIC_REST_USERNAME=username, RESTIC_REST_PASSWORD=credential)
        return env


class ResticArchive:
    def __init__(self, settings: ResticSettings, *, executable: str = 'restic', command_timeout: int = 600):
        if type(command_timeout) is not int or not 1 <= command_timeout <= 600:
            raise ValueError("bounded backup command timeout required")
        self.command_timeout = command_timeout
        self.settings, self.executable = settings, executable

    def _run(self, args: list[str], *, cwd: Path | None = None) -> str:
        environment = self.settings.environment()
        encryption_password = _private_file(self.settings.password_file)
        try:
            result = subprocess.run([self.executable, '--no-cache', '--cacert', str(self.settings.ca_file), *args],
                cwd=cwd, env=environment, capture_output=True, text=True, timeout=self.command_timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise OSError('encrypted backup command did not complete') from None
        if result.returncode != 0:
            # Preserve the actual failure, but never publish raw authentication,
            # repository addresses, key locations or terminal control sequences.
            basic = base64.b64encode((environment['RESTIC_REST_USERNAME'] + ':' +
                                    environment['RESTIC_REST_PASSWORD']).encode()).decode()
            hidden = [encryption_password, environment['RESTIC_REST_PASSWORD'], basic,
                      self.settings.repository, self.settings.repository[5:], self.settings.allowed_authority,
                      *[str(getattr(self.settings, name)) for name in
                        ['password_file','credential_file','username_file','ca_file']]]
            detail = result.stderr
            for value in sorted(hidden, key=len, reverse=True):
                detail = detail.replace(value, '[redacted]')
            detail = re.sub(r'(?:https?|rest):[^\s]+', '[endpoint]', detail)
            detail = re.sub(r'[\x00-\x1f\x7f]', ' ', detail)[-1600:]
            operation = args[0] if args and re.fullmatch(r'[a-z-]+', args[0]) else 'operation'
            raise OSError(f'encrypted backup {operation} failed (exit {result.returncode}): {detail}')
        if len(result.stdout) > 16*1024*1024:
            raise ValueError('backup command output exceeds metadata bound')
        return result.stdout

    def repository_id(self) -> str:
        data = json.loads(self._run(['cat', 'config']))
        if not isinstance(data, dict) or not isinstance(data.get('id'), str) or not HEX.fullmatch(data['id']):
            raise ValueError('repository identity not confirmed')
        return data['id']

    def upload(self, bundle: Path, *, expected_sha256: str, expected_cluster_id: str) -> dict:
        root = Path(bundle).absolute()
        report = verify_bundle(root, expected_sha256=expected_sha256, expected_cluster_id=expected_cluster_id)
        for secret in [self.settings.password_file, self.settings.username_file, self.settings.credential_file]:
            if secret.resolve().is_relative_to(root.resolve()):
                raise ValueError('secret files must remain outside the backup')
        if root != root.resolve() or any(c in str(root) for c in ':\n\r\0\\'):
            raise ValueError('canonical backup directory required')
        repository_id = self.repository_id()
        tag = 'pickup-' + uuid4().hex
        # Absolute source ensures the snapshot tree matches its absolute metadata.
        self._run(['backup', '--json', '--host', 'pickup-backup', '--tag', tag, '--', str(root)])
        snapshots = json.loads(self._run(['snapshots', '--json', '--tag', tag]))
        if not isinstance(snapshots, list) or len(snapshots) != 1 or snapshots[0].get('paths') != [str(root)]:
            raise ValueError('new backup snapshot was not uniquely confirmed')
        receipt = dict(format=1, repository_id=repository_id, snapshot_id=snapshots[0]['id'],
            bundle_sha256=expected_sha256, cluster_id=expected_cluster_id, snapshot_path=str(root),
            source_commit=report['source_commit'], target_lsn=report['target_lsn'], scope=SCOPE)
        validate_receipt(receipt, expected_sha256=expected_sha256, expected_cluster_id=expected_cluster_id)
        verify_bundle(root, expected_sha256=expected_sha256, expected_cluster_id=expected_cluster_id)
        self._run(['check', '--read-data'])
        # A checksum-valid Restic snapshot could still contain a moving source.
        # Read it back and compare the original seal before publishing a receipt.
        with tempfile.TemporaryDirectory(prefix='pickup-remote-readback-') as temporary:
            self.restore(receipt, Path(temporary)/'bundle', expected_sha256=expected_sha256,
                         expected_cluster_id=expected_cluster_id)
        return receipt

    def archive_directory(self, directory: Path, *, tag: str) -> str:
        """Append one snapshot of a canonical directory (e.g. a WAL batch) and return its full ID.

        Uses only the append-only writer credential; it neither initializes,
        removes nor rewrites snapshots.
        """
        root = Path(directory).absolute()
        if root != root.resolve() or any(c in str(root) for c in ':\n\r\0\\') or not root.is_dir():
            raise ValueError('canonical directory required')
        if not isinstance(tag, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', tag):
            raise ValueError('bounded snapshot tag required')
        unique = tag + '-' + uuid4().hex
        self._run(['backup', '--json', '--host', 'pickup-backup', '--tag', tag, '--tag', unique, '--', str(root)])
        snapshots = json.loads(self._run(['snapshots', '--json', '--tag', unique]))
        if (not isinstance(snapshots, list) or len(snapshots) != 1 or snapshots[0].get('paths') != [str(root)]
                or not HEX.fullmatch(str(snapshots[0].get('id', '')))):
            raise ValueError('new snapshot was not uniquely confirmed')
        return snapshots[0]['id']

    def list_files(self, snapshot_id: str, directory: str) -> dict[str, int]:
        """Names and sizes of the regular files directly inside `directory` of a snapshot."""
        if not isinstance(snapshot_id, str) or not HEX.fullmatch(snapshot_id):
            raise ValueError('full snapshot identity required')
        files = {}
        for line in self._run(['ls', '--json', snapshot_id]).splitlines():
            entry = json.loads(line)
            if entry.get('struct_type') == 'node' and entry.get('type') == 'file':
                path = PurePosixPath(entry.get('path', ''))
                if str(path.parent) == directory and type(entry.get('size')) is int:
                    files[path.name] = entry['size']
        return files

    def restore(self, receipt: dict, destination: Path, *, expected_sha256: str, expected_cluster_id: str) -> dict:
        validate_receipt(receipt, expected_sha256=expected_sha256, expected_cluster_id=expected_cluster_id)
        target = _fresh_target(destination)
        if self.repository_id() != receipt['repository_id']:
            raise ValueError('wrong encrypted repository')
        snapshots = json.loads(self._run(['snapshots', '--json', receipt['snapshot_id']]))
        if (not isinstance(snapshots, list) or len(snapshots) != 1
                or snapshots[0].get('id') != receipt['snapshot_id']
                or snapshots[0].get('paths') != [receipt['snapshot_path']]):
            raise ValueError('pinned snapshot metadata differs from receipt')
        # Only this newly created destination is written. A failed verification
        # leaves it for inspection; it never replaces an operating database.
        target.mkdir(mode=0o700, exist_ok=False)
        self._run(['restore', receipt['snapshot_id'] + ':' + receipt['snapshot_path'],
                   '--target', str(target), '--verify'])
        report = verify_bundle(target, expected_sha256=expected_sha256, expected_cluster_id=expected_cluster_id)
        if report['source_commit'] != receipt['source_commit'] or report['target_lsn'] != receipt['target_lsn']:
            raise ValueError('restored checkpoint differs from receipt')
        return report
