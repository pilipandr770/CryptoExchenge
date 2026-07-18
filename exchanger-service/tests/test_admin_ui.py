from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.custody import models as _custody_models  # noqa: F401
from app.custody.models import DepositAddress, HotWallet
from app.extensions import db
from app.ledger import models as _ledger_models  # noqa: F401
from app.ledger.models import LedgerEntry
from app.liquidity.base import Quote, SwapExecutionResult
from app.swap import states
from app.swap.models import SwapOrder


@pytest.fixture
def auth_client(client):
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})
    return client


class _FakeLiquidityAdapter:
    def get_quote(self, from_asset, to_asset, amount):
        return Quote(
            quote_id="q1", from_asset=from_asset, to_asset=to_asset, from_amount=amount,
            to_amount=Decimal("2500"), expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            raw_provider_response={},
        )

    def execute_swap(self, quote):
        return SwapExecutionResult(tx_hash="0xswaptx", to_amount_executed=quote.to_amount, status="submitted", raw_provider_response={})

    def get_swap_status(self, tx_hash, min_confirmations):
        return "confirmed"


class _FakeAml18Client:
    def __init__(self, requirement_result=None, check_name_result=None):
        self._requirement_result = requirement_result or {"required": False}
        self._check_name_result = check_name_result or {"decision": "accepted", "score": 5.0, "matches": []}

    def wallet_ownership_requirement(self, transfer_amount_eur):
        return self._requirement_result

    def create_wallet_ownership_challenge(self, network, address):
        return {"challenge_id": "chal-1", "message": "sign me", "network": network, "address": address, "expires_at": "2026-01-01T00:00:00Z"}

    def check_name(self, name, date_of_birth=None, country=None):
        return self._check_name_result


def _order_deposit_confirmed(app, **overrides):
    defaults = dict(from_chain="ethereum", from_asset="ETH", from_amount=Decimal("1"), to_chain="ethereum", to_asset="USDC")
    defaults.update(overrides)
    order = SwapOrder(**defaults)
    order.mark_status(states.DEPOSIT_CONFIRMED)
    db.session.add(order)
    db.session.commit()
    return order


def _order_ready_for_review(app):
    order = SwapOrder(from_chain="ethereum", from_asset="ETH", from_amount=Decimal("1"), to_chain="ethereum", to_asset="USDC")
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)
    order.mark_status(states.PENDING_MANUAL_REVIEW)
    order.screening_decision = "review"
    order.screening_score = 80.0
    db.session.add(order)
    db.session.commit()
    return order


# --- auth gating ------------------------------------------------------


