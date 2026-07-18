from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.liquidity.base import LiquidityAdapterError, Quote, SwapExecutionResult
from app.liquidity.btc_treasury_adapter import BtcTreasuryAdapter, TreasuryRebalanceRequiredError


class _FakeEvmDexAdapter:
    def __init__(self):
        self.quote_calls = []
        self.execute_calls = []

    def get_quote(self, from_asset, to_asset, amount):
        self.quote_calls.append((from_asset, to_asset, amount))
        return Quote(
            quote_id="wbtc-quote-1",
            from_asset=from_asset,
            to_asset=to_asset,
            from_amount=amount,
            to_amount=amount * 60000,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            raw_provider_response={"to": "0xExchange"},
        )

    def execute_swap(self, quote):
        self.execute_calls.append(quote)
        return SwapExecutionResult(
            tx_hash="0xwbtcswap",
            to_amount_executed=quote.to_amount,
            status="submitted",
            raw_provider_response=quote.raw_provider_response,
        )


def test_get_quote_delegates_to_evm_adapter_when_treasury_sufficient():
    evm = _FakeEvmDexAdapter()
    adapter = BtcTreasuryAdapter(evm, treasury_wbtc_balance=lambda: Decimal("1.0"))

    quote = adapter.get_quote("BTC", "USDC", Decimal("0.5"))

    assert evm.quote_calls == [("WBTC", "USDC", Decimal("0.5"))]
    assert quote.from_asset == "BTC"
    assert quote.to_amount == Decimal("0.5") * 60000
    assert quote.raw_provider_response["requires_treasury_rebalance"] is True


def test_get_quote_raises_when_treasury_insufficient():
    evm = _FakeEvmDexAdapter()
    adapter = BtcTreasuryAdapter(evm, treasury_wbtc_balance=lambda: Decimal("0.1"))

    with pytest.raises(TreasuryRebalanceRequiredError) as excinfo:
        adapter.get_quote("BTC", "USDC", Decimal("0.5"))

    assert excinfo.value.required == Decimal("0.5")
    assert excinfo.value.available == Decimal("0.1")
    assert evm.quote_calls == []  # never delegates when short


def test_get_quote_rejects_non_btc_source():
    evm = _FakeEvmDexAdapter()
    adapter = BtcTreasuryAdapter(evm, treasury_wbtc_balance=lambda: Decimal("10"))

    with pytest.raises(LiquidityAdapterError):
        adapter.get_quote("ETH", "USDC", Decimal("1"))


def test_execute_swap_delegates_with_wbtc_asset():
    evm = _FakeEvmDexAdapter()
    adapter = BtcTreasuryAdapter(evm, treasury_wbtc_balance=lambda: Decimal("10"))

    quote = adapter.get_quote("BTC", "USDC", Decimal("1"))
    result = adapter.execute_swap(quote)

    assert evm.execute_calls[0].from_asset == "WBTC"
    assert result.tx_hash == "0xwbtcswap"
