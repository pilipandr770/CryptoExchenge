from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.accounts import auth
from app.accounts.balances import InsufficientBalanceError, credit_user, user_balance
from app.custody import models as _custody_models  # noqa: F401
from app.extensions import db
from app.ledger import models as _ledger_models  # noqa: F401
from app.liquidity.base import Quote, SwapExecutionResult
from app.swap import orchestrator, states
from app.swap.models import SwapOrder

MARGIN_PERCENT = Decimal("1.5")


class _FakeLiquidityAdapter:
    def __init__(self, to_amount=Decimal("2500"), tx_hash="0xswaptx"):
        self.to_amount = to_amount
        self.tx_hash = tx_hash
        self.quote_calls = 0

    def get_quote(self, from_asset, to_asset, amount):
        self.quote_calls += 1
        return Quote(
            quote_id=f"q{self.quote_calls}", from_asset=from_asset, to_asset=to_asset,
            from_amount=amount, to_amount=self.to_amount,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            raw_provider_response={},
        )

    def execute_swap(self, quote):
        return SwapExecutionResult(
            tx_hash=self.tx_hash, to_amount_executed=quote.to_amount,
            status="submitted", raw_provider_response={},
        )

    def get_swap_status(self, tx_hash, min_confirmations):
        return "confirmed"


def _funded_user(balance_asset="ETH", balance_amount=Decimal("2")):
    user = auth.register_user(f"user{id(object())}@example.com", "pw", "Jane Doe")
    db.session.flush()
    credit_user(user.id, balance_asset, balance_amount, entry_type="deposit")
    db.session.commit()
    return user


def _balance_order(user, from_asset="ETH", from_amount=Decimal("1"), to_asset="USDC"):
    order = SwapOrder(
        from_chain="ethereum", from_asset=from_asset, from_amount=from_amount,
        to_chain="ethereum", to_asset=to_asset,
        user_id=user.id, funding_source="account_balance",
    )
    db.session.add(order)
    db.session.commit()
    return order


# --- lock_quote_from_balance ----------------------------------------------


def test_lock_quote_from_balance_debits_and_locks_quote(app):
    user = _funded_user()
    order = _balance_order(user)
    liquidity = _FakeLiquidityAdapter()

    orchestrator.lock_quote_from_balance(order, liquidity, MARGIN_PERCENT)

    assert order.status == states.QUOTE_LOCKED
    assert user_balance(user.id, "ETH") == Decimal("1")  # 2 - 1 debited
    assert order.to_amount_quoted_gross == Decimal("2500")
    assert order.to_amount_quoted == Decimal("2500") * Decimal("0.985")


def test_lock_quote_from_balance_raises_on_insufficient_balance(app):
    user = _funded_user(balance_amount=Decimal("0.1"))
    order = _balance_order(user, from_amount=Decimal("1"))
    liquidity = _FakeLiquidityAdapter()

    with pytest.raises(InsufficientBalanceError):
        orchestrator.lock_quote_from_balance(order, liquidity, MARGIN_PERCENT)

    assert order.status == states.DEPOSIT_PENDING  # untouched
    # SQLite (this test suite's backend) stores Numeric as float under the
    # hood, so a Decimal round-trips to ~15 significant digits, not exactly --
    # Postgres (the real deployment target) doesn't have this limitation.
    assert abs(user_balance(user.id, "ETH") - Decimal("0.1")) < Decimal("1e-9")  # unchanged
    assert liquidity.quote_calls == 0  # never reached the adapter


def test_lock_quote_from_balance_is_noop_for_external_deposit_orders(app):
    user = _funded_user()
    order = SwapOrder(
        from_chain="ethereum", from_asset="ETH", from_amount=Decimal("1"),
        to_chain="ethereum", to_asset="USDC", user_id=user.id,
        funding_source="external_deposit",
    )
    db.session.add(order)
    db.session.commit()
    liquidity = _FakeLiquidityAdapter()

    orchestrator.lock_quote_from_balance(order, liquidity, MARGIN_PERCENT)

    assert order.status == states.DEPOSIT_PENDING  # unaffected
    assert liquidity.quote_calls == 0


