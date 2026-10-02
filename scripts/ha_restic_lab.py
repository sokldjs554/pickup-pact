"""Self-created HTTPS append-only repository for a single-host DR rehearsal.

No existing repository, account, host or user directory can be supplied. Secret
files and ciphertext stay outside the published evidence and are cleaned by the
owning temporary-directory context only.
"""
from __future__ import annotations
import base64
from dataclasses import replace
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
from demo.route.ha.remote_backup import ResticArchive, ResticSettings


class ResticLab:
    def __init__(self):
        if os.environ.get('PICKUP_HA_TEST') != '1':
            raise ValueError('explicit isolated test opt-in required')
        self.temp = None
        self.process = None
        self.log = None
        self.checks = []
        self.password = secrets.token_hex(32)
        self.encryption = secrets.token_hex(32)

    def __enter__(self):
        for binary in ['restic','rest-server','openssl','htpasswd']:
            if shutil.which(binary) is None:
                raise OSError('network rehearsal dependency is not installed: '+binary)
        self.temp = tempfile.TemporaryDirectory(prefix='pickup-restic-lab-')
        self.root = Path(self.temp.name)
        try:
            secret_dir = self.root/'credentials'; secret_dir.mkdir(mode=0o700)
            self.data = self.root/'encrypted-store'; self.data.mkdir(mode=0o700)
            paths = {}
            for name, value in [('password_file', self.encryption), ('username_file', 'backup'),
                                ('credential_file', self.password)]:
                paths[name] = secret_dir/name
                paths[name].write_text(value+'\n'); paths[name].chmod(0o600)
            self.cert = secret_dir/'ca.pem'; self.key = secret_dir/'server.key'
            self._certificate(self.cert, self.key)
            self.htpasswd = secret_dir/'htpasswd'
            subprocess.run(['htpasswd','-iBc',str(self.htpasswd),'backup'], input=self.password+'\n',
                capture_output=True, text=True, check=True, timeout=10)
            self.htpasswd.chmod(0o600)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0)); self.port = sock.getsockname()[1]
            self.settings = ResticSettings(repository=f'rest:https://127.0.0.1:{self.port}/backup/',
                allowed_authority=f'127.0.0.1:{self.port}', ca_file=self.cert, **paths)
            self.archive = ResticArchive(self.settings)
            self.start()
            # Only this lab initializes its newly created empty store. The public
            # adapter intentionally has no init/forget/prune operation.
            self.archive._run(['init','--repository-version','2'])
            assert (self.data/'backup/config').is_file()
            return self
        except BaseException:
            self.__exit__(None,None,None)
            raise

    @staticmethod
    def _certificate(cert, key):
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=pickup-rehearsal','-addext','subjectAltName=IP:127.0.0.1',
            '-keyout',str(key),'-out',str(cert)], capture_output=True, check=True, timeout=15)
        key.chmod(0o600)

    def _opener(self):
        return build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context(cafile=str(self.cert))))

    def _request(self, suffix, method='GET'):
        auth=base64.b64encode(('backup:'+self.password).encode()).decode()
        return Request(self.settings.repository[5:]+suffix, method=method,
                       headers={'Authorization':'Basic '+auth})

    def start(self, *, quota=1024*1024*1024):
        if self.process is not None and self.process.poll() is None:
            raise ValueError('test backup server is already running')
        self.log = (self.root/'server.log').open('ab')
        self.process = subprocess.Popen(['rest-server','--listen',f'127.0.0.1:{self.port}',
            '--path',str(self.data),'--htpasswd-file',str(self.htpasswd),'--append-only','--private-repos',
            '--tls','--tls-cert',str(self.cert),'--tls-key',str(self.key),'--tls-min-ver','1.3',
            '--max-size',str(quota)], stdout=self.log, stderr=self.log)
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            if self.process.poll() is not None:
                raise OSError('owned HTTPS backup process stopped during startup')
            try:
                with self._opener().open(self._request('config'),timeout=1): return
            except HTTPError as exc:
                if exc.code == 404: return  # New, authenticated repository, not initialized yet.
                raise OSError('backup server authentication failed during startup') from None
            except (URLError,OSError): time.sleep(.1)
        raise OSError('HTTPS backup process did not become ready')

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(5)
        if self.log is not None and not self.log.closed: self.log.close()

    def negative_controls(self, receipt, bundle, digest, cluster):
        def rejects(label, operation):
            try: operation()
            except (OSError,ValueError): self.checks.append(label)
            else: raise AssertionError(label+' was not rejected')
        bad = self.root/'credentials/wrong-password'
        bad.write_text(secrets.token_hex(32)); bad.chmod(0o600)
        rejects('wrong_encryption_key_rejected', lambda:
            ResticArchive(replace(self.settings,password_file=bad),command_timeout=5).repository_id())
        rejects('wrong_server_credential_rejected', lambda:
            ResticArchive(replace(self.settings,credential_file=bad),command_timeout=5).repository_id())
        other_ca = self.root/'credentials/other-ca.pem'
        self._certificate(other_ca,self.root/'credentials/other.key')
        rejects('untrusted_tls_certificate_rejected', lambda:
            ResticArchive(replace(self.settings,ca_file=other_ca),command_timeout=5).repository_id())
        try:
            with self._opener().open(self._request('snapshots/'+receipt['snapshot_id'],'DELETE'),timeout=5): pass
        except HTTPError as exc:
            if exc.code != 403: raise AssertionError('append-only check returned unexpected status')
            self.checks.append('server_refused_snapshot_deletion_403')
        else: raise AssertionError('backup writer could delete a pinned snapshot')
        self.stop()
        rejects('unavailable_store_did_not_issue_receipt', lambda:
            ResticArchive(self.settings,command_timeout=5).upload(bundle,
                expected_sha256=digest,expected_cluster_id=cluster))
        self.start(quota=1)
        rejects('quota_failure_did_not_issue_receipt', lambda:
            ResticArchive(self.settings,command_timeout=5).upload(bundle,
                expected_sha256=digest,expected_cluster_id=cluster))
        self.stop(); self.start()
        # Modify an owned ciphertext pack, check detection, then restore the
        # original bytes; this is not an operation on a user-owned repository.
        pack = next((self.data/'backup/data').glob('*/*'))
        original = pack.read_bytes()
        try:
            pack.write_bytes(bytes([original[0]^1])+original[1:])
            rejects('ciphertext_corruption_rejected', lambda:
                ResticArchive(self.settings,command_timeout=15)._run(['check','--read-data']))
        finally: pack.write_bytes(original)
        self.archive._run(['check','--read-data'])
        return list(self.checks)

    def __exit__(self, *_):
        try: self.stop()
        finally:
            if self.temp is not None: self.temp.cleanup()
