# scheduler.py
import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

CYCLE_MINUTES = int(os.getenv("SOL_TRADING_CYCLE_MINUTES", 15))

# Minimum SOL threshold for executing purchases — never go below 0.009 SOL.
# Covers: wSOL ATA rent (~0.002) + priority fee (~0.0005) + safety buffer.
MIN_SOL_PER_COMPRA = 0.009
_ultima_notifica_sol_basso: datetime | None = None
_ultima_notifica_errore_ai: datetime | None = None

# ── Telegram notifications ────────────────────────────────────────────────────────

async def notifica(msg: str):
    token   = os.getenv("SOL_TRADING_TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("SOL_TRADING_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        import httpx
        msg = msg[:4000]
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}
            )
            if r.status_code != 200:
                log.error(f"Telegram error: {r.text}")
    except Exception as e:
        log.error(f"Telegram error: {e}")


# ── Update open trade P&L ─────────────────────────────────────────────────

async def aggiorna_trade_aperti(wallet: dict, prezzi: dict):
    """
    Updates current P&L for open positions (last trade = buy).
    Called at each cycle before the decision.
    Uses get_posizioni_aperte() to find only genuinely open positions
    (avoids freezing P&L after the first update).
    """
    try:
        from database import get_session, Trade, get_posizioni_aperte

        posizioni = get_posizioni_aperte()
        if not posizioni:
            return

        with get_session() as s:
            for pos in posizioni:
                token          = pos["token"]
                prezzo_entrata = pos["prezzo_entrata"]
                prezzo_attuale = prezzi.get(token, {}).get("price", 0)

                if not prezzo_attuale or not prezzo_entrata:
                    continue

                # Find the most recent buy trade for this token
                ultimo = (
                    s.query(Trade)
                    .filter(
                        Trade.token == token,
                        Trade.azione == "compra",
                        Trade.eseguito == True,
                        Trade.prezzo_entrata > 0,
                    )
                    .order_by(Trade.data.desc())
                    .first()
                )
                if not ultimo:
                    continue

                risultato_pct  = round(
                    (prezzo_attuale - prezzo_entrata) / prezzo_entrata * 100, 4
                )
                risultato_usdc = round(
                    float(ultimo.importo_usdc or 0) * risultato_pct / 100, 4
                )
                ultimo.prezzo_uscita  = prezzo_attuale
                ultimo.risultato_pct  = risultato_pct
                ultimo.risultato_usdc = risultato_usdc
                log.info(
                    f"Trade #{ultimo.id} {token} updated: "
                    f"entry=${prezzo_entrata} current=${prezzo_attuale} "
                    f"P&L={risultato_pct:+.2f}% (${risultato_usdc:+.4f})"
                )

    except Exception as e:
        log.error(f"aggiorna_trade_aperti error: {e}")


# ── Automatic take profit ───────────────────────────────────────────────────

