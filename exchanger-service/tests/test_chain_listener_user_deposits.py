from decimal import Decimal

from app.accounts import auth
from app.accounts.balances import user_balance
from app.chain_listeners.btc_listener import BtcListener
from app.chain_listeners.evm_listener import EvmListener
from app.custody.models import DepositAddress
from app.extensions import db
from app.ledger.models import LedgerEntry


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _user(email="user@example.com"):
    user = auth.register_user(email, "pw", "Jane Doe")
    db.session.commit()
    return user


def _user_deposit_address(user, chain="ethereum", address="0xUserDeposit1", index=0):
    addr = DepositAddress(chain=chain, address=address, derivation_index=index, user_id=user.id)
    db.session.add(addr)
    db.session.commit()
    return addr


# --- EVM ------------------------------------------------------------------


def test_scan_for_user_deposits_credits_balance_and_treasury(app, monkeypatch):
    user = _user()
    addr = _user_deposit_address(user)
    listener = EvmListener(rpc_url="https://rpc.test", min_confirmations=1)

    def _fake_post(url, json=None, timeout=None):
        method = json["method"]
        if method == "eth_blockNumber":
            return _FakeResponse({"result": "0x64"})
        if method == "eth_getBlockByNumber":
            block_number = int(json["params"][0], 16)
            if block_number == 100:
                return _FakeResponse({"result": {"number": hex(100), "transactions": [
                    {"to": addr.address, "value": hex(2 * 10**18), "hash": "0xdeposittx1"},
                ]}})
            return _FakeResponse({"result": {"number": hex(block_number), "transactions": []}})
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr("app.chain_listeners.evm_listener.requests.post", _fake_post)

    credited = listener.scan_for_user_deposits(from_block=95, to_block=100, chain="ethereum")

    assert credited == [(user.id, "ETH", Decimal("2"), "0xdeposittx1")]
    assert user_balance(user.id, "ETH") == Decimal("2")

    treasury_entry = LedgerEntry.query.filter_by(account="treasury:ethereum:ETH").one()
    assert treasury_entry.amount == Decimal("2")
    assert treasury_entry.tx_hash == "0xdeposittx1"


def test_scan_for_user_deposits_does_not_double_credit_on_rescan(app, monkeypatch):
    user = _user()
    addr = _user_deposit_address(user)
    listener = EvmListener(rpc_url="https://rpc.test", min_confirmations=1)

    def _fake_post(url, json=None, timeout=None):
        method = json["method"]
        if method == "eth_blockNumber":
            return _FakeResponse({"result": "0x64"})
        if method == "eth_getBlockByNumber":
            block_number = int(json["params"][0], 16)
            if block_number == 100:
                return _FakeResponse({"result": {"number": hex(100), "transactions": [
                    {"to": addr.address, "value": hex(1 * 10**18), "hash": "0xdeposittx2"},
                ]}})
            return _FakeResponse({"result": {"number": hex(block_number), "transactions": []}})
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr("app.chain_listeners.evm_listener.requests.post", _fake_post)

    first = listener.scan_for_user_deposits(from_block=95, to_block=100, chain="ethereum")
    second = listener.scan_for_user_deposits(from_block=95, to_block=100, chain="ethereum")  # same range rescanned

    assert len(first) == 1
    assert second == []  # already credited, not repeated
    assert user_balance(user.id, "ETH") == Decimal("1")


def test_scan_for_user_deposits_no_addresses_skips_rpc(app, monkeypatch):
    listener = EvmListener(rpc_url="https://rpc.test", min_confirmations=1)

    def _fake_post(url, json=None, timeout=None):
        raise AssertionError("should not call RPC when there are no user-owned addresses")

    monkeypatch.setattr("app.chain_listeners.evm_listener.requests.post", _fake_post)
    assert listener.scan_for_user_deposits(from_block=95, to_block=100) == []


def test_scan_for_user_deposits_ignores_order_scoped_addresses(app, monkeypatch):
    # An address with no user_id (the admin's own order flow) must not be
    # picked up by the account-balance scanner.
    addr = DepositAddress(chain="ethereum", address="0xOrderScoped", derivation_index=0, label="admin-order")
    db.session.add(addr)
    db.session.commit()

    listener = EvmListener(rpc_url="https://rpc.test", min_confirmations=1)

    def _fake_post(url, json=None, timeout=None):
        raise AssertionError("should not call RPC when there are no user-owned addresses")

    monkeypatch.setattr("app.chain_listeners.evm_listener.requests.post", _fake_post)
    assert listener.scan_for_user_deposits(from_block=95, to_block=100) == []


# --- BTC --------------------------------------------------------------


def test_poll_user_deposits_credits_balance_and_treasury(app, monkeypatch):
    user = _user()
    addr = _user_deposit_address(user, chain="bitcoin", address="bc1quser1")
    listener = BtcListener(esplora_base_url="https://esplora.test/api", min_confirmations=1)

    def _fake_get(url, timeout=None):
        if url.endswith("/blocks/tip/height"):
            return _FakeResponse(100)
        if url.endswith(f"/address/{addr.address}/utxo"):
            return _FakeResponse([{"txid": "btcdeposit1", "value": 50000000, "status": {"confirmed": True, "block_height": 99}}])
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("app.chain_listeners.btc_listener.requests.get", _fake_get)

    credited = listener.poll_user_deposits()

    assert credited == [(user.id, "BTC", Decimal("0.5"), "btcdeposit1")]
    assert user_balance(user.id, "BTC") == Decimal("0.5")
    treasury_entry = LedgerEntry.query.filter_by(account="treasury:bitcoin:BTC").one()
    assert treasury_entry.tx_hash == "btcdeposit1"


def test_poll_user_deposits_does_not_double_credit(app, monkeypatch):
    user = _user()
    addr = _user_deposit_address(user, chain="bitcoin", address="bc1quser2")
    listener = BtcListener(esplora_base_url="https://esplora.test/api", min_confirmations=1)

    def _fake_get(url, timeout=None):
        if url.endswith("/blocks/tip/height"):
            return _FakeResponse(100)
        return _FakeResponse([{"txid": "btcdeposit2", "value": 10000000, "status": {"confirmed": True, "block_height": 99}}])

    monkeypatch.setattr("app.chain_listeners.btc_listener.requests.get", _fake_get)

    first = listener.poll_user_deposits()
    second = listener.poll_user_deposits()

    assert len(first) == 1
    assert second == []
    # SQLite (this test suite's backend) stores Numeric as float under the
    # hood; Postgres (the real deployment target) doesn't have this limitation.
    assert abs(user_balance(user.id, "BTC") - Decimal("0.1")) < Decimal("1e-9")


def test_poll_user_deposits_no_addresses_skips_http(app, monkeypatch):
    listener = BtcListener(esplora_base_url="https://esplora.test/api", min_confirmations=1)

    def _fake_get(url, timeout=None):
        raise AssertionError("should not call Esplora when there are no user-owned addresses")

    monkeypatch.setattr("app.chain_listeners.btc_listener.requests.get", _fake_get)
    assert listener.poll_user_deposits() == []
