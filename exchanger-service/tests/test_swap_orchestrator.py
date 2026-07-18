from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.custody import models as _custody_models  # noqa: F401
from app.ledger import models as _ledger_models  # noqa: F401
from app.liquidity.base import LiquidityAdapterError, Quote, SwapExecutionResult
from app.liquidity.btc_treasury_adapter import TreasuryRebalanceRequiredError
from app.swap import orchestrator, states
from app.swap.models import SwapOrder


class _FakeLiquidityAdapter:
    def __init__(self, to_amount=Decimal("2500"), tx_hash="0xswaptx", swap_status_sequence=None, raise_treasury=False):
        self.to_amount = to_amount
        self.tx_hash = tx_hash
        self._swap_status_sequence = list(swap_status_sequence or ["confirmed"])
        self.raise_treasury = raise_treasury
        self.execute_calls = 0
        self.quote_calls = 0

    def get_quote(self, from_asset, to_asset, amount):
        self.quote_calls += 1
        if self.raise_treasury:
            raise TreasuryRebalanceRequiredError(required=amount, available=Decimal("0"))
        return Quote(
            quote_id=f"q{self.quote_calls}",
            from_asset=from_asset, to_asset=to_asset,
            from_amount=amount, to_amount=self.to_amount,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            raw_provider_response={"to": "0xExchange", "data": "0xdead"},
        )

    def execute_swap(self, quote):
        self.execute_calls += 1
        return SwapExecutionResult(
            tx_hash=self.tx_hash, to_amount_executed=quote.to_amount,
            status="submitted", raw_provider_response=quote.raw_provider_response,
        )

    def get_swap_status(self, tx_hash, min_confirmations):
        if len(self._swap_status_sequence) > 1:
            return self._swap_status_sequence.pop(0)
        return self._swap_status_sequence[0]


class _FakeAml18Client:
    def __init__(self, check_name_result=None, requirement_result=None, challenge_result=None):
        self._check_name_result = check_name_result or {"decision": "accepted", "score": 5.0, "matches": []}
        self._requirement_result = requirement_result or {"required": False}
        self._challenge_result = challenge_result

    def check_name(self, name, date_of_birth=None, country=None):
        return self._check_name_result

    def wallet_ownership_requirement(self, transfer_amount_eur):
        return self._requirement_result

    def create_wallet_ownership_challenge(self, network, address):
        return self._challenge_result

    def verify_wallet_ownership_signed_message(self, *a, **k):
        raise AssertionError("not used in these tests")


MARGIN_PERCENT = Decimal("1.5")


def _order(app, **overrides):
    from app.extensions import db

    defaults = dict(from_chain="ethereum", from_asset="ETH", from_amount=Decimal("1"), to_chain="ethereum", to_asset="USDC")
    defaults.update(overrides)
    order = SwapOrder(**defaults)
    db.session.add(order)
    db.session.commit()
    return order


