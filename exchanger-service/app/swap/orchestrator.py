"""Advances a SwapOrder one state-machine step at a time (ТЗ section 5/11
step 4 -- built before chain listeners exist, so every function here is
called explicitly rather than by a poll loop for now; chain_listeners
(app/chain_listeners) call confirm_deposit()/poll_swap_completion() once
they exist).

Idempotency (ТЗ section 8): every function checks whether its step already
recorded a result (a tx hash, a quote, a verification id) before doing any
external side effect, so retrying the same call after a crash re-observes
existing state instead of double-submitting a transaction.
"""

from decimal import Decimal

from app.audit.models import AuditLog
from app.compliance_client.screening_gate import enforce_screening_gate, enforce_withdrawal_gate
from app.extensions import db
from app.ledger.models import LedgerEntry
from app.liquidity.base import LiquidityAdapterError, Quote
from app.liquidity.btc_treasury_adapter import TreasuryRebalanceRequiredError
from app.pricing.margin import apply_margin
from app.swap import states


class WithdrawalNotClearedError(Exception):
    def __init__(self, order_id: int):
        super().__init__(f"SwapOrder {order_id} has not cleared the wallet-ownership gate yet")


def _audit(actor: str, action: str, order, detail: dict = None):
    db.session.add(AuditLog(
        actor=actor,
        action=action,
        target_type="SwapOrder",
        target_id=str(order.id),
        detail=detail or {},
    ))


def confirm_deposit(order, tx_hash: str, actor: str = "system"):
    """DEPOSIT_PENDING -> DEPOSIT_CONFIRMED, called by a chain listener once
    it has observed enough confirmations on the deposit address."""
    if order.status != states.DEPOSIT_PENDING:
        return order

    order.deposit_tx_hash = tx_hash
    order.mark_status(states.DEPOSIT_CONFIRMED)
    label = order.deposit_address.label if order.deposit_address else "unknown"
    db.session.add(LedgerEntry(
        swap_order_id=order.id, account=f"user:{label}",
        asset=order.from_asset, amount=order.from_amount, entry_type="deposit",
    ))
    _audit(actor, "deposit_confirmed", order, {"tx_hash": tx_hash})
    db.session.commit()
    return order


def advance_to_screening(order, actor: str = "system"):
    """DEPOSIT_CONFIRMED -> SCREENING."""
    if order.status != states.DEPOSIT_CONFIRMED:
        return order
    order.mark_status(states.SCREENING)
    _audit(actor, "screening_started", order)
    db.session.commit()
    return order


def run_screening(order, aml18_client, name: str, date_of_birth: str = None, country: str = None, actor: str = "system"):
    """Calls the AML-18 compliance gate. Leaves the order in SCREENING on an
    "accepted" decision (ready for lock_quote); the gate itself parks it in
    PENDING_MANUAL_REVIEW on "review"/"rejected"."""
    if order.status != states.SCREENING:
        return order

    result = enforce_screening_gate(order, aml18_client, name, date_of_birth=date_of_birth, country=country)
    _audit(actor, "screening_decision", order, result)
    db.session.commit()
    return order


def run_screening_and_lock_quote(
    order, aml18_client, liquidity_adapter, margin_percent: Decimal,
    name: str, date_of_birth: str = None, country: str = None, actor: str = "system",
):
    """Convenience entry point for /admin/orders/<id>/run-screening (and,
    later, an auto-triggered public-order flow): DEPOSIT_CONFIRMED ->
    SCREENING -> (accepted) -> QUOTE_LOCKED in one call, or stops at
    PENDING_MANUAL_REVIEW for an operator to clear via approve_manual_review()
    + lock_quote(). A no-op outside DEPOSIT_CONFIRMED/SCREENING, same as its
    component steps."""
    advance_to_screening(order, actor=actor)
    run_screening(order, aml18_client, name, date_of_birth=date_of_birth, country=country, actor=actor)
    if order.status == states.SCREENING:
        lock_quote(order, liquidity_adapter, margin_percent, actor=actor)
    return order


