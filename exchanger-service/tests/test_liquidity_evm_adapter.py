from decimal import Decimal

import pytest
from eth_account import Account

from app.liquidity.base import LiquidityAdapterError, Quote
from app.liquidity.evm_dex_adapter import EvmDexAdapter, _utcnow


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


def _adapter(**overrides):
    defaults = dict(
        api_base_url="https://api.0x.test",
        rpc_url="https://rpc.test",
        sender_private_key=Account.create().key.hex(),
        chain_id=1,
        api_key="test-0x-key",
        quote_ttl_seconds=30,
    )
    defaults.update(overrides)
    return EvmDexAdapter(**defaults)


def test_get_quote_sizes_amounts_and_persists_raw_response(monkeypatch):
    adapter = _adapter()

    captured = {}

    def _fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        assert params["sellToken"] == "ETH"
        assert params["buyToken"] == "USDC"
        assert params["sellAmount"] == str(2 * 10**18)  # 2 ETH in wei
        assert params["chainId"] == 1
        return _FakeResponse(payload={
            "buyAmount": str(5000 * 10**6),  # 5000 USDC
            "transaction": {
                "to": "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
                "data": "0xdeadbeef",
                "value": "2000000000000000000",
                "gas": "250000",
            },
        })

    monkeypatch.setattr("app.liquidity.evm_dex_adapter.requests.get", _fake_get)

    quote = adapter.get_quote("ETH", "USDC", Decimal("2"))

    assert quote.to_amount == Decimal("5000")
    assert quote.from_amount == Decimal("2")
    assert quote.raw_provider_response["transaction"]["to"] == "0xDef1C0ded9bec7F1a1670819833240f027b25EfF"
    assert quote.expires_at > _utcnow()
    assert "url" in captured
    assert captured["url"].endswith("/swap/allowance-holder/quote")
    assert captured["headers"]["0x-api-key"] == "test-0x-key"
    assert captured["headers"]["0x-version"] == "v2"
    assert "taker" in captured["params"]  # sender_private_key is configured


def test_get_quote_omits_taker_when_no_signing_key_configured(monkeypatch):
    adapter = _adapter(sender_private_key="")
    captured = {}

    def _fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return _FakeResponse(payload={"buyAmount": "1", "transaction": {"to": "0x0", "data": "0x", "value": "0"}})

    monkeypatch.setattr("app.liquidity.evm_dex_adapter.requests.get", _fake_get)
    adapter.get_quote("ETH", "USDC", Decimal("1"))

    assert "taker" not in captured["params"]


