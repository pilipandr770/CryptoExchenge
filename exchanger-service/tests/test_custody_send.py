import pytest
from eth_account import Account

from app.custody import btc_wallet, send
from app.custody.send import SendError, select_utxos

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


class _FakeResponse:
    def __init__(self, json_payload=None, text=""):
        self._json_payload = json_payload
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_payload


# --- EVM ------------------------------------------------------------------


def test_send_evm_native_builds_and_broadcasts_transaction(monkeypatch):
    sender = Account.create()
    calls = []

    def _fake_post(url, json=None, timeout=None):
        method = json["method"]
        calls.append(method)
        if method == "eth_getTransactionCount":
            return _FakeResponse({"result": "0x2"})
        if method == "eth_gasPrice":
            return _FakeResponse({"result": "0x3b9aca00"})
        if method == "eth_sendRawTransaction":
            return _FakeResponse({"result": "0xwithdrawaltx"})
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr("app.custody.send.requests.post", _fake_post)

    tx_hash = send.send_evm_native(
        rpc_url="https://rpc.test",
        sender_private_key=sender.key.hex(),
        chain_id=1,
        to_address="0x668bb685f8e3891e11Ae5aca9012C59326A87fa0",
        amount_wei=10**18,
    )

    assert tx_hash == "0xwithdrawaltx"
    assert calls == ["eth_getTransactionCount", "eth_gasPrice", "eth_sendRawTransaction"]


def test_send_evm_native_raises_on_rpc_error(monkeypatch):
    sender = Account.create()

    def _fake_post(url, json=None, timeout=None):
        return _FakeResponse({"error": {"message": "insufficient funds"}})

    monkeypatch.setattr("app.custody.send.requests.post", _fake_post)

    with pytest.raises(SendError):
        send.send_evm_native("https://rpc.test", sender.key.hex(), 1, "0x668bb685f8e3891e11Ae5aca9012C59326A87fa0", 10**18)


# --- select_utxos -----------------------------------------------------------


def test_select_utxos_greedy_largest_first():
    utxos = [
        {"txid": "a", "vout": 0, "value": 1000, "status": {"confirmed": True}},
        {"txid": "b", "vout": 0, "value": 5000, "status": {"confirmed": True}},
        {"txid": "c", "vout": 0, "value": 2000, "status": {"confirmed": True}},
    ]
    selected, total = select_utxos(utxos, target_sats=6000)
    assert [u["txid"] for u in selected] == ["b", "c"]
    assert total == 7000


def test_select_utxos_ignores_unconfirmed():
    utxos = [
        {"txid": "a", "vout": 0, "value": 100000, "status": {"confirmed": False}},
        {"txid": "b", "vout": 0, "value": 5000, "status": {"confirmed": True}},
    ]
    with pytest.raises(SendError):
        select_utxos(utxos, target_sats=6000)


def test_select_utxos_raises_when_insufficient():
    utxos = [{"txid": "a", "vout": 0, "value": 1000, "status": {"confirmed": True}}]
    with pytest.raises(SendError):
        select_utxos(utxos, target_sats=5000)


# --- BTC send ---------------------------------------------------------------


def test_send_btc_builds_signs_and_broadcasts(monkeypatch):
    btc_wallet.configure_network("mainnet")
    private_key = btc_wallet.derive_private_key(TEST_MNEMONIC, "mainnet", 0)
    from_address = private_key.get_public_key().get_address().to_string()
    destination = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"  # well-known P2PKH address (genesis donation addr)

    broadcast_calls = []

    def _fake_get(url, timeout=None):
        assert url.endswith(f"/address/{from_address}/utxo")
        return _FakeResponse([
            {"txid": "ab" * 32, "vout": 0, "value": 100000, "status": {"confirmed": True}},
        ])

    def _fake_post(url, data=None, timeout=None):
        assert url.endswith("/tx")
        broadcast_calls.append(data)
        return _FakeResponse(text="deadbeef" * 8)

    monkeypatch.setattr("app.custody.send.requests.get", _fake_get)
    monkeypatch.setattr("app.custody.send.requests.post", _fake_post)

    wif = private_key.to_wif()
    txid = send.send_btc(
        esplora_base_url="https://esplora.test/api",
        sender_wif=wif,
        to_address=destination,
        amount_sats=50000,
        fee_sats=2000,
    )

    assert txid == "deadbeef" * 8
    assert len(broadcast_calls) == 1
    raw_hex = broadcast_calls[0]
    assert isinstance(raw_hex, str) and len(raw_hex) > 0
    # Round-trip: a syntactically valid, self-consistent signed transaction.
    from bitcoinutils.transactions import Transaction
    parsed = Transaction.from_raw(raw_hex)
    assert len(parsed.inputs) == 1
    assert len(parsed.outputs) == 2  # destination + change


def test_send_btc_rejects_non_p2pkh_destination(monkeypatch):
    btc_wallet.configure_network("mainnet")
    private_key = btc_wallet.derive_private_key(TEST_MNEMONIC, "mainnet", 0)

    with pytest.raises(SendError):
        send.send_btc(
            esplora_base_url="https://esplora.test/api",
            sender_wif=private_key.to_wif(),
            to_address="bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",  # bech32
            amount_sats=1000,
        )


def test_send_btc_raises_when_insufficient_utxos(monkeypatch):
    btc_wallet.configure_network("mainnet")
    private_key = btc_wallet.derive_private_key(TEST_MNEMONIC, "mainnet", 0)
    from_address = private_key.get_public_key().get_address().to_string()

    def _fake_get(url, timeout=None):
        return _FakeResponse([{"txid": "ab" * 32, "vout": 0, "value": 100, "status": {"confirmed": True}}])

    monkeypatch.setattr("app.custody.send.requests.get", _fake_get)

    with pytest.raises(SendError):
        send.send_btc(
            esplora_base_url="https://esplora.test/api",
            sender_wif=private_key.to_wif(),
            to_address="1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
            amount_sats=50000,
        )
