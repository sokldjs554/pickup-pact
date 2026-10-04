"""Disposable private CA for TLS contract tests. Keys never leave tmp_path."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))

from ha_rehearsal_pki import PrivateAuthority  # noqa: E402,F401


def tls_env(authority: PrivateAuthority, cert, key, hosts=('localhost',), generation=1):
    return {'PICKUP_INTERNAL_TLS_CA': str(authority.cert), 'PICKUP_INTERNAL_TLS_CERT': cert,
            'PICKUP_INTERNAL_TLS_KEY': key, 'PICKUP_INTERNAL_HOSTS': json.dumps(list(hosts)),
            'PICKUP_OPERATING_GENERATION': str(generation)}
