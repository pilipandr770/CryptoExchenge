"""Single-operator admin UI (ТЗ section 9) -- not a public dashboard, just a
window into the pipeline for the developer running their own demo. Session
login only (ADMIN_USERNAME/ADMIN_PASSWORD from .env); every other route is
gated by @login_required.
"""

import logging
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy import select

from app.accounts.balances import all_user_balances
from app.accounts.models import User
from app.audit.models import AuditLog
from app.compliance_client.aml18_client import Aml18Client
from app.compliance_client.screening_gate import WalletOwnershipVerificationFailedError, submit_withdrawal_verification
from app.custody import btc_wallet, evm_wallet, key_management, send
from app.custody.models import DepositAddress, HotWallet
from app.custody.send import SendError
from app.extensions import db
from app.ledger import reconciliation
from app.liquidity import factory as liquidity_factory
from app.liquidity.base import LiquidityAdapterError
from app.liquidity.btc_treasury_adapter import TreasuryRebalanceRequiredError
from app.liquidity.evm_dex_adapter import ASSET_DECIMALS, EvmDexAdapter
from app.swap import orchestrator, states
from app.swap.models import SwapOrder

admin_ui_bp = Blueprint("admin_ui", __name__, url_prefix="/admin", template_folder="templates")

logger = logging.getLogger(__name__)

PAGE_SIZE = 25
NATIVE_WITHDRAWAL_CHAINS = {"ethereum", "polygon"}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_ui.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# --- auth ----------------------------------------------------------------


@admin_ui_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("admin_logged_in"):
            return redirect(url_for("admin_ui.list_orders"))
        return render_template("admin_ui/login.html")

    username = request.form.get("username", "")
    password = request.form.get("password", "")
    expected_password = current_app.config["ADMIN_PASSWORD"]

    if not expected_password:
        flash("ADMIN_PASSWORD is not configured -- set it in .env before logging in.", "error")
        return render_template("admin_ui/login.html"), 400

    if username != current_app.config["ADMIN_USERNAME"] or password != expected_password:
        flash("Invalid username or password.", "error")
        return render_template("admin_ui/login.html"), 401

    session["admin_logged_in"] = True
    session["admin_username"] = username
    return redirect(request.args.get("next") or url_for("admin_ui.list_orders"))


@admin_ui_bp.get("/logout")
def logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin_ui.login"))


@admin_ui_bp.get("/")
@login_required
def index():
    return redirect(url_for("admin_ui.list_orders"))


# --- factories: build service clients from live config at request time ---
# (kept as free functions so tests can monkeypatch them without touching
# route bodies).


def _aml18_client() -> Aml18Client:
    cfg = current_app.config
    return Aml18Client(cfg["AML18_BASE_URL"], cfg["AML18_API_KEY"], cfg["AML18_REQUEST_TIMEOUT_SECONDS"])


def _load_mnemonic() -> str:
    cfg = current_app.config
    return key_management.load_mnemonic(cfg["HOT_WALLET_KEYS_FILE"], cfg["HOT_WALLET_KEYS_FERNET_KEY"])


def _liquidity_adapter_for(order: SwapOrder):
    return liquidity_factory.liquidity_adapter_for(order.from_asset)


