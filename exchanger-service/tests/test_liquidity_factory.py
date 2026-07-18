from decimal import Decimal

from app.liquidity.evm_dex_adapter import EvmDexAdapter
from app.liquidity.factory import liquidity_adapter_for


def test_liquidity_adapter_for_does_not_require_hot_wallet_for_quotes(app, monkeypatch):
    """Regression test: liquidity_adapter_for() used to eagerly call
    load_hot_wallet_mnemonic(), which raises KeyManagementError whenever
    the hot wallet isn't configured yet (HOT_WALLET_KEYS_FILE=""/
    HOT_WALLET_KEYS_FERNET_KEY="" -- the case in TestConfig and in a fresh
    deployment before RUNBOOK.md step 4). That crashed the public quote
    preview (app/public_ui) with an unhandled 500, even though get_quote()
    never touches the signing key -- only execute_swap() does."""
    adapter = liquidity_adapter_for("ETH")
    assert isinstance(adapter, EvmDexAdapter)
    assert adapter.sender_private_key == ""

    def _fake_get(url, params=None, headers=None, timeout=None):
        class _Resp:
            status_code = 200

            def json(self):
                return {"buyAmount": str(2500 * 10**6)}

        return _Resp()

    monkeypatch.setattr("app.liquidity.evm_dex_adapter.requests.get", _fake_get)
    quote = adapter.get_quote("ETH", "USDC", Decimal("1"))
    assert quote.to_amount == Decimal("2500")


def test_liquidity_adapter_for_btc_also_tolerates_missing_hot_wallet(app):
    adapter = liquidity_adapter_for("BTC")
    assert adapter._evm_dex_adapter.sender_private_key == ""
