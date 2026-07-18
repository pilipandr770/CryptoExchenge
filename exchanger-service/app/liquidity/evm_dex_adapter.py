"""Wraps the 0x Swap API v2 AllowanceHolder endpoint
(https://api.0x.org/swap/allowance-holder/quote). Picked over 1inch for MVP
per ТЗ section 6: it's a plain REST quote/swap endpoint that needs no
bespoke smart contract of our own, and AllowanceHolder (vs Permit2) needs
no EIP-712 signing step -- just sign and send the returned transaction,
same shape as v1 used to work. execute_swap signs and submits via raw
JSON-RPC -- mirrors app.wallet_ownership.adapters.EVMTestTransferAdapter in
the AML-18 compliance-service rather than pulling in the much heavier
web3.py.

v1 -> v2 migration note: 0x retired the unauthenticated v1 API. v2 requires
an `0x-api-key` header on every call (get one at 0x.org) plus an
`0x-version: v2` header, and nests the executable transaction fields
(`to`/`data`/`value`/`gas`/`gasPrice`) under a `transaction` object in the
response instead of top-level. This was corrected based on live testing
against the real API during development (see RUNBOOK.md) -- verify against
a real ZEROX_API_KEY before relying on it; the exact field names for
ERC-20 `allowanceTarget` in particular were not independently confirmed
against official docs at the time this was written.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests
from eth_account import Account

from app.liquidity.base import LiquidityAdapter, LiquidityAdapterError, Quote, SwapExecutionResult

# Base-unit decimals for assets this adapter knows how to size sellAmount/
# buyAmount for. An unknown asset fails loudly rather than silently
# mis-scaling an amount by the wrong power of ten -- extend as new assets
# are onboarded.
ASSET_DECIMALS = {
    "ETH": 18,
    "WETH": 18,
    "WBTC": 8,
    "USDC": 6,
    "USDT": 6,
    "DAI": 18,
}

_ERC20_APPROVE_SELECTOR = "095ea7b3"


def _utcnow():
    return datetime.now(timezone.utc)


def _encode_erc20_approve(spender: str, amount: int) -> str:
    spender_padded = spender.lower().replace("0x", "").rjust(64, "0")
    amount_padded = format(amount, "x").rjust(64, "0")
    return "0x" + _ERC20_APPROVE_SELECTOR + spender_padded + amount_padded


class EvmDexAdapter(LiquidityAdapter):
    def __init__(
        self,
        api_base_url: str,
        rpc_url: str = "",
        sender_private_key: str = "",
        chain_id: int = 1,
        api_key: str = "",
        quote_ttl_seconds: int = 30,
        request_timeout_seconds: float = 10,
    ):
        self.api_base_url = api_base_url.rstrip("/")
        self.rpc_url = rpc_url
        self.sender_private_key = sender_private_key
        self.chain_id = chain_id
        self.api_key = api_key
        self.quote_ttl_seconds = quote_ttl_seconds
        self.request_timeout_seconds = request_timeout_seconds

    def _decimals(self, asset: str) -> int:
        try:
            return ASSET_DECIMALS[asset.upper()]
        except KeyError:
            raise LiquidityAdapterError(f"unknown asset decimals for {asset!r}") from None

    def get_quote(self, from_asset: str, to_asset: str, amount: Decimal) -> Quote:
        sell_amount = int(amount * (10 ** self._decimals(from_asset)))
        params = {
            "sellToken": from_asset,
            "buyToken": to_asset,
            "sellAmount": str(sell_amount),
            "chainId": self.chain_id,
        }
        # `taker` is needed for a fully executable quote (calldata sized to
        # the actual sender). Omitted when no signing key is configured --
        # a read-only quote preview (app/public_ui) must still work before
        # the hot wallet is set up; see app/liquidity/factory.py.
        if self.sender_private_key:
            params["taker"] = Account.from_key(self.sender_private_key).address

        headers = {"0x-api-key": self.api_key, "0x-version": "v2"}

        try:
            response = requests.get(
                f"{self.api_base_url}/swap/allowance-holder/quote",
                params=params,
                headers=headers,
                timeout=self.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise LiquidityAdapterError(f"0x quote request failed: {exc}") from exc

        if response.status_code != 200:
            raise LiquidityAdapterError(f"0x quote failed ({response.status_code}): {response.text}")

        data = response.json()
        buy_amount = Decimal(data["buyAmount"]) / (10 ** self._decimals(to_asset))

        return Quote(
            quote_id=uuid.uuid4().hex,
            from_asset=from_asset,
            to_asset=to_asset,
            from_amount=amount,
            to_amount=buy_amount,
            expires_at=_utcnow() + timedelta(seconds=self.quote_ttl_seconds),
            raw_provider_response=data,
        )

    def _ensure_signing_configured(self):
        if not self.rpc_url or not self.sender_private_key:
            raise LiquidityAdapterError(
                "EVM swap execution is not configured (missing RPC URL or hot wallet key)"
            )

    def _rpc(self, method: str, params: list):
        try:
            response = requests.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=self.request_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise LiquidityAdapterError(f"EVM RPC call {method} failed: {exc}") from exc
        except ValueError as exc:
            raise LiquidityAdapterError(f"EVM RPC call {method} returned non-json response") from exc

        if "error" in data:
            raise LiquidityAdapterError(f"EVM RPC error on {method}: {data['error']}")
        return data["result"]

    def _send_transaction(self, sender, to: str, data: str, value: int, gas: int) -> str:
        nonce_hex = self._rpc("eth_getTransactionCount", [sender.address, "pending"])
        gas_price_hex = self._rpc("eth_gasPrice", [])
        tx = {
            "nonce": int(nonce_hex, 16),
            "to": to,
            "data": data,
            "value": value,
            "gas": gas,
            "gasPrice": int(gas_price_hex, 16),
            "chainId": self.chain_id,
        }
        signed = Account.sign_transaction(tx, self.sender_private_key)
        return self._rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])

    def execute_swap(self, quote: Quote) -> SwapExecutionResult:
        if _utcnow() >= quote.expires_at:
            raise LiquidityAdapterError(f"quote {quote.quote_id} expired at {quote.expires_at.isoformat()}")

        self._ensure_signing_configured()
        sender = Account.from_key(self.sender_private_key)
        provider_tx = quote.raw_provider_response
        # v2 nests the executable transaction under `transaction` instead
        # of top-level (v1's shape) -- see this module's docstring.
        tx_fields = provider_tx.get("transaction", provider_tx)

        sell_token_address = provider_tx.get("sellTokenAddress") or provider_tx.get("sellToken")
        allowance_target = (
            provider_tx.get("allowanceTarget")
            or provider_tx.get("issues", {}).get("allowance", {}).get("spender")
        )
        is_native_sell = quote.from_asset.upper() == "ETH"

        if not is_native_sell and sell_token_address and allowance_target:
            # Always approve the exact sellAmount before submitting the swap
            # rather than checking on-chain allowance first -- simpler, and
            # correct regardless of prior approval state (this MVP doesn't
            # bother with infinite-approve gas savings).
            self._send_transaction(
                sender,
                to=sell_token_address,
                data=_encode_erc20_approve(allowance_target, int(provider_tx["sellAmount"])),
                value=0,
                gas=60000,
            )

        tx_hash = self._send_transaction(
            sender,
            to=tx_fields["to"],
            data=tx_fields["data"],
            value=int(tx_fields.get("value", "0")),
            gas=int(tx_fields.get("gas") or tx_fields.get("estimatedGas") or 300000),
        )

        return SwapExecutionResult(
            tx_hash=tx_hash,
            to_amount_executed=quote.to_amount,
            status="submitted",
            raw_provider_response=provider_tx,
        )

    def get_swap_status(self, tx_hash: str, min_confirmations: int) -> str:
        """Polled by the orchestrator to detect SWAP_EXECUTING -> SWAP_COMPLETE.
        Returns "pending" | "confirmed" | "failed"."""
        self._ensure_signing_configured()
        receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            return "pending"
        if receipt.get("status") != "0x1":
            return "failed"

        latest_block_hex = self._rpc("eth_blockNumber", [])
        confirmations = int(latest_block_hex, 16) - int(receipt["blockNumber"], 16) + 1
        return "confirmed" if confirmations >= min_confirmations else "pending"
