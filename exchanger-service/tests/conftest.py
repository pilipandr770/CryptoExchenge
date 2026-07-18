import pytest

from app import create_app
from app.config import Config


class TestConfig(Config):
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    TESTING = True
    SECRET_KEY = "test-secret"
    ADMIN_USERNAME = "admin"
    ADMIN_PASSWORD = "test-password"
    AML18_BASE_URL = "http://aml18.test"
    AML18_API_KEY = "aml18_sk_test"
    ZEROX_API_BASE_URL = "https://api.0x.test"
    ACCOUNTS_TOTP_ENCRYPTION_KEY = "471GHlAG75PSyrdGrKQ_LnDxxBBoCd5VFGm1zG91CyI="
    ACCOUNTS_TOTP_ISSUER = "Test Exchanger"


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        from app.extensions import db
        db.create_all()
        yield app


@pytest.fixture
def client(app):
    return app.test_client()
