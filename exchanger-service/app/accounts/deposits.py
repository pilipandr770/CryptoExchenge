"""Persistent, one-per-(user, chain) deposit addresses for the
account-based flow -- reuses the same HD-derivation + index-allocation
logic previously duplicated in admin_ui/public_ui's create_order routes,
now the single place it lives.
"""

from flask import current_app

from app.custody import btc_wallet, evm_wallet
from app.custody.models import DepositAddress
from app.extensions import db
from app.liquidity.factory import load_hot_wallet_mnemonic


def get_or_create_deposit_address(user, chain: str) -> DepositAddress:
    existing = DepositAddress.query.filter_by(user_id=user.id, chain=chain).first()
    if existing is not None:
        return existing

    mnemonic = load_hot_wallet_mnemonic()
    existing_max_index = (
        db.session.query(db.func.max(DepositAddress.derivation_index))
        .filter_by(chain=chain)
        .scalar()
    )
    next_index = 0 if existing_max_index is None else existing_max_index + 1

    if chain == "bitcoin":
        address = btc_wallet.derive_address(mnemonic, current_app.config["BTC_NETWORK"], next_index)
    else:
        # Polygon and Ethereum share the same BIP-44 EVM derivation path.
        address = evm_wallet.derive_address(mnemonic, next_index)

    deposit_address = DepositAddress(
        chain=chain, address=address, derivation_index=next_index,
        user_id=user.id, label=f"user:{user.id}",
    )
    db.session.add(deposit_address)
    db.session.flush()
    return deposit_address
