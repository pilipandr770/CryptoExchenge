from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.custody import models as _custody_models  # noqa: F401
from app.custody.key_management import KeyManagementError
from app.custody.models import DepositAddress
from app.extensions import db
from app.ledger import models as _ledger_models  # noqa: F401
from app.liquidity.base import LiquidityAdapterError, Quote
from app.liquidity.btc_treasury_adapter import TreasuryRebalanceRequiredError
from app.swap.models import SwapOrder

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


class _FakeLiquidityAdapter:
    def __init__(self, to_amount=Decimal("2500"), raise_error=None):
        self.to_amount = to_amount
        self.raise_error = raise_error
        self.calls = []

    def get_quote(self, from_asset, to_asset, amount):
        self.calls.append((from_asset, to_asset, amount))
        if self.raise_error:
            raise self.raise_error
        return Quote(
            quote_id="q1", from_asset=from_asset, to_asset=to_asset, from_amount=amount,
            to_amount=self.to_amount, expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            raw_provider_response={},
        )


# --- quote preview ---------------------------------------------------------


def test_landing_page_renders(client):
    resp = client.get("/exchange/")
    assert resp.status_code == 200
    assert b"You send" in resp.data


def test_preview_quote_shows_margin_adjusted_amount(client, monkeypatch):
    monkeypatch.setattr("app.liquidity.factory.liquidity_adapter_for", lambda asset: _FakeLiquidityAdapter())

    resp = client.post("/exchange/quote", data={
        "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
    })
    assert resp.status_code == 200
    # TestConfig doesn't override MARGIN_PERCENT -- default 1.5% off 2500.
    assert b"2462.5" in resp.data


def test_preview_quote_rejects_missing_fields(client):
    resp = client.post("/exchange/quote", data={"from_chain": "ethereum"})
    assert resp.status_code == 400
    assert b"required" in resp.data


def test_preview_quote_rejects_unsupported_chain(client):
    resp = client.post("/exchange/quote", data={
        "from_chain": "solana", "from_asset": "SOL", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
    })
    assert resp.status_code == 400
    assert b"Unsupported chain" in resp.data


def test_preview_quote_handles_adapter_error_gracefully(client, monkeypatch):
    monkeypatch.setattr(
        "app.liquidity.factory.liquidity_adapter_for",
        lambda asset: _FakeLiquidityAdapter(raise_error=LiquidityAdapterError("0x is down")),
    )
    resp = client.post("/exchange/quote", data={
        "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
    })
    assert resp.status_code == 400
    assert b"try again" in resp.data


def test_preview_quote_handles_treasury_shortfall_gracefully(client, monkeypatch):
    monkeypatch.setattr(
        "app.liquidity.factory.liquidity_adapter_for",
        lambda asset: _FakeLiquidityAdapter(raise_error=TreasuryRebalanceRequiredError(Decimal("1"), Decimal("0"))),
    )
    resp = client.post("/exchange/quote", data={
        "from_chain": "bitcoin", "from_asset": "BTC", "from_amount": "0.1",
        "to_chain": "ethereum", "to_asset": "USDC",
    })
    assert resp.status_code == 400
    assert b"temporarily unavailable" in resp.data


# --- order creation ---------------------------------------------------------


def test_create_order_derives_address_and_redirects_to_status(client, monkeypatch):
    monkeypatch.setattr("app.liquidity.factory.load_hot_wallet_mnemonic", lambda: TEST_MNEMONIC)

    resp = client.post("/exchange/create", data={
        "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
        "client_name": "Jane Doe", "client_email": "jane@example.com",
        "withdrawal_address": "0xBeneficiary",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"0x9858EfFD232B4033E47d90003D41EC34EcaEda94" in resp.data  # derived deposit address

    order = SwapOrder.query.one()
    assert order.client_name == "Jane Doe"
    assert order.client_email == "jane@example.com"
    assert order.withdrawal_address == "0xBeneficiary"
    assert order.deposit_address.address == "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"


def test_create_order_requires_client_name_and_withdrawal_address(client, monkeypatch):
    monkeypatch.setattr("app.liquidity.factory.load_hot_wallet_mnemonic", lambda: TEST_MNEMONIC)

    resp = client.post("/exchange/create", data={
        "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
    })
    assert resp.status_code == 400
    assert SwapOrder.query.count() == 0


def test_create_order_surfaces_key_management_error(client, monkeypatch):
    def _raise():
        raise KeyManagementError("hot wallet not configured")

    monkeypatch.setattr("app.liquidity.factory.load_hot_wallet_mnemonic", _raise)

    resp = client.post("/exchange/create", data={
        "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
        "client_name": "Jane Doe", "withdrawal_address": "0xBeneficiary",
    })
    assert resp.status_code == 503
    assert SwapOrder.query.count() == 0


def test_create_order_reuses_existing_admin_deposit_addresses_index(client, app, monkeypatch):
    # An admin-created deposit address on the same chain must not collide
    # with a client-created one -- next_index allocation is shared.
    db.session.add(DepositAddress(chain="ethereum", address="0xExisting", derivation_index=0, label="admin-order"))
    db.session.commit()

    monkeypatch.setattr("app.liquidity.factory.load_hot_wallet_mnemonic", lambda: TEST_MNEMONIC)

    client.post("/exchange/create", data={
        "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
        "client_name": "Jane Doe", "withdrawal_address": "0xBeneficiary",
    })

    new_address = DepositAddress.query.filter_by(derivation_index=1).one()
    assert new_address.address == "0x6Fac4D18c912343BF86fa7049364Dd4E424Ab9C0"


# --- order status page + token-based access -------------------------------


def test_order_status_page_shows_deposit_address(client, monkeypatch):
    monkeypatch.setattr("app.liquidity.factory.load_hot_wallet_mnemonic", lambda: TEST_MNEMONIC)
    client.post("/exchange/create", data={
        "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
        "client_name": "Jane Doe", "withdrawal_address": "0xBeneficiary",
    })
    order = SwapOrder.query.one()

    resp = client.get(f"/exchange/order/{order.public_token}")
    assert resp.status_code == 200
    assert order.deposit_address.address.encode() in resp.data
    assert b"Waiting for your deposit" in resp.data


def test_order_status_page_404_for_unknown_token(client):
    resp = client.get("/exchange/order/does-not-exist")
    assert resp.status_code == 404


def test_order_status_page_not_accessible_by_numeric_id(client, monkeypatch):
    # The public route only accepts the opaque token -- /exchange/order/<int>
    # must not resolve to an order via its sequential database id.
    monkeypatch.setattr("app.liquidity.factory.load_hot_wallet_mnemonic", lambda: TEST_MNEMONIC)
    client.post("/exchange/create", data={
        "from_chain": "ethereum", "from_asset": "ETH", "from_amount": "1",
        "to_chain": "ethereum", "to_asset": "USDC",
        "client_name": "Jane Doe", "withdrawal_address": "0xBeneficiary",
    })
    order = SwapOrder.query.one()

    resp = client.get(f"/exchange/order/{order.id}")
    assert resp.status_code == 404


def test_public_token_is_unique_and_opaque(app):
    order1 = SwapOrder(from_chain="ethereum", from_asset="ETH", from_amount=Decimal("1"), to_chain="ethereum", to_asset="USDC")
    order2 = SwapOrder(from_chain="ethereum", from_asset="ETH", from_amount=Decimal("1"), to_chain="ethereum", to_asset="USDC")
    db.session.add_all([order1, order2])
    db.session.commit()

    assert order1.public_token != order2.public_token
    assert str(order1.id) != order1.public_token
