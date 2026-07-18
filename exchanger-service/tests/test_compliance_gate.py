import pytest

from app.compliance_client.screening_gate import (
    WalletOwnershipVerificationFailedError,
    enforce_screening_gate,
    enforce_withdrawal_gate,
    submit_withdrawal_verification,
)
from app.custody import models as _custody_models  # noqa: F401
from app.ledger import models as _ledger_models  # noqa: F401
from app.swap import states
from app.swap.models import SwapOrder


class _FakeAml18Client:
    def __init__(self, check_name_result=None, requirement_result=None, challenge_result=None, verify_result=None):
        self._check_name_result = check_name_result
        self._requirement_result = requirement_result
        self._challenge_result = challenge_result
        self._verify_result = verify_result
        self.calls = []

    def check_name(self, name, date_of_birth=None, country=None):
        self.calls.append(("check_name", name, date_of_birth, country))
        return self._check_name_result

    def wallet_ownership_requirement(self, transfer_amount_eur):
        self.calls.append(("requirement", transfer_amount_eur))
        return self._requirement_result

    def create_wallet_ownership_challenge(self, network, address):
        self.calls.append(("create_challenge", network, address))
        return self._challenge_result

    def verify_wallet_ownership_signed_message(self, challenge_id, signature, transfer_amount_eur=None, transaction_id=None):
        self.calls.append(("verify", challenge_id, signature, transfer_amount_eur, transaction_id))
        return self._verify_result


def _order(**overrides):
    defaults = dict(
        from_chain="ethereum", from_asset="ETH", from_amount=1,
        to_chain="ethereum", to_asset="USDC",
    )
    defaults.update(overrides)
    order = SwapOrder(**defaults)
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)
    return order


# --- enforce_screening_gate --------------------------------------------


def test_screening_gate_accepted_leaves_order_in_screening():
    order = _order()
    client = _FakeAml18Client(check_name_result={"decision": "accepted", "score": 10.0, "matches": []})

    result = enforce_screening_gate(order, client, name="Jane Doe")

    assert result["decision"] == "accepted"
    assert order.status == states.SCREENING
    assert order.screening_decision == "accepted"
    assert order.screening_score == 10.0


def test_screening_gate_review_parks_order():
    order = _order()
    client = _FakeAml18Client(check_name_result={"decision": "review", "score": 78.0, "matches": [{"entity_id": 1}]})

    enforce_screening_gate(order, client, name="Some Name")

    assert order.status == states.PENDING_MANUAL_REVIEW
    assert order.screening_decision == "review"


def test_screening_gate_rejected_also_parks_order():
    order = _order()
    client = _FakeAml18Client(check_name_result={"decision": "rejected", "score": 99.0, "matches": []})

    enforce_screening_gate(order, client, name="Sanctioned Person")

    assert order.status == states.PENDING_MANUAL_REVIEW
    assert order.screening_decision == "rejected"


# --- enforce_withdrawal_gate / submit_withdrawal_verification -----------


def _withdrawal_order():
    order = _order()
    order.mark_status(states.QUOTE_LOCKED)
    order.mark_status(states.SWAP_EXECUTING)
    order.mark_status(states.SWAP_COMPLETE)
    order.mark_status(states.WITHDRAWAL_REQUESTED)
    order.withdrawal_address = "0xBeneficiary"
    return order


def test_withdrawal_gate_skips_verification_below_threshold():
    order = _withdrawal_order()
    client = _FakeAml18Client(requirement_result={"required": False, "threshold_eur": 1000, "transfer_amount_eur": 500})

    result = enforce_withdrawal_gate(order, client, transfer_amount_eur=500)

    assert result == {"required": False}
    assert order.status == states.WITHDRAWAL_REQUESTED  # unchanged -- caller advances directly


def test_withdrawal_gate_creates_challenge_above_threshold():
    order = _withdrawal_order()
    client = _FakeAml18Client(
        requirement_result={"required": True, "threshold_eur": 1000, "transfer_amount_eur": 5000},
        challenge_result={"challenge_id": "chal-1", "network": "ETH", "address": "0xBeneficiary", "message": "sign me", "expires_at": "2026-01-01T00:00:00Z"},
    )

    result = enforce_withdrawal_gate(order, client, transfer_amount_eur=5000)

    assert result["required"] is True
    assert order.status == states.WITHDRAWAL_VERIFICATION
    assert order.wallet_ownership_challenge_id == "chal-1"
    assert order.wallet_ownership_challenge_message == "sign me"
    assert ("create_challenge", "ETH", "0xBeneficiary") in client.calls


def test_submit_withdrawal_verification_advances_on_success():
    order = _withdrawal_order()
    order.wallet_ownership_challenge_id = "chal-1"
    order.mark_status(states.WITHDRAWAL_VERIFICATION)

    client = _FakeAml18Client(verify_result={"verification_id": "ver-1", "verified": True, "status": "verified"})

    result = submit_withdrawal_verification(order, client, signature="0xsig")

    assert result["verified"] is True
    assert order.wallet_ownership_verification_id == "ver-1"
    # Verification clears the compliance check but does not itself send
    # funds -- orchestrator.send_withdrawal() owns the WITHDRAWAL_SENT
    # transition, at the moment it actually calls send_fn().
    assert order.status == states.WITHDRAWAL_VERIFICATION


def test_submit_withdrawal_verification_raises_on_failure():
    order = _withdrawal_order()
    order.wallet_ownership_challenge_id = "chal-1"
    order.mark_status(states.WITHDRAWAL_VERIFICATION)

    client = _FakeAml18Client(verify_result={"verification_id": "ver-1", "verified": False, "status": "failed", "last_error": "wrong signer"})

    with pytest.raises(WalletOwnershipVerificationFailedError):
        submit_withdrawal_verification(order, client, signature="0xbadsig")

    assert order.status == states.WITHDRAWAL_VERIFICATION  # stays put for retry
