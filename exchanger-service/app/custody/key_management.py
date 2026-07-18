"""Dev-only hot wallet key storage: a single BIP-39 mnemonic, Fernet-
encrypted at rest, used to derive both the EVM and BTC hot wallet keys via
HD paths (see evm_wallet.py / btc_wallet.py).

TODO(production): replace with HSM/MPC custody before any third-party
client funds touch this service -- a single encrypted file on the same host
that runs the app is not an acceptable key-management story for anything
beyond the developer's own minimal personal-funds demo (ТЗ section 8).
"""

import json
import os

from cryptography.fernet import Fernet, InvalidToken


class KeyManagementError(Exception):
    pass


def generate_fernet_key() -> str:
    return Fernet.generate_key().decode()


def generate_mnemonic() -> str:
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    _, mnemonic = Account.create_with_mnemonic()
    return mnemonic


def _fernet(fernet_key: str) -> Fernet:
    if not fernet_key:
        raise KeyManagementError("HOT_WALLET_KEYS_FERNET_KEY is not set")
    try:
        return Fernet(fernet_key.encode())
    except (ValueError, TypeError) as exc:
        raise KeyManagementError(f"invalid HOT_WALLET_KEYS_FERNET_KEY: {exc}") from exc


def save_mnemonic(keys_file: str, fernet_key: str, mnemonic: str) -> None:
    if not keys_file:
        raise KeyManagementError("HOT_WALLET_KEYS_FILE is not set")
    fernet = _fernet(fernet_key)
    encrypted = fernet.encrypt(json.dumps({"mnemonic": mnemonic}).encode())
    directory = os.path.dirname(keys_file)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(keys_file, "wb") as f:
        f.write(encrypted)


def load_mnemonic(keys_file: str, fernet_key: str) -> str:
    if not keys_file or not os.path.exists(keys_file):
        raise KeyManagementError(f"hot wallet keys file not found: {keys_file!r}")
    fernet = _fernet(fernet_key)
    with open(keys_file, "rb") as f:
        encrypted = f.read()
    try:
        payload = fernet.decrypt(encrypted)
    except InvalidToken as exc:
        raise KeyManagementError("could not decrypt hot wallet keys file -- wrong Fernet key?") from exc
    return json.loads(payload)["mnemonic"]
