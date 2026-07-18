import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pyotp
import pytest

from app.accounts import auth
from app.accounts.balances import credit_user, user_balance
from app.accounts.models import User
from app.extensions import db
from app.liquidity.base import Quote, SwapExecutionResult
from app.swap.models import SwapOrder

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


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

    def verify_wallet_ownership_signed_message(self, challenge_id, signature, transfer_amount_eur=None, transaction_id=None):
        return {"verification_id": "ver-1", "verified": True, "status": "verified"}


class _FakeLiquidityAdapter:
    def __init__(self, to_amount=Decimal("2500"), tx_hash="0xswaptx"):
        self.to_amount = to_amount
        self.tx_hash = tx_hash

    def get_quote(self, from_asset, to_asset, amount):
        return Quote(
            quote_id="q1", from_asset=from_asset, to_asset=to_asset, from_amount=amount,
            to_amount=self.to_amount, expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            raw_provider_response={},
        )

    def execute_swap(self, quote):
        return SwapExecutionResult(tx_hash=self.tx_hash, to_amount_executed=quote.to_amount, status="submitted", raw_provider_response={})

    def get_swap_status(self, tx_hash, min_confirmations):
        return "confirmed"


def _extract_secret(html: str) -> str:
    match = re.search(r"<code>([A-Z2-7]+)</code>", html)
    assert match, "TOTP secret not found in 2FA setup page"
    return match.group(1)


