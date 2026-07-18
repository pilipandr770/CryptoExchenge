import pytest

from app.custody import btc_wallet, evm_wallet, key_management

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


# --- key_management: Fernet roundtrip -----------------------------------


def test_save_and_load_mnemonic_roundtrip(tmp_path):
    keys_file = str(tmp_path / "keys.enc.json")
    fernet_key = key_management.generate_fernet_key()

    key_management.save_mnemonic(keys_file, fernet_key, TEST_MNEMONIC)
    loaded = key_management.load_mnemonic(keys_file, fernet_key)

    assert loaded == TEST_MNEMONIC


def test_load_mnemonic_missing_file_raises(tmp_path):
    with pytest.raises(key_management.KeyManagementError):
        key_management.load_mnemonic(str(tmp_path / "does-not-exist.json"), key_management.generate_fernet_key())


def test_load_mnemonic_wrong_key_raises(tmp_path):
    keys_file = str(tmp_path / "keys.enc.json")
    key_management.save_mnemonic(keys_file, key_management.generate_fernet_key(), TEST_MNEMONIC)

    with pytest.raises(key_management.KeyManagementError):
        key_management.load_mnemonic(keys_file, key_management.generate_fernet_key())


def test_generate_mnemonic_produces_usable_seed():
    mnemonic = key_management.generate_mnemonic()
    address = evm_wallet.derive_address(mnemonic, index=0)
    assert address.startswith("0x")
    assert len(address) == 42


# --- evm_wallet: deterministic HD derivation -----------------------------


def test_evm_derive_address_matches_known_test_vector():
    # Standard BIP-39 test mnemonic, m/44'/60'/0'/0/0 -- widely-cited vector.
    address = evm_wallet.derive_address(TEST_MNEMONIC, index=0)
    assert address == "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"


def test_evm_derive_different_indexes_give_different_addresses():
    assert evm_wallet.derive_address(TEST_MNEMONIC, 0) != evm_wallet.derive_address(TEST_MNEMONIC, 1)


def test_evm_derive_private_key_signs_as_derived_address():
    from eth_account import Account
    from eth_account.messages import encode_defunct

    address = evm_wallet.derive_address(TEST_MNEMONIC, 0)
    private_key = evm_wallet.derive_private_key(TEST_MNEMONIC, 0)

    signable = encode_defunct(text="hello")
    signed = Account.sign_message(signable, private_key=private_key)
    recovered = Account.recover_message(signable, signature=signed.signature)
    assert recovered == address


# --- btc_wallet: deterministic HD derivation ------------------------------


def test_btc_derive_address_matches_known_test_vector():
    btc_wallet.configure_network("mainnet")
    address = btc_wallet.derive_address(TEST_MNEMONIC, network="mainnet", index=0)
    assert address == "1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA"


def test_btc_derive_different_indexes_give_different_addresses():
    btc_wallet.configure_network("mainnet")
    a0 = btc_wallet.derive_address(TEST_MNEMONIC, "mainnet", 0)
    a1 = btc_wallet.derive_address(TEST_MNEMONIC, "mainnet", 1)
    assert a0 != a1
