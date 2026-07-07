# trader.py
import os
import json
import logging
import asyncio
import base64
import httpx
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"   # v1, kept for get_quote()
JUPITER_SWAP_URL  = "https://api.jup.ag/swap/v1/swap"    # v1, kept for reference
JUPITER_ORDER_URL = "https://api.jup.ag/swap/v2/order"   # v2: quote+swap in one call
RPC_URL = os.getenv("SOL_TRADING_SOLANA_RPC", "https://api.mainnet-beta.solana.com")

# Jupiter Referral Program account (referral.jup.ag/dashboard-ultra) — earns a share
# of swap fees. Must be registered under project DkiqsTrw1u1bYFumumC7sCG2S8K25qc2vemJFHyW2wJc
# (the Ultra project /swap/v2/order expects) — verified on-chain and live-tested
# across USDC<->SOL, USDC->ETH, USDC->USDT, USDC->BTC.
JUPITER_REFERRAL_ACCOUNT = "7H4bLxfkAsqBSU5ZJn9aPrzUjz7pJWpcogUfUcRDD32i"

MINTS = {
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "SOL":  "So11111111111111111111111111111111111111112",
    "BTC":  "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
    "ETH":  "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
    "JUP":  "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF":  "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "PENGU": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
}

DECIMALS = {
    "USDC": 6, "USDT": 6, "SOL": 9,
    "BTC": 8, "ETH": 8, "JUP": 6,
    "BONK": 5, "WIF": 6, "PENGU": 6,
}

DRY_RUN = os.getenv("SOL_TRADING_DRY_RUN", "true").lower() == "true"

def _load_keypair_from_env():
    try:
        from solders.keypair import Keypair
        raw = os.getenv("SOL_TRADING_SOLANA_PRIVATE_KEY", "")
        if not raw:
            raise ValueError("SOL_TRADING_SOLANA_PRIVATE_KEY not set")
        if raw.startswith("["):
            secret = bytes(json.loads(raw))
            return Keypair.from_bytes(secret)
        else:
            return Keypair.from_base58_string(raw)
    except Exception as e:
        log.error(f"Keypair error: {e}")
        return None

# Loaded once at import time — must NOT be re-read via os.getenv() on every call,
# so that mutating os.environ later in the process (e.g. another module doing its
# own load_dotenv with override=True) can never swap in the wrong wallet's key.
_KEYPAIR = _load_keypair_from_env()

def load_keypair():
    return _KEYPAIR

def get_pubkey() -> str:
    kp = load_keypair()
    return str(kp.pubkey()) if kp else ""

async def get_balance_sol() -> float:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(RPC_URL, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance",
                "params": [get_pubkey()]
            })
            return r.json()["result"]["value"] / 1e9
    except Exception as e:
        log.error(f"SOL balance error: {e}")
        return 0.0

async def get_balance_token(mint: str) -> float:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(RPC_URL, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [get_pubkey(), {"mint": mint}, {"encoding": "jsonParsed"}]
            })
            accounts = r.json()["result"]["value"]
            if not accounts:
                return 0.0
            info = accounts[0]["account"]["data"]["parsed"]["info"]
            return float(info["tokenAmount"]["uiAmount"] or 0)
    except Exception as e:
        log.error(f"Token balance error {mint}: {e}")
        return 0.0

async def get_wallet_completo() -> dict:
    pubkey = get_pubkey()
    if not pubkey:
        return {}
    token_volatili = [t for t in MINTS if t not in ("USDC", "USDT", "SOL")]
    risultati = await asyncio.gather(
        get_balance_sol(),
        get_balance_token(MINTS["USDC"]),
        get_balance_token(MINTS["USDT"]),
        *[get_balance_token(MINTS[t]) for t in token_volatili],
    )
    sol, usdc, usdt = risultati[0], risultati[1], risultati[2]
    result = {
        "pubkey": pubkey,
        "SOL":  round(sol,  6),
        "USDC": round(usdc, 2),
        "USDT": round(usdt, 2),
    }
    for i, token in enumerate(token_volatili):
        bal = risultati[3 + i]
        if bal > 0:
            result[token] = round(bal, 8)
    return result

