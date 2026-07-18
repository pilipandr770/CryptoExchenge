from decimal import Decimal

import pytest

from app.accounts import auth
from app.accounts.balances import (
    InsufficientBalanceError,
    all_user_balances,
    credit_user,
    debit_user,
    user_balance,
)
from app.accounts.transfers import RecipientNotFoundError, SameAccountTransferError, create_transfer
from app.extensions import db


def _user(email="user@example.com"):
    user = auth.register_user(email, "pw", "Jane Doe")
    db.session.commit()
    return user


# --- balance primitives --------------------------------------------------


def test_user_balance_defaults_to_zero(app):
    user = _user()
    assert user_balance(user.id, "ETH") == Decimal("0")


def test_credit_then_debit_nets_correctly(app):
    user = _user()
    credit_user(user.id, "ETH", Decimal("2"), entry_type="deposit")
    db.session.commit()
    assert user_balance(user.id, "ETH") == Decimal("2")

    debit_user(user.id, "ETH", Decimal("0.5"), entry_type="withdrawal")
    db.session.commit()
    assert user_balance(user.id, "ETH") == Decimal("1.5")


def test_debit_raises_and_writes_nothing_when_insufficient(app):
    user = _user()
    credit_user(user.id, "ETH", Decimal("1"), entry_type="deposit")
    db.session.commit()

    with pytest.raises(InsufficientBalanceError) as excinfo:
        debit_user(user.id, "ETH", Decimal("5"), entry_type="withdrawal")

    assert excinfo.value.available == Decimal("1")
    assert excinfo.value.requested == Decimal("5")
    assert user_balance(user.id, "ETH") == Decimal("1")  # unchanged


def test_all_user_balances_omits_zero_and_other_users(app):
    user = _user("multi@example.com")
    other = _user("other@example.com")

    credit_user(user.id, "ETH", Decimal("2"), entry_type="deposit")
    credit_user(user.id, "USDC", Decimal("100"), entry_type="swap_in")
    credit_user(user.id, "WBTC", Decimal("1"), entry_type="deposit")
    debit_user(user.id, "WBTC", Decimal("1"), entry_type="withdrawal")  # nets to zero
    credit_user(other.id, "ETH", Decimal("999"), entry_type="deposit")
    db.session.commit()

    balances = all_user_balances(user.id)
    assert balances == {"ETH": Decimal("2"), "USDC": Decimal("100")}


def test_balances_are_isolated_per_user(app):
    user_a = _user("a@example.com")
    user_b = _user("b@example.com")

    credit_user(user_a.id, "ETH", Decimal("10"), entry_type="deposit")
    db.session.commit()

    assert user_balance(user_a.id, "ETH") == Decimal("10")
    assert user_balance(user_b.id, "ETH") == Decimal("0")


# --- transfers -----------------------------------------------------------


def test_create_transfer_moves_balance_between_users(app):
    sender = _user("sender@example.com")
    recipient = _user("recipient@example.com")
    credit_user(sender.id, "USDC", Decimal("100"), entry_type="deposit")
    db.session.commit()

    transfer = create_transfer(sender, "recipient@example.com", "USDC", Decimal("30"))
    db.session.commit()

    assert transfer.sender_id == sender.id
    assert transfer.recipient_id == recipient.id
    assert transfer.amount == Decimal("30")
    assert user_balance(sender.id, "USDC") == Decimal("70")
    assert user_balance(recipient.id, "USDC") == Decimal("30")


def test_create_transfer_recipient_lookup_is_case_insensitive(app):
    sender = _user("sender2@example.com")
    _user("Recipient2@Example.com")
    credit_user(sender.id, "USDC", Decimal("10"), entry_type="deposit")
    db.session.commit()

    transfer = create_transfer(sender, "RECIPIENT2@EXAMPLE.COM", "USDC", Decimal("5"))
    assert transfer is not None


def test_create_transfer_raises_for_unknown_recipient(app):
    sender = _user("sender3@example.com")
    credit_user(sender.id, "USDC", Decimal("10"), entry_type="deposit")
    db.session.commit()

    with pytest.raises(RecipientNotFoundError):
        create_transfer(sender, "nobody@example.com", "USDC", Decimal("5"))


def test_create_transfer_rejects_self_transfer(app):
    sender = _user("self@example.com")
    credit_user(sender.id, "USDC", Decimal("10"), entry_type="deposit")
    db.session.commit()

    with pytest.raises(SameAccountTransferError):
        create_transfer(sender, "self@example.com", "USDC", Decimal("5"))


def test_create_transfer_raises_and_leaves_balances_untouched_when_insufficient(app):
    sender = _user("poor@example.com")
    _user("rich@example.com")
    credit_user(sender.id, "USDC", Decimal("1"), entry_type="deposit")
    db.session.commit()

    with pytest.raises(InsufficientBalanceError):
        create_transfer(sender, "rich@example.com", "USDC", Decimal("100"))

    assert user_balance(sender.id, "USDC") == Decimal("1")
