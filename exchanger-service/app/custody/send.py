"""Sends withdrawals on-chain from the custody hot wallet -- the last, most
sensitive step of the pipeline. EVM: raw JSON-RPC signed by eth_account
(same pattern as evm_dex_adapter.py / AML-18's wallet_ownership adapters).
BTC: bitcoinutils builds and signs a standard P2PKH spend, broadcast via
the same Esplora API the chain listener polls.

Real fund movement. This code has been verified by build/sign/serialize
round-trip only in this environment (no live node available) -- RUNBOOK.md
requires a testnet/regtest dry run before it is ever pointed at mainnet.
Destination addresses are P2PKH-only for MVP scope, matching the address
type this service's own HD wallet derives (app/custody/btc_wallet.py);
sending to a bech32/P2WPKH/P2TR destination raises SendError rather than
silently mishandling it.
"""

import requests
from bitcoinutils.keys import P2pkhAddress, PrivateKey
from bitcoinutils.script import Script
from bitcoinutils.transactions import Transaction, TxInput, TxOutput
from eth_account import Account


class SendError(Exception):
    pass


# --- EVM --------------------------------------------------------------


def send_evm_native(
    rpc_url: str,
    sender_private_key: str,
    chain_id: int,
    to_address: str,
    amount_wei: int,
    request_timeout_seconds: float = 10,
) -> str:
    def _rpc(method: str, params: list):
        try:
            response = requests.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=request_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise SendError(f"EVM RPC call {method} failed: {exc}") from exc
        except ValueError as exc:
            raise SendError(f"EVM RPC call {method} returned non-json response") from exc

        if "error" in data:
            raise SendError(f"EVM RPC error on {method}: {data['error']}")
        return data["result"]

    sender = Account.from_key(sender_private_key)
    nonce_hex = _rpc("eth_getTransactionCount", [sender.address, "pending"])
    gas_price_hex = _rpc("eth_gasPrice", [])

    tx = {
        "nonce": int(nonce_hex, 16),
        "to": to_address,
        "value": amount_wei,
        "gas": 21000,
        "gasPrice": int(gas_price_hex, 16),
        "chainId": chain_id,
    }
    signed = Account.sign_transaction(tx, sender_private_key)
    return _rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])


# --- BTC --------------------------------------------------------------


def _esplora_get(base_url: str, path: str, timeout: float):
    try:
        response = requests.get(f"{base_url}{path}", timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SendError(f"Esplora GET {path} failed: {exc}") from exc
    return response.json()


def _esplora_broadcast(base_url: str, raw_tx_hex: str, timeout: float) -> str:
    try:
        response = requests.post(f"{base_url}/tx", data=raw_tx_hex, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SendError(f"Esplora POST /tx failed: {exc}") from exc
    return response.text.strip()  # Esplora's broadcast response body is the raw txid


def select_utxos(utxos: list, target_sats: int) -> tuple:
    """Greedy largest-first selection over confirmed UTXOs only -- simplest
    correct strategy for a low-volume personal-funds demo, not
    fee-optimized. Returns (selected_utxos, total_sats)."""
    confirmed = [u for u in utxos if u.get("status", {}).get("confirmed")]
    confirmed.sort(key=lambda u: u["value"], reverse=True)

    selected = []
    total = 0
    for utxo in confirmed:
        selected.append(utxo)
        total += utxo["value"]
        if total >= target_sats:
            return selected, total

    raise SendError(f"insufficient confirmed UTXOs: need {target_sats} sats, have {total} sats")


def send_btc(
    esplora_base_url: str,
    sender_wif: str,
    to_address: str,
    amount_sats: int,
    fee_sats: int = 2000,
    request_timeout_seconds: float = 10,
) -> str:
    esplora_base_url = esplora_base_url.rstrip("/")
    private_key = PrivateKey(sender_wif)
    from_address = private_key.get_public_key().get_address()

    try:
        to_addr = P2pkhAddress(to_address)
    except ValueError as exc:
        raise SendError(f"unsupported BTC destination address {to_address!r} (P2PKH only in this MVP): {exc}") from exc

    utxos = _esplora_get(esplora_base_url, f"/address/{from_address.to_string()}/utxo", request_timeout_seconds)
    selected, total_in = select_utxos(utxos, amount_sats + fee_sats)
    change_sats = total_in - amount_sats - fee_sats

    tx_inputs = [TxInput(utxo["txid"], utxo["vout"]) for utxo in selected]
    tx_outputs = [TxOutput(amount_sats, to_addr.to_script_pub_key())]
    if change_sats > 0:
        tx_outputs.append(TxOutput(change_sats, from_address.to_script_pub_key()))

    tx = Transaction(tx_inputs, tx_outputs)

    script_pubkey = from_address.to_script_pub_key()
    pubkey_hex = private_key.get_public_key().to_hex()
    for index, txin in enumerate(tx_inputs):
        signature = private_key.sign_input(tx, index, script_pubkey)
        txin.script_sig = Script([signature, pubkey_hex])

    return _esplora_broadcast(esplora_base_url, tx.serialize(), request_timeout_seconds)
