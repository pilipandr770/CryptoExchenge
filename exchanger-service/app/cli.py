import time

import click


@click.command("listen-deposits")
@click.option("--once", is_flag=True, help="Run a single poll pass instead of looping forever.")
def listen_deposits_command(once):
    """Polls EVM + BTC chains for confirmed deposits and advances matching
    SwapOrders (DEPOSIT_PENDING -> DEPOSIT_CONFIRMED). Run this continuously
    alongside the web process (see RUNBOOK.md) -- deposits are never
    detected without it; nothing in the web request path polls on its own."""
    from flask import current_app

    cfg = current_app.config
    evm_listener = None
    btc_listener = None

    if cfg["EVM_RPC_URL"]:
        from app.chain_listeners.evm_listener import EvmListener
        evm_listener = EvmListener(cfg["EVM_RPC_URL"], cfg["EVM_MIN_CONFIRMATIONS"])

    if cfg["BTC_ESPLORA_API_BASE_URL"]:
        from app.chain_listeners.btc_listener import BtcListener
        btc_listener = BtcListener(cfg["BTC_ESPLORA_API_BASE_URL"], cfg["BTC_MIN_CONFIRMATIONS"])

    if evm_listener is None and btc_listener is None:
        click.echo("Neither EVM_RPC_URL nor BTC_ESPLORA_API_BASE_URL is configured -- nothing to poll.")
        return

    last_evm_block_scanned = None

    while True:
        if evm_listener is not None:
            try:
                head = evm_listener.latest_block_number()
                from_block = last_evm_block_scanned + 1 if last_evm_block_scanned is not None else max(head - 5, 0)
                for order in evm_listener.scan_range(from_block, head, chain="ethereum"):
                    click.echo(f"confirmed deposit: order={order.id} tx={order.deposit_tx_hash}")
                last_evm_block_scanned = head
            except Exception as exc:
                click.echo(f"EVM listener error: {exc}")

        if btc_listener is not None:
            try:
                for order in btc_listener.poll():
                    click.echo(f"confirmed deposit: order={order.id} tx={order.deposit_tx_hash}")
            except Exception as exc:
                click.echo(f"BTC listener error: {exc}")

        if once:
            break
        time.sleep(min(cfg["EVM_LISTENER_POLL_INTERVAL_SECONDS"], cfg["BTC_LISTENER_POLL_INTERVAL_SECONDS"]))


def register_cli(app):
    app.cli.add_command(listen_deposits_command)