def _send_withdrawal_fn(order: SwapOrder) -> str:
    """Native-asset withdrawal only in this MVP (ТЗ's own "done" definition
    is ETH/BTC end-to-end, see section 1) -- an ERC-20 (USDC etc.)
    withdrawal raises SendError rather than silently sending the wrong
    asset or amount."""
    cfg = current_app.config
    mnemonic = _load_mnemonic()

    if order.to_chain in NATIVE_WITHDRAWAL_CHAINS:
        if order.to_asset.upper() != "ETH":
            raise SendError(
                f"withdrawing {order.to_asset} on {order.to_chain} is not implemented in this MVP "
                "(native-asset withdrawal only -- see app/custody/send.py)"
            )
        private_key = evm_wallet.derive_private_key(mnemonic, 0)
        decimals = ASSET_DECIMALS["ETH"]
        # Only the client's net (post-margin) amount is ever sent -- see
        # app/pricing/margin.py and orchestrator.poll_swap_completion.
        amount_wei = int(order.to_amount_payout * (10 ** decimals))
        return send.send_evm_native(
            cfg["EVM_RPC_URL"], private_key, cfg["EVM_CHAIN_ID"], order.withdrawal_address, amount_wei,
        )

    if order.to_chain == "bitcoin":
        private_key = btc_wallet.derive_private_key(mnemonic, cfg["BTC_NETWORK"], 0)
        amount_sats = int(order.to_amount_payout * (10 ** 8))
        return send.send_btc(
            cfg["BTC_ESPLORA_API_BASE_URL"], private_key.to_wif(), order.withdrawal_address, amount_sats,
        )

    raise SendError(f"unsupported withdrawal chain {order.to_chain!r}")


def _decimal_form_field(field_name: str):
    """Parses a form field as Decimal from its raw string, not float --
    `request.form.get(name, type=float)` would round-trip the value through
    binary floating point before it ever reaches a Numeric(36,18) column
    (e.g. "0.499" -> 0.498999999999999999...), silently corrupting a money
    amount. Returns None if absent or not a valid decimal."""
    raw = request.form.get(field_name)
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _audit(actor: str, action: str, target_type: str, target_id: str, detail: dict = None):
    db.session.add(AuditLog(actor=actor, action=action, target_type=target_type, target_id=target_id, detail=detail or {}))


# --- orders ----------------------------------------------------------------


SUPPORTED_CHAINS = ("ethereum", "polygon", "bitcoin")


@admin_ui_bp.get("/orders/new")
@login_required
def new_order_form():
    return render_template("admin_ui/order_new.html", chains=SUPPORTED_CHAINS)


@admin_ui_bp.post("/orders/new")
@login_required
def create_order():
    from_chain = (request.form.get("from_chain") or "").strip()
    from_asset = (request.form.get("from_asset") or "").strip().upper()
    from_amount = _decimal_form_field("from_amount")
    to_chain = (request.form.get("to_chain") or "").strip()
    to_asset = (request.form.get("to_asset") or "").strip().upper()
    label = (request.form.get("label") or "").strip()
    operator = session.get("admin_username", current_app.config["ADMIN_USERNAME"])

    if not all([from_chain, from_asset, from_amount, to_chain, to_asset, label]):
        flash("Label, deposit chain/asset/amount, and swap-to chain/asset are all required.", "error")
        return redirect(url_for("admin_ui.new_order_form"))

    if from_chain not in SUPPORTED_CHAINS or to_chain not in SUPPORTED_CHAINS:
        flash(f"Unsupported chain (must be one of {', '.join(SUPPORTED_CHAINS)}).", "error")
        return redirect(url_for("admin_ui.new_order_form"))

    try:
        mnemonic = _load_mnemonic()
    except key_management.KeyManagementError as exc:
        flash(f"Could not load the hot wallet mnemonic: {exc}", "error")
        return redirect(url_for("admin_ui.new_order_form"))

    existing_max_index = (
        db.session.query(db.func.max(DepositAddress.derivation_index))
        .filter_by(chain=from_chain)
        .scalar()
    )
    next_index = 0 if existing_max_index is None else existing_max_index + 1

    if from_chain == "bitcoin":
        address = btc_wallet.derive_address(mnemonic, current_app.config["BTC_NETWORK"], next_index)
    else:
        # Polygon and Ethereum share the same BIP-44 EVM derivation path --
        # standard practice for EVM-compatible chains, same address on both.
        address = evm_wallet.derive_address(mnemonic, next_index)

    deposit_address = DepositAddress(chain=from_chain, address=address, derivation_index=next_index, label=label)
    db.session.add(deposit_address)
    db.session.flush()

    client_name = (request.form.get("client_name") or "").strip() or None

    order = SwapOrder(
        deposit_address_id=deposit_address.id,
        from_chain=from_chain, from_asset=from_asset, from_amount=from_amount,
        to_chain=to_chain, to_asset=to_asset,
        client_name=client_name,
    )
    db.session.add(order)
    db.session.flush()

    _audit(operator, "order_created", "SwapOrder", str(order.id), {
        "from_chain": from_chain, "from_asset": from_asset, "from_amount": str(from_amount),
        "deposit_address": address, "label": label,
    })
    db.session.commit()

    flash(f"Order #{order.id} created. Send the deposit to: {address}", "success")
    return redirect(url_for("admin_ui.order_detail", order_id=order.id))


