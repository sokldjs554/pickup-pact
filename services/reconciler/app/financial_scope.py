"""Conservative scope for full-order, single-posting correction proposals.

Partial refunds and attribution across multiple postings require another policy;
this module deliberately blocks them instead of guessing an amount.
"""
from decimal import Decimal, InvalidOperation
from .models import EventEnvelope

FINANCIAL_TYPES = {'SettlementPosted': 'KRW', 'RewardGranted': 'PTS'}


def accounting_amount(value: object) -> int:
    if isinstance(value, bool) or value is None or len(str(value)) > 60:
        raise ValueError('amount must be a positive integral accounting quantity')
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('invalid accounting amount') from exc
    if not number.is_finite() or not 0 < number <= 1_000_000_000_000:
        raise ValueError('accounting amount is outside supported bounds')
    if number != number.to_integral_value():
        raise ValueError('fractional accounting quantities are not supported')
    return int(number)


def scope_blockers(events: list[EventEnvelope], repairs: set[str]) -> list[str]:
    blockers = []
    if any(e.schema_version != 1 for e in events):
        blockers.append('unsupported_event_schema')
    cancels = [e for e in events if e.event_type == 'CommitmentCancelled']
    if not cancels:
        return blockers
    if any(str(e.payload.get('scope', 'FULL')).upper() != 'FULL'
           or e.payload.get('partial') is True
           or e.payload.get('partial_refund') is True
           or e.payload.get('items') or e.payload.get('line_items') for e in cancels):
        blockers.append('unsupported_partial_cancellation')
    for kind, repair, reverse in (
        ('SettlementPosted', 'REVERSE_SETTLEMENT', 'SettlementReversed'),
        ('RewardGranted', 'REVERSE_REWARD', 'RewardReversed'),
    ):
        postings = [e for e in events if e.event_type == kind]
        reversals = [e for e in events if e.event_type == reverse]
        if len(postings) > 1 or len(reversals) > 1:
            blockers.append('multiple_financial_postings')
        if len(postings) == 1 and reversals:
            post = postings[0]
            for rev in reversals:
                try:
                    if ('amount' in rev.payload and
                        accounting_amount(rev.payload['amount']) != accounting_amount(post.payload.get('amount'))):
                        blockers.append('ambiguous_reversal_attribution')
                    if rev.causation_id and rev.causation_id != post.event_id:
                        blockers.append('ambiguous_reversal_attribution')
                except ValueError:
                    blockers.append('ambiguous_reversal_attribution')
        if repair not in repairs:
            continue
        for post in postings:
            try:
                accounting_amount(post.payload.get('amount'))
            except ValueError:
                blockers.append('invalid_financial_amount')
            expected = FINANCIAL_TYPES[kind]
            if any(post.payload.get(key, expected) != expected for key in ('currency', 'unit')):
                blockers.append('invalid_financial_unit')
            if kind == 'RewardGranted' and post.payload.get('source', 'order_reward') not in ('order_reward', 'order_purchase'):
                blockers.append('unsupported_reward_policy')
    return list(dict.fromkeys(blockers))