def test_orders_requires_login(client):
    resp = client.get("/admin/orders")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_login_rejects_wrong_password(client):
    resp = client.post("/admin/login", data={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


def test_login_succeeds_and_grants_access(client):
    resp = client.post("/admin/login", data={"username": "admin", "password": "test-password"}, follow_redirects=True)
    assert resp.status_code == 200
    resp = client.get("/admin/orders")
    assert resp.status_code == 200


def test_logout_revokes_access(auth_client):
    auth_client.get("/admin/logout")
    resp = auth_client.get("/admin/orders")
    assert resp.status_code == 302


# --- orders list / detail ------------------------------------------------


def test_orders_list_renders(auth_client, app):
    order = _order_ready_for_review(app)
    resp = auth_client.get("/admin/orders")
    assert resp.status_code == 200
    assert f"#{order.id}".encode() in resp.data


def test_order_detail_renders_review_action(auth_client, app):
    order = _order_ready_for_review(app)
    resp = auth_client.get(f"/admin/orders/{order.id}")
    assert resp.status_code == 200
    assert b"Approve and lock quote" in resp.data


def test_order_detail_404_for_unknown_order(auth_client):
    resp = auth_client.get("/admin/orders/99999")
    assert resp.status_code == 404


# --- run screening (monkeypatched aml18 + liquidity factories) -----------


def test_run_screening_form_renders_for_deposit_confirmed_order(auth_client, app):
    order = _order_deposit_confirmed(app)
    resp = auth_client.get(f"/admin/orders/{order.id}")
    assert resp.status_code == 200
    assert b"Run screening and lock quote" in resp.data


def test_run_screening_accepted_locks_quote(auth_client, app, monkeypatch):
    order = _order_deposit_confirmed(app)
    monkeypatch.setattr("app.admin_ui.routes._liquidity_adapter_for", lambda o: _FakeLiquidityAdapter())
    monkeypatch.setattr("app.admin_ui.routes._aml18_client", lambda: _FakeAml18Client())

    resp = auth_client.post(f"/admin/orders/{order.id}/run-screening", data={"client_name": "Jane Doe"}, follow_redirects=True)
    assert resp.status_code == 200

    refreshed = db.session.get(SwapOrder, order.id)
    assert refreshed.status == states.QUOTE_LOCKED
    assert refreshed.client_name == "Jane Doe"
    assert refreshed.screening_decision == "accepted"


def test_run_screening_review_stops_for_operator(auth_client, app, monkeypatch):
    order = _order_deposit_confirmed(app)
    monkeypatch.setattr("app.admin_ui.routes._liquidity_adapter_for", lambda o: _FakeLiquidityAdapter())
    monkeypatch.setattr(
        "app.admin_ui.routes._aml18_client",
        lambda: _FakeAml18Client(check_name_result={"decision": "review", "score": 85.0, "matches": []}),
    )

    resp = auth_client.post(f"/admin/orders/{order.id}/run-screening", data={"client_name": "Suspicious Name"}, follow_redirects=True)
    assert resp.status_code == 200

    refreshed = db.session.get(SwapOrder, order.id)
    assert refreshed.status == states.PENDING_MANUAL_REVIEW


def test_run_screening_requires_client_name(auth_client, app):
    order = _order_deposit_confirmed(app)
    resp = auth_client.post(f"/admin/orders/{order.id}/run-screening", data={}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"client name is required" in resp.data

    refreshed = db.session.get(SwapOrder, order.id)
    assert refreshed.status == states.DEPOSIT_CONFIRMED


# --- approve review / execute swap (monkeypatched liquidity factory) -----


def test_approve_review_advances_order(auth_client, app, monkeypatch):
    order = _order_ready_for_review(app)
    monkeypatch.setattr("app.admin_ui.routes._liquidity_adapter_for", lambda o: _FakeLiquidityAdapter())

    resp = auth_client.post(f"/admin/orders/{order.id}/approve-review", follow_redirects=True)
    assert resp.status_code == 200

    refreshed = db.session.get(SwapOrder, order.id)
    assert refreshed.status == states.QUOTE_LOCKED
    assert refreshed.quote_id == "q1"


def test_execute_swap_and_poll_completes_order(auth_client, app, monkeypatch):
    order = _order_ready_for_review(app)
    monkeypatch.setattr("app.admin_ui.routes._liquidity_adapter_for", lambda o: _FakeLiquidityAdapter())
    auth_client.post(f"/admin/orders/{order.id}/approve-review")

    resp = auth_client.post(f"/admin/orders/{order.id}/execute-swap", follow_redirects=True)
    assert resp.status_code == 200

    refreshed = db.session.get(SwapOrder, order.id)
    assert refreshed.status == states.SWAP_COMPLETE
    assert refreshed.swap_tx_hash == "0xswaptx"


# --- withdrawal flow (monkeypatched aml18 + send factories) --------------


def _order_at_swap_complete(app):
    order = SwapOrder(from_chain="ethereum", from_asset="ETH", from_amount=Decimal("1"), to_chain="ethereum", to_asset="ETH")
    order.mark_status(states.DEPOSIT_CONFIRMED)
    order.mark_status(states.SCREENING)
    order.mark_status(states.QUOTE_LOCKED)
    order.mark_status(states.SWAP_EXECUTING)
    order.mark_status(states.SWAP_COMPLETE)
    order.to_amount_executed = Decimal("0.5")
    order.to_amount_payout = Decimal("0.5")  # normally set by poll_swap_completion
    db.session.add(order)
    db.session.commit()
    return order


def test_request_withdrawal_sends_immediately_when_not_required(auth_client, app, monkeypatch):
    order = _order_at_swap_complete(app)
    monkeypatch.setattr("app.admin_ui.routes._aml18_client", lambda: _FakeAml18Client(requirement_result={"required": False}))
    monkeypatch.setattr("app.admin_ui.routes._send_withdrawal_fn", lambda o: "0xwithdrawaltx")

    resp = auth_client.post(
        f"/admin/orders/{order.id}/request-withdrawal",
        data={"withdrawal_address": "0xBeneficiary", "transfer_amount_eur": "500"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    refreshed = db.session.get(SwapOrder, order.id)
    assert refreshed.status == states.WITHDRAWAL_SENT
    assert refreshed.withdrawal_tx_hash == "0xwithdrawaltx"


def test_request_withdrawal_requires_verification_above_threshold(auth_client, app, monkeypatch):
    order = _order_at_swap_complete(app)
    monkeypatch.setattr("app.admin_ui.routes._aml18_client", lambda: _FakeAml18Client(requirement_result={"required": True}))

    resp = auth_client.post(
        f"/admin/orders/{order.id}/request-withdrawal",
        data={"withdrawal_address": "0xBeneficiary", "transfer_amount_eur": "5000"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    refreshed = db.session.get(SwapOrder, order.id)
    assert refreshed.status == states.WITHDRAWAL_VERIFICATION
    assert refreshed.wallet_ownership_challenge_id == "chal-1"
    assert b"sign me" in resp.data


def test_submit_verification_sends_withdrawal(auth_client, app, monkeypatch):
    order = _order_at_swap_complete(app)
    monkeypatch.setattr("app.admin_ui.routes._aml18_client", lambda: _FakeAml18Client(requirement_result={"required": True}))
    auth_client.post(
        f"/admin/orders/{order.id}/request-withdrawal",
        data={"withdrawal_address": "0xBeneficiary", "transfer_amount_eur": "5000"},
    )

    class _VerifyingAml18Client(_FakeAml18Client):
        def verify_wallet_ownership_signed_message(self, challenge_id, signature, transfer_amount_eur=None, transaction_id=None):
            return {"verification_id": "ver-1", "verified": True, "status": "verified"}

    monkeypatch.setattr("app.admin_ui.routes._aml18_client", lambda: _VerifyingAml18Client())
    monkeypatch.setattr("app.admin_ui.routes._send_withdrawal_fn", lambda o: "0xwithdrawaltx")

    resp = auth_client.post(
        f"/admin/orders/{order.id}/submit-verification",
        data={"signature": "0xsig", "transfer_amount_eur": "5000"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    refreshed = db.session.get(SwapOrder, order.id)
    assert refreshed.status == states.WITHDRAWAL_SENT
    assert refreshed.withdrawal_tx_hash == "0xwithdrawaltx"


# --- treasury ----------------------------------------------------------


def test_treasury_page_renders_hot_wallets_and_ledger(auth_client, app):
    db.session.add(HotWallet(chain="ethereum", address="0xHotWallet", derivation_path="m/44'/60'/0'/0/0", balance_cache=Decimal("1.5")))
    db.session.add(LedgerEntry(account="treasury:ethereum:WBTC", asset="WBTC", amount=Decimal("2"), entry_type="swap_in"))
    db.session.commit()

    resp = auth_client.get("/admin/treasury")
    assert resp.status_code == 200
    assert b"0xHotWallet" in resp.data
    assert b"treasury:ethereum:WBTC" in resp.data


def test_treasury_rebalance_records_ledger_pair(auth_client, app):
    resp = auth_client.post(
        "/admin/treasury/rebalance",
        data={"btc_amount": "0.5", "wbtc_amount": "0.499", "note": "manual conversion via exchange"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    from app.ledger import reconciliation
    balances = reconciliation.ledger_balances_by_account_asset()
    # SQLite (this test suite's backend) stores Numeric columns as floats
    # under the hood, so a Decimal survives the round trip only to ~15
    # significant digits -- Postgres (the real deployment target per
    # docker-compose.yml) does not have this limitation. Tolerance here
    # confirms the app layer preserved the value correctly; it isn't
    # papering over app-level precision loss (see test_decimal_form_field
    # coverage below for that).
    assert abs(balances[("treasury:bitcoin:BTC", "BTC")] - Decimal("-0.5")) < Decimal("1e-9")
    assert abs(balances[("treasury:ethereum:WBTC", "WBTC")] - Decimal("0.499")) < Decimal("1e-9")


def test_treasury_rebalance_requires_both_amounts(auth_client):
    resp = auth_client.post("/admin/treasury/rebalance", data={"btc_amount": "0.5"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"required" in resp.data


# --- ledger --------------------------------------------------------------


def test_ledger_page_renders_without_configured_rpc(auth_client, app):
    # No EVM_RPC_URL / BTC_ESPLORA_API_BASE_URL configured beyond test
    # defaults -- page must degrade gracefully, not 500.
    resp = auth_client.get("/admin/ledger")
    assert resp.status_code == 200


# --- _decimal_form_field: app-layer precision, independent of SQLite -----


def test_decimal_form_field_preserves_exact_precision(app):
    from app.admin_ui.routes import _decimal_form_field

    with app.test_request_context("/", method="POST", data={"amount": "0.499"}):
        value = _decimal_form_field("amount")
    assert str(value) == "0.499"


def test_decimal_form_field_returns_none_for_missing_or_invalid(app):
    from app.admin_ui.routes import _decimal_form_field

    with app.test_request_context("/", method="POST", data={}):
        assert _decimal_form_field("amount") is None

    with app.test_request_context("/", method="POST", data={"amount": "not-a-number"}):
        assert _decimal_form_field("amount") is None


# --- order intake --------------------------------------------------------

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


def test_new_order_form_renders(auth_client):
    resp = auth_client.get("/admin/orders/new")
    assert resp.status_code == 200
    assert b"Create deposit / order" in resp.data


def test_create_order_derives_address_and_creates_order(auth_client, app, monkeypatch):
    monkeypatch.setattr("app.admin_ui.routes._load_mnemonic", lambda: TEST_MNEMONIC)

    resp = auth_client.post(
        "/admin/orders/new",
        data={
            "label": "demo-investor-1",
            "from_chain": "ethereum",
            "from_asset": "ETH",
            "from_amount": "0.1",
            "to_chain": "ethereum",
            "to_asset": "USDC",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"0x9858EfFD232B4033E47d90003D41EC34EcaEda94" in resp.data  # index-0 EVM address for the test mnemonic

    order = SwapOrder.query.one()
    assert order.status == states.DEPOSIT_PENDING
    assert order.from_chain == "ethereum"
    assert order.deposit_address.address == "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    assert order.deposit_address.label == "demo-investor-1"
    assert order.deposit_address.derivation_index == 0


def test_create_order_second_deposit_address_gets_next_index(auth_client, app, monkeypatch):
    monkeypatch.setattr("app.admin_ui.routes._load_mnemonic", lambda: TEST_MNEMONIC)

    for label in ("first", "second"):
        auth_client.post("/admin/orders/new", data={
            "label": label, "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "0.1",
            "to_chain": "ethereum", "to_asset": "USDC",
        })

    addresses = [a.address for a in DepositAddress.query.order_by(DepositAddress.derivation_index).all()]
    assert addresses == [
        "0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
        "0x6Fac4D18c912343BF86fa7049364Dd4E424Ab9C0",
    ]


def test_create_order_rejects_missing_fields(auth_client):
    resp = auth_client.post("/admin/orders/new", data={"label": "x"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"required" in resp.data
    assert SwapOrder.query.count() == 0


def test_create_order_rejects_unsupported_chain(auth_client, monkeypatch):
    monkeypatch.setattr("app.admin_ui.routes._load_mnemonic", lambda: TEST_MNEMONIC)
    resp = auth_client.post(
        "/admin/orders/new",
        data={"label": "x", "from_chain": "solana", "from_asset": "SOL", "from_amount": "1", "to_chain": "ethereum", "to_asset": "USDC"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Unsupported chain" in resp.data
    assert SwapOrder.query.count() == 0


def test_create_order_surfaces_key_management_error(auth_client):
    # No HOT_WALLET_KEYS_FILE/FERNET_KEY configured in TestConfig -- must
    # fail gracefully with a flash message, not a 500.
    resp = auth_client.post(
        "/admin/orders/new",
        data={"label": "x", "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1", "to_chain": "ethereum", "to_asset": "USDC"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"hot wallet mnemonic" in resp.data
    assert SwapOrder.query.count() == 0
