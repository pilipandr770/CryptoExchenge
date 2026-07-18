from datetime import datetime, timezone

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class LedgerEntry(db.Model):
    """Double-entry-style bookkeeping row. Each side of a movement (deposit
    in, treasury swap in/out, withdrawal out, fee) gets its own row rather
    than a single signed balance mutation, so reconciliation.py can sum by
    account/asset and compare against on-chain balances (ТЗ section 5/9)."""

    __tablename__ = "ledger_entries"

    id = db.Column(db.Integer, primary_key=True)
    swap_order_id = db.Column(db.Integer, db.ForeignKey("swap_orders.id"), nullable=True)
    account = db.Column(db.String(128), nullable=False)  # "user:<label>" | "treasury:<chain>:<asset>" | "fee"
    asset = db.Column(db.String(16), nullable=False)
    amount = db.Column(db.Numeric(36, 18), nullable=False)  # signed: + credit, - debit
    entry_type = db.Column(db.String(16), nullable=False)  # deposit | swap_in | swap_out | withdrawal | fee
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    swap_order = db.relationship("SwapOrder", back_populates="ledger_entries")
