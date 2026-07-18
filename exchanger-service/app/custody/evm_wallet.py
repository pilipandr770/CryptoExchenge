"""HD-derived EVM hot wallet accounts, BIP-44 path m/44'/60'/0'/0/{index} --
the same mnemonic used by btc_wallet.py derives both chains' keys, per
key_management.py's single-seed dev storage.
"""

from eth_account import Account

Account.enable_unaudited_hdwallet_features()

_ACCOUNT_PATH_TEMPLATE = "m/44'/60'/0'/0/{index}"


def derive_account(mnemonic: str, index: int = 0):
    return Account.from_mnemonic(mnemonic, account_path=_ACCOUNT_PATH_TEMPLATE.format(index=index))


def derive_address(mnemonic: str, index: int = 0) -> str:
    return derive_account(mnemonic, index).address


def derive_private_key(mnemonic: str, index: int = 0) -> str:
    return derive_account(mnemonic, index).key.hex()
