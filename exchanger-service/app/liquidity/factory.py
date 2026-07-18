"""Builds the right LiquidityAdapter for a given from_asset, from live
app.config -- the one place admin_ui, public_ui, and app/cli.py all go for
this, so the EvmDexAdapter/BtcTreasuryAdapter wiring lives in exactly one
spot instead of being duplicated per caller.
"""

from flask import current_app

from app.custody import evm_wallet
from app.custody.key_management import KeyManagementError, load_mnemonic
from app.ledger import reconciliation
from app.liquidity.btc_treasury_adapter import BtcTreasuryAdapter
from app.liquidity.evm_dex_adapter import EvmDexAdapter


def load_hot_wallet_mnemonic() -> str:
    cfg = current_app.config
    return load_mnemonic(cfg["HOT_WALLET_KEYS_FILE"], cfg["HOT_WALLET_KEYS_FERNET_KEY"])


def liquidity_adapter_for(from_asset: str):
    cfg = current_app.config

    try:
        sender_private_key = evm_wallet.derive_private_key(load_hot_wallet_mnemonic(), 0)
    except KeyManagementError:
        # get_quote() never needs a signing key -- only execute_swap() does,
        # and EvmDexAdapter._ensure_signing_configured() already raises its
        # own clear error there if this is blank. A public quote preview
        # (app/public_ui) must work even before the hot wallet is set up.
        sender_private_key = ""

    evm_adapter = EvmDexAdapter(
        api_base_url=cfg["ZEROX_API_BASE_URL"],
        rpc_url=cfg["EVM_RPC_URL"],
        sender_private_key=sender_private_key,
        chain_id=cfg["EVM_CHAIN_ID"],
        api_key=cfg["ZEROX_API_KEY"],
        quote_ttl_seconds=cfg["SWAP_QUOTE_TTL_SECONDS"],
        request_timeout_seconds=cfg["ZEROX_REQUEST_TIMEOUT_SECONDS"],
    )
    if from_asset.upper() == "BTC":
        return BtcTreasuryAdapter(
            evm_adapter,
            treasury_wbtc_balance=lambda: reconciliation.treasury_ledger_balance("ethereum", "WBTC"),
        )
    return evm_adapter
