"""Public response contracts. Open event payloads remain explicitly extensible.

Core money/order/recovery fields are typed. Additive metadata is preserved for
old recorded sessions; exclude_unset avoids silently inventing legacy fields.
"""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

Money=Annotated[StrictInt,Field(ge=0)]
class Extensible(BaseModel):
    model_config=ConfigDict(extra='allow')

class FundingBreakdown(Extensible):
    mode: Literal['synthetic']
    unit: Literal['KRW']
    merchant_store_id: str
    gross: Money
    customer_cash: Money
    merchant_coupon: Money
    platform_coupon: Money
    platform_points: Money
    merchant_receivable: Money
    platform_support: Money
    liability_policy: str
    @model_validator(mode='after')
    def balanced(self):
        if self.customer_cash+self.platform_coupon+self.platform_points!=self.merchant_receivable:
            raise ValueError('merchant funding does not balance')
        if self.merchant_receivable+self.merchant_coupon!=self.gross:
            raise ValueError('merchant discount does not match gross')
        if self.platform_support!=self.platform_coupon+self.platform_points:
            raise ValueError('platform contribution does not balance')
        return self

class ProductSpec(Extensible):
    drink: Literal['americano','latte']
    milk: Literal['regular','oat']
    decaf: StrictBool
    volume_ml: int|None
    temperature: str|None

class TransferTerms(Extensible):
    version: Literal['partner-transfer-v1']
    terms_id: str
    source_store_id: str|None
    target_store_id: str
    eligible: StrictBool
    reason_codes: list[str]
    source_product: ProductSpec|None
    target_product: ProductSpec
    funding: FundingBreakdown
    notice: str
    merchant_rule: str
    confirmation: str

class Pricing(Extensible):
    gross: Money
    coupon_id: str|None
    coupon_discount: Money
    points_used: Money
    cash_due: Money
    points_to_earn: Money
    unit: Literal['KRW']
    @model_validator(mode='after')
    def balanced(self):
        if self.cash_due+self.coupon_discount+self.points_used!=self.gross:
            raise ValueError('pricing does not balance')
        return self

class Plan(Extensible):
    store_id: str
    name: str
    price: Money
    pricing: Pricing
    quote_id: str
    feasible: StrictBool
    reasons: list[str]
    reason_labels: list[str]
    arrival_at: int
    ready_at: int
    transfer_terms: TransferTerms|None=None

class Order(Extensible):
    id: str
    store_id: str
    store_name: str
    state: Literal['RESERVED','PREPARING','READY','PICKED_UP','CANCELLED']
    price: Money
    pickup_code: str|None
    pricing: Pricing|None=None
    commercial_terms: TransferTerms|None=None

class Wallet(Extensible):
    scope: Literal['journey_demo']
    balance: Money
    held_points: Money
    held_coupon: str|None
    spent: Money
    earned: Money
    available_points: Money
    used_coupons: list[str]

class Event(Extensible):
    seq: Annotated[StrictInt,Field(ge=1)]
    type: str
    title: str
    at: int
    data: dict

class Receipt(Extensible):
    order_id: str|None
    capture_count: Money
    authorization_count: Money
    net_paid: Money
    payable: Money
    points_spent: Money
    points_earned: Money
    transfers: list[Event]
    settlement: FundingBreakdown|None=None

class Recovery(Extensible):
    created_at: float
    next_retry_at: float|None
    deadline_at: float
    retry_count: Money
    state: Literal['SCHEDULED','FINISHED','REVIEW_REQUIRED']
    last_attempt_at: float|None

class Handoff(Extensible):
    id: str
    action: str
    phase: str
    status: Literal['PENDING','REJECTED','COMPLETED']
    decision: Literal['UNDECIDED','COMMIT','ABORT']
    message: str
    failure: str|None
    source: str|None
    target: str
    history: list[dict]
    attempts: Money
    recovery: Recovery|None=None

class JourneyView(Extensible):
    id: str
    version: Annotated[StrictInt,Field(ge=1)]
    mode: Literal['synthetic']
    clock: Money
    intent: dict
    order: Order|None
    wallet: Wallet
    receipt: Receipt
    events: list[Event]
    all_plans: list[Plan]
    recommendations: list[Plan]
    current_plan: Plan|None
    handoff: Handoff|None=None
    handoff_pending: StrictBool=False
    automatic_recovery_enabled: StrictBool
    duplicate: StrictBool

class RuntimeView(Extensible):
    mode: Literal['synthetic']
    merchant_transport: Literal['http','local']
    automatic_recovery: StrictBool
    predecision_timeout_seconds: float
    retry_limit: Money
    scope: str

class ReceiptView(Extensible):
    mode: Literal['synthetic']
    order: Order|None
    receipt: Receipt
    events: list[Event]

class PolicyResult(Extensible):
    has_order: StrictBool
    same_order: StrictBool
    original_preserved: StrictBool
    cash_due: Money
    predicted_arrival: int|None
    meets_modeled_deadline: StrictBool
    customer_commands: Money
    same_request_retries: Money
    authorization_count: Money
    capture_count: Money
    held_points: Money
    points_spent: Money
    reason: str

class ComparisonCase(Extensible):
    scenario: str
    label: str
    stay: PolicyResult
    cancel_reorder: PolicyResult
    reserve_first_reorder: PolicyResult
    guarded_transfer: PolicyResult

class ComparisonView(Extensible):
    intent: dict
    policies: list[Literal['stay','cancel_reorder','reserve_first_reorder','guarded_transfer']]
    cases: list[ComparisonCase]
    executions: list[dict]
    semantic_sha256: str
    mode: Literal['controlled_command_execution']
    baseline_design: str
    disclosure: str
