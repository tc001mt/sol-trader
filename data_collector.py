# data_collector.py
import os
import logging
import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
import httpx
import pandas as pd
import ta
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

load_dotenv()
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
NEWSAPI_BASE   = "https://newsapi.org/v2"
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
BINANCE_BASE   = "https://api.binance.com/api/v3"

BINANCE_SYMBOLS = {
    "SOL":  "SOLUSDT",
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "JUP":  "JUPUSDT",
    "BONK": "BONKUSDT",
    "WIF":  "WIFUSDT",
}

# CoinGecko IDs
TOKEN_IDS = {
    "SOL":  "solana",
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "JUP":  "jupiter-exchange-solana",
    "BONK": "bonk",
    "WIF":  "dogwifcoin",
    "USDC": "usd-coin",
}

# Crypto RSS feeds — all free, browser User-Agent to avoid blocks
RSS_FEEDS_CRYPTO = [
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
    "https://crypto.news/feed/",
    "https://www.newsbtc.com/feed/",
    "https://bitcoinmagazine.com/feed",            # corrected and working
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://beincrypto.com/feed/",
]

# Geopolitical RSS feeds — BBC, Sky, Al Jazeera, NDTV
RSS_FEEDS_GEO = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.skynews.com/feeds/rss/world.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://feeds.feedburner.com/ndtvnews-world-news",
]

# Keywords to filter news relevant to crypto
KEYWORDS_CRYPTO = [
    "bitcoin", "solana", "ethereum", "crypto", "btc", "sol", "eth",
    "defi", "sec", "etf", "regulation", "hack", "exchange",
    "fed", "trump", "tariff", "inflation", "rate", "blockchain",
    "binance", "coinbase", "stablecoin", "altcoin", "web3",
    "token", "wallet", "nft", "dao", "dex", "liquidity",
]

# Geopolitical keywords relevant to crypto markets
KEYWORDS_GEO = [
    "ukraine", "russia", "israel", "iran", "middle east", "war",
    "trump", "fed", "rate", "inflation", "economy", "sanction",
    "nato", "china", "taiwan", "oil", "energy", "ceasefire",
    "attack", "missile", "escalat", "negotiate", "peace",
    "dollar", "treasury", "gdp", "recession", "market",
]

# Specific keywords for Trump news
KEYWORDS_TRUMP = [
    "trump", "donald trump", "trump jr", "melania",
    "truth social", "maga", "tariff", "doge", "wlfi",
    "world liberty", "crypto president",
]

# ── Cache and Rate Limiting ───────────────────────────────────────────────────

# Global price cache with timestamp
_PRICE_CACHE = {}
_CACHE_TTL = timedelta(minutes=5)

# Technical indicators cache (market_chart CoinGecko — 5 min TTL)
_IND_CACHE: dict = {}
_IND_CACHE_TTL = timedelta(minutes=5)

# Binance futures cache (funding rate updates every 8h — 15 min cache)
_FUTURES_CACHE: dict = {}
_FUTURES_CACHE_TTL = timedelta(minutes=15)

BINANCE_FUTURES_BASE = "https://fapi.binance.com"
FUTURES_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}

# Rate limiting Binance
_BINANCE_LAST_CALL = None
_BINANCE_MIN_INTERVAL = timedelta(seconds=2)

