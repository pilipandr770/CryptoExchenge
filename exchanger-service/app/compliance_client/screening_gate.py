"""Enforces the AML-18 compliance gate at the two pipeline chokepoints ТЗ
section 4.4 calls out: sanctions screening before SWAP_EXECUTING, and
wallet-ownership verification before WITHDRAWAL_SENT. Both functions here
mutate the given SwapOrder's status directly (matching ТЗ's description of
screening_gate.py) rather than just returning a decision for the caller to
act on -- the pipeline must never advance past a flagged decision by
accident.
"""

from app.compliance_client.aml18_client import Aml18Client
from app.swap import states

CHAIN_TO_NETWORK = {
    "ethereum": "ETH",
    "polygon": "POLYGON",
    "bitcoin": "BTC",
}


class WalletOwnershipVerificationFailedError(Exception):
    pass


def enforce_screening_gate(order, client: Aml18Client, name: str, date_of_birth: str = None, country: str = None) -> dict:
    """Calls AML-18 check-name and persists the result on `order`. A
    "review" or "rejected" decision parks the order in
    PENDING_MANUAL_REVIEW; only "accepted" leaves it in SCREENING for the
    orchestrator to advance to QUOTE_LOCKED."""
    result = client.check_name(name, date_of_birth=date_of_birth, country=country)
    order.screening_decision = result["decision"]
    order.screening_score = result["score"]

    if result["decision"] in ("review", "rejected"):
        order.mark_status(states.PENDING_MANUAL_REVIEW)

    return result


def enforce_withdrawal_gate(order, client: Aml18Client, transfer_amount_eur: float) -> dict:
    """Checks whether wallet-ownership verification is required for this
    withdrawal. If not required, the caller is free to advance straight to
    WITHDRAWAL_SENT. If required, creates a signed-message challenge and
    moves the order to WITHDRAWAL_VERIFICATION -- the operator signs the
    challenge message out-of-band (e.g. in a wallet app) and submits it via
    submit_withdrawal_verification() below, from /admin/orders/<id>."""
    requirement = client.wallet_ownership_requirement(transfer_amount_eur)
    if not requirement["required"]:
        return {"required": False}

    network = CHAIN_TO_NETWORK.get(order.to_chain, order.to_chain.upper())
    challenge = client.create_wallet_ownership_challenge(network=network, address=order.withdrawal_address)

    order.wallet_ownership_challenge_id = challenge["challenge_id"]
    order.wallet_ownership_challenge_message = challenge["message"]
    order.mark_status(states.WITHDRAWAL_VERIFICATION)

    return {"required": True, "challenge": challenge}


def submit_withdrawal_verification(
    order, client: Aml18Client, signature: str, transfer_amount_eur: float = None,
) -> dict:
    """Called from the admin UI once the operator has signed the challenge
    message. Raises WalletOwnershipVerificationFailedError (leaving the
    order in WITHDRAWAL_VERIFICATION for a retry) if the signature doesn't
    recover to the claimed address. On success, records the verification
    but deliberately does NOT advance the order to WITHDRAWAL_SENT -- that
    status must only ever be set at the moment funds actually leave the hot
    wallet, which is orchestrator.send_withdrawal()'s job, not this
    compliance check's."""
    result = client.verify_wallet_ownership_signed_message(
        order.wallet_ownership_challenge_id,
        signature,
        transfer_amount_eur=transfer_amount_eur,
        transaction_id=str(order.id),
    )
    order.wallet_ownership_verification_id = result["verification_id"]

    if not result.get("verified"):
        raise WalletOwnershipVerificationFailedError(result.get("last_error") or "signature verification failed")

    return result