async def get_quote(token_in: str, token_out: str, amount_in: float, slippage_bps: int = 150) -> dict:
    mint_in = MINTS.get(token_in)
    mint_out = MINTS.get(token_out)
    if not mint_in or not mint_out:
        raise ValueError(f"Unknown mint: {token_in} or {token_out}")

    dec_in = DECIMALS.get(token_in, 6)
    amount_raw = int(amount_in * (10 ** dec_in))

    params = {
        "inputMint": mint_in,
        "outputMint": mint_out,
        "amount": amount_raw,
        "slippageBps": slippage_bps,
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(JUPITER_QUOTE_URL, params=params)
        r.raise_for_status()
        quote = r.json()

    dec_out = DECIMALS.get(token_out, 6)
    amount_out = int(quote["outAmount"]) / (10 ** dec_out)
    price_impact = float(quote.get("priceImpact", 0))
    log.info(f"Quote: {amount_in} {token_in} → {amount_out:.6f} {token_out} impact={price_impact:.3f}%")
    return {
        "quote": quote,
        "amount_out": amount_out,
        "price_impact": price_impact,
    }

async def esegui_swap(token_in: str, token_out: str, amount_in: float, slippage_bps: int = 150) -> dict:
    if DRY_RUN:
        log.info(f"[DRY RUN] Swap {amount_in} {token_in} → {token_out}")
        return {
            "success": True,
            "dry_run": True,
            "token_in": token_in,
            "token_out": token_out,
            "amount_in": amount_in,
            "tx_hash": "DRY_RUN_NO_TX",
        }

    kp = load_keypair()
    if not kp:
        return {"success": False, "error": "Keypair not available"}

    mint_in  = MINTS.get(token_in)
    mint_out = MINTS.get(token_out)
    if not mint_in or not mint_out:
        return {"success": False, "error": f"Unknown token: {token_in} or {token_out}"}

    dec_in     = DECIMALS.get(token_in, 6)
    amount_raw = int(amount_in * (10 ** dec_in))

    # Jupiter v2: quote + swap in a single GET call
    params: dict = {
        "inputMint":        mint_in,
        "outputMint":       mint_out,
        "amount":           amount_raw,
        "taker":            str(kp.pubkey()),
        "slippageBps":      slippage_bps,
        # Cap priority fee instead of leaving it fully automatic (v1 had priorityLevelWithMaxLamports.maxLamports=500_000)
        "priorityFeeLamports": 500_000,
        "broadcastFeeType":    "maxCap",
    }
    params["referralAccount"] = JUPITER_REFERRAL_ACCOUNT
    params["referralFee"]     = int(os.getenv("SOL_TRADING_JUPITER_REFERRAL_FEE_BPS", "50"))

    # Prefer a normal AMM route over JupiterZ (RFQ): RFQ quotes carry their own
    # short expiry (~53s observed) on top of blockhash validity — 5 of 6 SOL
    # buys on 2026-07-07 timed out specifically on jupiterz/rfq routes, the
    # one that succeeded was metis. Only fall back to allowing JupiterZ if no
    # route exists without it (thin/exotic pairs), so we never block a trade
    # that would otherwise be possible.
    params["excludeRouters"] = "jupiterz"

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(JUPITER_ORDER_URL, params=params)
            if not r.is_success:
                log.error(f"Jupiter order error: {r.text[:500]}")
            r.raise_for_status()
            order = r.json()
    except Exception as e:
        return {"success": False, "error": f"Order error: {e}"}

    if order.get("error") or not order.get("transaction"):
        log.warning(f"No route excluding JupiterZ ({order.get('error')}) — retrying with it allowed")
        params.pop("excludeRouters", None)
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(JUPITER_ORDER_URL, params=params)
                r.raise_for_status()
                order = r.json()
        except Exception as e:
            return {"success": False, "error": f"Order error (retry): {e}"}

    if order.get("errorCode"):
        return {"success": False, "error": f"Jupiter: {order.get('errorMessage', order['errorCode'])}"}

    # v2 "priceImpact" is already a percentage (-1.23 = -1.23%); "priceImpactPct" is the decimal form.
    price_impact = abs(float(order.get("priceImpact", 0)))
    if price_impact > 2.0:
        return {"success": False, "error": f"Price impact too high: {price_impact:.2f}%"}

    tx_b64 = order.get("transaction")
    if not tx_b64:
        return {"success": False, "error": "No transaction in order response"}

    dec_out    = DECIMALS.get(token_out, 6)
    amount_out = int(order.get("outAmount", 0)) / (10 ** dec_out)
    log.info(f"Order: {amount_in} {token_in} → {amount_out:.6f} {token_out} | impact={price_impact:.3f}%")
    log.info(
        f"Order meta: router={order.get('router')} swapType={order.get('swapType')} "
        f"expireAt={order.get('expireAt')} lastValidBlockHeight={order.get('lastValidBlockHeight')} "
        f"prioritizationFeeLamports={order.get('prioritizationFeeLamports')}"
    )

    from solders.transaction import VersionedTransaction
    from solders import message as solders_message

    raw_tx    = base64.b64decode(tx_b64)
    tx        = VersionedTransaction.from_bytes(raw_tx)
    signature = kp.sign_message(solders_message.to_bytes_versioned(tx.message))
    signed_tx = VersionedTransaction.populate(tx.message, [signature])
    signed_b64 = base64.b64encode(bytes(signed_tx)).decode()

    # maxRetries=0: the RPC node's own built-in rebroadcast is very limited
    # (a handful of attempts over a few seconds) — we rebroadcast ourselves
    # below, for the whole blockhash validity window, since a single narrow
    # burst of retries is not enough during any real network congestion.
    send_params = {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}

    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(RPC_URL, json={
            "jsonrpc": "2.0", "id": 1,
            "method":  "sendTransaction",
            "params":  [signed_b64, send_params]
        })
        result = r.json()

    if "error" in result:
        log.error(f"sendTransaction error: {result['error']}")
        return {"success": False, "error": f"RPC error: {result['error']}"}

    txid = result["result"]
    log.info(f"Tx sent: {txid}. Waiting for confirmation...")

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
                        "params":  [signed_b64, send_params]
                    })
                    rb_json = rb.json()
                    if "error" in rb_json:
                        log.warning(f"Rebroadcast #{i} error: {rb_json['error']}")
            except Exception as e:
                log.warning(f"Rebroadcast #{i} failed (non-fatal): {e}")

        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(RPC_URL, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "getSignatureStatuses",
                "params":  [[txid], {"searchTransactionHistory": True}]
            })
            status = r.json().get("result", {}).get("value", [None])[0]
            if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
                if status.get("err"):
                    log.error(f"Tx executed with error: {status['err']}")
                    return {"success": False, "error": f"Execution error: {status['err']}"}
                log.info(f"Swap confirmed! txid={txid}")
                return {
                    "success": True,
                    "dry_run": False,
                    "token_in":   token_in,
                    "token_out":  token_out,
                    "amount_in":  amount_in,
                    "amount_out": amount_out,
                    "tx_hash":    txid,
                }

    log.error(f"Timeout: tx {txid} not confirmed")
    return {"success": False, "error": "Transaction not confirmed in time"}

