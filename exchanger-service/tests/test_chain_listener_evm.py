from decimal import Decimal

from app.chain_listeners.evm_listener import EvmListener
from app.custody import models as _custody_models  # noqa: F401
from app.custody.models import DepositAddress
from app.extensions import db
from app.ledger import models as _ledger_models  # noqa: F401
from app.swap.models import SwapOrder
from app.swap.states import DEPOSIT_CONFIRMED, DEPOSIT_PENDING


class _FakeResponse:
    def __init__(self, result):
        self._result = result

    def raise_for_status(self):
        pass

    def json(self):
        return {"result": self._result}


def _pending_order(deposit_address="0xDeposit1"):
    addr = DepositAddress(chain="ethereum", address=deposit_address, derivation_index=0, label="demo-1")
    db.session.add(addr)
    db.session.flush()
    order = SwapOrder(
        deposit_address_id=addr.id, from_chain="ethereum", from_asset="ETH", from_amount=Decimal("1"),
        to_chain="ethereum", to_asset="USDC",
    )
    db.session.add(order)
    db.session.commit()
    return order


def test_scan_range_confirms_deposit_with_enough_confirmations(app, monkeypatch):
    order = _pending_order()
    listener = EvmListener(rpc_url="https://rpc.test", min_confirmations=2)

    def _fake_post(url, json=None, timeout=None):
        method = json["method"]
        if method == "eth_blockNumber":
            return _FakeResponse("0x64")  # head = 100
        if method == "eth_getBlockByNumber":
            block_number = int(json["params"][0], 16)
            if block_number == 97:
                return _FakeResponse({
                    "number": hex(97),
                    "transactions": [{"to": order.deposit_address.address, "value": hex(10**18), "hash": "0xdeposittx"}],
                })
            return _FakeResponse({"number": hex(block_number), "transactions": []})
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr("app.chain_listeners.evm_listener.requests.post", _fake_post)

    confirmed = listener.scan_range(from_block=95, to_block=100, chain="ethereum")

    assert len(confirmed) == 1
    assert order.status == DEPOSIT_CONFIRMED
    assert order.deposit_tx_hash == "0xdeposittx"


def test_scan_range_skips_blocks_without_enough_confirmations(app, monkeypatch):
    order = _pending_order()
    listener = EvmListener(rpc_url="https://rpc.test", min_confirmations=10)

    def _fake_post(url, json=None, timeout=None):
        method = json["method"]
        if method == "eth_blockNumber":
            return _FakeResponse("0x64")  # head = 100, so block 97 only has 4 confirmations
        if method == "eth_getBlockByNumber":
            block_number = int(json["params"][0], 16)
            if block_number == 97:
                return _FakeResponse({
                    "number": hex(97),
                    "transactions": [{"to": order.deposit_address.address, "value": hex(10**18), "hash": "0xdeposittx"}],
                })
            return _FakeResponse({"number": hex(block_number), "transactions": []})
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr("app.chain_listeners.evm_listener.requests.post", _fake_post)

    confirmed = listener.scan_range(from_block=95, to_block=100, chain="ethereum")

    assert confirmed == []
    assert order.status == DEPOSIT_PENDING


def test_scan_range_ignores_unrelated_transactions(app, monkeypatch):
    order = _pending_order()
    listener = EvmListener(rpc_url="https://rpc.test", min_confirmations=1)

    def _fake_post(url, json=None, timeout=None):
        method = json["method"]
        if method == "eth_blockNumber":
            return _FakeResponse("0x64")
        if method == "eth_getBlockByNumber":
            block_number = int(json["params"][0], 16)
            return _FakeResponse({
                "number": hex(block_number),
                "transactions": [{"to": "0xSomeoneElse", "value": hex(10**18), "hash": "0xnotours"}],
            })
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr("app.chain_listeners.evm_listener.requests.post", _fake_post)

    confirmed = listener.scan_range(from_block=95, to_block=100, chain="ethereum")

    assert confirmed == []
    assert order.status == DEPOSIT_PENDING


def test_scan_range_no_pending_orders_skips_rpc_calls(app, monkeypatch):
    listener = EvmListener(rpc_url="https://rpc.test", min_confirmations=1)

    def _fake_post(url, json=None, timeout=None):
        raise AssertionError("should not call RPC when there are no pending orders")

    monkeypatch.setattr("app.chain_listeners.evm_listener.requests.post", _fake_post)

    assert listener.scan_range(from_block=95, to_block=100) == []
