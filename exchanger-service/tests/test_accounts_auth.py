import pyotp
import pytest

from app.accounts import auth
from app.accounts.models import User
from app.extensions import db


class _FakeAml18Client:
    def __init__(self, check_name_result=None):
        self._check_name_result = check_name_result or {"decision": "accepted", "score": 5.0, "matches": []}
        self.calls = []

    def check_name(self, name, date_of_birth=None, country=None):
        self.calls.append((name, date_of_birth, country))
        return self._check_name_result


# --- registration ----------------------------------------------------------


def test_register_user_hashes_password_and_defaults_active(app):
    user = auth.register_user("Jane@Example.com", "hunter2", "Jane Doe", date_of_birth="1990-01-01", country="DE")
    db.session.commit()

    assert user.email == "jane@example.com"  # normalized
    assert user.password_hash != "hunter2"
    assert user.check_password("hunter2") is True
    assert user.check_password("wrong") is False
    assert user.is_active is True
    assert user.totp_enabled is False


def test_register_user_rejects_duplicate_email(app):
    auth.register_user("dup@example.com", "pw1", "Name One")
    db.session.commit()

    with pytest.raises(auth.RegistrationError):
        auth.register_user("dup@example.com", "pw2", "Name Two")


def test_register_user_email_matching_is_case_insensitive(app):
    auth.register_user("Case@Example.com", "pw", "Name")
    db.session.commit()

    with pytest.raises(auth.RegistrationError):
        auth.register_user("case@example.com", "pw2", "Other Name")


# --- screening ---------------------------------------------------------


def test_screen_new_user_accepted_activates_account(app):
    user = auth.register_user("accepted@example.com", "pw", "Jane Doe")
    client = _FakeAml18Client()

    result = auth.screen_new_user(user, client)
    db.session.commit()

    assert result["decision"] == "accepted"
    assert user.screening_decision == "accepted"
    assert user.screening_score == 5.0
    assert user.is_active is True
    assert client.calls == [("Jane Doe", None, None)]


def test_screen_new_user_review_deactivates_account(app):
    user = auth.register_user("flagged@example.com", "pw", "Suspicious Name")
    client = _FakeAml18Client(check_name_result={"decision": "review", "score": 80.0, "matches": []})

    auth.screen_new_user(user, client)
    db.session.commit()

    assert user.screening_decision == "review"
    assert user.is_active is False


# --- authenticate ------------------------------------------------------


def test_authenticate_returns_user_on_correct_credentials(app):
    auth.register_user("login@example.com", "correct-password", "Jane Doe")
    db.session.commit()

    user = auth.authenticate("login@example.com", "correct-password")
    assert user is not None
    assert user.email == "login@example.com"


def test_authenticate_returns_none_on_wrong_password(app):
    auth.register_user("login2@example.com", "correct-password", "Jane Doe")
    db.session.commit()

    assert auth.authenticate("login2@example.com", "wrong-password") is None


def test_authenticate_returns_none_for_unknown_email(app):
    assert auth.authenticate("nobody@example.com", "whatever") is None


# --- login_required decorator -------------------------------------------


def test_login_required_redirects_when_no_session(app):
    with app.test_request_context("/account/dashboard"):
        from flask import session

        @auth.login_required
        def view():
            return "ok"

        response = view()
        assert response.status_code == 302


def test_login_required_allows_when_session_set(app):
    with app.test_request_context("/account/dashboard"):
        from flask import session

        session["user_id"] = 1

        @auth.login_required
        def view():
            return "ok"

        assert view() == "ok"


# --- current_user --------------------------------------------------------


def test_current_user_returns_none_without_session(app):
    with app.test_request_context("/"):
        assert auth.current_user() is None


def test_current_user_returns_the_logged_in_user(app):
    user = auth.register_user("whoami@example.com", "pw", "Jane Doe")
    db.session.commit()

    with app.test_request_context("/"):
        from flask import session
        session["user_id"] = user.id
        found = auth.current_user()
        assert found is not None
        assert found.email == "whoami@example.com"


# --- TOTP ------------------------------------------------------------------


def test_generate_totp_secret_is_valid_base32():
    secret = auth.generate_totp_secret()
    assert len(secret) >= 16
    pyotp.TOTP(secret)  # does not raise


def test_provisioning_uri_contains_email_and_issuer(app):
    secret = auth.generate_totp_secret()
    with app.test_request_context("/"):
        uri = auth.provisioning_uri(secret, "someone@example.com")
    assert "someone%40example.com" in uri or "someone@example.com" in uri
    assert "Test%20Exchanger" in uri or "Test Exchanger" in uri


def test_verify_totp_code_accepts_current_code_rejects_wrong():
    secret = auth.generate_totp_secret()
    code = pyotp.TOTP(secret).now()
    assert auth.verify_totp_code(secret, code) is True
    assert auth.verify_totp_code(secret, "000000") is False
    assert auth.verify_totp_code(secret, "") is False


def test_enable_totp_and_get_totp_secret_roundtrip(app):
    user = auth.register_user("totp@example.com", "pw", "Jane Doe")
    db.session.commit()
    secret = auth.generate_totp_secret()

    auth.enable_totp(user, secret)
    db.session.commit()

    assert user.totp_enabled is True
    assert user.totp_secret_encrypted != secret.encode()  # actually encrypted, not plaintext
    assert auth.get_totp_secret(user) == secret


def test_get_totp_secret_raises_when_not_enabled(app):
    user = auth.register_user("nototp@example.com", "pw", "Jane Doe")
    db.session.commit()

    with pytest.raises(auth.KeyManagementError):
        auth.get_totp_secret(user)


def test_enable_totp_raises_when_encryption_key_missing(app):
    app.config["ACCOUNTS_TOTP_ENCRYPTION_KEY"] = ""
    user = auth.register_user("nokey@example.com", "pw", "Jane Doe")
    db.session.commit()

    with pytest.raises(auth.KeyManagementError):
        auth.enable_totp(user, auth.generate_totp_secret())
