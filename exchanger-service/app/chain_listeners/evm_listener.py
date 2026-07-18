"""Polls the EVM chain for confirmed native-asset deposits to open
DepositAddress rows. Native-asset (ETH/MATIC) deposits only for MVP --
ERC-20 deposit detection would need per-token eth_getLogs Transfer-event
scanning, out of scope for this ETH/WBTC-centric MVP (ТЗ section 2).

ТЗ section 7: N confirmations configurable (12 default on mainnet, fewer
for L2/testnet demo runs -- see EVM_MIN_CONFIRMATIONS).
"""

import requests

from app.custody.models import DepositAddress
from app.swap import orchestrator
from app.swap.models import SwapOrder
from app.swap.states import DEPOSIT_PENDING


class EvmListenerError(Exception):
    pass


class EvmListener:
    def __init__(self, rpc_url: str, min_confirmations: int, request_timeout_seconds: float = 10):
        self.rpc_url = rpc_url
        self.min_confirmations = min_confirmations
        self.request_timeout_seconds = request_timeout_seconds

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
            raise EvmListenerError(f"EVM RPC call {method} failed: {exc}") from exc
        except ValueError as exc:
            raise EvmListenerError(f"EVM RPC call {method} returned non-json response") from exc

        if "error" in data:
            raise EvmListenerError(f"EVM RPC error on {method}: {data['error']}")
        return data["result"]

    def latest_block_number(self) -> int:
        return int(self._rpc("eth_blockNumber", []), 16)

    def get_block(self, block_number: int) -> dict:
        return self._rpc("eth_getBlockByNumber", [hex(block_number), True])

    def scan_range(self, from_block: int, to_block: int, chain: str = "ethereum") -> list:
        """Scans [from_block, to_block] for native transfers into any open
        DEPOSIT_PENDING order's deposit address on `chain`. Confirms (via
        orchestrator.confirm_deposit) any deposit that has reached
        min_confirmations as of the current chain head. Returns the
        SwapOrders confirmed this call."""
        pending_orders = (
            SwapOrder.query
            .join(DepositAddress, SwapOrder.deposit_address_id == DepositAddress.id)
            .filter(SwapOrder.status == DEPOSIT_PENDING, DepositAddress.chain == chain)
            .all()
        )
        if not pending_orders:
            return []

        by_address = {order.deposit_address.address.lower(): order for order in pending_orders}
        head = self.latest_block_number()
        confirmed = []

        for block_number in range(from_block, to_block + 1):
            block = self.get_block(block_number)
            if block is None:
                continue

            confirmations = head - int(block["number"], 16) + 1
            if confirmations < self.min_confirmations:
                continue

            for tx in block.get("transactions", []):
                to_address = (tx.get("to") or "").lower()
                order = by_address.get(to_address)
                if order is None:
                    continue
                if int(tx["value"], 16) <= 0:
                    continue

                orchestrator.confirm_deposit(order, tx["hash"])
                confirmed.append(order)
                del by_address[to_address]  # one deposit per order for MVP

        return confirmed
