"""Storage-independent merchant transitions, shared by SQLite and PostgreSQL."""
from copy import deepcopy

TERMINAL_PHASES = {'RELEASED', 'ABORTED', 'CANCELLED', 'CLAIMED'}


def transition(row, payload, *, accepting, used, capacity):
    world, oid, gen, action, token = (payload[k] for k in ('world', 'order_id', 'generation', 'action', 'transfer_id'))
    def failure(code):
        return {'ok': False, 'code': code, 'phase': row['phase'] if row else None}
    if action in {'ADMIT', 'HOLD'}:
        if payload['reject'] or not accepting:
            return failure('MERCHANT_REJECTED')
        if row and (gen <= row['generation'] or row['phase'] not in TERMINAL_PHASES):
            return failure('STALE_GENERATION')
        if used >= capacity:
            return failure('CAPACITY_FULL')
        seat = dict(world=world, order_id=oid, generation=gen,
                    phase='RESERVED' if action == 'ADMIT' else 'HELD',
                    transfer_id=token if action == 'HOLD' else '')
    elif action == 'ABORT_TARGET' and (not row or (row['generation'] < gen and row['phase'] in TERMINAL_PHASES)):
        seat = dict(world=world, order_id=oid, generation=gen, phase='ABORTED', transfer_id=token)
    else:
        if not row or row['generation'] != gen:
            return failure('STALE_GENERATION')
        expected = {'FREEZE': ('RESERVED',), 'UNFREEZE': ('FROZEN',),
                    'RELEASE_SOURCE': ('FROZEN',), 'ACTIVATE': ('HELD',),
                    'ABORT_TARGET': ('HELD',), 'START': ('RESERVED',),
                    'READY': ('PREPARING',), 'CLAIM': ('READY', 'CLAIMED'),
                    'CANCEL': ('RESERVED', 'CANCELLED')}.get(action)
        if expected is None or row['phase'] not in expected:
            return failure('MERCHANT_STATE_CONFLICT')
        if action in {'UNFREEZE', 'RELEASE_SOURCE', 'ACTIVATE', 'ABORT_TARGET'} and row['transfer_id'] != token:
            return failure('TRANSFER_FENCE')
        seat = deepcopy(row)
        seat['phase'] = {'FREEZE': 'FROZEN', 'UNFREEZE': 'RESERVED', 'RELEASE_SOURCE': 'RELEASED',
                         'ACTIVATE': 'RESERVED', 'ABORT_TARGET': 'ABORTED', 'START': 'PREPARING',
                         'READY': 'READY', 'CLAIM': 'CLAIMED', 'CANCEL': 'CANCELLED'}[action]
        seat['transfer_id'] = token if action in {'FREEZE', 'ABORT_TARGET', 'RELEASE_SOURCE'} else ''
    return {'ok': True, 'code': 'ACCEPTED', 'seat': seat}