def test_full_happy_path_to_withdrawal_sent(app):
    order = _order(app)
    liquidity = _FakeLiquidityAdapter()
    aml18 = _FakeAml18Client()

    orchestrator.confirm_deposit(order, "0xdeposittx")
    assert order.status == states.DEPOSIT_CONFIRMED
    assert order.ledger_entries[0].entry_type == "deposit"

    orchestrator.advance_to_screening(order)
    assert order.status == states.SCREENING

    orchestrator.run_screening(order, aml18, name="Jane Doe")
    assert order.status == states.SCREENING  # accepted -- stays for lock_quote
    assert order.screening_decision == "accepted"

    orchestrator.lock_quote(order, liquidity, MARGIN_PERCENT)
    assert order.status == states.QUOTE_LOCKED
    assert order.quote_id == "q1"
    assert order.to_amount_quoted_gross == Decimal("2500")
    assert order.to_amount_quoted == Decimal("2500") * Decimal("0.985")  # 1.5% margin baked in

    orchestrator.execute_swap(order, liquidity)
    assert order.status == states.SWAP_EXECUTING
    assert order.swap_tx_hash == "0xswaptx"
    assert liquidity.execute_calls == 1

    orchestrator.poll_swap_completion(order, liquidity, min_confirmations=1)
    assert order.status == states.SWAP_COMPLETE
    entry_types = {e.entry_type for e in order.ledger_entries}
    assert {"deposit", "swap_in", "swap_out", "fee"} <= entry_types
    assert order.to_amount_payout == Decimal("2500") * Decimal("0.985")
    fee_entry = next(e for e in order.ledger_entries if e.entry_type == "fee")
    assert fee_entry.account == "revenue:margin:USDC"
    assert fee_entry.amount == order.to_amount_executed - order.to_amount_payout

    orchestrator.request_withdrawal(order, "0xBeneficiary", actor="operator")
    assert order.status == states.WITHDRAWAL_REQUESTED

    orchestrator.gate_withdrawal(order, aml18, transfer_amount_eur=Decimal("500"))
    assert order.status == states.WITHDRAWAL_REQUESTED  # not required, unchanged

    sent = {}

    def _send_fn(o):
        sent["order_id"] = o.id
        sent["amount"] = o.to_amount_payout
        return "0xwithdrawaltx"

    orchestrator.send_withdrawal(order, _send_fn)
    assert order.status == states.WITHDRAWAL_SENT
    assert order.withdrawal_tx_hash == "0xwithdrawaltx"
    assert sent["order_id"] == order.id
    assert sent["amount"] == order.to_amount_payout  # client is paid the NET amount, not the gross

    orchestrator.poll_withdrawal_completion(order, lambda tx, n: "confirmed", min_confirmations=1)
    assert order.status == states.DONE


def test_screening_review_blocks_until_operator_approves(app):
    order = _order(app)
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)
    liquidity = _FakeLiquidityAdapter()
    aml18 = _FakeAml18Client(check_name_result={"decision": "review", "score": 80.0, "matches": []})

    orchestrator.run_screening(order, aml18, name="Suspicious Name")
    assert order.status == states.PENDING_MANUAL_REVIEW

    # execute_swap is a polling-style no-op outside its applicable states --
    # it doesn't move the order until the review is explicitly approved.
    orchestrator.execute_swap(order, liquidity)
    assert order.status == states.PENDING_MANUAL_REVIEW
    assert liquidity.execute_calls == 0

    orchestrator.approve_manual_review(order, operator="admin")
    orchestrator.lock_quote(order, liquidity, MARGIN_PERCENT)
    assert order.status == states.QUOTE_LOCKED


def test_treasury_rebalance_detour_then_retry(app):
    order = _order(app, from_chain="bitcoin", from_asset="BTC", to_chain="ethereum", to_asset="USDC")
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)

    short_liquidity = _FakeLiquidityAdapter(raise_treasury=True)
    orchestrator.lock_quote(order, short_liquidity, MARGIN_PERCENT)
    assert order.status == states.PENDING_TREASURY_REBALANCE
    assert order.requires_treasury_rebalance is True
    assert order.margin_percent == MARGIN_PERCENT  # snapshotted even on the treasury-detour path

    funded_liquidity = _FakeLiquidityAdapter()
    orchestrator.retry_after_rebalance(order, funded_liquidity, operator="admin")
    assert order.status == states.SWAP_EXECUTING
    assert order.swap_tx_hash == "0xswaptx"


def test_execute_swap_failure_marks_order_failed(app):
    order = _order(app)
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)
    order.mark_status(states.QUOTE_LOCKED)
    order.quote_id = "q1"
    order.to_amount_quoted_gross = Decimal("2500")
    order.margin_percent = MARGIN_PERCENT
    order.quote_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
    order.quote_raw_response = {}

    class _FailingAdapter(_FakeLiquidityAdapter):
        def execute_swap(self, quote):
            raise LiquidityAdapterError("insufficient liquidity")

    orchestrator.execute_swap(order, _FailingAdapter())
    assert order.status == states.FAILED
    assert "insufficient liquidity" in order.last_error


