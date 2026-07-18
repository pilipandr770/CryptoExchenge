from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class User(db.Model):
    """A registered account holder. Screened exactly once, at registration
    (app/accounts/auth.py::screen_new_user) -- the decision is stored here
    and never re-checked per swap, unlike the old order-scoped screening in
    app/swap/models.py::SwapOrder (kept for the admin's own
    external-deposit order flow, which has no registered user behind it)."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(256), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(256), nullable=False)

    full_name = db.Column(db.String(256), nullable=False)
    date_of_birth = db.Column(db.String(32), nullable=True)
    country = db.Column(db.String(256), nullable=True)

    screening_decision = db.Column(db.String(16), nullable=True)  # accepted | review | rejected
    screening_score = db.Column(db.Float, nullable=True)

    # TODO(production): replace with HSM/KMS-backed secret storage -- the
    # same dev-only compromise as app/custody/key_management.py's hot
    # wallet mnemonic. Fernet-encrypted with ACCOUNTS_TOTP_ENCRYPTION_KEY
    # (see app/accounts/auth.py), never stored plaintext.
    totp_secret_encrypted = db.Column(db.LargeBinary, nullable=True)
    totp_enabled = db.Column(db.Boolean, nullable=False, default=False)

    # False either because screening came back review/rejected (blocked
    # until an admin approves via /admin/users/<id>) or because an admin
    # froze the account later -- same flag, both cases need a human to
    # clear it via the same action.
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)


class Transfer(db.Model):
    """An instant, synchronous internal balance transfer between two
    platform users -- no blockchain step, no state machine, just a debit +
    credit ledger pair (see app/accounts/balances.py) plus this record for
    history."""

    __tablename__ = "transfers"

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    asset = db.Column(db.String(16), nullable=False)
    amount = db.Column(db.Numeric(36, 18), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    sender = db.relationship("User", foreign_keys=[sender_id])
    recipient = db.relationship("User", foreign_keys=[recipient_id])
