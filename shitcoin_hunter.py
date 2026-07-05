#!/usr/bin/env python3
"""
shitcoin_hunter.py — Solana memecoin momentum hunter
Standalone service, independent from the main bot (own wallet, own SHITCOIN_HUNTER_* env namespace).
Experimental — see the Disclaimer in README.md before running with real funds.

5-minute cycle: discovers top 100 Solana tokens by volume dynamically,
detects volume spikes, uses AI buy signal, automated TP/SL exits.
Config via .env: SHITCOIN_HUNTER_AMOUNT_USD, SHITCOIN_HUNTER_MAX_POSITIONS,
                 SHITCOIN_HUNTER_TAKE_PROFIT, SHITCOIN_HUNTER_STOP_LOSS
"""

import os
import json
import base64
import logging
import asyncio
from datetime import datetime, timezone, timedelta

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(__file__)
load_dotenv(os.path.join(_BASE_DIR, ".env"))

# Captured once at import time, like trader.py's own keypair — not re-read via
# os.getenv() on every call, so it stays correct even if something else in the
# same process mutates os.environ later.
_HUNTER_PRIVATE_KEY_RAW = os.environ.get("SHITCOIN_HUNTER_SOLANA_PRIVATE_KEY", "")

# ── Logging ───────────────────────────────────────────────────────────────────
# Scoped to the "hunter" logger only (not logging.basicConfig, which would
# hijack the root logger of whatever process imports this module — e.g. the
# dashboard's gunicorn workers importing get_wallet_completo() — and redirect
# unrelated logging (data_collector, etc.) into hunter.log).
_LOG_DIR = os.path.join(_BASE_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
log = logging.getLogger("hunter")
log.setLevel(logging.INFO)
if not log.handlers:
    _formatter = logging.Formatter(
        "%(asctime)s [HUNTER] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    _stream_handler = logging.StreamHandler()
    _stream_handler.setFormatter(_formatter)
    _file_handler = logging.FileHandler(os.path.join(_LOG_DIR, "hunter.log"))
    _file_handler.setFormatter(_formatter)
    log.addHandler(_stream_handler)
    log.addHandler(_file_handler)
    log.propagate = False

# ── Config ────────────────────────────────────────────────────────────────────
AMOUNT_USD      = float(os.getenv("SHITCOIN_HUNTER_AMOUNT_USD",   "5"))
MAX_POSITIONS   = int(os.getenv("SHITCOIN_HUNTER_MAX_POSITIONS",  "10"))
TAKE_PROFIT_PCT = float(os.getenv("SHITCOIN_HUNTER_TAKE_PROFIT",  "50"))
STOP_LOSS_PCT   = float(os.getenv("SHITCOIN_HUNTER_STOP_LOSS",    "25"))
MONITOR_SECONDS = int(os.getenv("SHITCOIN_HUNTER_MONITOR_SECONDS", "20"))
BUY_FAIL_COOLDOWN_HOURS = float(os.getenv("SHITCOIN_HUNTER_BUY_FAIL_COOLDOWN_HOURS", "4"))
ENABLED         = os.getenv("SHITCOIN_HUNTER_ENABLED", "true").lower() == "true"
DRY_RUN         = os.getenv("SHITCOIN_HUNTER_DRY_RUN",  "true").lower() == "true"

RPC_URL          = os.getenv("SHITCOIN_HUNTER_SOLANA_RPC", "https://api.mainnet-beta.solana.com")
OPENROUTER_KEY   = os.getenv("SHITCOIN_HUNTER_OPENROUTER_API_KEY") or os.getenv("SOL_TRADING_OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("SHITCOIN_HUNTER_OPENROUTER_MODEL_FAST", "google/gemini-2.5-flash-lite")
COINGECKO_KEY    = os.getenv("SHITCOIN_HUNTER_COINGECKO_API_KEY")

JUPITER_ORDER_URL = "https://api.jup.ag/swap/v2/order"   # v2: quote+swap in one call
JUPITER_PRICE_URL = "https://api.jup.ag/price/v3"        # real-time exit price, same source as swap

# Jupiter Referral Program account (referral.jup.ag/dashboard-ultra) — earns a share
# of swap fees. Must be registered under project DkiqsTrw1u1bYFumumC7sCG2S8K25qc2vemJFHyW2wJc
# (the Ultra project /swap/v2/order expects) — verified on-chain and live-tested
# across USDC<->SOL, USDC->ETH, USDC->USDT, USDC->BTC.
JUPITER_REFERRAL_ACCOUNT = "7H4bLxfkAsqBSU5ZJn9aPrzUjz7pJWpcogUfUcRDD32i"
CG_BASE           = "https://api.coingecko.com/api/v3"
USDC_MINT         = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT         = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SOL_MINT          = "So11111111111111111111111111111111111111112"
MIN_SOL_RESERVE   = 0.009
SOL_RESERVE_TARGET_USD = 2.0   # top up to this value when SOL drops below MIN_SOL_RESERVE

# Discovery filters
MIN_MARKET_CAP  = 500_000    # $500k — enough for Jupiter to route
MIN_VOLUME_24H  = 100_000    # $100k — real activity
MAX_PRICE_IMPACT = 3.0       # % — reject illiquid swaps

# Tokens that should never be bought (stablecoins, wrapped BTC/ETH, native SOL)
BLACKLIST_CG_IDS = {
    "usd-coin", "tether", "solana", "wrapped-solana",
    "bitcoin", "ethereum", "wrapped-bitcoin", "wrapped-ether",
    "jupiter-exchange-solana",   # DeFi infra, not a memecoin
}

DATA_DIR         = os.path.join(_BASE_DIR, "data")
STATE_FILE       = os.path.join(DATA_DIR, "shitcoin_state.json")
MINT_CACHE_FILE  = os.path.join(DATA_DIR, "hunter_mint_cache.json")

# ── Seed mints (pre-known, avoids cold-start API lookups) ─────────────────────
SEED_MINTS = {
    "bonk":                     "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "dogwifcoin":               "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "pudgy-penguins":           "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
    "popcat":                   "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "book-of-meme":             "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82",
    "cat-in-a-dogs-world":      "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",
    "fartcoin":                 "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
    "moo-deng":                 "ED5nyyWEzpPPiWimP8vYm7sD7TD3LAt3Q3gRTWHzc8Wu",
    "official-trump":           "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",
    "slerf":                    "7BgBvyjrZX1YKz4oh9mjb8ZScatkkwb8DzFx7ynKAxCs",
    "myro":                     "HhJpBhRRn4g56VsyLuT8DL5Bv31HkXqsrahTTUCZeZg4",
}

# ── Mint cache (persistent, grows as new tokens are discovered) ───────────────
_mint_cache: dict[str, str] = {}  # cg_id → solana_mint

def _load_mint_cache():
    global _mint_cache
    _mint_cache = dict(SEED_MINTS)   # always start with seeds
    if os.path.exists(MINT_CACHE_FILE):
        try:
            with open(MINT_CACHE_FILE) as f:
                _mint_cache.update(json.load(f))
        except Exception as e:
            log.warning(f"Mint cache load error: {e}")

def _save_mint_cache():
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(MINT_CACHE_FILE, "w") as f:
            json.dump(_mint_cache, f, indent=2)
    except Exception as e:
        log.warning(f"Mint cache save error: {e}")

async def get_solana_mint(cg_id: str) -> str | None:
    """Return Solana mint for a CoinGecko ID, fetching from API if not cached."""
    if cg_id in _mint_cache:
        return _mint_cache[cg_id]
    headers = {"x-cg-demo-api-key": COINGECKO_KEY} if COINGECKO_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{CG_BASE}/coins/{cg_id}",
                params={"localization": "false", "tickers": "false",
                        "market_data": "false", "community_data": "false",
                        "developer_data": "false"},
                headers=headers,
            )
            r.raise_for_status()
            mint = r.json().get("platforms", {}).get("solana")
        if mint:
            _mint_cache[cg_id] = mint
            _save_mint_cache()
            log.info(f"Cached mint for {cg_id}: {mint[:8]}...")
        return mint
    except Exception as e:
        log.warning(f"Mint lookup {cg_id}: {e}")
        return None

# ── State ─────────────────────────────────────────────────────────────────────

_state_lock = asyncio.Lock()  # serializes run_cycle vs monitor_positions on shared state

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.error(f"State load error: {e}")
    return {"positions": {}, "vol_history": {}}

def save_state(state: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"State save error: {e}")

# ── Telegram ──────────────────────────────────────────────────────────────────

async def notifica(msg: str):
    bot_token = os.getenv("SHITCOIN_HUNTER_TELEGRAM_BOT_TOKEN")
    chat_id   = os.getenv("SHITCOIN_HUNTER_TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg[:4000], "parse_mode": "HTML"},
            )
    except Exception as e:
        log.error(f"Telegram: {e}")

# ── Market data — dynamic discovery ───────────────────────────────────────────

async def get_prices() -> list[dict]:
    """
    Fetch top 100 Solana ecosystem tokens by 24h volume from CoinGecko.
    Filters by market cap and volume thresholds.
    Returns list of {cg_id, symbol, price, ch1h, ch24h, vol, mc}.
    """
    headers = {"x-cg-demo-api-key": COINGECKO_KEY} if COINGECKO_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                f"{CG_BASE}/coins/markets",
                params={
                    "vs_currency":             "usd",
                    "category":                "solana-ecosystem",
                    "order":                   "volume_desc",
                    "per_page":                100,
                    "page":                    1,
                    "sparkline":               "false",
                    "price_change_percentage": "1h,24h",
                },
                headers=headers,
            )
            r.raise_for_status()
            coins = r.json()
    except Exception as e:
        log.error(f"CoinGecko error: {e}")
        return []

    result = []
    for coin in coins:
        cg_id = coin.get("id", "")
        if cg_id in BLACKLIST_CG_IDS:
            continue
        mc    = float(coin.get("market_cap")  or 0)
        vol   = float(coin.get("total_volume") or 0)
        price = float(coin.get("current_price") or 0)
        if mc < MIN_MARKET_CAP or vol < MIN_VOLUME_24H or price <= 0:
            continue
        result.append({
            "cg_id":  cg_id,
            "symbol": (coin.get("symbol") or cg_id).upper()[:10],
            "price":  price,
            "ch1h":   float(coin.get("price_change_percentage_1h_in_currency")  or 0),
            "ch24h":  float(coin.get("price_change_percentage_24h_in_currency") or 0),
            "vol":    vol,
            "mc":     mc,
        })

    log.info(f"Discovered {len(result)} tradeable Solana tokens")
    return result

async def get_jupiter_prices(mints: list[str]) -> dict[str, float]:
    """Batch fetch USD prices from Jupiter — the same source used for swap execution,
    so TP/SL decisions match what a sell would actually realize."""
    if not mints:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(JUPITER_PRICE_URL, params={"ids": ",".join(mints)})
            r.raise_for_status()
            data = r.json()
        return {
            mint: float(v["usdPrice"])
            for mint, v in data.items() if v and v.get("usdPrice")
        }
    except Exception as e:
        log.error(f"Jupiter price: {e}")
        return {}

# ── Solana RPC helpers ────────────────────────────────────────────────────────

def _load_hunter_keypair():
    try:
        from solders.keypair import Keypair
        raw = _HUNTER_PRIVATE_KEY_RAW
        if not raw:
            raise ValueError("SOLANA_PRIVATE_KEY not set")
        if raw.startswith("["):
            secret = bytes(json.loads(raw))
            return Keypair.from_bytes(secret)
        else:
            return Keypair.from_base58_string(raw)
    except Exception as e:
        log.error(f"Hunter keypair error: {e}")
        return None

_HUNTER_KEYPAIR = _load_hunter_keypair()

def load_keypair():
    return _HUNTER_KEYPAIR

def _pubkey() -> str:
    return str(_HUNTER_KEYPAIR.pubkey()) if _HUNTER_KEYPAIR else ""

async def get_sol_balance() -> float:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(RPC_URL, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "getBalance",
                "params":  [_pubkey()],
            })
            return r.json()["result"]["value"] / 1e9
    except Exception as e:
        log.error(f"SOL balance: {e}")
        return 0.0

