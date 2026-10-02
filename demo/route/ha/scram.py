"""Client-side SCRAM-SHA-256 verifiers (RFC 7677 / PostgreSQL format).

``ALTER ROLE ... PASSWORD '<verifier>'`` stores the verifier as is, so the clear
password never reaches the server, its logs or ``pg_stat_activity``.

    printf '%s' "$PASSWORD" | python -m demo.route.ha.scram
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import os
import sys


def verifier(password: str, *, iterations: int = 4096, salt: bytes | None = None) -> str:
    # ASCII only: PostgreSQL applies SASLprep, which leaves printable ASCII unchanged.
    if not isinstance(password, str) or len(password) < 24 or not password.isascii() or not password.isprintable():
        raise ValueError('a generated printable ASCII password of at least 24 characters is required')
    if type(iterations) is not int or iterations < 4096:
        raise ValueError('at least 4096 iterations required')
    salt = os.urandom(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
    client_key = hmac.new(salted, b'Client Key', 'sha256').digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b'Server Key', 'sha256').digest()
    encode = lambda value: base64.b64encode(value).decode()  # noqa: E731
    return f'SCRAM-SHA-256${iterations}:{encode(salt)}${encode(stored_key)}:{encode(server_key)}'


if __name__ == '__main__':
    print(verifier(sys.stdin.read().rstrip('\n')))
