"""HD-derived BTC hot wallet keys, BIP-44 path m/44'/{coin_type}'/0'/0/{index}
(coin_type 0 on mainnet, 1 on testnet/regtest) -- the same mnemonic used by
evm_wallet.py derives both chains' keys, per key_management.py's single-seed
dev storage.
"""

from bitcoinutils.hdwallet import HDWallet
from bitcoinutils.setup import setup as _bitcoinutils_setup

_COIN_TYPE_BY_NETWORK = {"mainnet": "0", "testnet": "1", "regtest": "1"}


def configure_network(network: str) -> None:
    """bitcoinutils' setup() is process-global -- call this once at app
    startup (see app/__init__.py) with BTC_NETWORK before deriving/signing
    anything, or address encoding will use the wrong version bytes."""
    _bitcoinutils_setup("testnet" if network in ("testnet", "regtest") else "mainnet")


def _derivation_path(network: str, index: int) -> str:
    coin_type = _COIN_TYPE_BY_NETWORK.get(network, "0")
    return f"m/44'/{coin_type}'/0'/0/{index}"


def derive_private_key(mnemonic: str, network: str, index: int = 0):
    hd = HDWallet.from_mnemonic(mnemonic)
    hd.from_path(_derivation_path(network, index))
    return hd.get_private_key()


def derive_address(mnemonic: str, network: str, index: int = 0) -> str:
    return derive_private_key(mnemonic, network, index).get_public_key().get_address().to_string()
