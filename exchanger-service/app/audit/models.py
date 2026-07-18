from datetime import datetime, timezone

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class AuditLog(db.Model):
    """Append-only log of every pipeline step and operator action: who
    (actor -- "system" for orchestrator-driven steps, an admin username for
    manual review decisions), when, which step, and before/after state.
    Mirrors compliance-service's app/audit/models.py."""

    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    actor = db.Column(db.String(128), nullable=False)
    action = db.Column(db.String(64), nullable=False)
    target_type = db.Column(db.String(64), nullable=True)
    target_id = db.Column(db.String(64), nullable=True)
    detail = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
