"""Disposable private CA for TLS contract tests. Keys never leave tmp_path."""
from pathlib import Path
import os
import subprocess


def _run(*args, cwd):
    subprocess.run(['openssl', *args], cwd=cwd, check=True, capture_output=True, timeout=30)


class PrivateAuthority:
    def __init__(self, root: Path, name: str = 'pickup-test-ca'):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cert, self.key = self.root/(name+'.crt'), self.root/(name+'.key')
        _run('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
             '-keyout', str(self.key), '-out', str(self.cert), '-days', '2', '-subj', '/CN='+name,
             '-addext', 'basicConstraints=critical,CA:TRUE',
             '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
             '-addext', 'subjectKeyIdentifier=hash', cwd=self.root)
        os.chmod(self.key, 0o600)

    def issue(self, name: str, *, role: str | None, dns=('localhost',), ips=('127.0.0.1',), roles=None):
        key, csr, cert = self.root/(name+'.key'), self.root/(name+'.csr'), self.root/(name+'.crt')
        names = ['DNS:'+value for value in dns] + ['IP:'+value for value in ips]
        names += ['URI:urn:pickup-pact:role:'+value for value in (roles if roles is not None else [role] if role else [])]
        extensions = self.root/(name+'.ext')
        extensions.write_text('\n'.join([
            'basicConstraints=critical,CA:FALSE', 'keyUsage=critical,digitalSignature',
            'extendedKeyUsage=serverAuth,clientAuth', 'subjectKeyIdentifier=hash',
            'authorityKeyIdentifier=keyid:always', 'subjectAltName='+','.join(names)])+'\n')
        _run('req', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-keyout', str(key),
             '-out', str(csr), '-subj', '/CN='+name, cwd=self.root)
        _run('x509', '-req', '-in', str(csr), '-CA', str(self.cert), '-CAkey', str(self.key),
             '-CAcreateserial', '-out', str(cert), '-days', '2', '-extfile', str(extensions), cwd=self.root)
        os.chmod(key, 0o600)
        return str(cert), str(key)


def tls_env(authority: PrivateAuthority, cert, key, hosts=('localhost',), generation=1):
    import json
    return {'PICKUP_INTERNAL_TLS_CA': str(authority.cert), 'PICKUP_INTERNAL_TLS_CERT': cert,
            'PICKUP_INTERNAL_TLS_KEY': key, 'PICKUP_INTERNAL_HOSTS': json.dumps(list(hosts)),
            'PICKUP_OPERATING_GENERATION': str(generation)}
