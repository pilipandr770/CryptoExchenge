from app.accounts import auth
from app.accounts.deposits import get_or_create_deposit_address
from app.custody.key_management import KeyManagementError
from app.custody.models import DepositAddress
from app.extensions import db

import pytest

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


def _user(email="user@example.com"):
    user = auth.register_user(email, "pw", "Jane Doe")
    db.session.commit()
    return user


def test_get_or_create_deposit_address_derives_and_persists(app, monkeypatch):
    monkeypatch.setattr("app.accounts.deposits.load_hot_wallet_mnemonic", lambda: TEST_MNEMONIC)
    user = _user()

    address = get_or_create_deposit_address(user, "ethereum")

    assert address.user_id == user.id
    assert address.chain == "ethereum"
    assert address.address == "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    assert address.derivation_index == 0


def test_get_or_create_deposit_address_is_idempotent(app, monkeypatch):
    monkeypatch.setattr("app.accounts.deposits.load_hot_wallet_mnemonic", lambda: TEST_MNEMONIC)
    user = _user()

    first = get_or_create_deposit_address(user, "ethereum")
    second = get_or_create_deposit_address(user, "ethereum")

    assert first.id == second.id
    assert DepositAddress.query.filter_by(user_id=user.id, chain="ethereum").count() == 1


def test_get_or_create_deposit_address_different_users_get_different_indexes(app, monkeypatch):
    monkeypatch.setattr("app.accounts.deposits.load_hot_wallet_mnemonic", lambda: TEST_MNEMONIC)
    user_a = _user("a@example.com")
    user_b = _user("b@example.com")

    address_a = get_or_create_deposit_address(user_a, "ethereum")
    address_b = get_or_create_deposit_address(user_b, "ethereum")

    assert address_a.address != address_b.address
    assert address_a.derivation_index != address_b.derivation_index


def test_get_or_create_deposit_address_bitcoin_uses_configured_network(app, monkeypatch):
    monkeypatch.setattr("app.accounts.deposits.load_hot_wallet_mnemonic", lambda: TEST_MNEMONIC)
    from app.custody import btc_wallet
    btc_wallet.configure_network("mainnet")
    user = _user()

    address = get_or_create_deposit_address(user, "bitcoin")
    assert address.chain == "bitcoin"
    assert address.address.startswith("1")  # mainnet P2PKH


def test_get_or_create_deposit_address_surfaces_key_management_error(app, monkeypatch):
    def _raise():
        raise KeyManagementError("hot wallet not configured")

    monkeypatch.setattr("app.accounts.deposits.load_hot_wallet_mnemonic", _raise)
    user = _user()

    with pytest.raises(KeyManagementError):
        get_or_create_deposit_address(user, "ethereum")