def test_lock_quote_from_balance_does_not_call_aml18(app, monkeypatch):
    # No aml18_client parameter exists on this function at all -- confirm
    # it truly can't reach the network by not injecting one and verifying
    # the flow still completes (would TypeError if it tried to use one).
    user = _funded_user()
    order = _balance_order(user)
    liquidity = _FakeLiquidityAdapter()

    orchestrator.lock_quote_from_balance(order, liquidity, MARGIN_PERCENT)
    assert order.screening_decision is None  # never touched -- reused from registration


# --- settle_to_balance -----------------------------------------------------


def test_settle_to_balance_credits_and_marks_done(app):
    user = _funded_user()
    order = _balance_order(user)
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)
    order.mark_status(states.QUOTE_LOCKED)
    order.mark_status(states.SWAP_EXECUTING)
    order.mark_status(states.SWAP_COMPLETE)
    order.to_amount_executed = Decimal("2500")
    order.to_amount_payout = Decimal("2462.5")
    db.session.commit()

    orchestrator.settle_to_balance(order)

    assert order.status == states.DONE
    assert user_balance(user.id, "USDC") == Decimal("2462.5")


def test_settle_to_balance_is_noop_for_external_deposit_orders(app):
    user = _funded_user()
    order = SwapOrder(
        from_chain="ethereum", from_asset="ETH", from_amount=Decimal("1"),
        to_chain="ethereum", to_asset="USDC", user_id=user.id,
        funding_source="external_deposit",
    )
    db.session.add(order)
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)
    order.mark_status(states.QUOTE_LOCKED)
    order.mark_status(states.SWAP_EXECUTING)
    order.mark_status(states.SWAP_COMPLETE)
    order.to_amount_payout = Decimal("100")
    db.session.commit()

    orchestrator.settle_to_balance(order)

    assert order.status == states.SWAP_COMPLETE  # untouched -- goes through request_withdrawal instead
    assert user_balance(user.id, "USDC") == Decimal("0")


# --- withdraw_from_balance -------------------------------------------------


def test_withdraw_from_balance_creates_order_at_withdrawal_requested(app):
    user = _funded_user(balance_asset="USDC", balance_amount=Decimal("500"))

    order = orchestrator.withdraw_from_balance(user, "ethereum", "USDC", Decimal("200"), "0xBeneficiary", actor="user")

    assert order.status == states.WITHDRAWAL_REQUESTED
    assert order.from_asset == order.to_asset == "USDC"
    assert order.funding_source == "account_balance"
    assert order.withdrawal_address == "0xBeneficiary"
    assert order.to_amount_payout == Decimal("200")
    assert user_balance(user.id, "USDC") == Decimal("300")


def test_withdraw_from_balance_raises_on_insufficient_balance_and_creates_no_order(app):
    user = _funded_user(balance_asset="USDC", balance_amount=Decimal("10"))

    with pytest.raises(InsufficientBalanceError):
        orchestrator.withdraw_from_balance(user, "ethereum", "USDC", Decimal("500"), "0xBeneficiary", actor="user")

    assert SwapOrder.query.count() == 0
    assert user_balance(user.id, "USDC") == Decimal("10")


def test_withdraw_from_balance_then_reuses_existing_send_withdrawal(app):
    user = _funded_user(balance_asset="USDC", balance_amount=Decimal("500"))
    order = orchestrator.withdraw_from_balance(user, "ethereum", "USDC", Decimal("200"), "0xBeneficiary", actor="user")

    sent = {}

    def _send_fn(o):
        sent["amount"] = o.to_amount_payout
        return "0xwithdrawaltx"

    result = orchestrator.send_withdrawal(order, _send_fn)

    assert result.status == states.WITHDRAWAL_SENT
    assert result.withdrawal_tx_hash == "0xwithdrawaltx"
    assert sent["amount"] == Decimal("200")