@admin_ui_bp.get("/orders")
@login_required
def list_orders():
    status_filter = request.args.get("status")
    page = max(1, request.args.get("page", 1, type=int))

    stmt = select(SwapOrder).order_by(SwapOrder.created_at.desc())
    if status_filter in states.ALL_STATUSES:
        stmt = stmt.where(SwapOrder.status == status_filter)
    else:
        status_filter = None

    pagination = db.paginate(stmt, page=page, per_page=PAGE_SIZE, error_out=False)

    return render_template(
        "admin_ui/orders_list.html",
        orders=pagination.items,
        all_statuses=states.ALL_STATUSES,
        current_filter=status_filter,
        page=page,
        has_prev=pagination.has_prev,
        has_next=pagination.has_next,
    )


@admin_ui_bp.get("/orders/<int:order_id>")
@login_required
def order_detail(order_id):
    order = db.session.get(SwapOrder, order_id)
    if order is None:
        return render_template("admin_ui/orders_list.html", orders=[], all_statuses=states.ALL_STATUSES, current_filter=None, page=1, has_prev=False, has_next=False), 404

    audit_entries = (
        AuditLog.query
        .filter_by(target_type="SwapOrder", target_id=str(order_id))
        .order_by(AuditLog.created_at.desc())
        .all()
    )
    return render_template("admin_ui/order_detail.html", order=order, audit_entries=audit_entries, states=states)


@admin_ui_bp.post("/orders/<int:order_id>/run-screening")
@login_required
def run_screening(order_id):
    order = db.session.get(SwapOrder, order_id)
    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_ui.list_orders"))

    client_name = (request.form.get("client_name") or order.client_name or "").strip()
    if not client_name:
        flash("A client name is required to run the sanctions screening check.", "error")
        return redirect(url_for("admin_ui.order_detail", order_id=order_id))

    operator = session.get("admin_username", current_app.config["ADMIN_USERNAME"])
    order.client_name = client_name

    try:
        orchestrator.run_screening_and_lock_quote(
            order, _aml18_client(), _liquidity_adapter_for(order), current_app.config["MARGIN_PERCENT"],
            name=client_name, date_of_birth=order.client_date_of_birth, country=order.client_country,
            actor=operator,
        )
        if order.status == states.QUOTE_LOCKED:
            flash(f"Order {order_id} screened and quote locked.", "success")
        elif order.status == states.PENDING_MANUAL_REVIEW:
            flash(f"Order {order_id} flagged for manual review (decision: {order.screening_decision}).", "success")
    except (states.InvalidTransitionError, LiquidityAdapterError) as exc:
        db.session.rollback()
        flash(f"Could not screen order {order_id}: {exc}", "error")

    return redirect(url_for("admin_ui.order_detail", order_id=order_id))


@admin_ui_bp.post("/orders/<int:order_id>/approve-review")
@login_required
def approve_review(order_id):
    order = db.session.get(SwapOrder, order_id)
    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_ui.list_orders"))

    operator = session.get("admin_username", current_app.config["ADMIN_USERNAME"])
    try:
        orchestrator.approve_manual_review(order, operator=operator)
        orchestrator.lock_quote(order, _liquidity_adapter_for(order), current_app.config["MARGIN_PERCENT"], actor=operator)
        flash(f"Order {order_id} approved and quote locked.", "success")
    except (states.InvalidTransitionError, LiquidityAdapterError) as exc:
        flash(f"Could not approve order {order_id}: {exc}", "error")

    return redirect(url_for("admin_ui.order_detail", order_id=order_id))


