import logging
import sys

from flask import Flask, jsonify

from app.config import Config
from app.extensions import db, migrate


def _configure_logging():
    # A bare `logging.getLogger(__name__)` in each module propagates to the
    # root logger, which has no handler by default -- INFO-level messages
    # would silently vanish under gunicorn otherwise (only WARNING+ reaches
    # the interpreter's lastResort handler). Mirrors compliance-service's
    # app/__init__.py.
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def create_app(config_class=Config):
    _configure_logging()

    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)

    # bitcoinutils' setup() is process-global and must run before any BTC
    # key derivation/address encoding happens (app.custody.btc_wallet).
    from app.custody.btc_wallet import configure_network as _configure_btc_network
    _configure_btc_network(app.config["BTC_NETWORK"])

    from app.swap.routes import swap_bp
    app.register_blueprint(swap_bp)

    from app.admin_ui.routes import admin_ui_bp
    app.register_blueprint(admin_ui_bp)

    from app.public_ui.routes import public_ui_bp
    app.register_blueprint(public_ui_bp)

    # Import models so Alembic/SQLAlchemy metadata picks them up.
    from app.custody import models as _custody_models  # noqa: F401
    from app.swap import models as _swap_models  # noqa: F401
    from app.ledger import models as _ledger_models  # noqa: F401
    from app.audit import models as _audit_models  # noqa: F401

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    from app.cli import register_cli
    register_cli(app)

    return app