def test_get_quote_raises_on_non_200(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(
        "app.liquidity.evm_dex_adapter.requests.get",
        lambda *a, **k: _FakeResponse(status_code=400, text="insufficient liquidity"),
    )
    with pytest.raises(LiquidityAdapterError):
        adapter.get_quote("ETH", "USDC", Decimal("2"))


def test_get_quote_rejects_unknown_asset():
    adapter = _adapter()
    with pytest.raises(LiquidityAdapterError):
        adapter.get_quote("NOTAREALTOKEN", "USDC", Decimal("1"))


def _make_quote(**overrides):
    from datetime import timedelta

    defaults = dict(
        quote_id="q1",
        from_asset="ETH",
        to_asset="USDC",
        from_amount=Decimal("1"),
        to_amount=Decimal("2500"),
        expires_at=_utcnow() + timedelta(seconds=30),
        raw_provider_response={
            "transaction": {
                "to": "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
                "data": "0xdeadbeef",
                "value": "1000000000000000000",
                "gas": "250000",
            },
        },
    )
    defaults.update(overrides)
    return Quote(**defaults)


def test_execute_swap_rejects_expired_quote():
    from datetime import timedelta

    adapter = _adapter()
    quote = _make_quote(expires_at=_utcnow() - timedelta(seconds=1))
    with pytest.raises(LiquidityAdapterError):
        adapter.execute_swap(quote)


def test_execute_swap_requires_signing_configuration():
    adapter = _adapter(rpc_url="", sender_private_key="")
    quote = _make_quote()
    with pytest.raises(LiquidityAdapterError):
        adapter.execute_swap(quote)


def test_execute_swap_native_sell_sends_single_transaction(monkeypatch):
    adapter = _adapter()
    quote = _make_quote()

    rpc_calls = []

    def _fake_post(url, json=None, timeout=None):
        rpc_calls.append(json["method"])
        if json["method"] == "eth_getTransactionCount":
            return _FakeResponse(payload={"result": "0x5"})
        if json["method"] == "eth_gasPrice":
            return _FakeResponse(payload={"result": "0x3b9aca00"})
        if json["method"] == "eth_sendRawTransaction":
            return _FakeResponse(payload={"result": "0xswaptxhash"})
        raise AssertionError(f"unexpected RPC method {json['method']}")

    monkeypatch.setattr("app.liquidity.evm_dex_adapter.requests.post", _fake_post)

    result = adapter.execute_swap(quote)

    assert result.tx_hash == "0xswaptxhash"
    assert result.status == "submitted"
    assert result.to_amount_executed == quote.to_amount
    # Native ETH sell: no approve tx, just the swap tx itself.
    assert rpc_calls.count("eth_sendRawTransaction") == 1


def test_execute_swap_erc20_sell_sends_approve_then_swap(monkeypatch):
    adapter = _adapter()
    quote = _make_quote(
        from_asset="USDC",
        raw_provider_response={
            "transaction": {
                "to": "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
                "data": "0xdeadbeef",
                "value": "0",
                "gas": "250000",
            },
            "sellTokenAddress": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "allowanceTarget": "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
            "sellAmount": "1000000",
        },
    )

    sent_tx_data = []

    def _fake_post(url, json=None, timeout=None):
        method = json["method"]
        if method == "eth_getTransactionCount":
            return _FakeResponse(payload={"result": "0x1"})
        if method == "eth_gasPrice":
            return _FakeResponse(payload={"result": "0x3b9aca00"})
        if method == "eth_sendRawTransaction":
            sent_tx_data.append(True)
            tx_hash = "0xapprovetxhash" if len(sent_tx_data) == 1 else "0xswaptxhash"
            return _FakeResponse(payload={"result": tx_hash})
        raise AssertionError(f"unexpected RPC method {method}")

    monkeypatch.setattr("app.liquidity.evm_dex_adapter.requests.post", _fake_post)

    result = adapter.execute_swap(quote)

    assert len(sent_tx_data) == 2  # approve, then swap
    assert result.tx_hash == "0xswaptxhash"


def test_get_swap_status_reports_pending_confirmed_failed(monkeypatch):
    adapter = _adapter()
    responses_queue = []

    def _fake_post(url, json=None, timeout=None):
        method = json["method"]
        if method == "eth_getTransactionReceipt":
            return _FakeResponse(payload={"result": responses_queue.pop(0)})
        if method == "eth_blockNumber":
            return _FakeResponse(payload={"result": "0x10"})
        raise AssertionError(f"unexpected RPC method {method}")

    monkeypatch.setattr("app.liquidity.evm_dex_adapter.requests.post", _fake_post)

    responses_queue.append(None)
    assert adapter.get_swap_status("0xabc", min_confirmations=1) == "pending"

    responses_queue.append({"status": "0x0", "blockNumber": "0x10"})
    assert adapter.get_swap_status("0xabc", min_confirmations=1) == "failed"

    responses_queue.append({"status": "0x1", "blockNumber": "0x10"})
    assert adapter.get_swap_status("0xabc", min_confirmations=1) == "confirmed"

    responses_queue.append({"status": "0x1", "blockNumber": "0xf"})
    assert adapter.get_swap_status("0xabc", min_confirmations=5) == "pending"
