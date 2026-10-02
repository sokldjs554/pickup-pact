"""Disposable private CA for TLS tests and single-host rehearsals (openssl CLI).

Keys stay in the caller's private directory with mode 0600. Never use these
certificates outside a disposable test or rehearsal.
"""
from pathlib import Path
import os
import subprocess

ROLE_URI = 'urn:pickup-pact:role:'


def _run(*args, cwd):
    subprocess.run(['openssl', *args], cwd=cwd, check=True, capture_output=True, timeout=30)


class PrivateAuthority:
    def __init__(self, root: Path, name: str = 'pickup-test-ca'):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.cert, self.key = self.root/(name+'.crt'), self.root/(name+'.key')
        _run('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
             '-keyout', str(self.key), '-out', str(self.cert), '-days', '2', '-subj', '/CN='+name,
             '-addext', 'basicConstraints=critical,CA:TRUE',
             '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
             '-addext', 'subjectKeyIdentifier=hash', cwd=self.root)
        os.chmod(self.key, 0o600)

    def issue(self, name: str, *, role: str | None, dns=('localhost',), ips=('127.0.0.1',), roles=None,
              common_name: str | None = None):
        key, csr, cert = self.root/(name+'.key'), self.root/(name+'.csr'), self.root/(name+'.crt')
        names = ['DNS:'+value for value in dns] + ['IP:'+value for value in ips]
        names += ['URI:'+ROLE_URI+value for value in (roles if roles is not None else [role] if role else [])]
        extensions = self.root/(name+'.ext')
        lines = ['basicConstraints=critical,CA:FALSE', 'keyUsage=critical,digitalSignature',
                 'extendedKeyUsage=serverAuth,clientAuth', 'subjectKeyIdentifier=hash',
                 'authorityKeyIdentifier=keyid:always']
        if names:
            lines.append('subjectAltName='+','.join(names))
        extensions.write_text('\n'.join(lines)+'\n')
        _run('req', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-keyout', str(key),
             '-out', str(csr), '-subj', '/CN='+(common_name or name), cwd=self.root)
        _run('x509', '-req', '-in', str(csr), '-CA', str(self.cert), '-CAkey', str(self.key),
             '-CAcreateserial', '-out', str(cert), '-days', '2', '-extfile', str(extensions), cwd=self.root)
        os.chmod(key, 0o600)
        return str(cert), str(key)
