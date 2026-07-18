"""Per-user balance helpers, built on the same double-entry LedgerEntry
model app/ledger/reconciliation.py already uses for treasury accounts -- a
user's balance for an asset is the sum of their LedgerEntry rows for that
asset, never a separately-maintained counter that could drift out of sync.
"""

from decimal import Decimal

from app.extensions import db
from app.ledger.models import LedgerEntry


class InsufficientBalanceError(Exception):
    def __init__(self, user_id: int, asset: str, requested: Decimal, available: Decimal):
        self.user_id = user_id
        self.asset = asset
        self.requested = requested
        self.available = available
        super().__init__(
            f"user {user_id} balance {available} {asset} is less than requested {requested} {asset}"
        )


def _account_string(user_id: int, asset: str) -> str:
    return f"user:{user_id}:{asset}"


def is_tx_already_credited(tx_hash: str) -> bool:
    """Idempotency check for chain_listeners crediting a persistent,
    reusable user-owned DepositAddress -- unlike an order-scoped address
    (good for exactly one deposit), the same tx_hash must never be
    credited twice across repeated poll cycles."""
    return LedgerEntry.query.filter_by(tx_hash=tx_hash, entry_type="deposit").first() is not None


def user_balance(user_id: int, asset: str) -> Decimal:
    total = (
        db.session.query(db.func.coalesce(db.func.sum(LedgerEntry.amount), 0))
        .filter_by(user_id=user_id, asset=asset)
        .scalar()
    )
    return Decimal(total)


def all_user_balances(user_id: int) -> dict:
    """{asset: Decimal}, omitting zero balances (fully spent/withdrawn
    assets don't clutter a dashboard)."""
    rows = (
        db.session.query(LedgerEntry.asset, db.func.coalesce(db.func.sum(LedgerEntry.amount), 0))
        .filter_by(user_id=user_id)
        .group_by(LedgerEntry.asset)
        .all()
    )
    return {asset: Decimal(total) for asset, total in rows if Decimal(total) != 0}


def credit_user(user_id: int, asset: str, amount: Decimal, entry_type: str, swap_order_id: int = None, tx_hash: str = None) -> None:
    db.session.add(LedgerEntry(
        user_id=user_id, account=_account_string(user_id, asset), asset=asset,
        amount=amount, entry_type=entry_type, swap_order_id=swap_order_id, tx_hash=tx_hash,
    ))


def debit_user(user_id: int, asset: str, amount: Decimal, entry_type: str, swap_order_id: int = None, tx_hash: str = None) -> None:
    """Raises InsufficientBalanceError -- without writing anything -- if
    the user's current balance can't cover `amount`."""
    available = user_balance(user_id, asset)
    if available < amount:
        raise InsufficientBalanceError(user_id, asset, amount, available)

    db.session.add(LedgerEntry(
        user_id=user_id, account=_account_string(user_id, asset), asset=asset,
        amount=-amount, entry_type=entry_type, swap_order_id=swap_order_id, tx_hash=tx_hash,
    ))
