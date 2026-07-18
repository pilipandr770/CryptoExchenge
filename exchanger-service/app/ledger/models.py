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
    # Set for account-balance entries (deposit/swap/withdrawal/transfer
    # against a registered User's balance); null for treasury/revenue
    # entries, which have no owning user. `account` stays the
    # human-readable string ("user:<id>:<asset>", "treasury:<chain>:<asset>",
    # "revenue:margin:<asset>") for continuity with existing reconciliation
    # code -- this FK is the structured, unambiguous way to query "this
    # user's entries" without parsing that string.
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    account = db.Column(db.String(128), nullable=False)  # "user:<id>:<asset>" | "treasury:<chain>:<asset>" | "fee"
    asset = db.Column(db.String(16), nullable=False)
    amount = db.Column(db.Numeric(36, 18), nullable=False)  # signed: + credit, - debit
    entry_type = db.Column(db.String(16), nullable=False)  # deposit | swap_in | swap_out | withdrawal | fee | transfer_in | transfer_out
    # The on-chain transaction/UTXO id behind a deposit, when there is one --
    # the idempotency key chain_listeners use to credit a persistent
    # user-owned DepositAddress exactly once per incoming transaction (unlike
    # an order-scoped address, a user's address is reused across many
    # deposits over time, so "has an order already confirmed this deposit"
    # isn't a sufficient check here).
    tx_hash = db.Column(db.String(128), nullable=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    swap_order = db.relationship("SwapOrder", back_populates="ledger_entries")
    user = db.relationship("User")
