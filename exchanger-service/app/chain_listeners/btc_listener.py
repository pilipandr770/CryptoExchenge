"""Polls a Blockstream-Esplora-compatible REST API (public
blockstream.info/api by default, see BTC_ESPLORA_API_BASE_URL) for confirmed
UTXOs on open DepositAddress rows. ТЗ section 7: N=1 confirmation is a
dev-demo compromise (BTC_MIN_CONFIRMATIONS default); raise to 2-6 for
anything resembling production.
"""

from decimal import Decimal

import requests

from app.accounts.balances import credit_user, is_tx_already_credited
from app.custody.models import DepositAddress
from app.extensions import db
from app.ledger.models import LedgerEntry
from app.swap import orchestrator
from app.swap.models import SwapOrder
from app.swap.states import DEPOSIT_PENDING


class BtcListenerError(Exception):
    pass


class BtcListener:
    def __init__(self, esplora_base_url: str, min_confirmations: int, request_timeout_seconds: float = 10):
        self.esplora_base_url = esplora_base_url.rstrip("/")
        self.min_confirmations = min_confirmations
        self.request_timeout_seconds = request_timeout_seconds

    def _get(self, path: str):
        try:
            response = requests.get(f"{self.esplora_base_url}{path}", timeout=self.request_timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise BtcListenerError(f"Esplora GET {path} failed: {exc}") from exc
        return response.json()

    def tip_height(self) -> int:
        return int(self._get("/blocks/tip/height"))

    def utxos_for_address(self, address: str) -> list:
        return self._get(f"/address/{address}/utxo")

    def poll(self) -> list:
        """Checks every open DEPOSIT_PENDING BTC order's deposit address for
        a confirmed UTXO and confirms it via orchestrator.confirm_deposit.
        Returns the SwapOrders confirmed this call."""
        pending_orders = (
            SwapOrder.query
            .join(DepositAddress, SwapOrder.deposit_address_id == DepositAddress.id)
            .filter(SwapOrder.status == DEPOSIT_PENDING, DepositAddress.chain == "bitcoin")
            .all()
        )
        if not pending_orders:
            return []

        tip = self.tip_height()
        confirmed = []

        for order in pending_orders:
            for utxo in self.utxos_for_address(order.deposit_address.address):
                status = utxo.get("status", {})
                if not status.get("confirmed"):
                    continue
                confirmations = tip - status["block_height"] + 1
                if confirmations < self.min_confirmations:
                    continue

                orchestrator.confirm_deposit(order, utxo["txid"])
                confirmed.append(order)
                break  # one deposit per order for MVP

        return confirmed

    def poll_user_deposits(self) -> list:
        """Checks every registered user's persistent BTC deposit address
        (see app/accounts/deposits.py) for confirmed UTXOs and credits
        their balance directly -- no SwapOrder involved. Unlike poll()'s
        order-scoped addresses (good for exactly one deposit), a user's
        address is reused indefinitely, so each UTXO is checked against
        is_tx_already_credited() before crediting. Returns a list of
        (user_id, "BTC", amount, txid) tuples credited this call."""
        user_addresses = (
            DepositAddress.query
            .filter(DepositAddress.user_id.isnot(None), DepositAddress.chain == "bitcoin")
            .all()
        )
        if not user_addresses:
            return []

        tip = self.tip_height()
        credited = []

        for deposit_address in user_addresses:
            for utxo in self.utxos_for_address(deposit_address.address):
                status = utxo.get("status", {})
                if not status.get("confirmed"):
                    continue
                confirmations = tip - status["block_height"] + 1
                if confirmations < self.min_confirmations:
                    continue

                tx_hash = utxo["txid"]
                if is_tx_already_credited(tx_hash):
                    continue

                amount = Decimal(utxo["value"]) / Decimal(10 ** 8)
                db.session.add(LedgerEntry(
                    account="treasury:bitcoin:BTC", asset="BTC",
                    amount=amount, entry_type="deposit", tx_hash=tx_hash,
                ))
                credit_user(deposit_address.user_id, "BTC", amount, entry_type="deposit", tx_hash=tx_hash)
                credited.append((deposit_address.user_id, "BTC", amount, tx_hash))

        db.session.commit()
        return credited
