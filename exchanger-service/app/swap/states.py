"""SwapOrder state machine, per ТЗ section 5.

PENDING_MANUAL_REVIEW and PENDING_TREASURY_REBALANCE are dead-ends an order
sits in until an operator acts (via admin_ui) -- they are not auto-advanced
by the orchestrator's poll loop.
"""

DEPOSIT_PENDING = "DEPOSIT_PENDING"
DEPOSIT_CONFIRMED = "DEPOSIT_CONFIRMED"
SCREENING = "SCREENING"
PENDING_MANUAL_REVIEW = "PENDING_MANUAL_REVIEW"
QUOTE_LOCKED = "QUOTE_LOCKED"
PENDING_TREASURY_REBALANCE = "PENDING_TREASURY_REBALANCE"
SWAP_EXECUTING = "SWAP_EXECUTING"
SWAP_COMPLETE = "SWAP_COMPLETE"
WITHDRAWAL_REQUESTED = "WITHDRAWAL_REQUESTED"
WITHDRAWAL_VERIFICATION = "WITHDRAWAL_VERIFICATION"
WITHDRAWAL_SENT = "WITHDRAWAL_SENT"
DONE = "DONE"
FAILED = "FAILED"
REFUND_PENDING = "REFUND_PENDING"
REFUNDED = "REFUNDED"

ALL_STATUSES = (
    DEPOSIT_PENDING, DEPOSIT_CONFIRMED, SCREENING, PENDING_MANUAL_REVIEW,
    QUOTE_LOCKED, PENDING_TREASURY_REBALANCE, SWAP_EXECUTING, SWAP_COMPLETE,
    WITHDRAWAL_REQUESTED, WITHDRAWAL_VERIFICATION, WITHDRAWAL_SENT,
    DONE, FAILED, REFUND_PENDING, REFUNDED,
)

TERMINAL_STATUSES = frozenset({DONE, FAILED, REFUNDED})

# The happy-path graph. Every non-terminal status can *additionally* always
# jump to FAILED or REFUND_PENDING (see _ERROR_ESCAPE_STATUSES below) --
# ТЗ: "из любого шага при ошибке".
_HAPPY_PATH_TRANSITIONS = {
    DEPOSIT_PENDING: {DEPOSIT_CONFIRMED},
    DEPOSIT_CONFIRMED: {SCREENING},
    # PENDING_TREASURY_REBALANCE can be discovered straight from SCREENING /
    # PENDING_MANUAL_REVIEW (a BTC-leg get_quote() call finds the treasury
    # short before a quote is ever locked) or from QUOTE_LOCKED (treasury
    # drained between quote-lock and execute_swap).
    SCREENING: {PENDING_MANUAL_REVIEW, QUOTE_LOCKED, PENDING_TREASURY_REBALANCE},
    PENDING_MANUAL_REVIEW: {QUOTE_LOCKED, PENDING_TREASURY_REBALANCE},
    QUOTE_LOCKED: {PENDING_TREASURY_REBALANCE, SWAP_EXECUTING},
    PENDING_TREASURY_REBALANCE: {SWAP_EXECUTING},
    SWAP_EXECUTING: {SWAP_COMPLETE},
    SWAP_COMPLETE: {WITHDRAWAL_REQUESTED},
    WITHDRAWAL_REQUESTED: {WITHDRAWAL_VERIFICATION, WITHDRAWAL_SENT},
    WITHDRAWAL_VERIFICATION: {WITHDRAWAL_SENT},
    WITHDRAWAL_SENT: {DONE},
    REFUND_PENDING: {REFUNDED},
}

_ERROR_ESCAPE_STATUSES = frozenset({FAILED, REFUND_PENDING})


def allowed_next_statuses(status: str) -> frozenset:
    if status in TERMINAL_STATUSES:
        return frozenset()
    happy = _HAPPY_PATH_TRANSITIONS.get(status, frozenset())
    return frozenset(happy) | _ERROR_ESCAPE_STATUSES


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in allowed_next_statuses(from_status)


class InvalidTransitionError(Exception):
    def __init__(self, from_status: str, to_status: str):
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"cannot transition SwapOrder from {from_status} to {to_status}")
