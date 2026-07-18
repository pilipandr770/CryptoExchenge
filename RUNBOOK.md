# RUNBOOK — personal-funds MVP demo

This service moves real cryptocurrency once you point it at mainnet. It is
built for exactly one operator (you), using exactly your own funds, to
demo a working custodial exchanger pipeline. Follow this runbook in order —
testnet first, mainnet last, minimal amounts always. There is no legal/
compliance text layer in this MVP (Impressum, ToS, disclaimers) — that's
explicitly out of scope until the technology is stable, see the ТЗ. Do not
onboard anyone else's funds onto this deployment.

## 0. Prerequisites

- Python 3.12+, Docker Desktop (or Docker Engine + Compose), git.
- A running AML+18 stack (`compliance-service`) — see `AML+18/README.md`.
  This exchanger calls it as an external client; it does not vendor or
  duplicate any of its code beyond the one new endpoint added to it
  (`POST /screening/check-name`, `compliance-service/app/screening/routes.py`).
- An Alchemy account (recommended EVM RPC provider — free tier covers this
  entire runbook, including Sepolia testnet). Infura or a self-hosted node
  work too; the code only ever speaks plain JSON-RPC, so nothing here is
  Alchemy-specific.

## 1. Register this service with AML-18

1. Start (or confirm running) the AML+18 stack: `compliance.local` on
   `http://localhost:8300`.
2. Sign up a developer project: open `http://localhost:8300/developer/signup`
   in a browser, fill in the form (name = "exchanger-service" or similar),
   submit.
3. Copy the issued API key (`aml18_sk_...`) — it is shown exactly once.
4. You'll paste it into `exchanger-service/.env` as `AML18_API_KEY` in
   step 3 below.

## 2. Get a 0x API key (REQUIRED)

0x retired its unauthenticated v1 API; every quote call now needs a key or
fails with 401. Sign up free at `0x.org/pricing` and copy the key -- you'll
paste it into `exchanger-service/.env` as `ZEROX_API_KEY` in step 3.

The exact response shape for a *successful* v2 quote was corrected here
based on live testing during development (see the docstring in
`app/liquidity/evm_dex_adapter.py`), not independently verified against a
real API key -- run the testnet pipeline in step 5 and watch for errors
around `execute_swap`/`ERC-20 approve` before trusting it with real funds.

## 3. Get an Alchemy API key (or your preferred RPC provider)

1. Create a free account at alchemy.com.
2. Create an app for **Ethereum Sepolia** first (testnet) and note its
   HTTPS URL: `https://eth-sepolia.g.alchemy.com/v2/<API_KEY>`.
3. Once you're ready for step 7 (mainnet), create a second app for
   **Ethereum Mainnet** and note that URL too.

## 4. Configure environment files

```
cd CryptoExcheng
cp .env.example .env
cd exchanger-service
cp .env.example .env
```

Edit `exchanger-service/.env`:
- `AML18_API_KEY` — from step 1.
- `ZEROX_API_KEY` — from step 2. Required, quotes fail without it.
- `EVM_RPC_URL` — leave blank for now; step 6 uses `EVM_SEPOLIA_RPC_URL`
  instead. Fill this in only in step 7, with your **mainnet** Alchemy URL.
- `EVM_SEPOLIA_RPC_URL` — your Sepolia Alchemy URL from step 3.
- `BTC_NETWORK=testnet` for now (mainnet in step 7).
- `ADMIN_PASSWORD` — pick a real password. Required; the admin login
  refuses to work without one.
- `SECRET_KEY` — any random string (`python -c "import secrets; print(secrets.token_hex(32))"`).
- `MARGIN_PERCENT` — the spread kept on every client-facing quote, baked
  into the rate (see app/pricing/margin.py). Defaults to 1.5.
- Leave `HOT_WALLET_KEYS_FILE` / `HOT_WALLET_KEYS_FERNET_KEY` for step 5.

## 5. Generate the hot wallet

The service derives both its EVM and BTC hot wallet keys from a single
BIP-39 mnemonic, encrypted at rest with a Fernet key
(`app/custody/key_management.py`). **TODO(production)** in that file marks
this as a deliberate dev-only compromise — replace with HSM/MPC custody
before any third-party funds ever touch this service.

```
cd exchanger-service
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -c "
from app.custody import key_management
fernet_key = key_management.generate_fernet_key()
mnemonic = key_management.generate_mnemonic()
key_management.save_mnemonic('./tmp/data/hot_wallet_keys.enc.json', fernet_key, mnemonic)
print('HOT_WALLET_KEYS_FERNET_KEY=' + fernet_key)
print('(mnemonic written to ./tmp/data/hot_wallet_keys.enc.json, encrypted)')
"
```

Paste the printed `HOT_WALLET_KEYS_FERNET_KEY=...` line into
`exchanger-service/.env`. Set `HOT_WALLET_KEYS_FILE=./tmp/data/hot_wallet_keys.enc.json`
(already the default).

**Never commit `tmp/data/hot_wallet_keys.enc.json` or the Fernet key.**
Both are already covered by `.gitignore`, but double-check before any
`git add -A`.

Derive and print the addresses you'll actually use:

```
.venv/Scripts/python -c "
from app.custody import key_management, evm_wallet, btc_wallet
mnemonic = key_management.load_mnemonic('./tmp/data/hot_wallet_keys.enc.json', '<paste HOT_WALLET_KEYS_FERNET_KEY>')
btc_wallet.configure_network('testnet')
print('EVM hot wallet:', evm_wallet.derive_address(mnemonic, 0))
print('BTC hot wallet (testnet):', btc_wallet.derive_address(mnemonic, 'testnet', 0))
"
```