async def rifugio_usdc() -> list:
    wallet = await get_wallet_completo()
    results = []
    token_volatili = ["SOL", "BTC", "ETH", "JUP", "BONK", "WIF", "LINK", "PENGU"]
    for token in token_volatili:
        saldo = wallet.get(token, 0)
        if token == "SOL":
            saldo = max(0, saldo - 0.05)
        if saldo > 0:
            log.info(f"Refuge: converting {saldo} {token} → USDC")
            result = await esegui_swap(token, "USDC", saldo)
            results.append(result)
            await asyncio.sleep(2)
    return results

async def esegui_decisione(decisione: dict, dati_mercato: dict) -> dict:
    from database import salva_trade, aggiorna_memoria_compressa
    azione = decisione.get("azione")
    token = decisione.get("token")
    importo = decisione.get("importo_usdc", 0)
    confidenza = decisione.get("confidenza", 0)
    prezzi = dati_mercato.get("prezzi", {})
    if azione == "aspetta":
        trade_id = salva_trade({
            "token": token or "N/A",
            "azione": "aspetta",
            "importo_usdc": 0,
            "prezzo_entrata": 0,
            "motivazione": decisione.get("motivazione"),
            "confidenza": confidenza,
            "livello_allerta": decisione.get("livello_allerta", "VERDE"),
            "fear_greed": dati_mercato.get("fear_greed", {}).get("value"),
            "sentiment": {"trump": decisione.get("sentiment_trump"), "geo": decisione.get("sentiment_geo")},
            "eseguito": False,
        })
        return {"success": True, "azione": "aspetta", "trade_id": trade_id}
    if azione == "rifugio":
        log.warning("REFUGE ACTIVATED — converting everything to USDC")
        results = await rifugio_usdc()
        salva_trade({
            "token": "USDC",
            "azione": "rifugio",
            "importo_usdc": 0,
            "prezzo_entrata": 1.0,
            "motivazione": decisione.get("motivazione"),
            "confidenza": 100,
            "livello_allerta": "ROSSO",
            "eseguito": True,
        })
        return {"success": True, "azione": "rifugio", "results": results}
    MIN_TRADE_USD = 1.0

    if azione == "compra":
        if not token or importo <= 0:
            return {"success": False, "error": "Token or amount missing"}
        wallet = await get_wallet_completo()
        usdc_disp = wallet.get("USDC", 0)
        if usdc_disp < MIN_TRADE_USD:
            return {"success": False, "error": f"Available USDC (${usdc_disp:.2f}) below minimum ${MIN_TRADE_USD}"}
        if importo < MIN_TRADE_USD:
            importo = MIN_TRADE_USD
            log.info(f"Calculated amount below minimum — using ${MIN_TRADE_USD}")
        # Intentional: with remaining USDC <=1.5 we spend ALL instead of stopping
        # at $1.0, to avoid leaving unused USDC dust. Do not replace with
        # a fixed value.
        if usdc_disp <= 1.5:
            importo = usdc_disp
            log.info(f"Low remaining USDC (${usdc_disp:.2f}) — using all")
        importo = min(importo, usdc_disp)
        prezzo = prezzi.get(token, {}).get("price", 0)
        result = await esegui_swap("USDC", token, importo)
        trade_id = salva_trade({
            "token": token,
            "azione": "compra",
            "importo_usdc": importo,
            "prezzo_entrata": prezzo,
            "motivazione": decisione.get("motivazione"),
            "confidenza": confidenza,
            "livello_allerta": decisione.get("livello_allerta", "VERDE"),
            "fear_greed": dati_mercato.get("fear_greed", {}).get("value"),
            "sentiment": {"trump": decisione.get("sentiment_trump"), "geo": decisione.get("sentiment_geo")},
            "tx_hash": result.get("tx_hash"),
            "eseguito": result.get("success", False),
        })
        aggiorna_memoria_compressa()
        return {**result, "trade_id": trade_id, "token": token, "importo": importo}
    if azione == "vendi":
        if not token:
            return {"success": False, "error": "Token missing"}
        wallet = await get_wallet_completo()
        saldo = wallet.get(token, 0)
        prezzo = prezzi.get(token, {}).get("price", 0)
        # Intentional: double reserve for SOL — 0.005 for small positions
        # (<=$1.5, sell almost all), 0.01 for normal positions (wider margin).
        # Do not merge into a single constant.
        # Intentional: the sell must NEVER be blocked by a missing/zero price —
        # price is only used to choose which reserve to apply,
        # never as a precondition that prevents the swap (this caused a real
        # sell outage for hours when the price was unavailable).
        if token == "SOL":
            SOL_FEE_RESERVE = 0.005
            valore_sol = saldo * prezzo if prezzo else 0
            if valore_sol <= 1.5:
                saldo = max(0, saldo - SOL_FEE_RESERVE)
                log.info(f"Low remaining SOL (${valore_sol:.2f}) — selling all, reserving {SOL_FEE_RESERVE} SOL for fees")
            else:
                saldo = max(0, saldo - 0.01)
        if saldo <= 0:
            return {"success": False, "error": f"No {token} in wallet"}

        # Optional partial sell (e.g. scheduler's SELL OVERRIDE, which takes profit
        # on half a position). A dedicated field, never inferred from Claude's own
        # importo_usdc on "vendi" decisions — that number is informational only and
        # was never guaranteed to reflect an intended sell amount, so respecting it
        # here would silently turn ordinary full-position sells into partial ones.
        frazione = decisione.get("vendi_frazione", 1.0)
        if frazione < 1.0:
            saldo = round(saldo * frazione, 9)
            if saldo <= 0:
                return {"success": False, "error": f"No {token} in wallet after applying vendi_frazione"}

        # Lookup buy price to compute realized P&L
        from database import get_session, Trade as TradeModel
        prezzo_entrata_storico = 0.0
        with get_session() as s:
            ultimo_compra = (
                s.query(TradeModel)
                .filter(
                    TradeModel.token == token,
                    TradeModel.azione == "compra",
                    TradeModel.eseguito == True,
                    TradeModel.prezzo_entrata > 0,
                )
                .order_by(TradeModel.data.desc())
                .first()
            )
            if ultimo_compra:
                prezzo_entrata_storico = float(ultimo_compra.prezzo_entrata or 0)
        risultato_pct = round(
            (prezzo - prezzo_entrata_storico) / prezzo_entrata_storico * 100, 4
        ) if prezzo_entrata_storico and prezzo else 0
        risultato_usdc = round((prezzo - prezzo_entrata_storico) * saldo, 4) if prezzo_entrata_storico and prezzo else 0

        result = await esegui_swap(token, "USDC", saldo)
        trade_id = salva_trade({
            "token": token,
            "azione": "vendi",
            "importo_usdc": saldo * prezzo,
            "prezzo_entrata": prezzo_entrata_storico or prezzo,
            "prezzo_uscita": prezzo,
            "risultato_pct": risultato_pct,
            "risultato_usdc": risultato_usdc,
            "motivazione": decisione.get("motivazione"),
            "confidenza": confidenza,
            "livello_allerta": decisione.get("livello_allerta", "VERDE"),
            "fear_greed": dati_mercato.get("fear_greed", {}).get("value"),
            "tx_hash": result.get("tx_hash"),
            "eseguito": result.get("success", False),
        })
        aggiorna_memoria_compressa()
        return {**result, "trade_id": trade_id}
    return {"success": False, "error": f"Unknown action: {azione}"}

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    async def test():
        print(f"DRY_RUN: {DRY_RUN}")
        print(f"Pubkey: {get_pubkey() or 'not configured'}")
        try:
            q = await get_quote("USDC", "SOL", 10)
            print(f"Quote 10 USDC → SOL: {q['amount_out']:.6f} SOL")
        except Exception as e:
            print(f"Error: {e}")
    asyncio.run(test())