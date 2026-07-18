"""Public, no-login client-facing UI -- "instant exchanger" style
(FixedFloat/SimpleSwap), the natural fit for the existing SwapOrder/
DepositAddress pipeline. Orders are tracked by an opaque public_token,
never the sequential integer id, so one client can never enumerate
another's order by guessing a URL.

Still demo/MVP scope (see RUNBOOK.md): an admin still manually drives every
pipeline step from /admin (run screening, execute swap, request
withdrawal...). This blueprint is a create/read-only window into the same
SwapOrder pipeline -- it adds no new automation, and exposes none of the
internal compliance/treasury detail the admin UI shows.
"""

import logging
from decimal import Decimal, InvalidOperation

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from app.audit.models import AuditLog
from app.custody import btc_wallet, evm_wallet
from app.custody.key_management import KeyManagementError
from app.custody.models import DepositAddress
from app.extensions import db
from app.liquidity import factory as liquidity_factory
from app.liquidity.base import LiquidityAdapterError
from app.liquidity.btc_treasury_adapter import TreasuryRebalanceRequiredError
from app.pricing.margin import apply_margin
from app.swap import states
from app.swap.models import SwapOrder

public_ui_bp = Blueprint("public_ui", __name__, url_prefix="/exchange", template_folder="templates")

logger = logging.getLogger(__name__)

SUPPORTED_CHAINS = ("ethereum", "polygon", "bitcoin")

# Deliberately vague on anything that would leak internal pipeline detail
# to a client -- "under review" not "flagged by sanctions screening",
# "preparing your exchange" not "waiting on treasury rebalance".
PUBLIC_STATUS_LABELS = {
    states.DEPOSIT_PENDING: "Waiting for your deposit",
    states.DEPOSIT_CONFIRMED: "Deposit received, verifying",
    states.SCREENING: "Verifying",
    states.PENDING_MANUAL_REVIEW: "Under review",
    states.QUOTE_LOCKED: "Preparing your exchange",
    states.PENDING_TREASURY_REBALANCE: "Preparing your exchange",
    states.SWAP_EXECUTING: "Exchanging",
    states.SWAP_COMPLETE: "Preparing payout",
    states.WITHDRAWAL_REQUESTED: "Preparing payout",
    states.WITHDRAWAL_VERIFICATION: "Verifying payout address",
    states.WITHDRAWAL_SENT: "Payout sent",
    states.DONE: "Completed",
    states.FAILED: "Failed -- please contact support",
    states.REFUND_PENDING: "Refund in progress",
    states.REFUNDED: "Refunded",
}


def _decimal_form_field(field_name: str):
    raw = request.form.get(field_name)
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _form_values():
    return {
        "from_chain": (request.form.get("from_chain") or "").strip(),
        "from_asset": (request.form.get("from_asset") or "").strip().upper(),
        "from_amount": request.form.get("from_amount", ""),
        "to_chain": (request.form.get("to_chain") or "").strip(),
        "to_asset": (request.form.get("to_asset") or "").strip().upper(),
    }


@public_ui_bp.get("/")
def landing():
    return render_template("public_ui/landing.html", chains=SUPPORTED_CHAINS)


@public_ui_bp.post("/quote")
def preview_quote():
    form_values = _form_values()
    from_amount = _decimal_form_field("from_amount")

    if not all(form_values.values()) or from_amount is None or from_amount <= 0:
        flash("All fields are required and the amount must be positive.", "error")
        return render_template("public_ui/landing.html", chains=SUPPORTED_CHAINS, form_values=form_values), 400

    if form_values["from_chain"] not in SUPPORTED_CHAINS or form_values["to_chain"] not in SUPPORTED_CHAINS:
        flash(f"Unsupported chain (must be one of {', '.join(SUPPORTED_CHAINS)}).", "error")
        return render_template("public_ui/landing.html", chains=SUPPORTED_CHAINS, form_values=form_values), 400

    try:
        adapter = liquidity_factory.liquidity_adapter_for(form_values["from_asset"])
        quote = adapter.get_quote(form_values["from_asset"], form_values["to_asset"], from_amount)
    except TreasuryRebalanceRequiredError:
        flash("This pair is temporarily unavailable -- please try a smaller amount or a different asset.", "error")
        return render_template("public_ui/landing.html", chains=SUPPORTED_CHAINS, form_values=form_values), 400
    except LiquidityAdapterError as exc:
        logger.warning("public quote preview failed: %s", exc)
        flash("Could not get a live rate right now -- please try again in a moment.", "error")
        return render_template("public_ui/landing.html", chains=SUPPORTED_CHAINS, form_values=form_values), 400

    net_amount = apply_margin(quote.to_amount, current_app.config["MARGIN_PERCENT"])

    return render_template(
        "public_ui/landing.html", chains=SUPPORTED_CHAINS, form_values=form_values,
        preview={"to_amount": net_amount, "expires_at": quote.expires_at},
    )


