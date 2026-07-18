"""Instant, synchronous internal transfers between two registered users --
no blockchain step, no state machine: a debit + credit ledger pair (see
app/accounts/balances.py) plus a Transfer record for history.
"""

from decimal import Decimal

from app.accounts.balances import credit_user, debit_user
from app.accounts.models import Transfer, User
from app.extensions import db


class RecipientNotFoundError(Exception):
    pass


class SameAccountTransferError(Exception):
    pass


def create_transfer(sender: User, recipient_email: str, asset: str, amount: Decimal) -> Transfer:
    recipient = User.query.filter_by(email=recipient_email.strip().lower()).first()
    if recipient is None:
        raise RecipientNotFoundError(f"no account with email {recipient_email!r}")
    if recipient.id == sender.id:
        raise SameAccountTransferError("cannot transfer to your own account")

    # debit_user raises InsufficientBalanceError (and writes nothing) before
    # any ledger row is touched, so a failed transfer never partially applies.
    debit_user(sender.id, asset, amount, entry_type="transfer_out")
    credit_user(recipient.id, asset, amount, entry_type="transfer_in")

    transfer = Transfer(sender_id=sender.id, recipient_id=recipient.id, asset=asset, amount=amount)
    db.session.add(transfer)
    db.session.flush()
    return transfer
