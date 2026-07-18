import pytest

from app.custody import models as _custody_models  # noqa: F401 -- registers DepositAddress for SwapOrder's relationship
from app.ledger import models as _ledger_models  # noqa: F401 -- registers LedgerEntry for SwapOrder's relationship
from app.swap import states
from app.swap.models import SwapOrder


def test_happy_path_transition_sequence():
    order = SwapOrder(
        from_chain="ethereum", from_asset="ETH", from_amount=1,
        to_chain="ethereum", to_asset="USDC",
    )
    assert order.status == states.DEPOSIT_PENDING

    sequence = [
        states.DEPOSIT_CONFIRMED, states.SCREENING, states.QUOTE_LOCKED,
        states.SWAP_EXECUTING, states.SWAP_COMPLETE, states.WITHDRAWAL_REQUESTED,
        states.WITHDRAWAL_SENT, states.DONE,
    ]
    for next_status in sequence:
        order.mark_status(next_status)
        assert order.status == next_status


def test_manual_review_dead_end_then_resumes():
    order = SwapOrder(
        from_chain="ethereum", from_asset="ETH", from_amount=1,
        to_chain="ethereum", to_asset="USDC",
    )
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)
    order.mark_status(states.PENDING_MANUAL_REVIEW)
    with pytest.raises(states.InvalidTransitionError):
        order.mark_status(states.SWAP_EXECUTING)
    order.mark_status(states.QUOTE_LOCKED)
    assert order.status == states.QUOTE_LOCKED


def test_cannot_skip_steps():
    order = SwapOrder(
        from_chain="ethereum", from_asset="ETH", from_amount=1,
        to_chain="ethereum", to_asset="USDC",
    )
    with pytest.raises(states.InvalidTransitionError):
        order.mark_status(states.SWAP_EXECUTING)


def test_any_non_terminal_state_can_fail():
    for status in states.ALL_STATUSES:
        if status in states.TERMINAL_STATUSES:
            continue
        assert states.can_transition(status, states.FAILED)
        assert states.can_transition(status, states.REFUND_PENDING)


def test_terminal_states_have_no_transitions():
    for status in states.TERMINAL_STATUSES:
        assert states.allowed_next_statuses(status) == frozenset()


def test_withdrawal_can_skip_verification_below_threshold():
    assert states.can_transition(states.WITHDRAWAL_REQUESTED, states.WITHDRAWAL_SENT)


def test_treasury_rebalance_detour():
    order = SwapOrder(
        from_chain="bitcoin", from_asset="BTC", from_amount=1,
        to_chain="ethereum", to_asset="USDC",
    )
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)
    order.mark_status(states.QUOTE_LOCKED)
    order.mark_status(states.PENDING_TREASURY_REBALANCE)
    order.mark_status(states.SWAP_EXECUTING)
    assert order.status == states.SWAP_EXECUTING
