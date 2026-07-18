import pytest

from app.accounts import auth
from app.extensions import db


@pytest.fixture
def auth_client(client):
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})
    return client


def _user(email="user@example.com", is_active=True):
    user = auth.register_user(email, "pw", "Jane Doe", date_of_birth="1990-01-01", country="DE")
    user.screening_decision = "accepted" if is_active else "review"
    user.is_active = is_active
    db.session.commit()
    return user


def test_users_list_requires_login(client):
    resp = client.get("/admin/users")
    assert resp.status_code == 302


def test_users_list_renders(auth_client, app):
    user = _user()
    resp = auth_client.get("/admin/users")
    assert resp.status_code == 200
    assert user.email.encode() in resp.data


def test_user_detail_renders(auth_client, app):
    user = _user()
    resp = auth_client.get(f"/admin/users/{user.id}")
    assert resp.status_code == 200
    assert b"Jane Doe" in resp.data


def test_user_detail_404_redirects_for_unknown_user(auth_client):
    resp = auth_client.get("/admin/users/99999")
    assert resp.status_code == 302


def test_user_detail_shows_approve_action_when_inactive(auth_client, app):
    user = _user(is_active=False)
    resp = auth_client.get(f"/admin/users/{user.id}")
    assert b"Approve account" in resp.data


def test_user_detail_shows_freeze_action_when_active(auth_client, app):
    user = _user(is_active=True)
    resp = auth_client.get(f"/admin/users/{user.id}")
    assert b"Freeze account" in resp.data


def test_approve_user_activates_account(auth_client, app):
    user = _user(is_active=False)
    resp = auth_client.post(f"/admin/users/{user.id}/approve", follow_redirects=True)
    assert resp.status_code == 200

    from app.accounts.models import User
    refreshed = db.session.get(User, user.id)
    assert refreshed.is_active is True


def test_freeze_user_deactivates_account(auth_client, app):
    user = _user(is_active=True)
    resp = auth_client.post(f"/admin/users/{user.id}/freeze", follow_redirects=True)
    assert resp.status_code == 200

    from app.accounts.models import User
    refreshed = db.session.get(User, user.id)
    assert refreshed.is_active is False


def test_approve_user_writes_audit_log(auth_client, app):
    user = _user(is_active=False)
    auth_client.post(f"/admin/users/{user.id}/approve")

    from app.audit.models import AuditLog
    entry = AuditLog.query.filter_by(target_type="User", target_id=str(user.id)).first()
    assert entry is not None
    assert entry.action == "user_approved"