async def controlla_take_profit(wallet: dict, prezzi: dict) -> list:
    """
    Automatically sells tokens that have reached the take profit target.
    TAKE_PROFIT_PCT: env var (default 1.5%). Returns list of sold tokens.
    """
    take_profit_pct = float(os.getenv("SOL_TRADING_TAKE_PROFIT_PCT", "1.5"))
    venduti = []

    try:
        from database import get_session, Trade
        from sqlalchemy import and_
        from trader import esegui_decisione

        with get_session() as s:
            token_volatili = [k for k in wallet if k not in ("pubkey", "USDC", "USDT")]
            for token in token_volatili:
                saldo = wallet.get(token, 0)
                if token == "SOL":
                    # Don't sell SOL if it would bring us below the minimum fee threshold
                    if saldo <= MIN_SOL_PER_COMPRA:
                        log.info(f"Take profit SOL skipped: balance {saldo:.5f} <= fee threshold {MIN_SOL_PER_COMPRA}")
                        continue
                    saldo = max(0, saldo - MIN_SOL_PER_COMPRA)
                if saldo <= 0.0001:
                    continue

                ultimo = s.query(Trade).filter(
                    and_(
                        Trade.token == token,
                        Trade.azione == "compra",
                        Trade.eseguito == True,
                        Trade.prezzo_entrata > 0,
                    )
                ).order_by(Trade.data.desc()).first()

                if not ultimo:
                    continue

                prezzo_entrata = float(ultimo.prezzo_entrata)
                prezzo_attuale = prezzi.get(token, {}).get("price", 0)

                if not prezzo_attuale or not prezzo_entrata:
                    continue

                pct = (prezzo_attuale - prezzo_entrata) / prezzo_entrata * 100

                if pct >= take_profit_pct:
                    log.warning(
                        f"TAKE PROFIT {token}: +{pct:.2f}% >= {take_profit_pct}% "
                        f"(entry=${prezzo_entrata} current=${prezzo_attuale}) — selling {saldo}"
                    )
                    decisione_tp = {
                        "azione":          "vendi",
                        "token":           token,
                        "importo_usdc":    round(saldo * prezzo_attuale, 2),
                        "confidenza":      90,
                        "motivazione":     f"Take profit automatico: +{pct:.2f}% (target {take_profit_pct}%)",
                        "livello_allerta": "VERDE",
                        "sentiment_trump": "neutral",
                        "sentiment_geo":   "neutral",
                    }
                    result = await esegui_decisione(decisione_tp, {"prezzi": prezzi})
                    if result.get("success"):
                        venduti.append(token)
                        await notifica(
                            f"💰 <b>TAKE PROFIT {token}</b>\n"
                            f"Gain: +{pct:.2f}%\n"
                            f"Target: {take_profit_pct}%\n"
                            f"✅ TX: {str(result.get('tx_hash', ''))[:30]}"
                        )
                    else:
                        log.error(f"Take profit failed {token}: {result.get('error')}")

    except Exception as e:
        log.error(f"controlla_take_profit error: {e}")

    return venduti


# ── Main cycle ──────────────────────────────────────────────────────────

