"""Evidence-bound financial repair planning.

Full cancellation may reverse every still-open order settlement/reward posting.
Partial cancellation is executable only when the cancellation fact explicitly
allocates an amount to a concrete source posting. The planner never invents a
cross-posting allocation.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Iterable

from .models import EventEnvelope, FinancialRepairAction

POSTING_RULES = {
    "SettlementPosted": {
        "reverse_type": "SettlementReversed",
        "repair": "REVERSE_SETTLEMENT",
        "unit": "KRW",
    },
    "RewardGranted": {
        "reverse_type": "RewardReversed",
        "repair": "REVERSE_REWARD",
        "unit": "PTS",
    },
}


def accounting_amount(value: object) -> int:
    if isinstance(value, bool) or value is None or len(str(value)) > 60:
        raise ValueError("amount must be a positive integral accounting quantity")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid accounting amount") from exc
    if not number.is_finite() or not 0 < number <= 1_000_000_000_000:
        raise ValueError("accounting amount is outside supported bounds")
    if number != number.to_integral_value():
        raise ValueError("fractional accounting quantities are not supported")
    return int(number)


def _unit(event: EventEnvelope, expected: str) -> str:
    for key in ("currency", "unit"):
        value = event.payload.get(key)
        if value is not None and value != expected:
            raise ValueError("invalid_financial_unit")
    return expected


def _reversal_target(event: EventEnvelope) -> str | None:
    source = event.payload.get("source_event_id")
    if source is not None:
        return str(source)
    return event.causation_id


def _posting_balances(
    events: list[EventEnvelope],
) -> tuple[dict[str, tuple[EventEnvelope, int, str, str]], list[str]]:
    blockers: list[str] = []
    balances: dict[str, tuple[EventEnvelope, int, str, str]] = {}

    for posting_type, rule in POSTING_RULES.items():
        posts = [event for event in events if event.event_type == posting_type]
        reversals = [event for event in events if event.event_type == rule["reverse_type"]]
        post_ids = {post.event_id for post in posts}

        for reversal in reversals:
            target = _reversal_target(reversal)
            if target is None:
                blockers.append("ambiguous_reversal_attribution")
            elif target not in post_ids:
                blockers.append("missing_financial_posting")

        for post in posts:
            try:
                amount = accounting_amount(post.payload.get("amount"))
                unit = _unit(post, rule["unit"])
            except ValueError as exc:
                blockers.append(str(exc))
                continue

            if posting_type == "RewardGranted":
                source = post.payload.get("source", "order_reward")
                # Goodwill/guarantee compensation is not an order-purchase
                # accrual and is therefore not automatically revoked.
                if source not in ("order_reward", "order_purchase"):
                    continue

            matched = [rev for rev in reversals if _reversal_target(rev) == post.event_id]
            reversed_amount = 0
            for reversal in matched:
                try:
                    _unit(reversal, unit)
                    raw_amount = reversal.payload.get("amount")
                    if raw_amount is None:
                        # Backward-compatible full reversal is safe only when
                        # the event explicitly targets this one posting.
                        reverse_amount = amount
                    else:
                        reverse_amount = accounting_amount(raw_amount)
                    reversed_amount += reverse_amount
                except ValueError as exc:
                    reason = str(exc)
                    blockers.append(
                        "invalid_reversal_unit" if reason == "invalid_financial_unit"
                        else "ambiguous_reversal_attribution"
                    )

            if reversed_amount > amount:
                blockers.append("financial_over_reversal")
                continue
            balances[post.event_id] = (
                post,
                amount - reversed_amount,
                unit,
                rule["repair"],
            )

    return balances, list(dict.fromkeys(blockers))


def _allocation_rows(cancel: EventEnvelope) -> list[dict]:
    rows = cancel.payload.get("allocations")
    if not isinstance(rows, list) or not rows:
        raise ValueError("partial_cancellation_allocation_required")
    return rows


def plan_financial_repairs(
    events: list[EventEnvelope],
    *,
    cancelled: bool,
) -> tuple[list[FinancialRepairAction], list[str]]:
    blockers: list[str] = []
    if any(event.schema_version != 1 for event in events):
        blockers.append("unsupported_event_schema")
    if not cancelled:
        return [], blockers

    cancels = [event for event in events if event.event_type == "CommitmentCancelled"]
    if not cancels:
        return [], blockers
    if len(cancels) != 1:
        blockers.append("multiple_cancellation_facts")
        return [], list(dict.fromkeys(blockers))

    balances, balance_blockers = _posting_balances(events)
    blockers.extend(balance_blockers)
    if blockers:
        return [], list(dict.fromkeys(blockers))

    cancel = cancels[0]
    scope = str(cancel.payload.get("scope", "FULL")).upper()
    actions: list[FinancialRepairAction] = []

    if scope == "FULL":
        for target_id, (_post, remaining, unit, repair) in sorted(balances.items()):
            if remaining <= 0:
                continue
            actions.append(
                FinancialRepairAction(
                    target_event_id=target_id,
                    repair=repair,
                    amount=remaining,
                    unit=unit,
                )
            )
        return actions, []

    if scope != "PARTIAL":
        return [], ["unsupported_cancellation_scope"]

    try:
        allocations = _allocation_rows(cancel)
    except ValueError as exc:
        return [], [str(exc)]

    seen_targets: set[str] = set()
    krw_total = 0
    pts_total = 0
    for allocation in allocations:
        if not isinstance(allocation, dict):
            blockers.append("invalid_partial_allocation")
            continue
        target = str(allocation.get("target_event_id", ""))
        if not target or target in seen_targets:
            blockers.append("invalid_partial_allocation")
            continue
        seen_targets.add(target)
        balance = balances.get(target)
        if balance is None:
            blockers.append("unknown_partial_allocation_target")
            continue
        _post, remaining, expected_unit, repair = balance
        try:
            amount = accounting_amount(allocation.get("amount"))
        except ValueError:
            blockers.append("invalid_partial_allocation")
            continue
        unit = allocation.get("unit")
        if unit != expected_unit:
            blockers.append("invalid_partial_allocation_unit")
            continue
        if amount > remaining:
            blockers.append("partial_allocation_exceeds_open_balance")
            continue
        actions.append(
            FinancialRepairAction(
                target_event_id=target,
                repair=repair,
                amount=amount,
                unit=expected_unit,
            )
        )
        if expected_unit == "KRW":
            krw_total += amount
        else:
            pts_total += amount

    if "amount" in cancel.payload:
        try:
            if krw_total != accounting_amount(cancel.payload["amount"]):
                blockers.append("partial_cancellation_amount_mismatch")
        except ValueError:
            blockers.append("invalid_partial_cancellation_amount")
    if "reward_points" in cancel.payload:
        try:
            if pts_total != accounting_amount(cancel.payload["reward_points"]):
                blockers.append("partial_reward_amount_mismatch")
        except ValueError:
            blockers.append("invalid_partial_reward_amount")

    if blockers:
        return [], list(dict.fromkeys(blockers))
    return actions, []


def scope_blockers(events: list[EventEnvelope], repairs: set[str]) -> list[str]:
    """Compatibility wrapper for older callers/tests."""
    cancelled = any(event.event_type == "CommitmentCancelled" for event in events)
    _actions, blockers = plan_financial_repairs(events, cancelled=cancelled)
    return blockers
