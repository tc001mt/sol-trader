# app.py
import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv
from database import (
    init_db, get_ultimi_trade, get_performance,
    get_performance_per_token, get_regole_attive,
    approva_regole, salva_regole, get_memoria_compressa
)

load_dotenv()
import trader  # import before shitcoin_hunter can ever run: bakes in the correct
                # keypair at process start, immune to shitcoin_hunter's later
                # load_dotenv(".env.hunter", override=True) clobbering os.environ
log = logging.getLogger(__name__)
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "changeme")

_WALLET_TOKEN_ORDER = {"SOL": 0, "USDC": 1, "USDT": 2}

def _wallet_sort_key(t):
    return (_WALLET_TOKEN_ORDER.get(t["token"], 99), t["usd"] is None, -(t["usd"] or 0))

def _service_running(unit: str):
    import subprocess
    try:
        r = subprocess.run(
            ["/usr/bin/systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() == "active"
    except Exception:
        return None

_MARKET_CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "last_market_status.json")

def _translate_motivo(motivo: str) -> dict:
    """Translates the reason into IT, EN, and RU via OpenRouter (sync, best-effort)."""
    try:
        import httpx
        api_key = os.getenv("SOL_TRADING_OPENROUTER_API_KEY")
        model   = os.getenv("SOL_TRADING_OPENROUTER_MODEL_FAST", "google/gemini-2.5-flash-lite")
        if not api_key or not motivo:
            return {}
        prompt = (
            f'Translate this short crypto market analysis text into Italian, English, and Russian.\n'
            f'Return ONLY valid JSON: {{"it": "<italian>", "en": "<english>", "ru": "<russian>"}}\n'
            f'No explanation. No extra text.\n\nText: "{motivo}"'
        )
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "max_tokens": 400, "temperature": 0.1,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        raw = raw.strip("` \n").removeprefix("json").strip()
        return json.loads(raw)
    except Exception as e:
        log.warning(f"_translate_motivo failed: {e}")
        return {}

def _save_market_cache(sic: dict):
    os.makedirs(os.path.dirname(_MARKET_CACHE_FILE), exist_ok=True)
    motivo = sic.get("motivo", "")
    t = _translate_motivo(motivo)
    payload = {
        **sic,
        "motivo_it": t.get("it", motivo),
        "motivo_en": t.get("en", motivo),
        "motivo_ru": t.get("ru", motivo),
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(_MARKET_CACHE_FILE, "w") as f:
        json.dump(payload, f)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/eventi")
def api_eventi():
    from database import get_session, Evento
    tipo  = request.args.get("tipo")
    limit = int(request.args.get("limit", 10))
    with get_session() as s:
        q = s.query(Evento).order_by(Evento.data.desc())
        if tipo:
            q = q.filter(Evento.tipo == tipo)
        rows = q.limit(limit).all()
        return jsonify([{
            "id":          r.id,
            "data":        r.data.isoformat() if r.data else None,
            "tipo":        r.tipo,
            "descrizione": r.descrizione,
            "trigger":     r.trigger,
        } for r in rows])
    
@app.route("/api/rifugio", methods=["POST"])
def api_rifugio():
    from trader import rifugio_usdc
    result = asyncio.run(rifugio_usdc())
    return jsonify({"success": True, "results": result})

@app.route("/api/regole/pending")
def api_regole_pending():
    from database import get_session, Regole, _regole_to_dict
    with get_session() as s:
        rules = s.query(Regole).filter(Regole.approvata == False).order_by(Regole.data_aggiornamento.desc()).all()
        return jsonify([_regole_to_dict(r) for r in rules])

@app.route("/api/regole/rifiuta/<int:regole_id>", methods=["POST"])
def api_rifiuta_regole(regole_id):
    from database import get_session, Regole
    with get_session() as s:
        regola = s.query(Regole).filter(Regole.id == regole_id).first()
        if regola:
            s.delete(regola)
    return jsonify({"ok": True})

@app.route("/api/regole/rifiuta-tutte", methods=["POST"])
def api_rifiuta_tutte():
    from database import get_session, Regole
    with get_session() as s:
        n = s.query(Regole).filter(Regole.approvata == False).delete()
    return jsonify({"ok": True, "eliminate": n})


@app.route("/api/status")
def api_status():
    try:
        from data_collector import get_prezzi, get_fear_greed, get_indicatori, get_futures_data, get_notizie_crypto, get_notizie_geo, get_trump_tweets
        from trader import get_wallet_completo

        async def fetch():
            import asyncio
            prezzi, fg, ind_sol, ind_btc, ind_eth, futures, wallet, nc, ng, trump = await asyncio.gather(
                get_prezzi(["SOL", "BTC", "ETH"]),
                get_fear_greed(),
                get_indicatori("SOL"),
                get_indicatori("BTC"),
                get_indicatori("ETH"),
                get_futures_data(["SOL", "BTC", "ETH"]),
                get_wallet_completo(),
                get_notizie_crypto(),
                get_notizie_geo(),
                get_trump_tweets(),
            )
            return prezzi, fg, {"SOL": ind_sol, "BTC": ind_btc, "ETH": ind_eth}, futures, wallet, nc, ng, trump

        prezzi, fg, indicatori, futures, wallet, nc, ng, trump = asyncio.run(fetch())

        # Calculate USD values for each token in the wallet
        prezzi_usd = {k: v.get("price", 0) for k, v in prezzi.items()}
        prezzi_usd["USDC"] = 1.0
        prezzi_usd["USDT"] = 1.0
        wallet_tokens = []
        totale_usd = 0.0
        for token, saldo in wallet.items():
            if token == "pubkey" or not isinstance(saldo, (int, float)):
                continue
            if saldo <= 0:
                continue
            if token not in prezzi_usd:
                # Price fetch failed/rate-limited this cycle — show the balance instead
                # of silently hiding it (a real balance must never look like dust).
                wallet_tokens.append({"token": token, "saldo": saldo, "usd": None})
                continue
            usd = round(saldo * prezzi_usd[token], 2)
            if usd < 0.01:
                continue
            totale_usd += usd
            wallet_tokens.append({"token": token, "saldo": saldo, "usd": usd})
        wallet_tokens.sort(key=_wallet_sort_key)

        return jsonify({
            "prezzi":        prezzi,
            "fear_greed":    fg,
            "indicatori":    indicatori,
            "futures":       futures,
            "wallet":        wallet,
            "wallet_tokens": wallet_tokens,
            "wallet_totale": round(totale_usd, 2),
            "running":       _service_running("mtelani_trading_scheduler.service"),
            "notizie_crypto":nc[:6],
            "notizie_geo":   ng[:6],
            "trump_tweets":  trump[:3],
            "sentiment":     {},
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        log.error(f"api_status error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
        
@app.route("/api/sicurezza")
def api_sicurezza():
    try:
        from data_collector import raccogli_tutto
        from claude_brain import valuta_sicurezza

        async def fetch():
            from trader import get_pubkey
            dati = await raccogli_tutto(["SOL", "BTC", "ETH"], pubkey=get_pubkey())
            sic  = await valuta_sicurezza(dati)
            return sic

        sic = asyncio.run(fetch())
        try:
            _save_market_cache(sic)
        except Exception as ce:
            log.warning(f"market cache write failed: {ce}")
        return jsonify(sic)
    except Exception as e:
        log.error(f"api_sicurezza error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/api/public/status")
def api_public_status():
    """Public endpoint: serves cached market status for external display. No auth required."""
    try:
        if os.path.exists(_MARKET_CACHE_FILE):
            with open(_MARKET_CACHE_FILE) as f:
                data = json.load(f)
            return jsonify(data)
        return jsonify({"livello": "VERDE", "motivo": "No data yet", "cached_at": None})
    except Exception as e:
        log.error(f"api_public_status error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/api/trades")
def api_trades():
    n = int(request.args.get("n", 20))
    return jsonify(get_ultimi_trade(n))

@app.route("/api/performance")
def api_performance():
    return jsonify({
        "globale":    get_performance(30),
        "per_token":  get_performance_per_token(),
    })

@app.route("/api/regole")
def api_regole():
    return jsonify(get_regole_attive())

@app.route("/api/regole/approva/<int:regole_id>", methods=["POST"])
def api_approva_regole(regole_id):
    approva_regole(regole_id)
    return jsonify({"ok": True})

@app.route("/api/trade/manuale", methods=["POST"])
def api_trade_manuale():
    """Execute a manual trade from the dashboard."""
    from trader import esegui_swap
    from data_collector import raccogli_tutto

    data = request.json
    token_in  = data.get("token_in", "USDC")
    token_out = data.get("token_out", "SOL")
    importo   = float(data.get("importo", 0))

    if importo <= 0:
        return jsonify({"error": "Invalid amount"}), 400

    async def do_swap():
        dati   = await raccogli_tutto([token_out])
        result = await esegui_swap(token_in, token_out, importo)
        return result

    result = asyncio.run(do_swap())
    return jsonify(result)

@app.route("/api/memoria")
def api_memoria():
    return jsonify({"memoria": get_memoria_compressa()})


@app.route("/api/hunter")
def api_hunter():
    base_dir       = os.path.dirname(__file__)
    state_file     = os.path.join(base_dir, "data", "shitcoin_state.json")

    # Config from the shared .env (SHITCOIN_HUNTER_* namespace), read-only
    config = {
        "amount_usd":    float(os.getenv("SHITCOIN_HUNTER_AMOUNT_USD",   5)),
        "max_positions": int(os.getenv("SHITCOIN_HUNTER_MAX_POSITIONS",   10)),
        "take_profit":   float(os.getenv("SHITCOIN_HUNTER_TAKE_PROFIT",   50)),
        "stop_loss":     float(os.getenv("SHITCOIN_HUNTER_STOP_LOSS",     25)),
        "enabled":       os.getenv("SHITCOIN_HUNTER_ENABLED", "true").lower() == "true",
        "dry_run":       os.getenv("SHITCOIN_HUNTER_DRY_RUN", "true").lower() == "true",
        "wallet":        os.getenv("SHITCOIN_HUNTER_SOLANA_PUBLIC_KEY", ""),
    }

    running = _service_running("shitcoin_hunter")

    # State file (positions + vol history written by hunter every cycle)
    state = {}
    if os.path.exists(state_file):
        try:
            with open(state_file) as f:
                state = json.load(f)
        except Exception:
            pass

    # Recent hunter trades from DB
    try:
        from database import get_session, Trade as TradeModel
        with get_session() as s:
            rows = (
                s.query(TradeModel)
                .filter(TradeModel.motivazione.like("%HUNTER%"))
                .order_by(TradeModel.data.desc())
                .limit(15)
                .all()
            )
            hunter_trades = [{
                "data":          r.data.isoformat() if r.data else None,
                "token":         r.token,
                "azione":        r.azione,
                "importo_usdc":  float(r.importo_usdc or 0),
                "prezzo_entrata":float(r.prezzo_entrata or 0),
                "prezzo_uscita": float(r.prezzo_uscita or 0) if r.prezzo_uscita else None,
                "risultato_pct": float(r.risultato_pct or 0) if r.risultato_pct else None,
                "risultato_usdc":float(r.risultato_usdc or 0) if r.risultato_usdc else None,
                "tx_hash":       r.tx_hash,
            } for r in rows]
    except Exception as e:
        log.warning(f"hunter trades query error: {e}")
        hunter_trades = []

    # Live wallet balances (SOL, USDC, and each open-position token), same shape
    # as the main bot's wallet_tokens/wallet_totale
    positions = state.get("positions", {})
    try:
        from shitcoin_hunter import get_wallet_completo
        from data_collector import get_prezzi

        async def fetch_wallet():
            return await asyncio.gather(get_wallet_completo(), get_prezzi(["SOL"]))

        wallet, sol_prezzo = asyncio.run(fetch_wallet())

        prezzi_usd = {"SOL": sol_prezzo.get("SOL", {}).get("price", 0), "USDC": 1.0}
        for p in positions.values():
            prezzi_usd[p.get("symbol", "")] = p.get("current_price") or p.get("entry_price", 0)

        wallet_tokens = []
        totale_usd = 0.0
        for token, saldo in wallet.items():
            if token == "pubkey" or not isinstance(saldo, (int, float)) or saldo <= 0:
                continue
            if token not in prezzi_usd:
                wallet_tokens.append({"token": token, "saldo": saldo, "usd": None})
                continue
            usd = round(saldo * prezzi_usd[token], 2)
            if usd < 0.01:
                continue
            totale_usd += usd
            wallet_tokens.append({"token": token, "saldo": saldo, "usd": usd})
        wallet_tokens.sort(key=_wallet_sort_key)
    except Exception as e:
        log.warning(f"hunter wallet error: {e}")
        wallet, wallet_tokens, totale_usd = {}, [], 0.0

    return jsonify({
        "config":        config,
        "running":       running,
        "positions":     positions,
        "recent_trades": hunter_trades,
        "wallet":        wallet,
        "wallet_tokens": wallet_tokens,
        "wallet_totale": round(totale_usd, 2),
    })



@app.route("/api/analisi", methods=["POST"])
def api_analisi():
    """Launch manual weekly analysis."""
    from claude_brain import analisi_settimanale
    result = asyncio.run(analisi_settimanale())
    return jsonify(result)

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=False)

