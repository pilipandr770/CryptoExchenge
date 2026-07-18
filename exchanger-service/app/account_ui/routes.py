"""Account-based client UI: register -> mandatory 2FA setup -> login ->
dashboard (persistent balance) -> deposit / swap / withdraw / transfer /
order history. Replaces app/public_ui entirely (project decision): every
action here requires a session, and every swap/withdrawal debits/credits a
real per-user balance instead of a one-shot anonymous order.
"""

import base64
import io
import logging
from decimal import Decimal, InvalidOperation

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from app.accounts import auth
from app.accounts.balances import InsufficientBalanceError, all_user_balances, user_balance
from app.accounts.deposits import get_or_create_deposit_address
from app.accounts.models import User
from app.accounts.transfers import RecipientNotFoundError, SameAccountTransferError, create_transfer
from app.compliance_client.aml18_client import Aml18Client, Aml18ClientError
from app.compliance_client.screening_gate import WalletOwnershipVerificationFailedError, submit_withdrawal_verification
from app.custody import btc_wallet, evm_wallet, send
from app.custody.key_management import KeyManagementError
from app.custody.send import SendError
from app.extensions import db
from app.liquidity import factory as liquidity_factory
from app.liquidity.base import LiquidityAdapterError
from app.liquidity.btc_treasury_adapter import TreasuryRebalanceRequiredError
from app.liquidity.evm_dex_adapter import ASSET_DECIMALS
from app.pricing.margin import apply_margin
from app.swap import orchestrator, states
from app.swap.models import SwapOrder

account_ui_bp = Blueprint("account_ui", __name__, url_prefix="/account", template_folder="templates")

logger = logging.getLogger(__name__)

SUPPORTED_CHAINS = ("ethereum", "polygon", "bitcoin")
NATIVE_WITHDRAWAL_CHAINS = {"ethereum", "polygon"}


# --- small per-blueprint factories (deliberately duplicated from admin_ui's
# equivalents rather than shared -- each blueprint's factories stay small,
# self-contained, and easy to monkeypatch independently in tests). --------


def _aml18_client() -> Aml18Client:
    cfg = current_app.config
    return Aml18Client(cfg["AML18_BASE_URL"], cfg["AML18_API_KEY"], cfg["AML18_REQUEST_TIMEOUT_SECONDS"])