@public_ui_bp.post("/create")
def create_order():
    form_values = _form_values()
    from_amount = _decimal_form_field("from_amount")
    client_name = (request.form.get("client_name") or "").strip()
    client_email = (request.form.get("client_email") or "").strip() or None
    withdrawal_address = (request.form.get("withdrawal_address") or "").strip()

    if (
        not all(form_values.values()) or from_amount is None or from_amount <= 0
        or not client_name or not withdrawal_address
    ):
        flash("Your name, a destination address, and all exchange fields are required.", "error")
        return render_template("public_ui/landing.html", chains=SUPPORTED_CHAINS, form_values=form_values), 400

    if form_values["from_chain"] not in SUPPORTED_CHAINS or form_values["to_chain"] not in SUPPORTED_CHAINS:
        flash(f"Unsupported chain (must be one of {', '.join(SUPPORTED_CHAINS)}).", "error")
        return render_template("public_ui/landing.html", chains=SUPPORTED_CHAINS, form_values=form_values), 400

    try:
        mnemonic = liquidity_factory.load_hot_wallet_mnemonic()
    except KeyManagementError as exc:
        logger.error("public order creation: hot wallet not configured: %s", exc)
        flash("This exchanger isn't accepting deposits right now -- please try again later.", "error")
        return render_template("public_ui/landing.html", chains=SUPPORTED_CHAINS, form_values=form_values), 503

    from_chain = form_values["from_chain"]
    existing_max_index = (
        db.session.query(db.func.max(DepositAddress.derivation_index))
        .filter_by(chain=from_chain)
        .scalar()
    )
    next_index = 0 if existing_max_index is None else existing_max_index + 1

    if from_chain == "bitcoin":
        address = btc_wallet.derive_address(mnemonic, current_app.config["BTC_NETWORK"], next_index)
    else:
        # Polygon and Ethereum share the same BIP-44 EVM derivation path.
        address = evm_wallet.derive_address(mnemonic, next_index)

    deposit_address = DepositAddress(
        chain=from_chain, address=address, derivation_index=next_index,
        label=f"client:{client_name}"[:128],
    )
    db.session.add(deposit_address)
    db.session.flush()

    order = SwapOrder(
        deposit_address_id=deposit_address.id,
        from_chain=from_chain, from_asset=form_values["from_asset"], from_amount=from_amount,
        to_chain=form_values["to_chain"], to_asset=form_values["to_asset"],
        client_name=client_name, client_email=client_email,
        withdrawal_address=withdrawal_address,
    )
    db.session.add(order)
    db.session.flush()

    db.session.add(AuditLog(
        actor=f"client:{client_name}", action="order_created", target_type="SwapOrder", target_id=str(order.id),
        detail={
            "from_chain": from_chain, "from_asset": form_values["from_asset"], "from_amount": str(from_amount),
            "deposit_address": address, "withdrawal_address": withdrawal_address,
        },
    ))
    db.session.commit()

    return redirect(url_for("public_ui.order_status", token=order.public_token))


@public_ui_bp.get("/order/<token>")
def order_status(token):
    order = SwapOrder.query.filter_by(public_token=token).first()
    if order is None:
        return render_template("public_ui/order_not_found.html"), 404

    return render_template(
        "public_ui/order_status.html",
        order=order,
        status_label=PUBLIC_STATUS_LABELS.get(order.status, order.status),
        is_terminal=order.status in states.TERMINAL_STATUSES,
    )