def _register_and_setup_2fa(client, email="jane@example.com", password="hunter2", monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr("app.account_ui.routes._aml18_client", lambda: _FakeAml18Client())
    resp = client.post("/account/register", data={
        "email": email, "password": password, "full_name": "Jane Doe",
    }, follow_redirects=True)
    secret = _extract_secret(resp.get_data(as_text=True))
    code = pyotp.TOTP(secret).now()
    client.post("/account/2fa-setup", data={"code": code})
    return secret


def _login(client, email="jane@example.com", password="hunter2", secret=None):
    client.post("/account/login", data={"email": email, "password": password})
    if secret:
        code = pyotp.TOTP(secret).now()
        client.post("/account/login/verify-2fa", data={"code": code})


# --- landing / registration -----------------------------------------------


def test_landing_renders(client):
    resp = client.get("/account/")
    assert resp.status_code == 200


def test_register_screens_and_redirects_to_2fa_setup(client, monkeypatch):
    monkeypatch.setattr("app.account_ui.routes._aml18_client", lambda: _FakeAml18Client())
    resp = client.post("/account/register", data={
        "email": "jane@example.com", "password": "hunter2", "full_name": "Jane Doe",
        "date_of_birth": "1990-01-01", "country": "DE",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"authenticator" in resp.data.lower() or b"2fa" in resp.data.lower() or b"QR" in resp.data

    user = User.query.filter_by(email="jane@example.com").one()
    assert user.screening_decision == "accepted"
    assert user.is_active is True
    assert user.totp_enabled is False  # not confirmed yet


def test_register_review_decision_deactivates_account(client, monkeypatch):
    monkeypatch.setattr(
        "app.account_ui.routes._aml18_client",
        lambda: _FakeAml18Client(check_name_result={"decision": "review", "score": 85.0, "matches": []}),
    )
    client.post("/account/register", data={
        "email": "flagged@example.com", "password": "hunter2", "full_name": "Suspicious Name",
    })
    user = User.query.filter_by(email="flagged@example.com").one()
    assert user.is_active is False


def test_register_rejects_duplicate_email(client, monkeypatch):
    monkeypatch.setattr("app.account_ui.routes._aml18_client", lambda: _FakeAml18Client())
    client.post("/account/register", data={"email": "dup@example.com", "password": "pw", "full_name": "Name One"})
    resp = client.post("/account/register", data={"email": "dup@example.com", "password": "pw2", "full_name": "Name Two"})
    assert resp.status_code == 400


# --- 2FA setup + login ------------------------------------------------


def test_2fa_setup_wrong_code_rejected(client, monkeypatch):
    monkeypatch.setattr("app.account_ui.routes._aml18_client", lambda: _FakeAml18Client())
    client.post("/account/register", data={"email": "twofa@example.com", "password": "pw", "full_name": "Jane Doe"})
    resp = client.post("/account/2fa-setup", data={"code": "000000"})
    assert resp.status_code == 400
    user = User.query.filter_by(email="twofa@example.com").one()
    assert user.totp_enabled is False


def test_2fa_setup_correct_code_logs_in(client, monkeypatch):
    secret = _register_and_setup_2fa(client, "confirmed@example.com", monkeypatch=monkeypatch)
    user = User.query.filter_by(email="confirmed@example.com").one()
    assert user.totp_enabled is True

    resp = client.get("/account/dashboard")
    assert resp.status_code == 200  # already logged in from setup


def test_login_requires_2fa_code(client, monkeypatch):
    secret = _register_and_setup_2fa(client, "login2fa@example.com", monkeypatch=monkeypatch)
    client.get("/account/logout")

    resp = client.post("/account/login", data={"email": "login2fa@example.com", "password": "hunter2"})
    assert resp.status_code == 302
    assert "verify-2fa" in resp.headers["Location"]

    dashboard_resp = client.get("/account/dashboard")
    assert dashboard_resp.status_code == 302  # not fully logged in until 2FA verified


def test_login_wrong_password_rejected(client, monkeypatch):
    _register_and_setup_2fa(client, "wrongpw@example.com", monkeypatch=monkeypatch)
    client.get("/account/logout")
    resp = client.post("/account/login", data={"email": "wrongpw@example.com", "password": "wrong"})
    assert resp.status_code == 401


def test_verify_2fa_correct_code_completes_login(client, monkeypatch):
    secret = _register_and_setup_2fa(client, "fullflow@example.com", monkeypatch=monkeypatch)
    client.get("/account/logout")
    client.post("/account/login", data={"email": "fullflow@example.com", "password": "hunter2"})

    code = pyotp.TOTP(secret).now()
    resp = client.post("/account/login/verify-2fa", data={"code": code}, follow_redirects=True)
    assert resp.status_code == 200

    dashboard_resp = client.get("/account/dashboard")
    assert dashboard_resp.status_code == 200


def test_verify_2fa_wrong_code_rejected(client, monkeypatch):
    _register_and_setup_2fa(client, "badcode@example.com", monkeypatch=monkeypatch)
    client.get("/account/logout")
    client.post("/account/login", data={"email": "badcode@example.com", "password": "hunter2"})

    resp = client.post("/account/login/verify-2fa", data={"code": "000000"})
    assert resp.status_code == 401


# --- deposit ---------------------------------------------------------------


def test_deposit_derives_address(client, monkeypatch, app):
    _register_and_setup_2fa(client, "deposit@example.com", monkeypatch=monkeypatch)
    monkeypatch.setattr("app.accounts.deposits.load_hot_wallet_mnemonic", lambda: TEST_MNEMONIC)

    resp = client.post("/account/deposit", data={"chain": "ethereum"})
    assert resp.status_code == 200
    assert b"0x9858EfFD232B4033E47d90003D41EC34EcaEda94" in resp.data


def test_deposit_requires_login(client):
    resp = client.get("/account/deposit")
    assert resp.status_code == 302


# --- swap --------------------------------------------------------------


def test_swap_quote_preview_shows_margin_adjusted_amount(client, monkeypatch, app):
    _register_and_setup_2fa(client, "swapuser@example.com", monkeypatch=monkeypatch)
    monkeypatch.setattr("app.liquidity.factory.liquidity_adapter_for", lambda asset: _FakeLiquidityAdapter())

    user = User.query.filter_by(email="swapuser@example.com").one()
    credit_user(user.id, "ETH", Decimal("5"), entry_type="deposit")
    db.session.commit()

    resp = client.post("/account/swap/quote", data={
        "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
    })
    assert resp.status_code == 200
    assert b"2462.5" in resp.data  # default 1.5% margin off 2500


def test_swap_quote_rejects_insufficient_balance(client, monkeypatch, app):
    _register_and_setup_2fa(client, "poorswap@example.com", monkeypatch=monkeypatch)
    monkeypatch.setattr("app.liquidity.factory.liquidity_adapter_for", lambda asset: _FakeLiquidityAdapter())

    resp = client.post("/account/swap/quote", data={
        "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
    })
    assert resp.status_code == 400
    assert b"Unzureichendes Guthaben" in resp.data


def test_swap_execute_debits_and_credits_balance(client, monkeypatch, app):
    _register_and_setup_2fa(client, "execswap@example.com", monkeypatch=monkeypatch)
    monkeypatch.setattr("app.liquidity.factory.liquidity_adapter_for", lambda asset: _FakeLiquidityAdapter())

    user = User.query.filter_by(email="execswap@example.com").one()
    credit_user(user.id, "ETH", Decimal("5"), entry_type="deposit")
    db.session.commit()

    resp = client.post("/account/swap/execute", data={
        "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
    }, follow_redirects=True)
    assert resp.status_code == 200

    order = SwapOrder.query.filter_by(user_id=user.id).one()
    assert order.funding_source == "account_balance"
    assert order.swap_tx_hash == "0xswaptx"
    assert user_balance(user.id, "ETH") == Decimal("4")  # 5 - 1 debited


# --- withdraw ------------------------------------------------------------


def test_withdraw_sends_immediately_when_not_required(client, monkeypatch, app):
    _register_and_setup_2fa(client, "withdrawer@example.com", monkeypatch=monkeypatch)
    monkeypatch.setattr("app.account_ui.routes._aml18_client", lambda: _FakeAml18Client(requirement_result={"required": False}))
    monkeypatch.setattr("app.account_ui.routes._send_withdrawal_fn", lambda o: "0xwithdrawaltx")

    user = User.query.filter_by(email="withdrawer@example.com").one()
    credit_user(user.id, "USDC", Decimal("500"), entry_type="deposit")
    db.session.commit()

    resp = client.post("/account/withdraw", data={
        "chain": "ethereum", "asset": "USDC", "amount": "200",
        "withdrawal_address": "0xBeneficiary", "transfer_amount_eur": "300",
    }, follow_redirects=True)
    assert resp.status_code == 200

    order = SwapOrder.query.filter_by(user_id=user.id).one()
    assert order.withdrawal_tx_hash == "0xwithdrawaltx"
    assert user_balance(user.id, "USDC") == Decimal("300")


def test_withdraw_requires_verification_above_threshold(client, monkeypatch, app):
    _register_and_setup_2fa(client, "withdrawer2@example.com", monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "app.account_ui.routes._aml18_client",
        lambda: _FakeAml18Client(requirement_result={"required": True}, challenge_result={
            "challenge_id": "chal-1", "message": "sign me", "network": "ETH", "address": "0xBeneficiary", "expires_at": "2026-01-01T00:00:00Z",
        }),
    )

    user = User.query.filter_by(email="withdrawer2@example.com").one()
    credit_user(user.id, "USDC", Decimal("500"), entry_type="deposit")
    db.session.commit()

    resp = client.post("/account/withdraw", data={
        "chain": "ethereum", "asset": "USDC", "amount": "200",
        "withdrawal_address": "0xBeneficiary", "transfer_amount_eur": "5000",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"sign me" in resp.data


# --- transfer --------------------------------------------------------------


def test_transfer_moves_balance_to_another_user(client, monkeypatch, app):
    sender_secret = _register_and_setup_2fa(client, "sender@example.com", monkeypatch=monkeypatch)
    client.get("/account/logout")
    _register_and_setup_2fa(client, "recipient@example.com", monkeypatch=monkeypatch)
    client.get("/account/logout")

    sender = User.query.filter_by(email="sender@example.com").one()
    recipient = User.query.filter_by(email="recipient@example.com").one()
    credit_user(sender.id, "USDC", Decimal("100"), entry_type="deposit")
    db.session.commit()

    _login(client, "sender@example.com", "hunter2", secret=sender_secret)

    resp = client.post("/account/transfer", data={
        "recipient_email": "recipient@example.com", "asset": "USDC", "amount": "30",
    }, follow_redirects=True)
    assert resp.status_code == 200

    assert user_balance(sender.id, "USDC") == Decimal("70")
    assert user_balance(recipient.id, "USDC") == Decimal("30")


def test_transfer_rejects_unknown_recipient(client, monkeypatch, app):
    secret = _register_and_setup_2fa(client, "sender2@example.com", monkeypatch=monkeypatch)
    user = User.query.filter_by(email="sender2@example.com").one()
    credit_user(user.id, "USDC", Decimal("100"), entry_type="deposit")
    db.session.commit()

    resp = client.post("/account/transfer", data={
        "recipient_email": "nobody@example.com", "asset": "USDC", "amount": "30",
    })
    assert resp.status_code == 400
    assert user_balance(user.id, "USDC") == Decimal("100")


def test_orders_are_scoped_to_current_user(client, monkeypatch, app):
    monkeypatch.setattr("app.account_ui.routes._aml18_client", lambda: _FakeAml18Client())
    monkeypatch.setattr("app.liquidity.factory.liquidity_adapter_for", lambda asset: _FakeLiquidityAdapter())

    secret_a = _register_and_setup_2fa(client, "ownera@example.com", monkeypatch=monkeypatch)
    user_a = User.query.filter_by(email="ownera@example.com").one()
    credit_user(user_a.id, "ETH", Decimal("5"), entry_type="deposit")
    db.session.commit()
    client.post("/account/swap/execute", data={
        "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
    })
    order = SwapOrder.query.filter_by(user_id=user_a.id).one()
    client.get("/account/logout")

    _register_and_setup_2fa(client, "ownerb@example.com", monkeypatch=monkeypatch)
    resp = client.get(f"/account/orders/{order.id}")
    assert resp.status_code == 302  # not found for this user -- redirected away

    resp = client.get("/account/orders")
    assert b"Noch keine Auftr" in resp.data  # ownerB has none of their own
