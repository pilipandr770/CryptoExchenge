import os
from decimal import Decimal


class Config:
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:////data/exchanger.db"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # AML-18 compliance-service client (see app/compliance_client). Signed up
    # as a developer project via AML-18's /developer/signup, same as any
    # other third-party integrator would be.
    AML18_BASE_URL = os.environ.get("AML18_BASE_URL", "http://localhost:8300")
    AML18_API_KEY = os.environ.get("AML18_API_KEY", "")
    AML18_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("AML18_REQUEST_TIMEOUT_SECONDS", "10"))

    # Self-hosted-wallet withdrawal above this EUR amount requires AML-18
    # wallet-ownership verification before WITHDRAWAL_SENT. Mirrors AML-18's
    # own WALLET_OWNERSHIP_THRESHOLD_EUR default -- kept as a separate env var
    # here since this service calls AML-18 as an external client rather than
    # sharing its config object.
    WALLET_OWNERSHIP_THRESHOLD_EUR = float(os.environ.get("WALLET_OWNERSHIP_THRESHOLD_EUR", "1000"))

    # --- Liquidity: 0x Swap API v2 (see app/liquidity/evm_dex_adapter.py) --
    # MVP choice per ТЗ section 6: 0x's REST quote/swap API needs no bespoke
    # smart contract of our own. ZEROX_API_KEY is REQUIRED -- 0x retired the
    # unauthenticated v1 API; every v2 call needs a key from 0x.org, or
    # quotes fail with a 401.
    ZEROX_API_BASE_URL = os.environ.get("ZEROX_API_BASE_URL", "https://api.0x.org")
    ZEROX_API_KEY = os.environ.get("ZEROX_API_KEY", "")
    ZEROX_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("ZEROX_REQUEST_TIMEOUT_SECONDS", "10"))
    SWAP_QUOTE_TTL_SECONDS = int(os.environ.get("SWAP_QUOTE_TTL_SECONDS", "30"))

    # --- Client-facing pricing (see app/pricing/margin.py) ----------------
    # The spread kept on every client-facing swap, baked into the quoted
    # rate rather than shown as a separate fee line (instant-exchanger
    # style). Snapshotted onto each SwapOrder at quote time so a later
    # config change never retroactively changes what a client was promised.
    MARGIN_PERCENT = Decimal(os.environ.get("MARGIN_PERCENT", "1.5"))

    # --- EVM custody / chain listener ---------------------------------
    # No default URL is set -- the API key belongs only in .env, never in
    # code. Alchemy is the recommended provider (free tier, Sepolia
    # support, plain JSON-RPC so nothing here is Alchemy-specific); see
    # RUNBOOK.md and .env.example.
    EVM_RPC_URL = os.environ.get("EVM_RPC_URL", "")
    EVM_CHAIN_ID = int(os.environ.get("EVM_CHAIN_ID", "1"))
    EVM_SEPOLIA_RPC_URL = os.environ.get("EVM_SEPOLIA_RPC_URL", "")
    # 12 confirmations is Ethereum-mainnet-conservative; drop this for
    # cheaper/faster L2 or testnet demo runs via .env, per ТЗ section 12.
    EVM_MIN_CONFIRMATIONS = int(os.environ.get("EVM_MIN_CONFIRMATIONS", "12"))
    EVM_LISTENER_POLL_INTERVAL_SECONDS = float(os.environ.get("EVM_LISTENER_POLL_INTERVAL_SECONDS", "15"))

    # --- BTC custody / chain listener -----------------------------------
    BTC_ESPLORA_API_BASE_URL = os.environ.get("BTC_ESPLORA_API_BASE_URL", "https://blockstream.info/api")
    BTC_NETWORK = os.environ.get("BTC_NETWORK", "mainnet")  # mainnet | testnet | regtest
    # 1 confirmation is a dev-demo compromise (ТЗ section 7); raise to 2-6 for
    # anything resembling production.
    BTC_MIN_CONFIRMATIONS = int(os.environ.get("BTC_MIN_CONFIRMATIONS", "1"))
    BTC_LISTENER_POLL_INTERVAL_SECONDS = float(os.environ.get("BTC_LISTENER_POLL_INTERVAL_SECONDS", "30"))

    # --- Key management (dev-only; see app/custody/key_management.py) ----
    # TODO(production): replace with HSM/MPC custody before any third-party
    # client funds touch this service.
    HOT_WALLET_KEYS_FILE = os.environ.get("HOT_WALLET_KEYS_FILE", "")
    HOT_WALLET_KEYS_FERNET_KEY = os.environ.get("HOT_WALLET_KEYS_FERNET_KEY", "")

    # --- Admin UI (single operator, not public) ---------------------------
    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