async def ciclo_trading():
    """Executed every CYCLE_MINUTES minutes."""
    log.info(f"=== CICLO TRADING {datetime.now(timezone.utc).isoformat()} ===")

    try:
        from data_collector import raccogli_tutto
        from claude_brain   import ciclo_decisionale
        from trader         import esegui_decisione, get_pubkey, get_wallet_completo
        from database       import get_regole_attive, get_ultimi_trade

        regole = get_regole_attive()
        tokens = regole.get("token_core", ["SOL", "BTC", "ETH"])

        # 1. Collect data
        log.info("Collecting data...")
        pubkey = get_pubkey()
        wallet = await get_wallet_completo()
        wallet_log = " ".join(
            f"{k}={v:.5f}" if isinstance(v, float) and v < 1 else f"{k}={v}"
            for k, v in wallet.items()
            if k != "pubkey" and isinstance(v, (int, float)) and v > 0
        )
        log.info(f"Wallet: {wallet_log}")

        # Warn if SOL is too low to pay swap fees (max 1 notification every 4h)
        global _ultima_notifica_sol_basso
        sol_disponibile = wallet.get("SOL", 0)
        if sol_disponibile < MIN_SOL_PER_COMPRA:
            ora = datetime.now(timezone.utc)
            if _ultima_notifica_sol_basso is None or (ora - _ultima_notifica_sol_basso) > timedelta(hours=4):
                _ultima_notifica_sol_basso = ora
                await notifica(
                    f"⚠️ <b>INSUFFICIENT SOL FOR FEES</b>\n"
                    f"SOL balance: {sol_disponibile:.5f} SOL\n"
                    f"Minimum required: {MIN_SOL_PER_COMPRA} SOL\n"
                    f"Purchases blocked (except SOL buy). Send at least 0.05 SOL to the wallet to resume."
                )
            log.warning(f"SOL {sol_disponibile:.5f} < {MIN_SOL_PER_COMPRA} — purchases blocked this session")

        # Add tokens already in the wallet to the collection set (in addition to token_core)
        tokens_wallet = [
            t for t in wallet
            if t not in ("pubkey", "SOL", "USDC", "USDT")
            and isinstance(wallet.get(t), (int, float))
            and wallet[t] > 0
        ]
        tokens_da_raccogliere = list(set(tokens + tokens_wallet))

        dati = await raccogli_tutto(tokens_da_raccogliere, pubkey=pubkey)
        dati["wallet"] = wallet

        # 2. Update open trade P&L with fresh prices
        await aggiorna_trade_aperti(wallet, dati.get("prezzi", {}))

        # 2b. Automatic take profit — sells if target is reached
        venduti = await controlla_take_profit(wallet, dati.get("prezzi", {}))
        if venduti:
            wallet = await get_wallet_completo()
            dati["wallet"] = wallet

        # 3. Claude decides
        log.info("Claude analizza...")
        result    = await ciclo_decisionale(dati)
        sicurezza = result["sicurezza"]
        decisione = result["decisione"]
        livello   = result["livello"]

        # AI decision call failed (e.g. OpenRouter out of credits) — this is NOT
        # a real "aspetta" choice, alert loudly instead of silently drifting for
        # hours indistinguishable from normal caution (happened 2026-07-08: 4h,
        # 12 cycles, before anyone noticed the bot wasn't deciding anything).
        global _ultima_notifica_errore_ai
        if decisione.get("errore_tecnico"):
            log.error(f"⚠️ AI DECISION FAILED — not a real decision: {decisione.get('motivazione')}")
            ora = datetime.now(timezone.utc)
            if _ultima_notifica_errore_ai is None or (ora - _ultima_notifica_errore_ai) > timedelta(hours=1):
                _ultima_notifica_errore_ai = ora
                await notifica(
                    f"🔴 <b>AI DECISION FAILED</b>\n"
                    f"{decisione.get('motivazione', '')[:200]}\n"
                    f"The bot is NOT evaluating trades this cycle — check OpenRouter credits/API status."
                )

        # ── Data for override ─────────────────────────────────────────────────
        usdc_balance = wallet.get("USDC", 0)
        sol_balance  = wallet.get("SOL", 0)
        sol_price    = dati.get("prezzi", {}).get("SOL", {}).get("price", 0)
        sol_valore   = sol_balance * sol_price if sol_price else 0
        ind_sol      = dati.get("indicatori", {}).get("SOL", {})
        rsi          = float(ind_sol.get("rsi", 50) or 50)
        macd_cross   = ind_sol.get("macd_cross", "neutral") or "neutral"

        # 1h and 24h changes for flash crash detection
        prezzi_dati  = dati.get("prezzi", {})
        btc_1h  = float(prezzi_dati.get("BTC", {}).get("change_1h",  0) or 0)
        btc_24h = float(prezzi_dati.get("BTC", {}).get("change_24h", 0) or 0)
        sol_1h  = float(prezzi_dati.get("SOL", {}).get("change_1h",  0) or 0)
        sol_24h = float(prezzi_dati.get("SOL", {}).get("change_24h", 0) or 0)

        # Flash crash: strong 1h drop but 24h still moderate → spike, not trend
        is_flash_crash = (
            (btc_1h < -8 and btc_24h > -5) or
            (sol_1h < -8 and sol_24h > -5)
        )

        log.info(
            f"Portfolio: SOL=${sol_valore:.2f} USDC=${usdc_balance:.2f} "
            f"| RSI={rsi:.1f} MACD={macd_cross} | Level={livello}"
            + (f" | ⚡ FLASH CRASH DETECTED (1h>>24h)" if is_flash_crash else "")
        )

        # ── Active portfolio management ───────────────────────────────────────

        # REFUGE: RED level and SOL in portfolio
        # Flash crash guard: if change_1h crashes but change_24h is moderate → don't sell at the bottom
        if (
            livello == "ROSSO"
            and sol_valore > 5
            and decisione.get("azione") != "rifugio"
        ):
            if is_flash_crash:
                log.warning(
                    f"⚡ FLASH CRASH GUARD: RED level but 1h>>24h "
                    f"(BTC 1h={btc_1h:.1f}% 24h={btc_24h:.1f}% | SOL 1h={sol_1h:.1f}% 24h={sol_24h:.1f}%) "
                    f"— ignoring RED, waiting for confirmation in the next cycle"
                )
                livello = "GIALLO"
                decisione["livello_allerta"] = "GIALLO"
            else:
                log.warning("⚠️ REFUGE OVERRIDE: RED level confirmed on 1h+24h → converting everything to USDC")
            decisione = {
                "azione":          "rifugio",
                "token":           "USDC",
                "importo_usdc":    None,
                "confidenza":      100,
                "motivazione":     "Override: RED level. Taking refuge in USDC.",
                "livello_allerta": "ROSSO",
                "sentiment_trump": sicurezza.get("sentiment_trump", "neutral"),
                "sentiment_geo":   sicurezza.get("sentiment_geo", "neutral"),
            }
            result["decisione"] = decisione

        # SELL: RSI overbought + MACD bearish + SOL in portfolio
        elif (
            decisione.get("azione") == "aspetta"
            and sol_valore > 10
            and rsi > 70
            and macd_cross == "bearish"
            and livello in ("VERDE", "GIALLO")
        ):
            importo_sol   = round(sol_balance * 0.5, 6)
            importo_usdc  = round(importo_sol * sol_price, 2)
            importo_usdc = max(1.0, importo_usdc)
            log.warning(
                f"⚠️ SELL OVERRIDE: RSI={rsi:.1f} MACD={macd_cross} "
                f"→ selling {importo_sol} SOL (${importo_usdc})"
            )
            decisione = {
                "azione":          "vendi",
                "token":           "SOL",
                "importo_usdc":    importo_usdc,
                "vendi_frazione":  0.5,   # actually sell only half — trader.py used to ignore this and sell 100%
                "confidenza":      80,
                "motivazione":     f"Override: RSI={rsi:.1f} overbought, MACD bearish. Taking profit on 50% SOL.",
                "livello_allerta": livello,
                "sentiment_trump": sicurezza.get("sentiment_trump", "neutral"),
                "sentiment_geo":   sicurezza.get("sentiment_geo", "neutral"),
            }
            result["decisione"] = decisione

        # BUY: RSI oversold + USDC available + market ok
        elif (
            decisione.get("azione") == "aspetta"
            and usdc_balance >= 15
            and rsi < 40
            and livello == "VERDE"
            and macd_cross != "bearish"
        ):
            # Avoid repeated purchases in the last 4 hours
            ultimi = get_ultimi_trade(5)
            comprato_recente = any(
                t["azione"] == "compra"
                and t["token"] == "SOL"
                and t["data"]
                and (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(t["data"])
                ) < timedelta(hours=4)
                for t in ultimi
            )
            if not comprato_recente:
                importo = max(1.0, round(min(usdc_balance * 0.4, 20), 2))
                log.warning(
                    f"⚠️ BUY OVERRIDE: RSI={rsi:.1f} oversold "
                    f"→ buying ${importo} USDC of SOL"
                )
                decisione = {
                    "azione":          "compra",
                    "token":           "SOL",
                    "importo_usdc":    importo,
                    "confidenza":      80,
                    "motivazione":     f"Override: RSI={rsi:.1f} oversold, MACD {macd_cross}. Opportunistic buy.",
                    "livello_allerta": livello,
                    "sentiment_trump": sicurezza.get("sentiment_trump", "neutral"),
                    "sentiment_geo":   sicurezza.get("sentiment_geo", "neutral"),
                }
                result["decisione"] = decisione

        # SCALP MOMENTUM: MACD bullish + RSI in neutral-bullish zone
        # Captures intraday movements (+1-2%) without waiting for extreme conditions
        elif (
            decisione.get("azione") == "aspetta"
            and usdc_balance >= 10
            and macd_cross == "bullish"
            and 35 <= rsi <= 65
            and livello in ("VERDE", "GIALLO")
            and sol_valore < 20
        ):
            ultimi = get_ultimi_trade(5)
            comprato_recente = any(
                t["azione"] == "compra"
                and t["token"] == "SOL"
                and t["data"]
                and (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(t["data"])
                ) < timedelta(hours=2)
                for t in ultimi
            )
            if not comprato_recente:
                scalp_max = float(os.getenv("SOL_TRADING_SCALP_MAX_USDC", "15"))
                importo = max(1.0, round(min(usdc_balance * 0.15, scalp_max), 2))
                log.warning(
                    f"⚡ SCALP MOMENTUM: RSI={rsi:.1f} MACD={macd_cross} "
                    f"→ scalping ${importo} USDC of SOL"
                )
                decisione = {
                    "azione":          "compra",
                    "token":           "SOL",
                    "importo_usdc":    importo,
                    "confidenza":      75,
                    "motivazione":     f"Scalp momentum: MACD bullish, RSI={rsi:.1f}. Target take profit {os.getenv('SOL_TRADING_TAKE_PROFIT_PCT','1.5')}%.",
                    "livello_allerta": livello,
                    "sentiment_trump": sicurezza.get("sentiment_trump", "neutral"),
                    "sentiment_geo":   sicurezza.get("sentiment_geo", "neutral"),
                }
                result["decisione"] = decisione

        # ── End portfolio management ─────────────────────────────────────────

        log.info(
            f"Level: {livello} | Decision: {decisione.get('azione')} "
            f"{decisione.get('token')} conf={decisione.get('confidenza')}%"
        )

        # CONSECUTIVE BUY LIMIT: max 10 buys without an intermediate sell
        if decisione.get('azione') == 'compra':
            ultimi_20 = get_ultimi_trade(20)
            acquisti_consecutivi = 0
            for t in ultimi_20:
                if t['azione'] == 'compra' and t['eseguito']:
                    acquisti_consecutivi += 1
                elif t['azione'] == 'vendi' and t['eseguito']:
                    break
            
            if acquisti_consecutivi >= 10:
                log.warning(f'⚠️ BUY LIMIT: {acquisti_consecutivi} consecutive buys without a sell — blocking new buy')
                decisione = {
                    'azione': 'aspetta',
                    'token': None,
                    'importo_usdc': 0,
                    'confidenza': 0,
                    'motivazione': f'Safety limit: {acquisti_consecutivi} consecutive buys without a sell. Waiting for a realized exit.',
                    'livello_allerta': livello,
                    'sentiment_trump': sicurezza.get('sentiment_trump', 'neutral'),
                    'sentiment_geo': sicurezza.get('sentiment_geo', 'neutral'),
                }
                result['decisione'] = decisione

        # MACD BEARISH BLOCK: don't buy against the trend at low confidence
        if decisione.get("azione") == "compra":
            token_da_comprare = decisione.get("token", "SOL")
            ind_token = dati.get("indicatori", {}).get(token_da_comprare, ind_sol)
            macd_token = ind_token.get("macd_cross", "neutral") or "neutral"
            if macd_token == "bearish" and decisione.get("confidenza", 0) < 85:
                log.warning(
                    f"⚠️ MACD BEARISH BLOCK: buy {token_da_comprare} blocked "
                    f"(MACD={macd_token}, conf={decisione.get('confidenza')}% < 85%)"
                )
                decisione = {
                    "azione":          "aspetta",
                    "token":           None,
                    "importo_usdc":    0,
                    "confidenza":      0,
                    "motivazione":     f"MACD {macd_token} on {token_da_comprare}: buy blocked. Conf={decisione.get('confidenza')}% < 85%.",
                    "livello_allerta": livello,
                    "sentiment_trump": sicurezza.get("sentiment_trump", "neutral"),
                    "sentiment_geo":   sicurezza.get("sentiment_geo", "neutral"),
                }
                result["decisione"] = decisione

        # CIRCUIT BREAKER: pause a token after a confirmed losing streak
        if decisione.get("azione") == "compra":
            token_da_comprare = decisione.get("token", "SOL")
            from database import get_performance_recente
            perf = get_performance_recente(token_da_comprare, ore=24, min_trade=3)
            if perf["blocked"]:
                log.warning(
                    f"⚡ CIRCUIT BREAKER: {token_da_comprare} buy blocked — "
                    f"win_rate={perf['win_rate']}% profit=${perf['profit_usdc']} "
                    f"over last {perf['n_trade']} sells (24h)"
                )
                decisione = {
                    "azione":          "aspetta",
                    "token":           None,
                    "importo_usdc":    0,
                    "confidenza":      0,
                    "motivazione":     f"Circuit breaker: {token_da_comprare} win_rate={perf['win_rate']}% profit=${perf['profit_usdc']} over last {perf['n_trade']} sells (24h). Pausing.",
                    "livello_allerta": livello,
                    "sentiment_trump": sicurezza.get("sentiment_trump", "neutral"),
                    "sentiment_geo":   sicurezza.get("sentiment_geo", "neutral"),
                }
                result["decisione"] = decisione

        # 4. Execute if confidence is sufficient
        min_conf = regole.get("confidenza_minima", 60)
        azione   = decisione.get("azione")
        conf     = decisione.get("confidenza", 0)

        MIN_TRADE_USDC = 1.0

        if azione in ("compra", "vendi", "rifugio"):
            # Block sell if wallet doesn't have that token (AI hallucination on already-closed positions)
            if azione == "vendi":
                token_da_vendere = decisione.get("token")
                saldo_token = wallet.get(token_da_vendere, 0)
                if not isinstance(saldo_token, (int, float)) or saldo_token <= 0:
                    log.warning(f"Sell {token_da_vendere} ignored: wallet={saldo_token} (position already closed) — skip cycle")
                    azione = "aspetta"
                    decisione["azione"] = "aspetta"
                    decisione["token"] = None

            if (
                azione == "compra"
                and decisione.get("importo_usdc", 0) < MIN_TRADE_USDC
                and usdc_balance < MIN_TRADE_USDC
            ):
                log.info(f"USDC ${usdc_balance:.2f} and amount ${decisione.get('importo_usdc')} < ${MIN_TRADE_USDC} — skip micro-trade")
            elif azione == "compra" and sol_disponibile < MIN_SOL_PER_COMPRA and decisione.get("token") != "SOL":
                # Wants to buy another asset but SOL is low: replenish SOL reserve first
                sol_target = 0.01
                sol_mancante = sol_target - sol_disponibile
                importo_sol_usdc = max(1.0, round(sol_mancante * sol_price, 2)) if sol_price else 1.0
                log.warning(
                    f"SOL {sol_disponibile:.5f} < {MIN_SOL_PER_COMPRA} — override: buying ${importo_sol_usdc} SOL "
                    f"before proceeding with {decisione.get('token')}"
                )
                decisione = {
                    "azione":          "compra",
                    "token":           "SOL",
                    "importo_usdc":    importo_sol_usdc,
                    "confidenza":      95,
                    "motivazione":     f"Fee reserve replenishment: SOL {sol_disponibile:.5f} < {MIN_SOL_PER_COMPRA}. Target 0.01 SOL before buying {decisione.get('token')}.",
                    "livello_allerta": livello,
                    "sentiment_trump": sicurezza.get("sentiment_trump", "neutral"),
                    "sentiment_geo":   sicurezza.get("sentiment_geo", "neutral"),
                }
                result["decisione"] = decisione
                trade_result = await esegui_decisione(decisione, dati)
                await notifica(
                    f"⛽ <b>SOL FEE RESERVE REPLENISHMENT</b>\n"
                    f"💰 ${importo_sol_usdc}\n"
                    + (f"✅ TX: {str(trade_result.get('tx_hash',''))[:30]}"
                       if trade_result.get("success")
                       else f"❌ ERROR: {str(trade_result.get('error',''))[:100]}")
                )
            elif azione != "aspetta" and (conf >= min_conf or azione == "rifugio"):
                log.info("Executing trade...")
                trade_result = await esegui_decisione(decisione, dati)

                emoji = {"compra": "🟢", "vendi": "🔴", "rifugio": "🛡"}.get(azione, "ℹ️")
                await notifica(
                    f"{emoji} <b>{azione.upper()} {decisione.get('token')}</b>\n"
                    f"💰 ${decisione.get('importo_usdc', 0)}\n"
                    f"📊 Conf: {conf}% | Alert: {livello}\n"
                    f"RSI: {rsi:.1f} | MACD: {macd_cross}\n"
                    f"💬 {decisione.get('motivazione', '')[:200]}\n"
                    + (
                        f"✅ TX: {str(trade_result.get('tx_hash', ''))[:30]}"
                        if trade_result.get("success")
                        else f"❌ ERRORE: {str(trade_result.get('error', ''))[:100]}"
                    )
                )

                # After a sell, immediately update P&L
                if azione == "vendi" and trade_result.get("success"):
                    wallet_dopo = await get_wallet_completo()
                    await aggiorna_trade_aperti(
                        wallet_dopo, dati.get("prezzi", {})
                    )

            else:
                log.info(f"Conf {conf}% < min {min_conf}% — skip")
        else:
            log.info("Claude says: wait.")

    except Exception as e:
        log.error(f"Trading cycle error: {e}", exc_info=True)
        await notifica(f"❌ <b>CYCLE ERROR</b>\n{str(e)[:300]}")


