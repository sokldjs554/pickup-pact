"""Strict synthetic protocol; these are not any real card provider's rules."""
from __future__ import annotations
import hashlib
import json
import re

KEY = re.compile(r'[A-Za-z0-9:_-]{1,180}\Z')
DIGEST = re.compile(r'[a-f0-9]{64}\Z')
COMMON = {'action', 'world_id', 'order_id', 'operation_key', 'authorization_id',
          'amount_krw', 'currency', 'payment_revision', 'quote_fingerprint'}
ACTIONS = {'AUTHORIZE', 'CAPTURE', 'VOID'}


class PaymentError(ValueError):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


def key(value: object) -> str:
    if not isinstance(value, str) or not KEY.fullmatch(value):
        raise PaymentError('INVALID_IDENTIFIER', 422)
    return value


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def fingerprint(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate(command: dict) -> dict:
    if not isinstance(command, dict) or command.get('action') not in ACTIONS:
        raise PaymentError('INVALID_ACTION', 422)
    fields = COMMON | ({'card_token'} if command['action'] == 'AUTHORIZE' else set())
    if set(command) != fields:
        raise PaymentError('INVALID_FIELDS', 422)
    for field in ('world_id', 'order_id', 'operation_key', 'authorization_id'):
        key(command[field])
    if type(command['amount_krw']) is not int or not 0 < command['amount_krw'] <= 30000:
        raise PaymentError('INVALID_AMOUNT', 422)
    if type(command['payment_revision']) is not int or not 1 <= command['payment_revision'] <= 10000:
        raise PaymentError('INVALID_REVISION', 422)
    if command['currency'] != 'KRW':
        raise PaymentError('INVALID_CURRENCY', 422)
    if not isinstance(command['quote_fingerprint'], str) or not DIGEST.fullmatch(command['quote_fingerprint']):
        raise PaymentError('INVALID_QUOTE', 422)
    if command.get('card_token', 'demo-approved') not in {'demo-approved', 'demo-declined'}:
        raise PaymentError('INVALID_TEST_CARD', 422)
    return dict(command)


def bound(authorization: dict, command: dict) -> None:
    for field in ('world_id', 'order_id', 'authorization_id', 'amount_krw',
                  'currency', 'payment_revision', 'quote_fingerprint'):
        if authorization[field] != command[field]:
            raise PaymentError('AUTHORIZATION_BINDING_CONFLICT')