@admin_ui_bp.post("/orders/<int:order_id>/execute-swap")
@login_required
def trigger_execute_swap(order_id):
    order = db.session.get(SwapOrder, order_id)
    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_ui.list_orders"))

    orchestrator.execute_swap(order, _liquidity_adapter_for(order))
    orchestrator.poll_swap_completion(order, _liquidity_adapter_for(order), current_app.config["EVM_MIN_CONFIRMATIONS"])
    return redirect(url_for("admin_ui.order_detail", order_id=order_id))


@admin_ui_bp.post("/orders/<int:order_id>/retry-rebalance")
@login_required
def retry_rebalance(order_id):
    order = db.session.get(SwapOrder, order_id)
    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_ui.list_orders"))

    operator = session.get("admin_username", current_app.config["ADMIN_USERNAME"])
    try:
        orchestrator.retry_after_rebalance(order, _liquidity_adapter_for(order), operator=operator)
        flash(f"Order {order_id} resumed after treasury rebalance.", "success")
    except (states.InvalidTransitionError, TreasuryRebalanceRequiredError) as exc:
        flash(f"Could not resume order {order_id}: {exc}", "error")

    return redirect(url_for("admin_ui.order_detail", order_id=order_id))


@admin_ui_bp.post("/orders/<int:order_id>/request-withdrawal")
@login_required
def request_withdrawal(order_id):
    order = db.session.get(SwapOrder, order_id)
    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_ui.list_orders"))

    # Public orders (app/public_ui) already collected the client's own
    # withdrawal address at creation time -- don't let this form override
    # it with something else; only admin-created orders (which have no
    # address yet) take one from the form.
    withdrawal_address = order.withdrawal_address or (request.form.get("withdrawal_address") or "").strip()
    transfer_amount_eur = request.form.get("transfer_amount_eur", type=float)
    operator = session.get("admin_username", current_app.config["ADMIN_USERNAME"])

    if not withdrawal_address or transfer_amount_eur is None:
        flash("Withdrawal address and EUR-equivalent amount are both required.", "error")
        return redirect(url_for("admin_ui.order_detail", order_id=order_id))

    try:
        orchestrator.request_withdrawal(order, withdrawal_address, actor=operator)
        orchestrator.gate_withdrawal(order, _aml18_client(), transfer_amount_eur=transfer_amount_eur, actor=operator)
        if order.status == states.WITHDRAWAL_REQUESTED:
            # Gate said verification wasn't required -- safe to send now.
            orchestrator.send_withdrawal(order, _send_withdrawal_fn, actor=operator)
            flash(f"Withdrawal sent for order {order_id}.", "success")
        else:
            flash(f"Wallet-ownership verification required for order {order_id} -- sign the challenge and submit it below.", "success")
    except (states.InvalidTransitionError, SendError, orchestrator.WithdrawalNotClearedError) as exc:
        flash(f"Could not process withdrawal for order {order_id}: {exc}", "error")

    return redirect(url_for("admin_ui.order_detail", order_id=order_id))


@admin_ui_bp.post("/orders/<int:order_id>/submit-verification")
@login_required
def submit_verification(order_id):
    order = db.session.get(SwapOrder, order_id)
    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_ui.list_orders"))

    signature = (request.form.get("signature") or "").strip()
    transfer_amount_eur = request.form.get("transfer_amount_eur", type=float)
    operator = session.get("admin_username", current_app.config["ADMIN_USERNAME"])

    if not signature:
        flash("Signature is required.", "error")
        return redirect(url_for("admin_ui.order_detail", order_id=order_id))

    try:
        submit_withdrawal_verification(order, _aml18_client(), signature, transfer_amount_eur=transfer_amount_eur)
        db.session.commit()
        orchestrator.send_withdrawal(order, _send_withdrawal_fn, actor=operator)
        flash(f"Wallet ownership verified and withdrawal sent for order {order_id}.", "success")
    except WalletOwnershipVerificationFailedError as exc:
        db.session.commit()
        flash(f"Verification failed for order {order_id}: {exc}", "error")
    except (SendError, orchestrator.WithdrawalNotClearedError) as exc:
        flash(f"Verification succeeded but sending failed for order {order_id}: {exc}", "error")

    return redirect(url_for("admin_ui.order_detail", order_id=order_id))