# ── Prices ────────────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException))
)
async def _fetch_coingecko_prices(ids: str, headers: dict) -> dict:
    """Fetch prices from CoinGecko with automatic retry."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{COINGECKO_BASE}/simple/price",
            params={
                "ids": ids,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_24hr_vol": "true",
                "include_1hr_change": "true",
            },
            headers=headers
        )
        r.raise_for_status()
        return r.json()


async def _get_prezzi_binance(tokens: list) -> dict:
    """Binance fallback with rate limiting."""
    global _BINANCE_LAST_CALL
    
    # Rate limiting
    now = datetime.now(timezone.utc)
    if _BINANCE_LAST_CALL and (now - _BINANCE_LAST_CALL) < _BINANCE_MIN_INTERVAL:
        wait_time = (_BINANCE_MIN_INTERVAL - (now - _BINANCE_LAST_CALL)).total_seconds()
        await asyncio.sleep(wait_time)
    
    _BINANCE_LAST_CALL = datetime.now(timezone.utc)
    
    symbols = [BINANCE_SYMBOLS[t] for t in tokens if t in BINANCE_SYMBOLS]
    if not symbols:
        return {}
    try:
        import json as _json
        params = {"symbols": _json.dumps(symbols)}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{BINANCE_BASE}/ticker/24hr", params=params)
            r.raise_for_status()
            raw = r.json()
        reverse = {v: k for k, v in BINANCE_SYMBOLS.items()}
        result = {}
        for item in raw:
            symbol = reverse.get(item["symbol"])
            if symbol:
                result[symbol] = {
                    "price":      float(item["lastPrice"]),
                    "change_1h":  0,
                    "change_24h": round(float(item["priceChangePercent"]), 2),
                    "volume_24h": float(item["quoteVolume"]),
                }
        log.info(f"Binance prices OK: {list(result.keys())}")
        return result
    except Exception as e:
        log.error(f"Binance prices error: {e}")
        return {}


async def get_prezzi(tokens: list = None) -> dict:
    """Gets prices with local cache (TTL 5 minutes)."""
    global _PRICE_CACHE
    now = datetime.now(timezone.utc)
    
    # Check cache
    cache_key = ','.join(sorted(tokens or list(TOKEN_IDS.keys())))
    if cache_key in _PRICE_CACHE:
        cached_data, cached_time = _PRICE_CACHE[cache_key]
        if now - cached_time < _CACHE_TTL:
            age_seconds = (now - cached_time).seconds
            log.info(f"Prices from cache (age: {age_seconds}s)")
            return cached_data
    
    # Prepare CoinGecko request
    ids = ",".join(
        TOKEN_IDS[t] for t in (tokens or list(TOKEN_IDS.keys()))
        if t in TOKEN_IDS and t != "USDC"
    )
    
    headers = {}
    api_key = os.getenv("COINGECKO_API_KEY")
    if api_key:
        headers["x-cg-demo-api-key"] = api_key

    try:
        # Use function with automatic retry
        raw = await _fetch_coingecko_prices(ids, headers)

        result  = {}
        reverse = {v: k for k, v in TOKEN_IDS.items()}
        for cg_id, dati in raw.items():
            symbol = reverse.get(cg_id, cg_id.upper())
            result[symbol] = {
                "price":      dati.get("usd", 0),
                "change_1h":  round(dati.get("usd_1h_change", 0), 2),
                "change_24h": round(dati.get("usd_24h_change", 0), 2),
                "volume_24h": dati.get("usd_24h_vol", 0),
            }
        
        # Save to cache
        _PRICE_CACHE[cache_key] = (result, now)
        log.info(f"CoinGecko prices OK: {list(result.keys())}")
        return result

    except Exception as e:
        log.error(f"CoinGecko prices error: {e} — fallback Binance")
        target = [t for t in (tokens or list(TOKEN_IDS.keys())) if t != "USDC"]
        result = await _get_prezzi_binance(target)
        
        # Cache the Binance fallback too
        if result:
            _PRICE_CACHE[cache_key] = (result, now)
        
        return result


# ── Technical Indicators ──────────────────────────────────────────────────────

async def get_indicatori(token: str = "SOL", giorni: int = 30) -> dict:
    global _IND_CACHE
    now = datetime.now(timezone.utc)
    cache_key = f"{token}:{giorni}"

    # Return from cache if fresh (avoids CoinGecko rate limit with dashboard at 30s)
    if cache_key in _IND_CACHE:
        cached_data, cached_time = _IND_CACHE[cache_key]
        if now - cached_time < _IND_CACHE_TTL:
            return cached_data

    cg_id = TOKEN_IDS.get(token)
    if not cg_id:
        return {}

    headers = {}
    api_key = os.getenv("COINGECKO_API_KEY")
    if api_key:
        headers["x-cg-demo-api-key"] = api_key

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                f"{COINGECKO_BASE}/coins/{cg_id}/market_chart",
                params={"vs_currency": "usd", "days": giorni},
                headers=headers
            )
            r.raise_for_status()
            data = r.json()

        prices = [p[1] for p in data.get("prices", [])]
        if len(prices) < 26:
            return {}

        s    = pd.Series(prices)
        rsi  = ta.momentum.RSIIndicator(s, window=14).rsi().iloc[-1]
        macd = ta.trend.MACD(s)
        bb   = ta.volatility.BollingerBands(s, window=20)

        result = {
            "rsi":         round(float(rsi), 2),
            "macd":        round(float(macd.macd().iloc[-1]), 4),
            "macd_signal": round(float(macd.macd_signal().iloc[-1]), 4),
            "macd_cross":  "bullish" if macd.macd().iloc[-1] > macd.macd_signal().iloc[-1] else "bearish",
            "bb_upper":    round(float(bb.bollinger_hband().iloc[-1]), 4),
            "bb_lower":    round(float(bb.bollinger_lband().iloc[-1]), 4),
            "bb_pct":      round(float(bb.bollinger_pband().iloc[-1]), 4),
        }
        _IND_CACHE[cache_key] = (result, now)
        return result

    except Exception as e:
        log.error(f"Indicators {token} error: {e}")
        return {}


# ── Futures Binance (funding rate + open interest) ────────────────────────────

async def get_futures_data(tokens: list = None) -> dict:
    """
    Funding rate and open interest from Binance Futures (no API key).
    Funding rate: updates every 8h. Positive = longs pay shorts (long market).
    Signals:
      > +0.05%/8h  → overleveraged_long  (correction risk)
      +0.01/+0.05% → longs_dominant
      -0.01/+0.01% → neutral
      -0.05/-0.01% → shorts_dominant
      < -0.05%/8h  → short_squeeze_risk
    """
    global _FUTURES_CACHE
    now = datetime.now(timezone.utc)
    target_tokens = [t for t in (tokens or list(FUTURES_SYMBOLS.keys())) if t in FUTURES_SYMBOLS]
    cache_key = ",".join(sorted(target_tokens))

    if cache_key in _FUTURES_CACHE:
        cached_data, cached_time = _FUTURES_CACHE[cache_key]
        if now - cached_time < _FUTURES_CACHE_TTL:
            return cached_data

    result = {}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            for token in target_tokens:
                symbol = FUTURES_SYMBOLS[token]
                try:
                    fr_r, oi_r, ls_r = await asyncio.gather(
                        c.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/fundingRate",
                              params={"symbol": symbol, "limit": 1}),
                        c.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/openInterest",
                              params={"symbol": symbol}),
                        c.get(f"{BINANCE_FUTURES_BASE}/futures/data/globalLongShortAccountRatio",
                              params={"symbol": symbol, "period": "1h", "limit": 1}),
                    )
                    fr        = float(fr_r.json()[0]["fundingRate"]) * 100
                    oi        = float(oi_r.json()["openInterest"])
                    long_pct  = round(float(ls_r.json()[0]["longAccount"]) * 100, 1)
                    short_pct = round(100 - long_pct, 1)

                    if fr > 0.05:
                        signal = "overleveraged_long"
                    elif fr > 0.01:
                        signal = "longs_dominant"
                    elif fr < -0.05:
                        signal = "short_squeeze_risk"
                    elif fr < -0.01:
                        signal = "shorts_dominant"
                    else:
                        signal = "neutral"

                    result[token] = {
                        "funding_rate":  round(fr, 4),
                        "open_interest": round(oi, 0),
                        "long_pct":      long_pct,
                        "short_pct":     short_pct,
                        "signal":        signal,
                    }
                except Exception as e:
                    log.warning(f"Futures {token} error: {e}")

        if result:
            _FUTURES_CACHE[cache_key] = (result, now)
        log.info(f"Futures OK: { {k: v['signal'] for k,v in result.items()} }")
        return result

    except Exception as e:
        log.error(f"Futures data error: {e}")
        return {}


# ── Fear & Greed ──────────────────────────────────────────────────────────────

async def get_fear_greed() -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(FEAR_GREED_URL)
            r.raise_for_status()
            d = r.json()["data"][0]
            return {
                "value": int(d["value"]),
                "label": d["value_classification"],
            }
    except Exception as e:
        log.error(f"Fear&Greed error: {e}")
        return {"value": 50, "label": "Neutral"}


# ── RSS parser helper ─────────────────────────────────────────────────────────

async def _parse_rss(url: str, keywords: list, max_items: int = 5) -> list:
    """Downloads RSS feed and filters by keywords. Follows redirects automatically."""
    try:
        # Realistic browser User-Agent to avoid 403 blocks
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=True,
            max_redirects=5
        ) as c:
            r = await c.get(url, headers=headers)
            r.raise_for_status()

        root   = ET.fromstring(r.text)
        items  = root.findall(".//item")
        result = []
        fonte  = url.split("/")[2].replace("www.", "").replace("feeds.", "")

        for item in items:
            titolo = item.findtext("title", "").strip()
            data   = item.findtext("pubDate", "")
            if not titolo:
                continue
            if not any(k in titolo.lower() for k in keywords):
                continue

            result.append({
                "titolo": titolo,
                "fonte":  fonte,
                "data":   data,
            })

            if len(result) >= max_items:
                break

        return result

    except Exception as e:
        log.warning(f"RSS error {url}: {e}")
        return []

# ── Crypto news (RSS) ────────────────────────────────────────────────────────

async def get_notizie_crypto(token: str = None) -> list:
    """
    Crypto news from 8 free RSS feeds:
    CoinTelegraph, Decrypt, TheBlock, crypto.news,
    NewsBTC, Bitcoin Magazine, CoinDesk, BeInCrypto.
    """
    # If token specified, adds token keyword
    keywords = KEYWORDS_CRYPTO.copy()
    if token:
        keywords.append(token.lower())

    tasks   = [_parse_rss(url, keywords, 4) for url in RSS_FEEDS_CRYPTO]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    notizie = []
    seen    = set()
    for batch in results:
        if isinstance(batch, list):
            for n in batch:
                t = n.get("titolo", "")
                if t and t not in seen:
                    seen.add(t)
                    notizie.append(n)

    log.info(f"Crypto news: {len(notizie)} from RSS")
    return notizie[:15]


# ── Geopolitical news ────────────────────────────────────────────────────────

async def get_notizie_geo() -> list:
    """
    Geopolitical news from:
    1. NewsAPI if NEWSAPI_KEY available (more precise)
    2. Fallback: RSS BBC, Sky, Al Jazeera, NDTV
    """
    api_key = os.getenv("NEWSAPI_KEY")
    if api_key:
        try:
            query = (
                "ukraine OR russia OR \"middle east\" OR israel OR iran "
                "OR trump OR \"federal reserve\" OR inflation OR \"crypto regulation\""
            )
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    f"{NEWSAPI_BASE}/everything",
                    params={
                        "apiKey":   api_key,
                        "q":        query,
                        "language": "en",
                        "sortBy":   "publishedAt",
                        "pageSize": 15,
                    }
                )
                if r.status_code == 200:
                    articles = r.json().get("articles", [])
                    log.info(f"Geo news: {len(articles)} from NewsAPI")
                    return [
                        {
                            "titolo": a.get("title", ""),
                            "fonte":  a.get("source", {}).get("name", ""),
                            "data":   a.get("publishedAt", ""),
                        }
                        for a in articles[:15]
                        if a.get("title")
                    ]
        except Exception as e:
            log.warning(f"NewsAPI error: {e} — RSS fallback")

    # Fallback RSS
    tasks   = [_parse_rss(url, KEYWORDS_GEO, 5) for url in RSS_FEEDS_GEO]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    notizie = []
    seen    = set()
    for batch in results:
        if isinstance(batch, list):
            for n in batch:
                t = n.get("titolo", "")
                if t and t not in seen:
                    seen.add(t)
                    notizie.append(n)

    log.info(f"Geo news: {len(notizie)} from RSS")
    return notizie[:15]


# ── Trump tweets / news ──────────────────────────────────────────────────────

async def get_trump_tweets() -> list:
    """
    Trump news from:
    1. Twitter API if TWITTER_BEARER_TOKEN available
    2. Fallback: RSS crypto + geo filtered by Trump keywords
    """
    bearer = os.getenv("TWITTER_BEARER_TOKEN")
    if bearer:
        return await _trump_via_twitter_api(bearer)
    return await _trump_via_rss()


async def _trump_via_twitter_api(bearer: str) -> list:
    headers  = {"Authorization": f"Bearer {bearer}"}
    tweets   = []
    user_ids = {"realDonaldTrump": "25073877", "DonaldJTrumpJr": "939091"}

    async with httpx.AsyncClient(timeout=15) as c:
        for handle, uid in user_ids.items():
            try:
                r = await c.get(
                    f"https://api.twitter.com/2/users/{uid}/tweets",
                    headers=headers,
                    params={"max_results": 5, "tweet.fields": "created_at,text"}
                )
                if r.status_code == 200:
                    for t in r.json().get("data", []):
                        tweets.append({
                            "account": handle,
                            "testo":   t["text"],
                            "data":    t.get("created_at", ""),
                        })
            except Exception as e:
                log.error(f"Twitter {handle} error: {e}")
    return tweets


async def _trump_via_rss() -> list:
    """Trump news from all available RSS feeds."""
    all_feeds = RSS_FEEDS_CRYPTO + RSS_FEEDS_GEO
    tasks     = [_parse_rss(url, KEYWORDS_TRUMP, 3) for url in all_feeds]
    results   = await asyncio.gather(*tasks, return_exceptions=True)

    tweets = []
    seen   = set()
    for batch in results:
        if isinstance(batch, list):
            for n in batch:
                t = n.get("titolo", "")
                if t and t not in seen:
                    seen.add(t)
                    tweets.append({
                        "account": "news",
                        "testo":   t,
                        "data":    n.get("data", ""),
                    })

    log.info(f"Trump news: {len(tweets)} from RSS")
    return tweets[:8]


# ── Top Solana tokens ─────────────────────────────────────────────────────────

async def get_top_solana_tokens(limit: int = 10) -> list:
    headers = {}
    api_key = os.getenv("COINGECKO_API_KEY")
    if api_key:
        headers["x-cg-demo-api-key"] = api_key

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                f"{COINGECKO_BASE}/coins/markets",
                params={
                    "vs_currency":             "usd",
                    "category":                "solana-ecosystem",
                    "order":                   "volume_desc",
                    "per_page":                limit + 5,
                    "page":                    1,
                    "sparkline":               "false",
                    "price_change_percentage": "1h,24h",
                },
                headers=headers
            )
            r.raise_for_status()
            coins = r.json()

        tokens = []
        for c in coins:
            if (
                c.get("total_volume", 0) > 50_000
                and c.get("market_cap", 0) > 100_000
                and c.get("symbol", "").upper() not in ["USDC", "USDT"]
            ):
                tokens.append({
                    "symbol":     c["symbol"].upper(),
                    "cg_id":      c["id"],
                    "price":      c.get("current_price", 0),
                    "change_1h":  c.get("price_change_percentage_1h_in_currency", 0),
                    "change_24h": c.get("price_change_percentage_24h", 0),
                    "volume_24h": c.get("total_volume", 0),
                    "market_cap": c.get("market_cap", 0),
                })

        return tokens[:limit]

    except Exception as e:
        log.error(f"Top Solana tokens error: {e}")
        return []


# ── Solana Wallet ─────────────────────────────────────────────────────────────

async def get_wallet_balance(pubkey: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                "https://api.mainnet-beta.solana.com",
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method":  "getBalance",
                    "params":  [pubkey]
                }
            )
            sol_lamports = r.json().get("result", {}).get("value", 0)
            sol_balance  = sol_lamports / 1e9

        return {"SOL": round(sol_balance, 6), "pubkey": pubkey}

    except Exception as e:
        log.error(f"Wallet balance error: {e}")
        return {}


# ── Full collection (with sentiment analysis) ─────────────────────────────────────────

async def raccogli_tutto(token_list: list = None, pubkey: str = None) -> dict:
    """
    Main entry point — collects everything in parallel.
    Returns complete dict ready for claude_brain.py
    """
    tokens = token_list or ["SOL", "BTC", "ETH"]

    async def empty():
        return {}

    # Calculate indicators for all supported tokens in the list
    tokens_con_indicatori = [t for t in tokens if t in TOKEN_IDS and t not in ("USDC", "USDT")]

    base_results = await asyncio.gather(
        get_prezzi(tokens),
        get_fear_greed(),
        get_notizie_crypto(),
        get_notizie_geo(),
        get_trump_tweets(),
        get_top_solana_tokens(10),
        get_futures_data(tokens_con_indicatori),
        *[get_indicatori(t) for t in tokens_con_indicatori],
        return_exceptions=True
    )

    prezzi, fear_greed, notizie_crypto, notizie_geo, trump_tweets, top_solana, futures = base_results[:7]
    ind_results = base_results[7:]

    wallet = {}  # loaded by caller via trader.get_wallet_completo() (RPC Helius)

    def safe(v, default):
        return default if isinstance(v, Exception) else v

    indicatori = {t: safe(ind, {}) for t, ind in zip(tokens_con_indicatori, ind_results)}

    # ── Sentiment analysis — only if enabled and not in Gunicorn ────────────
    sentiment_crypto = {}
    sentiment_geo    = {}
    sentiment_trump  = {}

    if os.getenv("ENABLE_SENTIMENT", "false").lower() == "true":
        is_crypto_ok = not isinstance(notizie_crypto, Exception)
        is_geo_ok    = not isinstance(notizie_geo, Exception)
        is_trump_ok  = not isinstance(trump_tweets, Exception)

        try:
            from sentiment_analyzer import get_aggregate_sentiment
            if is_crypto_ok and notizie_crypto:
                sentiment_crypto = await get_aggregate_sentiment(notizie_crypto[:25])
            if is_geo_ok and notizie_geo:
                sentiment_geo = await get_aggregate_sentiment(notizie_geo[:25])
            if is_trump_ok and trump_tweets:
                sentiment_trump = await get_aggregate_sentiment(trump_tweets[:15])
        except Exception as e:
            log.warning(f"Sentiment not available: {e}")

    # ── Final output ──────────────────────────────────────────────────────────
    return {
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "prezzi":         safe(prezzi, {}),
        "fear_greed":     safe(fear_greed, {"value": 50, "label": "Neutral"}),
        "notizie_crypto": safe(notizie_crypto, []),
        "notizie_geo":    safe(notizie_geo, []),
        "trump_tweets":   safe(trump_tweets, []),
        "top_solana":     safe(top_solana, []),
        "wallet":         safe(wallet, {}),
        "indicatori":     indicatori,
        "futures":        safe(futures, {}),
        "sentiment": {
            "crypto": sentiment_crypto,
            "geo":    sentiment_geo,
            "trump":  sentiment_trump,
        },
    }

# ── Test ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)

    async def test():
        print("Data collection in progress...\n")
        dati = await raccogli_tutto(["SOL", "BTC", "ETH"])

        print(f"Prices:         {list(dati['prezzi'].keys())}")
        print(f"Fear&Greed:     {dati['fear_greed']}")
        print(f"Crypto news:    {len(dati['notizie_crypto'])}")
        for n in dati['notizie_crypto'][:3]:
            print(f"  [{n['fonte']}] {n['titolo'][:80]}")
        print(f"Geo news:       {len(dati['notizie_geo'])}")
        for n in dati['notizie_geo'][:3]:
            print(f"  [{n['fonte']}] {n['titolo'][:80]}")
        print(f"Trump news:     {len(dati['trump_tweets'])}")
        for t in dati['trump_tweets'][:3]:
            print(f"  [{t['account']}] {t['testo'][:80]}")
        print(f"Top Solana:     {len(dati['top_solana'])} tokens")
        print(f"Indicators SOL: {dati['indicatori']['SOL']}")
        print(f"Indicators BTC: {dati['indicatori']['BTC']}")
        if 'sentiment' in dati:
            print(f"Sentiment crypto: {dati['sentiment']['crypto'].get('avg_score')}")

    asyncio.run(test())