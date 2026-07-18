"""Phase 1 / Phase 2 compromise (ТЗ section 2): every liquidity source --
the real EVM DEX aggregator today, a native BTC bridge/liquidity-partner
later -- speaks this same get_quote/execute_swap interface, so the
orchestrator (app/swap/orchestrator.py) never needs to know which adapter
backs a given leg of a swap.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass
class Quote:
    quote_id: str
    from_asset: str
    to_asset: str
    from_amount: Decimal
    to_amount: Decimal
    expires_at: datetime
    raw_provider_response: dict = field(default_factory=dict)


@dataclass
class SwapExecutionResult:
    tx_hash: str
    to_amount_executed: Decimal
    status: str  # "submitted" | "confirmed" | "failed"
    raw_provider_response: dict = field(default_factory=dict)


class LiquidityAdapterError(Exception):
    pass


class LiquidityAdapter(ABC):
    @abstractmethod
    def get_quote(self, from_asset: str, to_asset: str, amount: Decimal) -> Quote:
        ...

    @abstractmethod
    def execute_swap(self, quote: Quote) -> SwapExecutionResult:
        ...
