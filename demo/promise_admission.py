from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from math import ceil, floor


class AdmissionDecision(StrEnum):
    ACCEPT = "ACCEPT"
    OFFER_LATER = "OFFER_LATER"
    PAUSE = "PAUSE"


@dataclass(frozen=True)
class PromiseAdmissionInput:
    backlog_units: int
    order_units: int
    service_rate_units_per_minute: float
    travel_minutes: int
    max_promise_minutes: int = 18
    safety_minutes: int = 2


@dataclass(frozen=True)
class PromiseAdmissionQuote:
    decision: AdmissionDecision
    earliest_ready_minutes: int
    quoted_minutes: int | None
    retry_after_minutes: int
    queue_cap_units: int
    reason: str

    def to_dict(self) -> dict:
        value = asdict(self)
        value["decision"] = self.decision.value
        return value


def quote_promise(value: PromiseAdmissionInput) -> PromiseAdmissionQuote:
    if value.backlog_units < 0:
        raise ValueError("backlog_units must be non-negative")
    if value.order_units < 1:
        raise ValueError("order_units must be positive")
    if value.service_rate_units_per_minute <= 0:
        raise ValueError("service_rate_units_per_minute must be positive")
    if value.travel_minutes < 0:
        raise ValueError("travel_minutes must be non-negative")
    if value.max_promise_minutes < 1:
        raise ValueError("max_promise_minutes must be positive")
    if value.safety_minutes < 0:
        raise ValueError("safety_minutes must be non-negative")

    total_units = value.backlog_units + value.order_units
    queue_cap_units = max(
        value.order_units,
        floor(value.service_rate_units_per_minute * value.max_promise_minutes),
    )
    work_minutes = ceil(total_units / value.service_rate_units_per_minute)
    earliest_ready = work_minutes + value.safety_minutes

    if total_units > queue_cap_units or earliest_ready > value.max_promise_minutes:
        excess_units = max(1, total_units - queue_cap_units)
        retry_after = max(
            1,
            ceil(excess_units / value.service_rate_units_per_minute),
        )
        return PromiseAdmissionQuote(
            decision=AdmissionDecision.PAUSE,
            earliest_ready_minutes=earliest_ready,
            quoted_minutes=None,
            retry_after_minutes=retry_after,
            queue_cap_units=queue_cap_units,
            reason="REMOTE_QUEUE_CAP",
        )

    quoted = max(value.travel_minutes, earliest_ready)
    if earliest_ready <= value.travel_minutes + 1:
        return PromiseAdmissionQuote(
            decision=AdmissionDecision.ACCEPT,
            earliest_ready_minutes=earliest_ready,
            quoted_minutes=quoted,
            retry_after_minutes=0,
            queue_cap_units=queue_cap_units,
            reason="ARRIVAL_ALIGNED",
        )

    return PromiseAdmissionQuote(
        decision=AdmissionDecision.OFFER_LATER,
        earliest_ready_minutes=earliest_ready,
        quoted_minutes=quoted,
        retry_after_minutes=0,
        queue_cap_units=queue_cap_units,
        reason="LATER_PROMISE_REQUIRED",
    )
