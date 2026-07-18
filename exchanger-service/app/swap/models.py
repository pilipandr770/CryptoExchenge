from datetime import datetime, timezone
from uuid import uuid4

from app.extensions import db
from app.swap.states import DEPOSIT_PENDING, InvalidTransitionError, can_transition


def _utcnow():
    return datetime.now(timezone.utc)


class SwapOrder(db.Model):
    """One custodial deposit -> swap -> withdrawal pipeline run. `status`
    only ever moves along app.swap.states' transition graph -- use
    `mark_status()`, never assign `.status` directly, so an invalid jump
    fails loudly instead of corrupting the pipeline."""

    __tablename__ = "swap_orders"

    id = db.Column(db.Integer, primary_key=True)
    # Opaque handle for the public order-status page (app/public_ui) -- the
    # sequential integer id must never be exposed there, or one client could
    # enumerate every other client's orders by incrementing a URL.
    public_token = db.Column(db.String(32), nullable=False, unique=True, default=lambda: uuid4().hex)
    status = db.Column(db.String(32), nullable=False, default=DEPOSIT_PENDING, index=True)

    # Client identity, for the AML-18 screening gate (POST /screening/check-name
    # needs at least a name) -- collected up front on public order creation
    # (app/public_ui), optional for admin-created orders.
    client_name = db.Column(db.String(256), nullable=True)
    client_email = db.Column(db.String(256), nullable=True)
    client_date_of_birth = db.Column(db.String(32), nullable=True)
    client_country = db.Column(db.String(256), nullable=True)

    deposit_address_id = db.Column(db.Integer, db.ForeignKey("deposit_addresses.id"), nullable=True)
    from_chain = db.Column(db.String(16), nullable=False)
    from_asset = db.Column(db.String(16), nullable=False)
    from_amount = db.Column(db.Numeric(36, 18), nullable=False)
    deposit_tx_hash = db.Column(db.String(128), nullable=True)

    to_chain = db.Column(db.String(16), nullable=False)
    to_asset = db.Column(db.String(16), nullable=False)
    # Client-facing (net, after margin) amounts -- what the client was
    # actually promised/paid. See app/pricing/margin.py.
    to_amount_quoted = db.Column(db.Numeric(36, 18), nullable=True)
    to_amount_payout = db.Column(db.Numeric(36, 18), nullable=True)
    # Raw adapter amounts, before margin -- internal accounting only, never
    # shown to the client.
    to_amount_quoted_gross = db.Column(db.Numeric(36, 18), nullable=True)
    to_amount_executed = db.Column(db.Numeric(36, 18), nullable=True)
    # Snapshotted at quote-lock time so a later MARGIN_PERCENT config change
    # can never retroactively change what this client was promised.
    margin_percent = db.Column(db.Numeric(8, 4), nullable=True)
    quote_id = db.Column(db.String(128), nullable=True)
    quote_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # The liquidity adapter's raw quote payload (0x's tx calldata, etc) --
    # persisted so execute_swap can be replayed from DB state alone after a
    # crash/restart, without needing to keep the in-memory Quote object.
    quote_raw_response = db.Column(db.JSON, nullable=True)
    swap_tx_hash = db.Column(db.String(128), nullable=True)

    # Set by BtcTreasuryAdapter when a BTC-leg swap needed WBTC the treasury
    # didn't have on hand -- surfaced on /admin/treasury for the manual
    # BTC<->WBTC rebalance step (ТЗ section 6, Phase 1).
    requires_treasury_rebalance = db.Column(db.Boolean, nullable=False, default=False)

    withdrawal_address = db.Column(db.String(128), nullable=True)
    withdrawal_tx_hash = db.Column(db.String(128), nullable=True)
    wallet_ownership_challenge_id = db.Column(db.String(64), nullable=True)
    wallet_ownership_challenge_message = db.Column(db.Text, nullable=True)
    wallet_ownership_verification_id = db.Column(db.String(64), nullable=True)

    screening_decision = db.Column(db.String(16), nullable=True)  # accepted | review | rejected
    screening_score = db.Column(db.Float, nullable=True)

    last_error = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    deposit_address = db.relationship("DepositAddress")
    ledger_entries = db.relationship("LedgerEntry", back_populates="swap_order")

    def __init__(self, **kwargs):
        # Column-level defaults only apply at flush time -- set these
        # eagerly so a freshly-constructed order has a real starting status
        # (for mark_status() to validate transitions against) and a real
        # public_token (for immediate use in a redirect URL) even before
        # the object is ever added/flushed.
        kwargs.setdefault("status", DEPOSIT_PENDING)
        kwargs.setdefault("public_token", uuid4().hex)
        super().__init__(**kwargs)

    def mark_status(self, new_status: str):
        if not can_transition(self.status, new_status):
            raise InvalidTransitionError(self.status, new_status)
        self.status = new_status
        self.updated_at = _utcnow()