def test_execute_swap_is_idempotent_on_retry(app):
    order = _order(app)
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)
    order.mark_status(states.QUOTE_LOCKED)
    order.quote_id = "q1"
    order.to_amount_quoted_gross = Decimal("2500")
    order.margin_percent = MARGIN_PERCENT
    order.quote_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
    order.quote_raw_response = {}

    liquidity = _FakeLiquidityAdapter()
    orchestrator.execute_swap(order, liquidity)
    assert liquidity.execute_calls == 1

    orchestrator.execute_swap(order, liquidity)
    assert liquidity.execute_calls == 1  # not called again -- swap_tx_hash already set


def test_poll_swap_completion_marks_failed_on_chain_failure(app):
    order = _order(app)
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)
    order.mark_status(states.QUOTE_LOCKED)
    order.mark_status(states.SWAP_EXECUTING)
    order.swap_tx_hash = "0xswaptx"

    liquidity = _FakeLiquidityAdapter(swap_status_sequence=["failed"])
    orchestrator.poll_swap_completion(order, liquidity, min_confirmations=1)
    assert order.status == states.FAILED


def test_send_withdrawal_blocked_when_verification_required_but_not_cleared(app):
    order = _order(app)
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)
    order.mark_status(states.QUOTE_LOCKED)
    order.mark_status(states.SWAP_EXECUTING)
    order.mark_status(states.SWAP_COMPLETE)
    order.to_amount_executed = Decimal("2500")
    order.to_amount_payout = Decimal("2500")  # normally set by poll_swap_completion

    orchestrator.request_withdrawal(order, "0xBeneficiary", actor="operator")

    aml18 = _FakeAml18Client(
        requirement_result={"required": True},
        challenge_result={"challenge_id": "chal-1", "message": "sign me", "network": "ETH", "address": "0xBeneficiary", "expires_at": "2026-01-01T00:00:00Z"},
    )
    orchestrator.gate_withdrawal(order, aml18, transfer_amount_eur=Decimal("5000"))
    assert order.status == states.WITHDRAWAL_VERIFICATION

    with pytest.raises(orchestrator.WithdrawalNotClearedError):
        orchestrator.send_withdrawal(order, lambda o: "0xshouldnotsend")

    # Once verification is recorded (simulating submit_withdrawal_verification success):
    order.wallet_ownership_verification_id = "ver-1"
    result = orchestrator.send_withdrawal(order, lambda o: "0xwithdrawaltx")
    assert result.status == states.WITHDRAWAL_SENT
    assert result.withdrawal_tx_hash == "0xwithdrawaltx"


# --- run_screening_and_lock_quote ----------------------------------------


def test_run_screening_and_lock_quote_accepted_locks_quote_in_one_call(app):
    order = _order(app)
    order.mark_status(states.DEPOSIT_CONFIRMED)
    liquidity = _FakeLiquidityAdapter()
    aml18 = _FakeAml18Client()

    orchestrator.run_screening_and_lock_quote(order, aml18, liquidity, MARGIN_PERCENT, name="Jane Doe")

    assert order.status == states.QUOTE_LOCKED
    assert order.screening_decision == "accepted"
    assert order.to_amount_quoted == Decimal("2500") * Decimal("0.985")


def test_run_screening_and_lock_quote_review_stops_at_manual_review(app):
    order = _order(app)
    order.mark_status(states.DEPOSIT_CONFIRMED)
    liquidity = _FakeLiquidityAdapter()
    aml18 = _FakeAml18Client(check_name_result={"decision": "review", "score": 90.0, "matches": []})

    orchestrator.run_screening_and_lock_quote(order, aml18, liquidity, MARGIN_PERCENT, name="Suspicious Name")

    assert order.status == states.PENDING_MANUAL_REVIEW
    assert order.quote_id is None  # never reached lock_quote
    assert liquidity.quote_calls == 0


def test_run_screening_and_lock_quote_is_a_noop_outside_deposit_confirmed(app):
    order = _order(app)
    liquidity = _FakeLiquidityAdapter()
    aml18 = _FakeAml18Client()

    orchestrator.run_screening_and_lock_quote(order, aml18, liquidity, MARGIN_PERCENT, name="Jane Doe")

    assert order.status == states.DEPOSIT_PENDING  # unchanged -- deposit not yet confirmed