@admin_ui_bp.post("/orders/<int:order_id>/poll-withdrawal")
@login_required
def poll_withdrawal(order_id):
    order = db.session.get(SwapOrder, order_id)
    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_ui.list_orders"))

    cfg = current_app.config
    if order.to_chain in NATIVE_WITHDRAWAL_CHAINS:
        adapter = EvmDexAdapter(api_base_url=cfg["ZEROX_API_BASE_URL"], rpc_url=cfg["EVM_RPC_URL"], chain_id=cfg["EVM_CHAIN_ID"])
        get_status_fn = adapter.get_swap_status
        min_confirmations = cfg["EVM_MIN_CONFIRMATIONS"]
    else:
        from app.chain_listeners.btc_listener import BtcListener
        listener = BtcListener(cfg["BTC_ESPLORA_API_BASE_URL"], cfg["BTC_MIN_CONFIRMATIONS"])

        def get_status_fn(tx_hash, min_conf):
            tip = listener.tip_height()
            tx = listener._get(f"/tx/{tx_hash}")
            status = tx.get("status", {})
            if not status.get("confirmed"):
                return "pending"
            confirmations = tip - status["block_height"] + 1
            return "confirmed" if confirmations >= min_conf else "pending"

        min_confirmations = cfg["BTC_MIN_CONFIRMATIONS"]

    orchestrator.poll_withdrawal_completion(order, get_status_fn, min_confirmations)
    return redirect(url_for("admin_ui.order_detail", order_id=order_id))


# --- treasury ----------------------------------------------------------


@admin_ui_bp.get("/treasury")
@login_required
def treasury():
    hot_wallets = HotWallet.query.order_by(HotWallet.chain).all()
    ledger_balances = reconciliation.ledger_balances_by_account_asset()
    treasury_balances = sorted(
        ({"account": account, "asset": asset, "balance": balance}
         for (account, asset), balance in ledger_balances.items() if account.startswith("treasury:")),
        key=lambda row: row["account"],
    )
    return render_template("admin_ui/treasury.html", hot_wallets=hot_wallets, treasury_balances=treasury_balances)


@admin_ui_bp.post("/treasury/rebalance")
@login_required
def treasury_rebalance():
    btc_amount = _decimal_form_field("btc_amount")
    wbtc_amount = _decimal_form_field("wbtc_amount")
    note = (request.form.get("note") or "").strip()
    operator = session.get("admin_username", current_app.config["ADMIN_USERNAME"])

    if not btc_amount or not wbtc_amount:
        flash("Both BTC and WBTC amounts are required to record a rebalance.", "error")
        return redirect(url_for("admin_ui.treasury"))

    from app.ledger.models import LedgerEntry

    db.session.add(LedgerEntry(account="treasury:bitcoin:BTC", asset="BTC", amount=-btc_amount, entry_type="swap_out"))
    db.session.add(LedgerEntry(account="treasury:ethereum:WBTC", asset="WBTC", amount=wbtc_amount, entry_type="swap_in"))
    _audit(operator, "treasury_rebalance_recorded", "Treasury", "BTC<->WBTC", {
        "btc_amount": str(btc_amount), "wbtc_amount": str(wbtc_amount), "note": note,
    })
    db.session.commit()

    flash(f"Recorded manual rebalance: {btc_amount} BTC -> {wbtc_amount} WBTC.", "success")
    return redirect(url_for("admin_ui.treasury"))


# --- ledger reconciliation ------------------------------------------------


