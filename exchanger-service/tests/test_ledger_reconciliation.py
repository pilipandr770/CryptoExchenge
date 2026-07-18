from decimal import Decimal

from app.extensions import db
from app.ledger import reconciliation
from app.ledger.models import LedgerEntry


def _add_entries(*rows):
    for account, asset, amount, entry_type in rows:
        db.session.add(LedgerEntry(account=account, asset=asset, amount=amount, entry_type=entry_type))
    db.session.commit()


def test_ledger_balances_sum_by_account_asset(app):
    _add_entries(
        ("treasury:ethereum:USDC", "USDC", Decimal("100"), "swap_in"),
        ("treasury:ethereum:USDC", "USDC", Decimal("-30"), "withdrawal"),
        ("user:demo-1", "ETH", Decimal("1"), "deposit"),
    )

    balances = reconciliation.ledger_balances_by_account_asset()

    assert balances[("treasury:ethereum:USDC", "USDC")] == Decimal("70")
    assert balances[("user:demo-1", "ETH")] == Decimal("1")


def test_treasury_ledger_balance_defaults_to_zero_when_no_entries(app):
    assert reconciliation.treasury_ledger_balance("ethereum", "WBTC") == Decimal("0")


def test_treasury_ledger_balance_sums_only_matching_account(app):
    _add_entries(
        ("treasury:ethereum:WBTC", "WBTC", Decimal("2"), "swap_in"),
        ("treasury:ethereum:WBTC", "WBTC", Decimal("-0.5"), "swap_out"),
        ("treasury:bitcoin:BTC", "BTC", Decimal("5"), "deposit"),  # different chain, must not leak in
    )

    assert reconciliation.treasury_ledger_balance("ethereum", "WBTC") == Decimal("1.5")


def test_reconcile_treasury_accounts_flags_mismatch(app):
    _add_entries(("treasury:ethereum:USDC", "USDC", Decimal("100"), "swap_in"))

    results = reconciliation.reconcile_treasury_accounts({("ethereum", "USDC"): Decimal("100")})
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].difference == Decimal("0")

    results = reconciliation.reconcile_treasury_accounts({("ethereum", "USDC"): Decimal("97")})
    assert results[0].ok is False
    assert results[0].difference == Decimal("-3")


def test_reconcile_treasury_accounts_respects_tolerance(app):
    _add_entries(("treasury:ethereum:WBTC", "WBTC", Decimal("1"), "swap_in"))

    results = reconciliation.reconcile_treasury_accounts(
        {("ethereum", "WBTC"): Decimal("1.0000000001")}, tolerance=Decimal("0.000001"),
    )
    assert results[0].ok is True


def test_reconcile_treasury_accounts_to_dict_is_json_serializable_shape(app):
    _add_entries(("treasury:ethereum:USDC", "USDC", Decimal("50"), "swap_in"))
    results = reconciliation.reconcile_treasury_accounts({("ethereum", "USDC"): Decimal("50")})
    payload = results[0].to_dict()
    assert payload["account"] == "treasury:ethereum:USDC"
    assert payload["asset"] == "USDC"
    assert Decimal(payload["ledger_balance"]) == Decimal("50")
    assert Decimal(payload["onchain_balance"]) == Decimal("50")
    assert Decimal(payload["difference"]) == Decimal("0")
    assert payload["ok"] is True
