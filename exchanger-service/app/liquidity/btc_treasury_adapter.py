"""Phase 1 BTC liquidity per ТЗ section 2/6: native BTC has no DEX, so a
"BTC -> X" swap is really "BTC deposit sits in custody; treasury's existing
WBTC (1:1 backed, topped up manually via /admin/treasury) swaps to X through
the same EvmDexAdapter used for EVM assets." Phase 2 replaces
`treasury_wbtc_balance` with a real bridge/liquidity-partner API without the
orchestrator ever knowing the difference -- it still only ever sees
get_quote/execute_swap.
"""

from dataclasses import replace
from decimal import Decimal
from typing import Callable

from app.liquidity.base import LiquidityAdapter, LiquidityAdapterError, Quote, SwapExecutionResult


class TreasuryRebalanceRequiredError(LiquidityAdapterError):
    """Raised when the treasury doesn't hold enough WBTC to cover a BTC-leg
    swap. The orchestrator catches this and parks the order in
    PENDING_TREASURY_REBALANCE until an operator runs the manual BTC<->WBTC
    conversion documented in RUNBOOK.md."""

    def __init__(self, required: Decimal, available: Decimal):
        self.required = required
        self.available = available
        super().__init__(
            f"treasury holds {available} WBTC, needs {required} WBTC -- "
            "run the manual BTC<->WBTC rebalance in /admin/treasury"
        )


class BtcTreasuryAdapter(LiquidityAdapter):
    def __init__(self, evm_dex_adapter: LiquidityAdapter, treasury_wbtc_balance: Callable[[], Decimal]):
        self._evm_dex_adapter = evm_dex_adapter
        self._treasury_wbtc_balance = treasury_wbtc_balance

    def get_quote(self, from_asset: str, to_asset: str, amount: Decimal) -> Quote:
        if from_asset.upper() != "BTC":
            raise LiquidityAdapterError(
                f"BtcTreasuryAdapter only handles from_asset=BTC, got {from_asset!r}"
            )

        available = self._treasury_wbtc_balance()
        if available < amount:
            raise TreasuryRebalanceRequiredError(required=amount, available=available)

        # 1:1 WBTC-backing means sizing the leg in WBTC is the same amount
        # of BTC being deposited.
        wbtc_quote = self._evm_dex_adapter.get_quote("WBTC", to_asset, amount)
        return replace(
            wbtc_quote,
            from_asset="BTC",
            raw_provider_response={
                **wbtc_quote.raw_provider_response,
                "requires_treasury_rebalance": True,
            },
        )

    def execute_swap(self, quote: Quote) -> SwapExecutionResult:
        # The quote we handed back claims from_asset="BTC" for the
        # orchestrator's benefit; the underlying adapter needs "WBTC" to
        # match what it actually quoted.
        wbtc_quote = replace(quote, from_asset="WBTC")
        return self._evm_dex_adapter.execute_swap(wbtc_quote)
