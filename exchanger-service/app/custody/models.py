from datetime import datetime, timezone

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class HotWallet(db.Model):
    """One row per chain the platform custodies funds on. `address` is the
    platform's own hot wallet -- swap execution and withdrawals sign from
    here. `balance_cache` is refreshed by the chain listeners / admin
    reconciliation job, not read live from-chain on every request."""

    __tablename__ = "hot_wallets"

    id = db.Column(db.Integer, primary_key=True)
    chain = db.Column(db.String(16), nullable=False, unique=True)  # "ethereum" | "bitcoin" | "polygon"
    address = db.Column(db.String(128), nullable=False)
    derivation_path = db.Column(db.String(64), nullable=False)
    balance_cache = db.Column(db.Numeric(36, 18), nullable=False, default=0)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class DepositAddress(db.Model):
    """A per-demo-client deposit address, derived from the hot wallet's HD
    seed at `derivation_index`. `label` is an operator-assigned free-text
    identifier (e.g. "demo-investor-1") -- there is no end-user account
    system in this MVP, see ТЗ section 1."""

    __tablename__ = "deposit_addresses"

    id = db.Column(db.Integer, primary_key=True)
    chain = db.Column(db.String(16), nullable=False)
    address = db.Column(db.String(128), nullable=False, unique=True)
    derivation_index = db.Column(db.Integer, nullable=False)
    label = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        db.UniqueConstraint("chain", "derivation_index", name="uq_deposit_address_chain_index"),
    )