# ── Weekly analysis ───────────────────────────────────────────────────────

async def ciclo_analisi_settimanale():
    """Every Sunday at 08:00 UTC."""
    log.info("=== WEEKLY ANALYSIS ===")
    try:
        from claude_brain import analisi_settimanale
        from database     import salva_regole

        result = await analisi_settimanale()
        if not result:
            return

        nuove_regole = result.get("regole_proposte", {})
        if nuove_regole:
            # note_strategiche is NOT saved to avoid a feedback loop:
            # Claude was re-reading its own notes as "strict rules" and
            # progressively raising thresholds until everything was blocked.
            regole_id = salva_regole({
                "confidenza_minima":  nuove_regole.get("confidenza_minima", 65),
                "stop_loss_pct":      nuove_regole.get("stop_loss_pct", 8),
                "max_trade_pct":      nuove_regole.get("max_trade_pct", 20),
                "token_core":         nuove_regole.get("token_core", ["SOL","BTC","ETH"]),
                "token_esclusi":      [],  # never exclude tokens from weekly analysis
                "note_strategiche":   f"Weekly analysis proposal {datetime.now(timezone.utc).strftime('%Y-%m-%d')}.",
                "approvata":          False,
            })
            await notifica(
                f"⚡ <b>WEEKLY ANALYSIS</b>\n"
                f"Win rate: {result.get('win_rate_attuale')}%\n"
                f"Profit: ${result.get('profit_totale')}\n"
                f"Change urgency: {result.get('urgenza_cambio')}\n"
                f"📋 New proposed rules (ID {regole_id}) — approve from dashboard\n"
                f"💬 {result.get('analisi','')[:300]}"
            )

    except Exception as e:
        log.error(f"Weekly analysis error: {e}", exc_info=True)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        handlers=[
            RotatingFileHandler(
                "/srv/trading_bot/logs/scheduler.log",
                maxBytes=5 * 1024 * 1024,  # 5 MB
                backupCount=5,
            ),
            logging.StreamHandler(),
        ]
    )

    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        ciclo_trading,
        IntervalTrigger(minutes=CYCLE_MINUTES),
        id="trading",
        next_run_time=datetime.now(timezone.utc),
    )

    scheduler.add_job(
        ciclo_analisi_settimanale,
        CronTrigger(day_of_week="sun", hour=8, minute=0),
        id="analisi_settimanale",
    )

    # TEMPORARILY DISABLED (2026-07-05): audit_settimanale cascades into
    # auto_apply.py, which rewrites trader.py/scheduler.py/data_collector.py/
    # claude_brain.py and restarts the service with no human review. We're
    # actively hand-editing these same files right now — re-enable once
    # that's done by uncommenting this block.
    # from log_audit import audit_settimanale
    # scheduler.add_job(
    #     audit_settimanale,
    #     CronTrigger(day_of_week="sun", hour=9, minute=0),
    #     id="audit_log",
    # )

    scheduler.start()
    log.info(f"Scheduler started — cycle every {CYCLE_MINUTES} min")
    await notifica(f"🚀 <b>SOL TRADER started</b>\nCycle every {CYCLE_MINUTES} min")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info("Scheduler stopped")


if __name__ == "__main__":
    asyncio.run(main())