def _send_withdrawal_fn(order: SwapOrder) -> str:
    """Native-asset withdrawal only in this MVP -- see
    app/admin_ui/routes.py's identical rationale."""
    cfg = current_app.config
    mnemonic = liquidity_factory.load_hot_wallet_mnemonic()

    if order.to_chain in NATIVE_WITHDRAWAL_CHAINS:
        if order.to_asset.upper() != "ETH":
            raise SendError(
                f"withdrawing {order.to_asset} on {order.to_chain} is not implemented in this MVP "
                "(native-asset withdrawal only -- see app/custody/send.py)"
            )
        private_key = evm_wallet.derive_private_key(mnemonic, 0)
        amount_wei = int(order.to_amount_payout * (10 ** ASSET_DECIMALS["ETH"]))
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
    see app/admin_ui/routes.py's identical helper for why this matters for
    money amounts landing in a Numeric(36,18) column."""
    raw = request.form.get(field_name)
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _swap_form_values() -> dict:
    return {
        "from_chain": (request.form.get("from_chain") or "").strip(),
        "from_asset": (request.form.get("from_asset") or "").strip().upper(),
        "from_amount": request.form.get("from_amount", ""),
        "to_chain": (request.form.get("to_chain") or "").strip(),
        "to_asset": (request.form.get("to_asset") or "").strip().upper(),
    }


def _qr_data_uri(otpauth_uri: str) -> str:
    import qrcode
    from qrcode.image.pure import PyPNGImage

    image = qrcode.make(otpauth_uri, image_factory=PyPNGImage)
    buf = io.BytesIO()
    image.save(buf)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _settle_pending_swaps(user_id: int) -> None:
    """Opportunistically advances any of this user's in-flight
    balance-funded swaps: polls for on-chain confirmation and, once
    confirmed, credits the destination balance. Called at the top of
    dashboard/orders views so a self-service swap completes without a
    background worker or an operator click."""
    executing_orders = SwapOrder.query.filter_by(
        user_id=user_id, funding_source="account_balance", status=states.SWAP_EXECUTING,
    ).all()
    for order in executing_orders:
        adapter = liquidity_factory.liquidity_adapter_for(order.from_asset)
        orchestrator.poll_swap_completion(order, adapter, current_app.config["EVM_MIN_CONFIRMATIONS"])
        if order.status == states.SWAP_COMPLETE:
            orchestrator.settle_to_balance(order)


# --- landing / registration / 2FA / login ---------------------------------


@account_ui_bp.get("/")
def landing():
    return render_template("account_ui/landing.html")


@account_ui_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("account_ui/register.html")

    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    full_name = (request.form.get("full_name") or "").strip()
    date_of_birth = (request.form.get("date_of_birth") or "").strip() or None
    country = (request.form.get("country") or "").strip() or None

    if not email or not password or not full_name:
        flash("E-Mail, Passwort und vollständiger Name sind erforderlich.", "error")
        return render_template("account_ui/register.html"), 400

    try:
        user = auth.register_user(email, password, full_name, date_of_birth=date_of_birth, country=country)
    except auth.RegistrationError:
        flash("Für diese E-Mail-Adresse existiert bereits ein Konto.", "error")
        return render_template("account_ui/register.html"), 400

    try:
        auth.screen_new_user(user, _aml18_client())
    except Aml18ClientError as exc:
        # Fail closed: an unscreened account must never be treated as
        # cleared. An admin can activate it later from /admin/users.
        logger.error("registration screening failed for user %s: %s", user.id, exc)
        user.is_active = False

    db.session.commit()

    session["pending_2fa_setup_user_id"] = user.id
    return redirect(url_for("account_ui.setup_2fa"))


@account_ui_bp.route("/2fa-setup", methods=["GET", "POST"])
def setup_2fa():
    user_id = session.get("pending_2fa_setup_user_id")
    if not user_id:
        return redirect(url_for("account_ui.login"))
    user = db.session.get(User, user_id)
    if user is None or user.totp_enabled:
        return redirect(url_for("account_ui.login"))

    if request.method == "GET":
        secret = auth.generate_totp_secret()
        session["pending_totp_secret"] = secret
        qr_data_uri = _qr_data_uri(auth.provisioning_uri(secret, user.email))
        return render_template("account_ui/setup_2fa.html", secret=secret, qr_data_uri=qr_data_uri)

    secret = session.get("pending_totp_secret")
    code = request.form.get("code") or ""
    if not secret or not auth.verify_totp_code(secret, code):
        flash("Ungültiger Code -- bitte in der Authenticator-App prüfen und erneut versuchen.", "error")
        qr_data_uri = _qr_data_uri(auth.provisioning_uri(secret or auth.generate_totp_secret(), user.email))
        return render_template("account_ui/setup_2fa.html", secret=secret, qr_data_uri=qr_data_uri), 400

    auth.enable_totp(user, secret)
    db.session.commit()
    session.pop("pending_totp_secret", None)
    session.pop("pending_2fa_setup_user_id", None)
    session["user_id"] = user.id

    if user.is_active:
        flash("Zwei-Faktor-Authentifizierung aktiviert. Willkommen!", "success")
    else:
        flash("Konto erstellt, wartet aber auf manuelle Prüfung, bevor Sie einzahlen oder handeln können.", "success")
    return redirect(url_for("account_ui.dashboard"))


@account_ui_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("user_id"):
            return redirect(url_for("account_ui.dashboard"))
        return render_template("account_ui/login.html")

    user = auth.authenticate(request.form.get("email") or "", request.form.get("password") or "")
    if user is None:
        flash("E-Mail oder Passwort ungültig.", "error")
        return render_template("account_ui/login.html"), 401

    if not user.totp_enabled:
        # Shouldn't normally happen (2FA setup is mandatory right after
        # registration) but handle it rather than dead-ending.
        session["pending_2fa_setup_user_id"] = user.id
        return redirect(url_for("account_ui.setup_2fa"))

    session["pending_login_user_id"] = user.id
    return redirect(url_for("account_ui.verify_login_2fa"))


@account_ui_bp.route("/login/verify-2fa", methods=["GET", "POST"])
def verify_login_2fa():
    user_id = session.get("pending_login_user_id")
    if not user_id:
        return redirect(url_for("account_ui.login"))
    user = db.session.get(User, user_id)

    if request.method == "GET":
        return render_template("account_ui/verify_2fa.html")

    try:
        secret = auth.get_totp_secret(user)
    except KeyManagementError as exc:
        logger.error("could not load TOTP secret for user %s: %s", user.id, exc)
        flash("Ihr Code konnte gerade nicht geprüft werden -- bitte versuchen Sie es erneut.", "error")
        return render_template("account_ui/verify_2fa.html"), 500

    if not auth.verify_totp_code(secret, request.form.get("code") or ""):
        flash("Ungültiger Code.", "error")
        return render_template("account_ui/verify_2fa.html"), 401

    session.pop("pending_login_user_id", None)
    session["user_id"] = user.id
    return redirect(url_for("account_ui.dashboard"))


@account_ui_bp.get("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("account_ui.login"))


# --- dashboard / deposit ---------------------------------------------------


@account_ui_bp.get("/dashboard")
@auth.login_required
def dashboard():
    user = auth.current_user()
    _settle_pending_swaps(user.id)
    balances = all_user_balances(user.id)
    return render_template("account_ui/dashboard.html", user=user, balances=balances)


@account_ui_bp.route("/deposit", methods=["GET", "POST"])
@auth.login_required
def deposit():
    user = auth.current_user()

    if request.method == "GET":
        return render_template("account_ui/deposit.html", chains=SUPPORTED_CHAINS, address=None)

    chain = (request.form.get("chain") or "").strip()
    if chain not in SUPPORTED_CHAINS:
        flash(f"Nicht unterstützte Chain (muss eine von {', '.join(SUPPORTED_CHAINS)} sein).", "error")
        return render_template("account_ui/deposit.html", chains=SUPPORTED_CHAINS, address=None), 400

    try:
        deposit_address = get_or_create_deposit_address(user, chain)
    except KeyManagementError as exc:
        logger.error("deposit address derivation failed: %s", exc)
        flash("Einzahlungen sind derzeit nicht verfügbar -- bitte versuchen Sie es später erneut.", "error")
        return render_template("account_ui/deposit.html", chains=SUPPORTED_CHAINS, address=None), 503

    return render_template(
        "account_ui/deposit.html", chains=SUPPORTED_CHAINS, address=deposit_address, selected_chain=chain,
    )


# --- swap (balance -> balance) ---------------------------------------------


@account_ui_bp.get("/swap")
@auth.login_required
def swap_form():
    balances = all_user_balances(auth.current_user().id)
    return render_template("account_ui/swap.html", chains=SUPPORTED_CHAINS, balances=balances)


@account_ui_bp.post("/swap/quote")
@auth.login_required
def swap_quote():
    user = auth.current_user()
    balances = all_user_balances(user.id)
    form_values = _swap_form_values()
    from_amount = _decimal_form_field("from_amount")

    if not all(form_values.values()) or from_amount is None or from_amount <= 0:
        flash("Alle Felder sind erforderlich, und der Betrag muss positiv sein.", "error")
        return render_template("account_ui/swap.html", chains=SUPPORTED_CHAINS, balances=balances, form_values=form_values), 400

    if form_values["from_chain"] not in SUPPORTED_CHAINS or form_values["to_chain"] not in SUPPORTED_CHAINS:
        flash(f"Nicht unterstützte Chain (muss eine von {', '.join(SUPPORTED_CHAINS)} sein).", "error")
        return render_template("account_ui/swap.html", chains=SUPPORTED_CHAINS, balances=balances, form_values=form_values), 400

    current_balance = user_balance(user.id, form_values["from_asset"])
    if current_balance < from_amount:
        flash(f"Unzureichendes Guthaben: Sie haben {current_balance} {form_values['from_asset']}.", "error")
        return render_template("account_ui/swap.html", chains=SUPPORTED_CHAINS, balances=balances, form_values=form_values), 400

    try:
        adapter = liquidity_factory.liquidity_adapter_for(form_values["from_asset"])
        quote = adapter.get_quote(form_values["from_asset"], form_values["to_asset"], from_amount)
    except TreasuryRebalanceRequiredError:
        flash("Dieses Paar ist vorübergehend nicht verfügbar -- bitte versuchen Sie einen kleineren Betrag oder ein anderes Asset.", "error")
        return render_template("account_ui/swap.html", chains=SUPPORTED_CHAINS, balances=balances, form_values=form_values), 400
    except LiquidityAdapterError as exc:
        logger.warning("account swap quote preview failed: %s", exc)
        flash("Gerade konnte kein aktueller Kurs abgerufen werden -- bitte versuchen Sie es gleich noch einmal.", "error")
        return render_template("account_ui/swap.html", chains=SUPPORTED_CHAINS, balances=balances, form_values=form_values), 400

    net_amount = apply_margin(quote.to_amount, current_app.config["MARGIN_PERCENT"])
    return render_template(
        "account_ui/swap.html", chains=SUPPORTED_CHAINS, balances=balances, form_values=form_values,
        preview={"to_amount": net_amount},
    )


@account_ui_bp.post("/swap/execute")
@auth.login_required
def swap_execute():
    user = auth.current_user()
    form_values = _swap_form_values()
    from_amount = _decimal_form_field("from_amount")

    if not all(form_values.values()) or from_amount is None or from_amount <= 0:
        flash("Alle Felder sind erforderlich, und der Betrag muss positiv sein.", "error")
        return redirect(url_for("account_ui.swap_form"))

    order = SwapOrder(
        from_chain=form_values["from_chain"], from_asset=form_values["from_asset"], from_amount=from_amount,
        to_chain=form_values["to_chain"], to_asset=form_values["to_asset"],
        user_id=user.id, funding_source="account_balance",
    )
    db.session.add(order)
    db.session.commit()

    adapter = liquidity_factory.liquidity_adapter_for(form_values["from_asset"])
    try:
        orchestrator.lock_quote_from_balance(order, adapter, current_app.config["MARGIN_PERCENT"], actor=user.email)
    except InsufficientBalanceError as exc:
        flash(f"Unzureichendes Guthaben: Sie haben {exc.available} {exc.asset}, benötigt werden {exc.requested} {exc.asset}.", "error")
        return redirect(url_for("account_ui.swap_form"))

    if order.status == states.QUOTE_LOCKED:
        orchestrator.execute_swap(order, adapter, actor=user.email)

    return redirect(url_for("account_ui.order_detail", order_id=order.id))


# --- withdraw / transfer ----------------------------------------------------


@account_ui_bp.get("/withdraw")
@auth.login_required
def withdraw_form():
    balances = all_user_balances(auth.current_user().id)
    return render_template("account_ui/withdraw.html", chains=SUPPORTED_CHAINS, balances=balances)


@account_ui_bp.post("/withdraw")
@auth.login_required
def withdraw_create():
    user = auth.current_user()
    balances = all_user_balances(user.id)

    chain = (request.form.get("chain") or "").strip()
    asset = (request.form.get("asset") or "").strip().upper()
    amount = _decimal_form_field("amount")
    withdrawal_address = (request.form.get("withdrawal_address") or "").strip()
    transfer_amount_eur = request.form.get("transfer_amount_eur", type=float)

    if not chain or not asset or amount is None or amount <= 0 or not withdrawal_address or transfer_amount_eur is None:
        flash("Alle Felder (einschließlich des EUR-Gegenwerts für die Prüfung des Compliance-Schwellenwerts) sind erforderlich.", "error")
        return render_template("account_ui/withdraw.html", chains=SUPPORTED_CHAINS, balances=balances), 400

    try:
        order = orchestrator.withdraw_from_balance(user, chain, asset, amount, withdrawal_address, actor=user.email)
    except InsufficientBalanceError as exc:
        flash(f"Unzureichendes Guthaben: Sie haben {exc.available} {exc.asset}, benötigt werden {exc.requested} {exc.asset}.", "error")
        return render_template("account_ui/withdraw.html", chains=SUPPORTED_CHAINS, balances=balances), 400

    try:
        orchestrator.gate_withdrawal(order, _aml18_client(), transfer_amount_eur=Decimal(str(transfer_amount_eur)), actor=user.email)
        if order.status == states.WITHDRAWAL_REQUESTED:
            orchestrator.send_withdrawal(order, _send_withdrawal_fn, actor=user.email)
            flash("Auszahlung gesendet.", "success")
        else:
            flash("Verifizierung des Wallet-Eigentums erforderlich -- bitte signieren Sie die auf der Auftragsseite angezeigte Challenge.", "success")
    except (SendError, orchestrator.WithdrawalNotClearedError) as exc:
        flash(f"Auszahlung konnte nicht verarbeitet werden: {exc}", "error")

    return redirect(url_for("account_ui.order_detail", order_id=order.id))


@account_ui_bp.get("/transfer")
@auth.login_required
def transfer_form():
    balances = all_user_balances(auth.current_user().id)
    return render_template("account_ui/transfer.html", balances=balances)


@account_ui_bp.post("/transfer")
@auth.login_required
def transfer_create():
    user = auth.current_user()
    balances = all_user_balances(user.id)

    recipient_email = (request.form.get("recipient_email") or "").strip()
    asset = (request.form.get("asset") or "").strip().upper()
    amount = _decimal_form_field("amount")

    if not recipient_email or not asset or amount is None or amount <= 0:
        flash("Empfänger-E-Mail, Asset und ein positiver Betrag sind erforderlich.", "error")
        return render_template("account_ui/transfer.html", balances=balances), 400

    try:
        create_transfer(user, recipient_email, asset, amount)
        db.session.commit()
    except RecipientNotFoundError:
        flash(f"Kein Konto für '{recipient_email}' gefunden.", "error")
        return render_template("account_ui/transfer.html", balances=balances), 400
    except SameAccountTransferError:
        flash("Sie können nicht auf Ihr eigenes Konto überweisen.", "error")
        return render_template("account_ui/transfer.html", balances=balances), 400
    except InsufficientBalanceError as exc:
        flash(f"Unzureichendes Guthaben: Sie haben {exc.available} {exc.asset}, benötigt werden {exc.requested} {exc.asset}.", "error")
        return render_template("account_ui/transfer.html", balances=balances), 400

    flash(f"{amount} {asset} an {recipient_email} gesendet.", "success")
    return redirect(url_for("account_ui.dashboard"))


# --- order history -----------------------------------------------------


@account_ui_bp.get("/orders")
@auth.login_required
def orders_list():
    user = auth.current_user()
    _settle_pending_swaps(user.id)
    orders = SwapOrder.query.filter_by(user_id=user.id).order_by(SwapOrder.created_at.desc()).all()
    return render_template("account_ui/orders_list.html", orders=orders)


@account_ui_bp.get("/orders/<int:order_id>")
@auth.login_required
def order_detail(order_id):
    user = auth.current_user()
    order = SwapOrder.query.filter_by(id=order_id, user_id=user.id).first()
    if order is None:
        flash("Auftrag nicht gefunden.", "error")
        return redirect(url_for("account_ui.orders_list"))

    if order.funding_source == "account_balance" and order.status == states.SWAP_EXECUTING:
        adapter = liquidity_factory.liquidity_adapter_for(order.from_asset)
        orchestrator.poll_swap_completion(order, adapter, current_app.config["EVM_MIN_CONFIRMATIONS"])
        if order.status == states.SWAP_COMPLETE:
            orchestrator.settle_to_balance(order)

    return render_template("account_ui/order_detail.html", order=order, states=states)


@account_ui_bp.post("/orders/<int:order_id>/submit-verification")
@auth.login_required
def submit_verification(order_id):
    user = auth.current_user()
    order = SwapOrder.query.filter_by(id=order_id, user_id=user.id).first()
    if order is None:
        flash("Auftrag nicht gefunden.", "error")
        return redirect(url_for("account_ui.orders_list"))

    signature = (request.form.get("signature") or "").strip()
    transfer_amount_eur = request.form.get("transfer_amount_eur", type=float)
    if not signature:
        flash("Signatur ist erforderlich.", "error")
        return redirect(url_for("account_ui.order_detail", order_id=order_id))

    try:
        submit_withdrawal_verification(order, _aml18_client(), signature, transfer_amount_eur=transfer_amount_eur)
        db.session.commit()
        orchestrator.send_withdrawal(order, _send_withdrawal_fn, actor=user.email)
        flash("Wallet-Eigentum verifiziert und Auszahlung gesendet.", "success")
    except WalletOwnershipVerificationFailedError as exc:
        db.session.commit()
        flash(f"Verifizierung fehlgeschlagen: {exc}", "error")
    except (SendError, orchestrator.WithdrawalNotClearedError) as exc:
        flash(f"Verifizierung erfolgreich, aber Senden fehlgeschlagen: {exc}", "error")

    return redirect(url_for("account_ui.order_detail", order_id=order_id))


@account_ui_bp.post("/orders/<int:order_id>/poll-withdrawal")
@auth.login_required
def poll_withdrawal(order_id):
    user = auth.current_user()
    order = SwapOrder.query.filter_by(id=order_id, user_id=user.id).first()
    if order is None:
        flash("Auftrag nicht gefunden.", "error")
        return redirect(url_for("account_ui.orders_list"))

    cfg = current_app.config
    if order.to_chain in NATIVE_WITHDRAWAL_CHAINS:
        adapter = liquidity_factory.liquidity_adapter_for(order.to_asset)
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
    return redirect(url_for("account_ui.order_detail", order_id=order_id))