def approve_manual_review(order, operator: str):
    """Operator override from /admin/orders/<id>: clears a
    PENDING_MANUAL_REVIEW order to continue into quote-locking."""
    if order.status != states.PENDING_MANUAL_REVIEW:
        raise states.InvalidTransitionError(order.status, states.QUOTE_LOCKED)
    _audit(operator, "manual_review_approved", order)
    db.session.commit()
    return order


def lock_quote(order, liquidity_adapter, margin_percent: Decimal, actor: str = "system"):
    """SCREENING -> QUOTE_LOCKED (or PENDING_MANUAL_REVIEW -> QUOTE_LOCKED
    once approve_manual_review() has been called). Requests a quote and
    persists it -- including the adapter's raw response, needed verbatim by
    execute_swap(). Routes a BTC-leg order lacking treasury WBTC to
    PENDING_TREASURY_REBALANCE instead of failing outright.

    `margin_percent` is snapshotted onto the order immediately (before the
    quote request, so it survives even the PENDING_TREASURY_REBALANCE
    detour) -- see app/pricing/margin.py. `to_amount_quoted` is the
    client-facing (net, after margin) amount; `to_amount_quoted_gross` is
    the raw adapter quote, kept for internal accounting only."""
    if order.status not in (states.SCREENING, states.PENDING_MANUAL_REVIEW):
        return order

    order.margin_percent = margin_percent

    try:
        quote = liquidity_adapter.get_quote(order.from_asset, order.to_asset, order.from_amount)
    except TreasuryRebalanceRequiredError as exc:
        order.requires_treasury_rebalance = True
        order.mark_status(states.PENDING_TREASURY_REBALANCE)
        _audit(actor, "treasury_rebalance_required", order, {
            "required": str(exc.required), "available": str(exc.available),
        })
        db.session.commit()
        return order

    order.quote_id = quote.quote_id
    order.to_amount_quoted_gross = quote.to_amount
    order.to_amount_quoted = apply_margin(quote.to_amount, margin_percent)
    order.quote_expires_at = quote.expires_at
    order.quote_raw_response = quote.raw_provider_response
    if quote.raw_provider_response.get("requires_treasury_rebalance"):
        order.requires_treasury_rebalance = True

    order.mark_status(states.QUOTE_LOCKED)
    _audit(actor, "quote_locked", order, {
        "quote_id": quote.quote_id,
        "to_amount_quoted_gross": str(quote.to_amount),
        "to_amount_quoted_net": str(order.to_amount_quoted),
        "margin_percent": str(margin_percent),
    })
    db.session.commit()
    return order


def retry_after_rebalance(order, liquidity_adapter, operator: str):
    """Operator confirms the manual BTC<->WBTC rebalance is done
    (/admin/treasury): re-requests the quote and moves straight into
    execute_swap(). PENDING_TREASURY_REBALANCE only transitions to
    SWAP_EXECUTING in the state graph, not back to QUOTE_LOCKED, so this
    doesn't reuse lock_quote()'s QUOTE_LOCKED transition. Reuses
    order.margin_percent as snapshotted by the original lock_quote() call --
    a client's promised rate never changes because of a treasury-side
    operational hiccup."""
    if order.status != states.PENDING_TREASURY_REBALANCE:
        raise states.InvalidTransitionError(order.status, states.SWAP_EXECUTING)

    quote = liquidity_adapter.get_quote(order.from_asset, order.to_asset, order.from_amount)
    order.quote_id = quote.quote_id
    order.to_amount_quoted_gross = quote.to_amount
    order.to_amount_quoted = apply_margin(quote.to_amount, order.margin_percent)
    order.quote_expires_at = quote.expires_at
    order.quote_raw_response = quote.raw_provider_response
    order.mark_status(states.SWAP_EXECUTING)
    _audit(operator, "quote_relocked_after_rebalance", order, {"quote_id": quote.quote_id})
    db.session.commit()
    return execute_swap(order, liquidity_adapter, actor=operator)