@admin_ui_bp.get("/ledger")
@login_required
def ledger():
    cfg = current_app.config
    onchain_balances = {}
    fetch_errors = []

    eth_wallet = HotWallet.query.filter_by(chain="ethereum").first()
    if eth_wallet and cfg["EVM_RPC_URL"]:
        try:
            import requests
            response = requests.post(
                cfg["EVM_RPC_URL"],
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [eth_wallet.address, "latest"]},
                timeout=cfg.get("EVM_REQUEST_TIMEOUT_SECONDS", 10),
            )
            response.raise_for_status()
            result = response.json()
            if "result" in result:
                from decimal import Decimal
                onchain_balances[("ethereum", "ETH")] = Decimal(int(result["result"], 16)) / Decimal(10 ** 18)
        except Exception as exc:  # best-effort -- degrade gracefully on any fetch failure
            fetch_errors.append(f"ethereum ETH balance: {exc}")

    btc_hot_wallet = HotWallet.query.filter_by(chain="bitcoin").first()
    if btc_hot_wallet and cfg["BTC_ESPLORA_API_BASE_URL"]:
        try:
            import requests
            response = requests.get(
                f"{cfg['BTC_ESPLORA_API_BASE_URL'].rstrip('/')}/address/{btc_hot_wallet.address}",
                timeout=10,
            )
            response.raise_for_status()
            stats = response.json()
            from decimal import Decimal
            funded = stats.get("chain_stats", {}).get("funded_txo_sum", 0)
            spent = stats.get("chain_stats", {}).get("spent_txo_sum", 0)
            onchain_balances[("bitcoin", "BTC")] = Decimal(funded - spent) / Decimal(10 ** 8)
        except Exception as exc:
            fetch_errors.append(f"bitcoin BTC balance: {exc}")

    results = reconciliation.reconcile_treasury_accounts(onchain_balances) if onchain_balances else []

    ledger_only = sorted(
        ({"account": account, "asset": asset, "balance": balance}
         for (account, asset), balance in reconciliation.ledger_balances_by_account_asset().items()
         if account.startswith("treasury:") and (account.split(":", 1)[1].split(":")[0], asset) not in onchain_balances),
        key=lambda row: row["account"],
    )

    return render_template("admin_ui/ledger.html", results=results, ledger_only=ledger_only, fetch_errors=fetch_errors)


# --- registered users (app/accounts) --------------------------------------


@admin_ui_bp.get("/users")
@login_required
def users_list():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template("admin_ui/users_list.html", users=users)


@admin_ui_bp.get("/users/<int:user_id>")
@login_required
def user_detail(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        flash("User not found.", "error")
        return redirect(url_for("admin_ui.users_list"))

    balances = all_user_balances(user.id)
    orders = SwapOrder.query.filter_by(user_id=user.id).order_by(SwapOrder.created_at.desc()).all()
    return render_template("admin_ui/user_detail.html", user=user, balances=balances, orders=orders)


@admin_ui_bp.post("/users/<int:user_id>/approve")
@login_required
def approve_user(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        flash("User not found.", "error")
        return redirect(url_for("admin_ui.users_list"))

    operator = session.get("admin_username", current_app.config["ADMIN_USERNAME"])
    previous_decision = user.screening_decision
    user.is_active = True
    _audit(operator, "user_approved", "User", str(user.id), {"previous_screening_decision": previous_decision})
    db.session.commit()

    flash(f"Account {user.email} approved.", "success")
    return redirect(url_for("admin_ui.user_detail", user_id=user_id))


@admin_ui_bp.post("/users/<int:user_id>/freeze")
@login_required
def freeze_user(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        flash("User not found.", "error")
        return redirect(url_for("admin_ui.users_list"))

    operator = session.get("admin_username", current_app.config["ADMIN_USERNAME"])
    user.is_active = False
    _audit(operator, "user_frozen", "User", str(user.id), {})
    db.session.commit()

    flash(f"Account {user.email} frozen.", "success")
    return redirect(url_for("admin_ui.user_detail", user_id=user_id))