**Checklist before continuing:** the deposit/hot-wallet addresses you just
printed are not published anywhere except to yourself right now (no commit,
no chat, no screenshot you'll share). They only need to be visible to you
and to whichever faucet/exchange you fund them from.

## 6. Run and verify end-to-end on testnet first

```
cd CryptoExcheng
docker compose up -d --build
```

This starts Postgres, `exchanger.local` (the web app, port 5100), and
`exchanger-listener` (the deposit-polling loop — nothing else polls chains
on its own, see `app/cli.py`).

```
docker compose exec exchanger.local flask db upgrade
```

Two ways to start an order, both feed the same pipeline:

- **As the operator**, open `http://localhost:5100/admin/login`, log in
  with `ADMIN_USERNAME`/`ADMIN_PASSWORD`, and click **+ New order**
  (`/admin/orders/new`).
- **As a client would**, open `http://localhost:5100/exchange/` (no
  login) — enter an amount, get a live quote (margin already applied),
  confirm with a name and destination address. This is the no-login,
  order-token flow described in `app/public_ui/routes.py`; it still relies
  on you, the operator, manually driving every pipeline step from
  `/admin` (run screening, execute swap, request withdrawal) -- it adds no
  automation, just a client-facing window onto the same orders.

Either way, fund the shown deposit address from a Sepolia faucet (e.g.
sepoliafaucet.com) for an ETH-chain order, or a Bitcoin testnet faucet for
a BTC-chain order, and watch the order move through the pipeline in
`/admin/orders/<id>` (or, for the client view, `/exchange/order/<token>`)
as `exchanger-listener` picks up the deposit.

Walk the full pipeline at least once on testnet: deposit confirmed →
run screening (needs a client name — collected up front on the public
flow, or typed into the admin form) → quote locked → swap executed →
withdrawal requested → (wallet-ownership verification if above threshold)
→ withdrawal sent → done. Fix anything that breaks before touching
mainnet.

## 7. Known MVP limitations (read before mainnet)

- **Native-asset withdrawal only.** `app/custody/send.py` sends ETH and BTC
  natively; withdrawing an ERC-20 (USDC, WBTC, ...) raises `SendError`
  rather than mis-sending. Extend `_send_withdrawal_fn` in
  `app/admin_ui/routes.py` if you need this.
- **BTC destination addresses are P2PKH only.** A bech32/P2WPKH/P2TR
  withdrawal destination raises `SendError`. This service's own derived BTC
  address is P2PKH, so round-tripping to another wallet you control in
  P2PKH form works out of the box.
- **BTC transaction signing has been verified by build/sign/serialize
  round-trip only** (`tests/test_custody_send.py`) — no live Bitcoin node
  was available to broadcast-test in the environment this was built in.
  Run a full regtest/testnet dry run (step 6) before mainnet; if it doesn't
  broadcast and confirm cleanly there, do not proceed.
- **0x Swap API v2 response shape corrected from live testing, not fully
  verified.** During development the original v1-based integration was
  caught failing against the real API (0x retired v1) and corrected to v2's
  AllowanceHolder endpoint -- confirmed to route and authenticate correctly,
  but a *successful* quote/execute response was not independently verified
  against official docs (the docs site wasn't fully fetchable at the time).
  Watch `execute_swap` closely on the testnet run in step 6, especially any
  ERC-20-source swap (the approve-then-swap path).
- **No live on-chain balance check for ERC-20 treasury assets**
  (`/admin/ledger` reconciles native ETH/BTC against chain state; WBTC/USDC
  balances shown are ledger-only, labeled as such on the page).
- **The public `/exchange/` flow is still operator-supervised, not
  self-service.** No automatic screening/quote-locking/swap-execution is
  triggered by a client's deposit; you still click through `/admin` for
  every step, same as an admin-created order. See ТЗ scope decision:
  "still demo, no real public onboarding."

## 8. Go to mainnet (minimal amounts)

Only after step 6's testnet run went cleanly end-to-end:

1. In `exchanger-service/.env`: set `EVM_RPC_URL` to your **mainnet**
   Alchemy URL, `EVM_CHAIN_ID=1`, `BTC_NETWORK=mainnet`,
   `EVM_MIN_CONFIRMATIONS=12`, `BTC_MIN_CONFIRMATIONS=2` (or higher).
2. Re-derive and reprint your mainnet addresses (step 5's second script,
   with `configure_network('mainnet')` and `btc_wallet.derive_address(mnemonic, 'mainnet', 0)`).
   These are **different addresses** from the testnet ones.
3. Re-confirm the checklist from step 5: the mainnet deposit address is not
   published anywhere but to yourself.
4. `docker compose restart exchanger.local exchanger-listener` (or
   `docker compose up -d --build` again if you changed the image).
5. Fund the mainnet hot wallet with a **small, personal** amount — enough
   to demo a real swap and withdrawal, not enough that a bug costs you
   anything you'd miss. There is no dollar figure this runbook can
   responsibly recommend beyond "an amount you would not be upset to lose
   entirely" — this is unaudited MVP code.
6. Run one full pipeline pass exactly as in step 5, on mainnet, watching
   `/admin/orders/<id>` at every step.