def execute_swap(order, liquidity_adapter, actor: str = "system"):
    """QUOTE_LOCKED -> SWAP_EXECUTING. Submits the swap transaction; does
    NOT wait for confirmation (see poll_swap_completion). If swap_tx_hash is
    already set, this is a retry after a crash -- re-observes state instead
    of resubmitting."""
    if order.swap_tx_hash:
        return order

    if order.status == states.QUOTE_LOCKED:
        order.mark_status(states.SWAP_EXECUTING)
        db.session.commit()
    elif order.status != states.SWAP_EXECUTING:
        return order

    quote = Quote(
        quote_id=order.quote_id,
        from_asset=order.from_asset,
        to_asset=order.to_asset,
        from_amount=order.from_amount,
        # The adapter deals exclusively in raw (gross, pre-margin) amounts --
        # to_amount_quoted is the client-facing net amount and must never be
        # fed back into a liquidity adapter call.
        to_amount=order.to_amount_quoted_gross,
        expires_at=order.quote_expires_at,
        raw_provider_response=order.quote_raw_response or {},
    )

    try:
        result = liquidity_adapter.execute_swap(quote)
    except LiquidityAdapterError as exc:
        order.last_error = str(exc)
        order.mark_status(states.FAILED)
        _audit(actor, "swap_execution_failed", order, {"error": str(exc)})
        db.session.commit()
        return order

    order.swap_tx_hash = result.tx_hash
    order.to_amount_executed = result.to_amount_executed
    _audit(actor, "swap_submitted", order, {"tx_hash": result.tx_hash})
    db.session.commit()
    return order


def poll_swap_completion(order, liquidity_adapter, min_confirmations: int, actor: str = "system"):
    """SWAP_EXECUTING -> SWAP_COMPLETE once the swap tx has enough
    confirmations. Writes the swap_in/swap_out ledger pair on completion,
    plus computes to_amount_payout (the net amount actually owed to the
    client) against the *executed* amount -- not the original quote -- so
    the house's margin percentage holds regardless of DEX slippage between
    quote and execution. The margin itself is recorded as a separate
    informational `revenue:margin:<asset>` ledger entry; it is not a
    reconciliation target (see app/ledger/reconciliation.py, which only
    reconciles `treasury:*` accounts against on-chain balances) since it
    has no on-chain existence of its own -- it's just the gap between what
    treasury:* is credited here and what send_withdrawal() later debits."""
    if order.status != states.SWAP_EXECUTING or not order.swap_tx_hash:
        return order

    status = liquidity_adapter.get_swap_status(order.swap_tx_hash, min_confirmations)

    if status == "confirmed":
        order.mark_status(states.SWAP_COMPLETE)

        margin_percent = order.margin_percent if order.margin_percent is not None else Decimal("0")
        order.to_amount_payout = apply_margin(order.to_amount_executed, margin_percent)
        fee = order.to_amount_executed - order.to_amount_payout

        db.session.add(LedgerEntry(
            swap_order_id=order.id, account=f"treasury:{order.to_chain}:{order.to_asset}",
            asset=order.to_asset, amount=order.to_amount_executed, entry_type="swap_in",
        ))
        db.session.add(LedgerEntry(
            swap_order_id=order.id, account=f"treasury:{order.from_chain}:{order.from_asset}",
            asset=order.from_asset, amount=-order.from_amount, entry_type="swap_out",
        ))
        if fee > 0:
            db.session.add(LedgerEntry(
                swap_order_id=order.id, account=f"revenue:margin:{order.to_asset}",
                asset=order.to_asset, amount=fee, entry_type="fee",
            ))
        _audit(actor, "swap_complete", order, {
            "tx_hash": order.swap_tx_hash, "to_amount_payout": str(order.to_amount_payout), "fee": str(fee),
        })
        db.session.commit()
    elif status == "failed":
        order.last_error = f"swap tx {order.swap_tx_hash} failed on-chain"
        order.mark_status(states.FAILED)
        _audit(actor, "swap_failed_onchain", order, {"tx_hash": order.swap_tx_hash})
        db.session.commit()

    return order


def request_withdrawal(order, withdrawal_address: str, actor: str):
    """SWAP_COMPLETE -> WITHDRAWAL_REQUESTED. Operator-initiated from
    /admin/orders/<id>, providing the destination address."""
    if order.status != states.SWAP_COMPLETE:
        raise states.InvalidTransitionError(order.status, states.WITHDRAWAL_REQUESTED)
    order.withdrawal_address = withdrawal_address
    order.mark_status(states.WITHDRAWAL_REQUESTED)
    _audit(actor, "withdrawal_requested", order, {"withdrawal_address": withdrawal_address})
    db.session.commit()
    return order


