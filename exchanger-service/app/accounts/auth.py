"""Registration, login, and TOTP 2FA for registered accounts (see
app/accounts/models.py::User). Session-keyed on `session["user_id"]`, a
distinct key from admin_ui's `session["admin_logged_in"]` so the two auth
systems never collide in the same browser session.
"""

from functools import wraps

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app, redirect, request, session

from app.accounts.models import User
from app.compliance_client.aml18_client import Aml18Client
from app.extensions import db


class RegistrationError(Exception):
    pass


class KeyManagementError(Exception):
    """Raised when ACCOUNTS_TOTP_ENCRYPTION_KEY is missing or invalid --
    named to match app.custody.key_management's error for the same class
    of "dev-only secret storage misconfigured" problem."""


# --- registration + one-time KYC screening --------------------------------


def register_user(email: str, password: str, full_name: str, date_of_birth: str = None, country: str = None) -> User:
    email = email.strip().lower()
    if User.query.filter_by(email=email).first() is not None:
        raise RegistrationError(f"an account with email {email!r} already exists")

    user = User(email=email, full_name=full_name, date_of_birth=date_of_birth, country=country, is_active=True)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    return user


def screen_new_user(user: User, aml18_client: Aml18Client) -> dict:
    """Calls AML-18's one-off sanctions check exactly once, at registration
    (ТЗ decision: KYC is not re-run per swap). Unlike
    app.compliance_client.screening_gate.enforce_screening_gate, this
    doesn't touch a SwapOrder state machine -- User has no such thing --
    so it talks to Aml18Client directly and just records the outcome:
    "review"/"rejected" leaves the account inactive until an admin approves
    it via /admin/users/<id>."""
    result = aml18_client.check_name(user.full_name, date_of_birth=user.date_of_birth, country=user.country)
    user.screening_decision = result["decision"]
    user.screening_score = result["score"]
    user.is_active = result["decision"] == "accepted"
    return result


# --- password auth --------------------------------------------------------


def authenticate(email: str, password: str) -> User:
    """Returns the User on valid credentials, or None. Does not check
    is_active/totp_enabled -- callers (the login route) decide what to do
    with a correct-password-but-inactive-or-2FA-pending account."""
    user = User.query.filter_by(email=email.strip().lower()).first()
    if user is None or not user.check_password(password):
        return None
    return user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            # A raw path, not url_for("account_ui.login", ...) -- this
            # decorator has no dependency on app.account_ui being
            # registered/importable, avoiding an import cycle between the
            # two modules.
            return redirect(f"/account/login?next={request.path}")
        return view(*args, **kwargs)
    return wrapped


def current_user() -> User:
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return db.session.get(User, user_id)


# --- TOTP 2FA --------------------------------------------------------------


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    issuer = current_app.config["ACCOUNTS_TOTP_ISSUER"]
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def verify_totp_code(secret: str, code: str) -> bool:
    if not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip())


def _fernet() -> Fernet:
    key = current_app.config["ACCOUNTS_TOTP_ENCRYPTION_KEY"]
    if not key:
        raise KeyManagementError("ACCOUNTS_TOTP_ENCRYPTION_KEY is not set")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise KeyManagementError(f"invalid ACCOUNTS_TOTP_ENCRYPTION_KEY: {exc}") from exc


def enable_totp(user: User, secret: str) -> None:
    """Encrypts and stores the TOTP secret, marking 2FA enabled. Call only
    after verify_totp_code() has confirmed the user actually scanned the
    QR/entered the secret correctly -- see the 2FA setup route."""
    user.totp_secret_encrypted = _fernet().encrypt(secret.encode())
    user.totp_enabled = True


def get_totp_secret(user: User) -> str:
    if not user.totp_secret_encrypted:
        raise KeyManagementError(f"user {user.id} has no TOTP secret stored")
    try:
        return _fernet().decrypt(user.totp_secret_encrypted).decode()
    except InvalidToken as exc:
        raise KeyManagementError("could not decrypt TOTP secret -- wrong ACCOUNTS_TOTP_ENCRYPTION_KEY?") from exc
