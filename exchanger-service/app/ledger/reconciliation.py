"""Reconciles the internal double-entry ledger (sum of LedgerEntry.amount
per account/asset) against real on-chain balances. Run from /admin/ledger
(ТЗ section 9) -- a mismatch means either a missed chain-listener event or a
bug, and must never go unnoticed in a custodial system, even a
personal-funds demo one.
"""

from collections import defaultdict
from decimal import Decimal

from app.ledger.models import LedgerEntry


def ledger_balances_by_account_asset() -> dict:
    """{(account, asset): Decimal} summed from every LedgerEntry row."""
    balances = defaultdict(lambda: Decimal("0"))
    rows = LedgerEntry.query.with_entities(LedgerEntry.account, LedgerEntry.asset, LedgerEntry.amount)
    for account, asset, amount in rows:
        balances[(account, asset)] += amount
    return dict(balances)


def treasury_ledger_balance(chain: str, asset: str) -> Decimal:
    """Sums just the treasury:<chain>:<asset> account -- used by
    BtcTreasuryAdapter's treasury_wbtc_balance callback and /admin/treasury."""
    account = f"treasury:{chain}:{asset}"
    return ledger_balances_by_account_asset().get((account, asset), Decimal("0"))


class ReconciliationResult:
    def __init__(self, account: str, asset: str, ledger_balance: Decimal, onchain_balance: Decimal, tolerance: Decimal):
        self.account = account
        self.asset = asset
        self.ledger_balance = ledger_balance
        self.onchain_balance = onchain_balance
        self.difference = onchain_balance - ledger_balance
        self.ok = abs(self.difference) <= tolerance

    def to_dict(self) -> dict:
        return {
            "account": self.account,
            "asset": self.asset,
            "ledger_balance": str(self.ledger_balance),
            "onchain_balance": str(self.onchain_balance),
            "difference": str(self.difference),
            "ok": self.ok,
        }


def reconcile_treasury_accounts(
    onchain_balances: dict, tolerance: Decimal = Decimal("0.00000001"),
) -> list:
    """`onchain_balances`: {(chain, asset): Decimal}, fetched live by the
    caller (admin_ui, via evm_wallet/btc_wallet RPC balance lookups) --
    reconciliation.py itself makes no chain calls, so it stays testable on
    synthetic ledger data alone. Returns one ReconciliationResult per
    (chain, asset) pair checked."""
    ledger_balances = ledger_balances_by_account_asset()
    results = []
    for (chain, asset), onchain_balance in onchain_balances.items():
        account = f"treasury:{chain}:{asset}"
        ledger_balance = ledger_balances.get((account, asset), Decimal("0"))
        results.append(ReconciliationResult(account, asset, ledger_balance, onchain_balance, tolerance))
    return results