def gate_withdrawal(order, aml18_client, transfer_amount_eur: Decimal, actor: str = "system"):
    """WITHDRAWAL_REQUESTED: checks the AML-18 wallet-ownership requirement.
    Moves the order to WITHDRAWAL_VERIFICATION if a challenge is needed;
    otherwise leaves it in WITHDRAWAL_REQUESTED, cleared for
    send_withdrawal() to send immediately."""
    if order.status != states.WITHDRAWAL_REQUESTED:
        return order

    result = enforce_withdrawal_gate(order, aml18_client, float(transfer_amount_eur))
    _audit(actor, "withdrawal_gate_checked", order, {"required": result["required"]})
    db.session.commit()
    return order


def send_withdrawal(order, send_fn, actor: str = "system"):
    """Sends the withdrawal on-chain and marks WITHDRAWAL_SENT --
    deliberately the ONLY place that transition happens, and only once the
    compliance gate has actually cleared:
      - from WITHDRAWAL_REQUESTED: only if no challenge was ever created
        (gate_withdrawal decided verification wasn't required).
      - from WITHDRAWAL_VERIFICATION: only if a verification_id was
        recorded (submit_withdrawal_verification succeeded).
    `send_fn(order) -> tx_hash` is injected so this module stays decoupled
    from the concrete evm_wallet/btc_wallet send implementations.
    """
    if order.status == states.WITHDRAWAL_SENT:
        return order  # idempotent no-op, already sent

    if order.status == states.WITHDRAWAL_REQUESTED:
        if order.wallet_ownership_challenge_id:
            raise WithdrawalNotClearedError(order.id)
    elif order.status == states.WITHDRAWAL_VERIFICATION:
        if not order.wallet_ownership_verification_id:
            raise WithdrawalNotClearedError(order.id)
    else:
        raise states.InvalidTransitionError(order.status, states.WITHDRAWAL_SENT)

    if order.withdrawal_tx_hash:
        # Already submitted (retry after crash) -- just finish the status
        # transition without resending.
        order.mark_status(states.WITHDRAWAL_SENT)
        db.session.commit()
        return order

    tx_hash = send_fn(order)
    order.withdrawal_tx_hash = tx_hash
    order.mark_status(states.WITHDRAWAL_SENT)
    db.session.add(LedgerEntry(
        # Only the net (post-margin) amount leaves treasury -- the margin
        # portion stays behind, already reflected by the revenue:margin
        # entry poll_swap_completion() wrote at SWAP_COMPLETE.
        swap_order_id=order.id, account=f"treasury:{order.to_chain}:{order.to_asset}",
        asset=order.to_asset, amount=-order.to_amount_payout, entry_type="withdrawal",
    ))
    _audit(actor, "withdrawal_sent", order, {"tx_hash": tx_hash})
    db.session.commit()
    return order


def poll_withdrawal_completion(order, get_status_fn, min_confirmations: int, actor: str = "system"):
    """WITHDRAWAL_SENT -> DONE once the withdrawal tx has enough
    confirmations. `get_status_fn(tx_hash, min_confirmations) -> "pending"|"confirmed"|"failed"`."""
    if order.status != states.WITHDRAWAL_SENT or not order.withdrawal_tx_hash:
        return order

    status = get_status_fn(order.withdrawal_tx_hash, min_confirmations)
    if status == "confirmed":
        order.mark_status(states.DONE)
        _audit(actor, "withdrawal_confirmed", order, {"tx_hash": order.withdrawal_tx_hash})
        db.session.commit()
    elif status == "failed":
        order.last_error = f"withdrawal tx {order.withdrawal_tx_hash} failed on-chain"
        order.mark_status(states.FAILED)
        _audit(actor, "withdrawal_failed_onchain", order, {"tx_hash": order.withdrawal_tx_hash})
        db.session.commit()

    return order
