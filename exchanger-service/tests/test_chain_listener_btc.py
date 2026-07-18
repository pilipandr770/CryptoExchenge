from decimal import Decimal

from app.chain_listeners.btc_listener import BtcListener
from app.custody import models as _custody_models  # noqa: F401
from app.custody.models import DepositAddress
from app.extensions import db
from app.ledger import models as _ledger_models  # noqa: F401
from app.swap.models import SwapOrder
from app.swap.states import DEPOSIT_CONFIRMED, DEPOSIT_PENDING


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _pending_btc_order(address="bc1qdeposit1"):
    addr = DepositAddress(chain="bitcoin", address=address, derivation_index=0, label="demo-1")
    db.session.add(addr)
    db.session.flush()
    order = SwapOrder(
        deposit_address_id=addr.id, from_chain="bitcoin", from_asset="BTC", from_amount=Decimal("0.1"),
        to_chain="ethereum", to_asset="USDC",
    )
    db.session.add(order)
    db.session.commit()
    return order


def test_poll_confirms_deposit_with_enough_confirmations(app, monkeypatch):
    order = _pending_btc_order()
    listener = BtcListener(esplora_base_url="https://esplora.test/api", min_confirmations=2)

    def _fake_get(url, timeout=None):
        if url.endswith("/blocks/tip/height"):
            return _FakeResponse(100)
        if url.endswith(f"/address/{order.deposit_address.address}/utxo"):
            return _FakeResponse([
                {"txid": "abc123", "value": 1000000, "status": {"confirmed": True, "block_height": 98}},
            ])
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("app.chain_listeners.btc_listener.requests.get", _fake_get)

    confirmed = listener.poll()

    assert len(confirmed) == 1
    assert order.status == DEPOSIT_CONFIRMED
    assert order.deposit_tx_hash == "abc123"


def test_poll_skips_unconfirmed_utxo(app, monkeypatch):
    order = _pending_btc_order()
    listener = BtcListener(esplora_base_url="https://esplora.test/api", min_confirmations=1)

    def _fake_get(url, timeout=None):
        if url.endswith("/blocks/tip/height"):
            return _FakeResponse(100)
        return _FakeResponse([{"txid": "abc123", "value": 1000000, "status": {"confirmed": False}}])

    monkeypatch.setattr("app.chain_listeners.btc_listener.requests.get", _fake_get)

    assert listener.poll() == []
    assert order.status == DEPOSIT_PENDING


def test_poll_skips_when_not_enough_confirmations(app, monkeypatch):
    order = _pending_btc_order()
    listener = BtcListener(esplora_base_url="https://esplora.test/api", min_confirmations=5)

    def _fake_get(url, timeout=None):
        if url.endswith("/blocks/tip/height"):
            return _FakeResponse(100)
        return _FakeResponse([{"txid": "abc123", "value": 1000000, "status": {"confirmed": True, "block_height": 99}}])

    monkeypatch.setattr("app.chain_listeners.btc_listener.requests.get", _fake_get)

    assert listener.poll() == []
    assert order.status == DEPOSIT_PENDING


def test_poll_no_pending_orders_skips_http_calls(app, monkeypatch):
    listener = BtcListener(esplora_base_url="https://esplora.test/api", min_confirmations=1)

    def _fake_get(url, timeout=None):
        raise AssertionError("should not call Esplora when there are no pending orders")

    monkeypatch.setattr("app.chain_listeners.btc_listener.requests.get", _fake_get)

    assert listener.poll() == []