async def get_usdc_balance() -> float:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(RPC_URL, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "getTokenAccountsByOwner",
                "params":  [_pubkey(), {"mint": USDC_MINT}, {"encoding": "jsonParsed"}],
            })
            accs = r.json()["result"]["value"]
            if not accs:
                return 0.0
            return float(accs[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    except Exception as e:
        log.error(f"USDC balance: {e}")
        return 0.0

async def _get_usdt_balance() -> float:
    raw, decimals = await get_token_raw(USDT_MINT)
    return raw / (10 ** decimals) if raw else 0.0

async def get_token_raw(mint: str) -> tuple[int, int]:
    """Returns (raw_amount, decimals) for a token in the hunter wallet."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(RPC_URL, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "getTokenAccountsByOwner",
                "params":  [_pubkey(), {"mint": mint}, {"encoding": "jsonParsed"}],
            })
            accs = r.json()["result"]["value"]
            if not accs:
                return 0, 6
            ta = accs[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
            return int(ta["amount"]), int(ta["decimals"])
    except Exception as e:
        log.error(f"Token raw {mint[:8]}: {e}")
        return 0, 6

async def get_wallet_completo() -> dict:
    """Balances for the hunter wallet: SOL, USDC, and each open position's token —
    same shape as trader.get_wallet_completo() for the dashboard."""
    state = load_state()
    positions = state.get("positions", {})

    sol, usdc = await asyncio.gather(get_sol_balance(), get_usdc_balance())
    result = {"pubkey": _pubkey(), "SOL": round(sol, 6), "USDC": round(usdc, 2)}

    mints = [(cg_id, p["mint"]) for cg_id, p in positions.items() if p.get("mint")]
    if mints:
        raws = await asyncio.gather(*[get_token_raw(mint) for _, mint in mints])
        for (cg_id, _), (raw, dec) in zip(mints, raws):
            bal = raw / (10 ** dec)
            if bal > 0:
                symbol = positions[cg_id].get("symbol", cg_id.upper())
                result[symbol] = round(bal, 8)
    return result

# ── Jupiter swap ──────────────────────────────────────────────────────────────

async def _swap(mint_in: str, mint_out: str, amount_raw: int) -> dict:
    """Execute swap via Jupiter v2 using raw mint addresses."""
    if DRY_RUN:
        log.info(f"[DRY RUN] {amount_raw} raw {mint_in[:8]}→{mint_out[:8]}")
        return {"success": True, "dry_run": True, "tx_hash": "DRY_RUN_NO_TX"}

    from solders.transaction import VersionedTransaction
    from solders import message as solders_message

    kp = load_keypair()
    if not kp:
        return {"success": False, "error": "Keypair not available"}

    # Jupiter v2: single GET call (quote + swap combined)
    params: dict = {
        "inputMint":        mint_in,
        "outputMint":       mint_out,
        "amount":           amount_raw,
        "taker":            str(kp.pubkey()),
        "slippageBps":      300,
        # Cap priority fee instead of leaving it fully automatic (v1 had priorityLevelWithMaxLamports.maxLamports=500_000)
        "priorityFeeLamports": 500_000,
        "broadcastFeeType":    "maxCap",
    }
    params["referralAccount"] = JUPITER_REFERRAL_ACCOUNT
    params["referralFee"]     = int(os.getenv("SHITCOIN_HUNTER_JUPITER_REFERRAL_FEE_BPS", "50"))

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(JUPITER_ORDER_URL, params=params)
            r.raise_for_status()
            order = r.json()
    except Exception as e:
        return {"success": False, "error": f"Order: {e}"}

    if order.get("errorCode"):
        return {"success": False, "error": f"Jupiter: {order.get('errorMessage', order['errorCode'])}"}

    # v2 "priceImpact" is already a percentage (-1.23 = -1.23%); "priceImpactPct" is the decimal form.
    price_impact = abs(float(order.get("priceImpact", 0)))
    if price_impact > MAX_PRICE_IMPACT:
        return {"success": False, "error": f"Price impact {price_impact:.2f}% > {MAX_PRICE_IMPACT}%"}

    tx_b64_raw = order.get("transaction")
    if not tx_b64_raw:
        return {"success": False, "error": "No transaction in order response"}
    log.info(
        f"Order meta: router={order.get('router')} swapType={order.get('swapType')} "
        f"expireAt={order.get('expireAt')} lastValidBlockHeight={order.get('lastValidBlockHeight')} "
        f"prioritizationFeeLamports={order.get('prioritizationFeeLamports')}"
    )

    # Sign and send
    raw_tx = base64.b64decode(tx_b64_raw)
    tx     = VersionedTransaction.from_bytes(raw_tx)
    sig    = kp.sign_message(solders_message.to_bytes_versioned(tx.message))
    signed = VersionedTransaction.populate(tx.message, [sig])
    tx_b64 = base64.b64encode(bytes(signed)).decode()

    # maxRetries=0: the RPC node's own built-in rebroadcast is very limited
    # (a handful of attempts over a few seconds) — we rebroadcast ourselves
    # below, for the whole blockhash validity window, since a single narrow
    # burst of retries is not enough during any real network congestion.
    send_params = {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}

    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(RPC_URL, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "sendTransaction",
                "params":  [tx_b64, send_params],
            })
            rpc_resp = r.json()
    except Exception as e:
        return {"success": False, "error": f"sendTransaction: {e}"}

    if "error" in rpc_resp:
        return {"success": False, "error": f"RPC: {rpc_resp['error']}"}

    txid = rpc_resp["result"]
    log.info(f"Tx sent: {txid}, polling...")

    for i in range(30):
        await asyncio.sleep(2)

        # Rebroadcast every ~2s (idempotent — Solana dedupes by signature) so the
        # tx keeps getting a chance to land for the full ~60-90s blockhash window,
        # instead of only the first few seconds after the initial send. Dense
        # spacing matters most early on, while the blockhash is freshest.
        if i > 0:
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    rb = await c.post(RPC_URL, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method":  "sendTransaction",
                        "params":  [tx_b64, send_params],
                    })
                    rb_json = rb.json()
                    if "error" in rb_json:
                        log.warning(f"Rebroadcast #{i} error: {rb_json['error']}")
            except Exception as e:
                log.warning(f"Rebroadcast #{i} failed (non-fatal): {e}")

        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(RPC_URL, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method":  "getSignatureStatuses",
                    "params":  [[txid], {"searchTransactionHistory": True}],
                })
                status = r.json().get("result", {}).get("value", [None])[0]
                if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
                    if status.get("err"):
                        return {"success": False, "error": f"Tx error: {status['err']}", "tx_hash": txid}
                    log.info(f"Confirmed: {txid}")
                    return {"success": True, "tx_hash": txid, "price_impact": price_impact}
        except Exception:
            pass

    return {"success": False, "error": "Confirmation timeout", "tx_hash": txid}

# ── SOL fee-reserve top-up ──────────────────────────────────────────────────────

async def replenish_sol_if_low():
    """
    SOL is consumed by network fees on every swap (buy, sell, and this top-up
    itself). Left unchecked, it eventually runs out and the bot can't pay fees
    for anything — not even to sell an open position. If SOL drops below
    MIN_SOL_RESERVE, swap just enough stablecoin into SOL to bring the reserve
    back up to SOL_RESERVE_TARGET_USD — USDC first, then USDT for any remainder
    (whichever the wallet actually holds).
    """
    sol = await get_sol_balance()
    if sol >= MIN_SOL_RESERVE:
        return

    prices = await get_jupiter_prices([SOL_MINT])
    sol_price = prices.get(SOL_MINT)
    if not sol_price:
        log.warning(f"SOL low ({sol:.5f}) but no SOL price available — skipping top-up this cycle")
        return

    sol_value_usd = sol * sol_price
    remaining = round(SOL_RESERVE_TARGET_USD - sol_value_usd, 2)
    if remaining <= 0:
        return

    log.warning(f"⛽ SOL LOW: {sol:.5f} SOL (${sol_value_usd:.2f}) — need ${remaining} top-up")

    for mint, symbol, get_balance in (
        (USDC_MINT, "USDC", get_usdc_balance),
        (USDT_MINT, "USDT", lambda: _get_usdt_balance()),
    ):
        if remaining < 0.5:
            break
        available = await get_balance()
        amount_usd = round(min(remaining, available), 2)
        if amount_usd < 0.5:
            continue
        amount_raw = int(amount_usd * 1_000_000)  # both USDC and USDT use 6 decimals
        result = await _swap(mint, SOL_MINT, amount_raw)
        if result.get("success"):
            log.info(f"SOL top-up OK: +${amount_usd} {symbol} → SOL")
            await notifica(f"⛽ <b>SOL REFILL</b>: swapped ${amount_usd} {symbol} → SOL (reserve was {sol:.5f} SOL)")
            remaining = round(remaining - amount_usd, 2)
        else:
            log.error(f"SOL top-up swap failed ({symbol}): {result.get('error')}")

    if remaining >= 0.5:
        log.warning(f"SOL top-up incomplete: still need ${remaining} more (USDC+USDT too low)")
        await notifica(
            f"⚠️ <b>SOL RESERVE LOW</b>: {sol:.5f} SOL (${sol_value_usd:.2f}), "
            f"couldn't top up — still need ${remaining} more and USDC/USDT balance "
            f"isn't enough to cover it. Deposit funds or the bot may soon be unable "
            f"to pay fees for buys, sells, or exits."
        )

# ── Buy / Sell ────────────────────────────────────────────────────────────────

async def buy_token(cg_id: str, symbol: str, mint: str, price: float, state: dict,
                     ch1h: float = 0, ch24h: float = 0, vol_spike: float | None = None):
    amount_raw = int(AMOUNT_USD * 1_000_000)  # USDC = 6 decimals
    spike_str = f"{vol_spike:.1f}x" if vol_spike is not None else "n/a"
    # Tracks how much the pump had already run before we bought — to check whether
    # we're chasing moves that are already over (see 2026-07-05 discussion).
    log.info(
        f"BUY {symbol} ({cg_id}) @ ${price:.8g} | ${AMOUNT_USD} | "
        f"pre-buy 1h={ch1h:+.1f}% 24h={ch24h:+.1f}% vol_spike={spike_str}"
    )

    result = await _swap(USDC_MINT, mint, amount_raw)

    if result.get("success"):
        state["positions"][cg_id] = {
            "symbol":       symbol,
            "mint":         mint,
            "entry_price":  price,
            "invested_usd": AMOUNT_USD,
            "buy_time":     datetime.now(timezone.utc).isoformat(),
            "tx_hash":      result.get("tx_hash", ""),
        }
        _log_trade({
            "token": symbol, "azione": "compra",
            "importo_usdc": AMOUNT_USD, "prezzo_entrata": price,
            "motivazione": f"🦟 HUNTER | volume spike {spike_str} | pre-buy 1h={ch1h:+.1f}% 24h={ch24h:+.1f}%",
            "eseguito": True, "tx_hash": result.get("tx_hash", ""),
        })
        await notifica(
            f"🦟 <b>HUNTER BUY</b>: {symbol}\n"
            f"💰 ${AMOUNT_USD} @ ${price:.8g}\n"
            f"📈 Pre-buy: 1h {ch1h:+.1f}% | 24h {ch24h:+.1f}% | spike {spike_str}\n"
            f"🎯 TP +{TAKE_PROFIT_PCT}% | SL -{STOP_LOSS_PCT}%\n"
            + (f"✅ TX: {result['tx_hash'][:30]}" if not result.get("dry_run") else "🔵 DRY RUN")
        )
    else:
        err = result.get("error", "unknown")
        log.error(f"BUY FAILED {symbol}: {err}")
        until = datetime.now(timezone.utc) + timedelta(hours=BUY_FAIL_COOLDOWN_HOURS)
        state.setdefault("buy_cooldown", {})[cg_id] = until.isoformat()
        await notifica(f"❌ <b>HUNTER BUY FAILED</b>: {symbol}\n{err[:200]}\n🕒 Cooldown {BUY_FAIL_COOLDOWN_HOURS}h")


async def sell_token(cg_id: str, reason: str, price: float, state: dict):
    pos    = state["positions"].get(cg_id, {})
    symbol = pos.get("symbol", cg_id.upper())
    mint   = pos.get("mint", "")

    if not mint:
        log.error(f"Sell {symbol}: no mint in position state")
        return

    raw, _ = await get_token_raw(mint)
    if raw == 0:
        log.warning(f"Sell {symbol}: zero balance, removing")
        state["positions"].pop(cg_id, None)
        return

    entry = float(pos.get("entry_price", 0))
    pct   = round((price - entry) / entry * 100, 2) if entry else 0
    inv   = float(pos.get("invested_usd", AMOUNT_USD))
    pnl   = round((price - entry) / entry * inv, 2) if entry else 0

    log.info(f"SELL {symbol} @ ${price:.8g} | {reason} | P&L {pct:+.1f}%")
    result = await _swap(mint, USDC_MINT, raw)

    if result.get("success"):
        _log_trade({
            "token": symbol, "azione": "vendi",
            "importo_usdc": inv, "prezzo_entrata": entry,
            "prezzo_uscita": price, "risultato_pct": pct, "risultato_usdc": pnl,
            "motivazione": f"🦟 HUNTER | {reason}",
            "eseguito": True, "tx_hash": result.get("tx_hash", ""),
        })
        state["positions"].pop(cg_id, None)
        await notifica(
            f"🦟 <b>HUNTER SELL</b>: {symbol}\n"
            f"📊 P&L: {pct:+.1f}% (${pnl:+.2f})\n"
            f"🏷️ {reason}\n"
            + (f"✅ TX: {result['tx_hash'][:30]}" if not result.get("dry_run") else "🔵 DRY RUN")
        )
    else:
        err = result.get("error", "unknown")
        log.error(f"SELL FAILED {symbol}: {err}")
        await notifica(f"❌ <b>HUNTER SELL FAILED</b>: {symbol}\n{err[:200]}")


def _log_trade(dati: dict):
    try:
        from database import salva_trade
        salva_trade(dati)
    except Exception as e:
        log.warning(f"DB log: {e}")

# ── AI buy decision ───────────────────────────────────────────────────────────

async def ask_claude(candidates: list[dict]) -> str:
    """
    Returns cg_id to buy or "NONE".
    candidates = [{cg_id, symbol, price, ch1h, ch24h, vol, vol_spike}, ...]
    """
    if not candidates:
        return "NONE"

    if not OPENROUTER_KEY:
        best = max(candidates, key=lambda x: x["vol_spike"])
        return best["cg_id"] if best["vol_spike"] >= 1.5 else "NONE"

    rows = "\n".join(
        f"{c['symbol']} ({c['cg_id']}): ${c['price']:.8g} | "
        f"1h {c['ch1h']:+.1f}% | 24h {c['ch24h']:+.1f}% | "
        f"vol ${c['vol']:,.0f} | spike {c['vol_spike']:.1f}x"
        for c in candidates
    )
    prompt = (
        "Solana memecoin momentum trader. Buy only the clearest pump signal.\n"
        "Buy rules: volume spike ≥ 1.3x AND 1h positive AND 24h positive.\n"
        "Do NOT buy: 24h already >35% (too late), 1h slowing or negative.\n\n"
        f"Tokens:\n{rows}\n\n"
        'Reply ONLY with valid JSON using the cg_id field: '
        '{"cg_id": "bonk"} or {"cg_id": "NONE", "reason": "<max 15 words>"}'
    )

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       OPENROUTER_MODEL,
                    "max_tokens":  150,
                    "temperature": 0.1,
                    "messages":    [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            raw  = r.json()["choices"][0]["message"]["content"].strip()
            raw  = raw.strip("`").removeprefix("json").strip()
            data = json.loads(raw)
        cg_id = data.get("cg_id", "NONE")
        if data.get("reason"):
            log.info(f"Claude NONE: {data['reason']}")
        valid_ids = {c["cg_id"] for c in candidates}
        return cg_id if cg_id in valid_ids else "NONE"
    except Exception as e:
        log.error(f"Claude: {e}")
        return "NONE"

# ── Main cycle ────────────────────────────────────────────────────────────────

async def run_cycle():
    if not ENABLED:
        return

    await _state_lock.acquire()
    try:
        state = load_state()

        # 0. Keep enough SOL for fees before anything else needs to swap this cycle
        await replenish_sol_if_low()

        # 1. Fetch top 100 Solana tokens (dynamic discovery, 1 CoinGecko call)
        tokens = await get_prices()
        if not tokens:
            log.warning("No price data, skipping cycle")
            return

        # Build lookup by cg_id for fast access
        prices_by_id = {t["cg_id"]: t for t in tokens}

        # 2. Rolling volume history keyed by cg_id (12 readings = 1 hour)
        vol_hist: dict = state.setdefault("vol_history", {})
        for t in tokens:
            hist = vol_hist.setdefault(t["cg_id"], [])
            hist.append(t["vol"])
            if len(hist) > 12:
                hist.pop(0)

        # 3. TP / SL on open positions
        for cg_id in list(state.get("positions", {})):
            t = prices_by_id.get(cg_id)
            if not t:
                continue
            entry = float(state["positions"][cg_id].get("entry_price", 0))
            curr  = t["price"]
            if entry <= 0:
                continue
            pct = (curr - entry) / entry * 100
            # Persist live price for dashboard
            state["positions"][cg_id]["current_price"] = curr
            state["positions"][cg_id]["current_pct"]   = round(pct, 2)
            if pct >= TAKE_PROFIT_PCT:
                await sell_token(cg_id, f"TP +{pct:.1f}%", curr, state)
            elif pct <= -STOP_LOSS_PCT:
                await sell_token(cg_id, f"SL {pct:.1f}%", curr, state)

        # 4. Consider buying
        open_count = len(state.get("positions", {}))
        if open_count < MAX_POSITIONS:
            sol = await get_sol_balance()
            if sol < MIN_SOL_RESERVE:
                log.warning(f"SOL too low for fees: {sol:.4f}")
            else:
                usdc = await get_usdc_balance()
                if usdc >= AMOUNT_USD:
                    already_held = set(state.get("positions", {}).keys())

                    # Prune expired cooldown entries, then skip tokens still in cooldown
                    cooldown = state.setdefault("buy_cooldown", {})
                    now = datetime.now(timezone.utc)
                    for cid in list(cooldown):
                        try:
                            expired = now >= datetime.fromisoformat(cooldown[cid])
                        except Exception:
                            expired = True
                        if expired:
                            cooldown.pop(cid, None)

                    candidates = []
                    for t in tokens:
                        cg_id = t["cg_id"]
                        if cg_id in already_held or cg_id in cooldown:
                            continue
                        hist = vol_hist.get(cg_id, [])
                        if len(hist) < 3:
                            continue  # need ≥15 min baseline
                        avg_vol   = sum(hist[:-1]) / len(hist[:-1])
                        vol_spike = t["vol"] / avg_vol if avg_vol > 0 else 1.0
                        candidates.append({**t, "vol_spike": round(vol_spike, 2)})

                    # Pre-filter: only pass interesting tokens to Claude
                    interesting = [
                        c for c in candidates
                        if c["vol_spike"] >= 1.3 or (c["ch1h"] > 1.5 and c["ch24h"] > 3)
                    ]

                    if interesting:
                        log.info(f"Candidates ({len(interesting)}): {[c['symbol'] for c in interesting[:8]]}")
                        chosen_id = await ask_claude(interesting)
                        if chosen_id and chosen_id != "NONE" and chosen_id not in state.get("positions", {}):
                            token_data = prices_by_id[chosen_id]
                            cand = next((c for c in interesting if c["cg_id"] == chosen_id), None)
                            # Look up or fetch Solana mint
                            mint = await get_solana_mint(chosen_id)
                            if mint:
                                await buy_token(
                                    chosen_id, token_data["symbol"],
                                    mint, token_data["price"], state,
                                    ch1h=(cand or token_data).get("ch1h", 0),
                                    ch24h=(cand or token_data).get("ch24h", 0),
                                    vol_spike=(cand or {}).get("vol_spike"),
                                )
                            else:
                                log.warning(f"No Solana mint for {chosen_id}, skipping")
                    else:
                        log.debug("No interesting candidates this cycle")

        # 5. Log open positions
        positions = state.get("positions", {})
        if positions:
            parts = [
                f"{p.get('symbol', cid)} {p.get('current_pct', 0):+.1f}%"
                for cid, p in positions.items()
            ]
            log.info(f"Open ({len(positions)}): {' | '.join(parts)}")

        save_state(state)

    except Exception as e:
        log.error(f"Cycle error: {e}", exc_info=True)
        await notifica(f"❌ <b>HUNTER ERROR</b>\n{str(e)[:300]}")
    finally:
        _state_lock.release()


async def monitor_positions():
    """Fast TP/SL check on open positions only, using Jupiter's own price
    (same source as swap execution) instead of waiting for the 5-min discovery cycle."""
    if not ENABLED:
        return

    async with _state_lock:
        state = load_state()
        positions = state.get("positions", {})
        if not positions:
            return

        mint_by_cg = {cg_id: p["mint"] for cg_id, p in positions.items() if p.get("mint")}
        prices = await get_jupiter_prices(list(mint_by_cg.values()))
        if not prices:
            return

        changed = False
        for cg_id, mint in mint_by_cg.items():
            curr = prices.get(mint)
            pos  = state["positions"].get(cg_id)
            if not curr or not pos:
                continue
            entry = float(pos.get("entry_price", 0))
            if entry <= 0:
                continue
            pct = (curr - entry) / entry * 100
            pos["current_price"] = curr
            pos["current_pct"]   = round(pct, 2)
            changed = True
            if pct >= TAKE_PROFIT_PCT:
                await sell_token(cg_id, f"TP +{pct:.1f}%", curr, state)
            elif pct <= -STOP_LOSS_PCT:
                await sell_token(cg_id, f"SL {pct:.1f}%", curr, state)

        if changed:
            save_state(state)

# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    _load_mint_cache()
    log.info(
        f"Shitcoin Hunter starting (dynamic discovery) — "
        f"${AMOUNT_USD}/trade | max {MAX_POSITIONS} pos | "
        f"TP +{TAKE_PROFIT_PCT}% | SL -{STOP_LOSS_PCT}% | DRY_RUN={DRY_RUN} | "
        f"position monitor every {MONITOR_SECONDS}s"
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_cycle, "interval", minutes=5, id="hunter_cycle",
                      max_instances=1, coalesce=True)
    scheduler.add_job(monitor_positions, "interval", seconds=MONITOR_SECONDS,
                      id="position_monitor", max_instances=1, coalesce=True)
    scheduler.start()

    await notifica(
        f"🦟 <b>SHITCOIN HUNTER started</b> (dynamic)\n"
        f"💰 ${AMOUNT_USD}/trade | max {MAX_POSITIONS} positions\n"
        f"🎯 TP +{TAKE_PROFIT_PCT}% | SL -{STOP_LOSS_PCT}%\n"
        f"🔍 Scanning top 100 Solana tokens | 5 min cycle\n"
        f"🔵 DRY_RUN: {DRY_RUN}"
    )

    await run_cycle()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info("Hunter stopped")


if __name__ == "__main__":
    asyncio.run(main())
