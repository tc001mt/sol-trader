# claude_brain.py
import os
import re
import json
import logging
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from dotenv import load_dotenv
from database import (
    get_memoria_compressa, get_regole_attive,
    salva_trade, salva_evento, aggiorna_memoria_compressa
)

load_dotenv()
log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

MODEL_MAIN = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4-5")
MODEL_FAST = os.getenv("OPENROUTER_MODEL_FAST", "google/gemini-2.5-flash-lite")

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are autonomous Solana trading AI. Expert trader. Full memory of past decisions.

CAVEMAN OUTPUT RULES — mandatory:
- No filler. No hedge. No "I would recommend". No "Based on analysis".
- Short sentences. Subject verb object.
- Numbers/JSON/addresses/symbols → untouched.
- Respond ONLY valid JSON. No markdown. No explanation outside JSON.

ABSOLUTE SAFETY RULES — never break:
- Never trade > max_trade_pct of wallet
- Never ignore stop_loss_pct
- If confidence < min_confidence → azione = "aspetta"
- If ROSSO alert → azione = "rifugio" always
- Never suggest leverage
- Never move > 50% wallet in single trade

SCALP MODE — short trades are valid:
- A +1% to +3% gain in 1-6 hours is a good trade. Do not wait for perfect long setup.
- If MACD bullish + RSI 35-65 + price momentum visible → compra is allowed.
- Scalp size: 10-15% of available USDC, never more than 20%.
- ATR% = average daily price range as % of price (volatility). ATR% > 8-10% → size at the low end (10%) or skip; low ATR% → normal sizing is fine.
- Use motivazione: "scalp momentum" to signal short-term intent.
- The system handles take profit automatically. Your job: identify the entry.
"""

# ── JSON cleaner ──────────────────────────────────────────────────────────────

def clean_json_response(raw: str) -> str:
    """Cleans the response to obtain valid JSON."""
    raw = raw.strip()

    # Remove markdown fence
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines)

    # Find the first { and the last }
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]

    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        pass

    # Attempt repair: escape newlines inside strings
    in_string = False
    escape    = False
    result    = []
    for char in raw:
        if char == '"' and not escape:
            in_string = not in_string
            result.append(char)
        elif char == '\\' and not escape:
            escape = True
            result.append(char)
        elif escape:
            escape = False
            result.append(char)
        elif in_string and char == '\n':
            result.append('\\n')
        elif in_string and char == '\r':
            result.append('\\r')
        elif in_string and char == '\t':
            result.append('\\t')
        else:
            result.append(char)

    raw = ''.join(result)
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        pass

    # Last attempt: extract fields with regex
    fields   = {}
    patterns = {
        'azione':                r'"azione"\s*:\s*"([^"]+)"',
        'token':                 r'"token"\s*:\s*"([^"]+)"',
        'livello':               r'"livello"\s*:\s*"([^"]+)"',
        'motivo':                r'"motivo"\s*:\s*"([^"]*)"',
        'importo_usdc':          r'"importo_usdc"\s*:\s*(\d+(?:\.\d+)?)',
        'confidenza':            r'"confidenza"\s*:\s*(\d+)',
        'motivazione':           r'"motivazione"\s*:\s*"([^"]*)"',
        'sentiment_trump':       r'"sentiment_trump"\s*:\s*"([^"]*)"',
        'sentiment_geo':         r'"sentiment_geo"\s*:\s*"([^"]*)"',
        'percentuale_rifugio':   r'"percentuale_rifugio"\s*:\s*(\d+)',
        'riferimento_storico':   r'"riferimento_storico"\s*:\s*([^,}\n]+)',
        'nuovo_filtro_proposto': r'"nuovo_filtro_proposto"\s*:\s*([^,}\n]+)',
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, raw, re.IGNORECASE | re.DOTALL)
        if match:
            value = match.group(1).strip()
            if key in ['importo_usdc', 'confidenza', 'percentuale_rifugio']:
                try:
                    fields[key] = float(value) if '.' in value else int(value)
                except Exception:
                    fields[key] = 0
            elif key in ['riferimento_storico', 'nuovo_filtro_proposto']:
                fields[key] = None if value.lower() == 'null' else value
            else:
                fields[key] = value

    if 'azione' in fields:
        return json.dumps(fields)

    log.error(f"Unable to parse JSON. Raw: {raw[:500]}")
    return json.dumps({
        "azione":      "aspetta",
        "confidenza":  0,
        "motivazione": "JSON parse error"
    })


# ── Core API call ─────────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
async def chiedi_claude(
    prompt: str,
    max_tokens: int = 1000,
    fast: bool = False,
    temperature: float = 0.1
) -> dict:
    """
    Calls OpenRouter and returns parsed JSON.
    fast=True  → MODEL_FAST (cheap, for safety checks)
    fast=False → MODEL_MAIN (reliable, for trade decisions)
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY missing")

    model = MODEL_FAST if fast else MODEL_MAIN
    log.debug(f"Using model: {model} (fast={fast})")

    clean_prompt = f"""{prompt}

IMPORTANT: Return ONLY valid JSON. No text before or after. No trailing commas. No comments. Use double quotes everywhere.
If you cannot generate valid JSON for any reason, return: {{"error": "cannot generate", "azione": "aspetta", "confidenza": 0}}
"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://localhost",
        "X-Title":       "SolanaTrader",
    }
    body = {
        "model":       model,
        "max_tokens":  max_tokens,
        "temperature": temperature,
        "messages":    [{"role": "user", "content": clean_prompt}],
        "system":      SYSTEM_PROMPT,
    }

    async with httpx.AsyncClient(timeout=45) as c:
        r = await c.post(OPENROUTER_URL, headers=headers, json=body)
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        log.info(f"RAW response ({model}): {raw[:300]}")

    cleaned = clean_json_response(raw)

    try:
        result = json.loads(cleaned)
        defaults = {
            "azione":       "aspetta",
            "confidenza":   0,
            "importo_usdc": 0,
            "livello":      "GIALLO",
            "motivo":       "default fallback",
        }
        for key, default in defaults.items():
            if key not in result:
                result[key] = default
        return result
    except json.JSONDecodeError as e:
        log.error(f"JSON parse error: {e} | cleaned: {cleaned[:300]}")
        return {
            "azione":       "aspetta",
            "confidenza":   0,
            "motivazione":  f"JSON parse error: {str(e)[:50]}",
            "importo_usdc": 0,
            "livello":      "GIALLO",
            "token":        None,
        }


# ── Context builder ───────────────────────────────────────────────────────────

def _build_context(dati: dict, regole: dict, memoria: str) -> str:
    """Assembles prompt with all data — compact caveman format."""

    prezzi_str = " | ".join(
        f"{sym}=${v['price']} 1h={v['change_1h']}% 24h={v['change_24h']}%"
        for sym, v in dati.get("prezzi", {}).items()
    )

    fg     = dati.get("fear_greed", {})
    fg_str = f"{fg.get('value', 50)}/100 ({fg.get('label', 'Neutral')})"

    ind     = dati.get("indicatori", {})
    ind_str = ""
    for sym, v in ind.items():
        if v:
            ind_str += (
                f"{sym}: RSI={v.get('rsi')} MACD={v.get('macd_cross')} "
                f"BB%={v.get('bb_pct')} ATR%={v.get('atr_pct')} | "
            )

    futures = dati.get("futures", {})
    futures_str = ""
    for sym, v in futures.items():
        if v:
            futures_str += (
                f"{sym}: funding={v.get('funding_rate'):+.4f}%/8h "
                f"long={v.get('long_pct')}% short={v.get('short_pct')}% "
                f"[{v.get('signal')}] | "
            )
    if not futures_str:
        futures_str = "n/a"

    notizie_crypto = "\n".join(
        f"- {n['titolo']}" for n in dati.get("notizie_crypto", [])[:8]
    ) or "none"

    notizie_geo = "\n".join(
        f"- [{n['fonte']}] {n['titolo']}" for n in dati.get("notizie_geo", [])[:8]
    ) or "none"

    trump = "\n".join(
        f"- @{t['account']}: {t['testo'][:120]}"
        for t in dati.get("trump_tweets", [])[:5]
    ) or "none"

    sent   = dati.get("sentiment", {})
    sent_c = sent.get("crypto", {})
    sent_g = sent.get("geo", {})
    sent_t = sent.get("trump", {})

    sentiment_str = (
        f"\n=SENTIMENT=\n"
        f"Crypto: avg={sent_c.get('avg_score', 0)} "
        f"pos={sent_c.get('positive_pct', 0)}% neg={sent_c.get('negative_pct', 0)}%\n"
        f"Geo:    avg={sent_g.get('avg_score', 0)} "
        f"pos={sent_g.get('positive_pct', 0)}% neg={sent_g.get('negative_pct', 0)}%\n"
        f"Trump:  avg={sent_t.get('avg_score', 0)} "
        f"pos={sent_t.get('positive_pct', 0)}% neg={sent_t.get('negative_pct', 0)}%"
    )

    top_sol = " | ".join(
        f"{t['symbol']} ${t['price']} 24h={t['change_24h']}%"
        for t in dati.get("top_solana", [])[:6]
    )

    wallet     = dati.get("wallet", {})
    wallet_str = json.dumps(wallet) if wallet else "not loaded"

    from database import get_posizioni_aperte
    posizioni = get_posizioni_aperte()
    prezzi_ctx = dati.get("prezzi", {})
    if posizioni:
        pos_parts = []
        for p in posizioni:
            token = p["token"]
            entry = p["prezzo_entrata"]
            current = prezzi_ctx.get(token, {}).get("price", 0)
            pnl = round((current - entry) / entry * 100, 2) if current and entry else p["pnl_pct"]
            pos_parts.append(f"{token} entry=${entry} pnl={pnl:+.2f}%")
        pos_str = " | ".join(pos_parts)
    else:
        pos_str = "none — you hold ZERO volatile tokens. Do NOT suggest vendi for any token."

    return f"""
=CONTEXT=
TIME: {dati.get('timestamp', '')}
WALLET: {wallet_str}
POSITIONS: {pos_str}

=MARKET=
PRICES: {prezzi_str}
FEAR_GREED: {fg_str}
INDICATORS: {ind_str}
FUTURES: {futures_str}
FUTURES_GUIDE: overleveraged_long(>+0.05%%)=correction_risk | longs_dominant=caution_buying | neutral=ok | short_squeeze_risk(<-0.05%%)=bounce_likely
TOP_SOLANA: {top_sol}

=NEWS_CRYPTO=
{notizie_crypto}

=NEWS_GEO=
{notizie_geo}

=TRUMP=
{trump}
{sentiment_str}

=MEMORY=
{memoria}

=RULES=
confidence_min={regole.get('confidenza_minima', 65)}
stop_loss={regole.get('stop_loss_pct', 8)}%
max_trade={regole.get('max_trade_pct', 20)}%
core_tokens={regole.get('token_core', [])}
excluded={regole.get('token_esclusi', [])}
"""


# ── Safety evaluation (MODEL_FAST) ───────────────────────────────────────────

async def valuta_sicurezza(dati: dict) -> dict:
    """
    Evaluates global alert level.
    Uses MODEL_FAST — frequent call, low cost.
    """
    regole  = get_regole_attive()
    memoria = get_memoria_compressa()
    ctx     = _build_context(dati, regole, memoria)

    prompt = f"""
{ctx}

=TASK=
Evaluate market safety. Assign alert level.

Respond ONLY this JSON:
{{
  "livello": "VERDE|GIALLO|ROSSO",
  "motivo": "short caveman reason",
  "percentuale_rifugio": 0,
  "sentiment_trump": "positive|negative|neutral|none",
  "sentiment_geo": "positive|negative|neutral"
}}

VERDE = normal trading
GIALLO = reduce exposure 50%
ROSSO = flee to USDC/USDT immediately

ROSSO triggers — require ACTUAL SUSTAINED CRASH, not a flash crash:
- BTC or SOL drops >8% in 1h (change_1h) AND also >8% in 24h (change_24h) → sustained crash, flee
- Confirmed major hack/exploit on Solana or major exchange
- Confirmed major war escalation directly hitting markets (not just headlines)

FLASH CRASH GUARD — do NOT trigger ROSSO if:
- change_1h is very negative (<-8%) BUT change_24h is still moderate (>-5%)
  → this is a flash crash: a sudden spike down that typically recovers within 1-3 hours.
  Selling at the bottom of a flash crash locks in maximum loss. Stay GIALLO, wait.
- RSI drops to extreme oversold (<25) in a single cycle while 24h is still moderate
  → same pattern: spike, not trend. Hold.

IMPORTANT — Fear & Greed index is NOT a ROSSO trigger by itself:
"Extreme Fear" in crypto is historically a CONTRARIAN signal — markets often
bottom and reverse during extreme fear. A low Fear&Greed reading while price
is flat or rising (positive change_1h/24h, bullish RSI/MACD) means stay
VERDE or GIALLO, never ROSSO. Only escalate to ROSSO when the PRICE ITSELF
is crashing right now — sentiment indices and news headlines describe mood,
not the chart. Never override actual bullish price action with a fear-index
panic call.
"""

    try:
        result = await chiedi_claude(prompt, max_tokens=200, fast=True, temperature=0.1)
        log.info(f"[FAST] Safety: {result.get('livello')} — {result.get('motivo')}")
        return result
    except Exception as e:
        log.error(f"Safety eval error: {e}")
        return {
            "livello":             "GIALLO",
            "motivo":              f"eval error: {e}",
            "percentuale_rifugio": 50,
            "sentiment_trump":     "none",
            "sentiment_geo":       "neutral",
        }


# ── Trade decision (MODEL_MAIN) ───────────────────────────────────────────────

async def decidi_trade(dati: dict, livello_allerta: str) -> dict:
    """
    Decides the next trade using historical memory.
    Uses MODEL_MAIN — critical decision, maximum reliability.
    """
    regole   = get_regole_attive()
    memoria  = get_memoria_compressa()
    ctx      = _build_context(dati, regole, memoria)
    min_conf = regole.get('confidenza_minima', 60)

    wallet   = dati.get("wallet", {})
    usdc_ora = float(wallet.get("USDC", 0))
    token_held = [t for t in wallet if t not in ("pubkey", "SOL", "USDC", "USDT")
                  and isinstance(wallet.get(t), (int, float)) and wallet[t] > 0]

    if usdc_ora < 1.0:
        usdc_warning = (
            f"\n⚠️ INSUFFICIENT USDC: you only have ${usdc_ora:.2f} USDC — buy is BLOCKED (minimum $1.00).\n"
            f"Tokens in portfolio available to sell: {', '.join(token_held) or 'none'}.\n"
            f"To re-enter a token, first sell one of the above to free up USDC.\n"
        )
    else:
        usdc_warning = ""

    prompt = f"""
{ctx}
ALERT_LEVEL: {livello_allerta}
{usdc_warning}
=TASK=
Decide next trade. Use memory. Consider all data including sentiment and notes in RULES.
Apply rules strictly. Confidence must be >= {min_conf} to trade, otherwise aspetta.

SCALP CHECK: Before deciding "aspetta", check for short-term opportunity:
- MACD bullish + RSI 35-65 + positive price momentum → compra scalp (10-15% USDC, target +1-2%)
- Recent news catalyst + price starting to move → compra scalp allowed
- Do NOT aspetta just because no perfect long-term setup exists.

REBALANCE CHECK: Look at POSITIONS and compare momentum between held tokens:
- If a held token shows bearish MACD + RSI falling AND another token shows bullish MACD + RSI rising
  → consider selling the weak one (vendi) this cycle so next cycle can buy the strong one.
- Example: holding BTC (bearish, RSI dropping) while SOL shows bullish MACD and rising RSI
  → vendi BTC now, system will buy SOL next cycle with the freed USDC.
- This is NOT panic selling — it is active rebalancing. Use it when divergence is clear.

Respond ONLY valid JSON:
{{"azione": "compra|vendi|aspetta|rifugio", "token": "SOL|BTC|ETH|...", "importo_usdc": 0, "confidenza": 0, "motivazione": "caveman reason", "riferimento_storico": null, "nuovo_filtro_proposto": null}}

If azione=aspetta → importo_usdc=0
If azione=rifugio → token=USDC
"""

    try:
        result = await chiedi_claude(prompt, max_tokens=500, fast=False, temperature=0.1)

        if not isinstance(result, dict):
            raise ValueError(f"Result not a dict: {type(result)}")

        azione = result.get("azione", "aspetta")
        if azione not in ["compra", "vendi", "aspetta", "rifugio"]:
            log.warning(f"Invalid action: {azione}, fallback aspetta")
            result["azione"] = "aspetta"

        if not isinstance(result.get("confidenza"), (int, float)):
            result["confidenza"] = 0

        log.info(
            f"[MAIN] Trade: {result.get('azione')} {result.get('token')} "
            f"${result.get('importo_usdc')} conf={result.get('confidenza')}%"
        )
        return result

    except Exception as e:
        log.error(f"Trade decision error: {e}")
        return {
            "azione":                "aspetta",
            "token":                 None,
            "importo_usdc":          0,
            "confidenza":            0,
            "motivazione":           f"error: {str(e)[:50]}",
            "riferimento_storico":   None,
            "nuovo_filtro_proposto": None,
        }


# ── Weekly analysis (MODEL_MAIN) ──────────────────────────────────────────────

async def analisi_settimanale() -> dict:
    """
    Analyzes trade history and proposes rule changes.
    Requires human approval from the dashboard.
    """
    from database import get_ultimi_trade, get_performance, get_performance_per_token

    regole      = get_regole_attive()
    performance = get_performance(30)
    per_token   = get_performance_per_token()
    trade_list  = get_ultimi_trade(50)

    prompt = f"""
=WEEKLY ANALYSIS TASK=

You are reviewing your own trading performance. Be brutally honest.

PERFORMANCE 30d:
{json.dumps(performance, indent=2, default=str)}

BY TOKEN:
{json.dumps(per_token, indent=2, default=str)}

LAST 50 TRADES:
{json.dumps(trade_list, indent=2, default=str)}

CURRENT RULES:
{json.dumps(regole, indent=2, default=str)}

Analyze: what worked, what failed, patterns missed.
Propose rule changes if needed.

Respond ONLY this JSON:
{{
  "analisi": "caveman brutal honest summary",
  "win_rate_attuale": 0,
  "profit_totale": 0,
  "cosa_ha_funzionato": "...",
  "cosa_ha_fallito": "...",
  "pattern_nuovo": "...",
  "regole_proposte": {{
    "confidenza_minima": 75,
    "stop_loss_pct": 5,
    "max_trade_pct": 15,
    "token_core": ["SOL","BTC","ETH"],
    "token_esclusi": [],
    "note_strategiche": "..."
  }},
  "urgenza_cambio": "alta|media|bassa"
}}
"""

    try:
        result = await chiedi_claude(prompt, max_tokens=600, fast=False, temperature=0.3)
        log.info(f"[MAIN] Weekly analysis OK — urgency: {result.get('urgenza_cambio')}")
        return result
    except Exception as e:
        log.error(f"Weekly analysis error: {e}")
        return {}


# ── Main decision cycle ───────────────────────────────────────────────────────

async def ciclo_decisionale(dati: dict) -> dict:
    """
    Entry point for scheduler.py.
    1. Evaluate safety (MODEL_FAST)
    2. If ROSSO → immediate refuge
    3. If VERDE/GIALLO → decide trade (MODEL_MAIN)
    4. Autonomous filters DISABLED — log only, no DB
    5. Update memory
    """
    # Step 1
    sicurezza = await valuta_sicurezza(dati)
    livello   = sicurezza.get("livello", "GIALLO")

    # Step 2
    if livello == "ROSSO":
        salva_evento(
            "RIFUGIO",
            f"USDC refuge activated: {sicurezza.get('motivo')}",
            trigger=sicurezza.get("motivo", "")
        )
        decisione = {
            "azione":          "rifugio",
            "token":           "USDC",
            "importo_usdc":    None,
            "confidenza":      100,
            "motivazione":     sicurezza.get("motivo"),
            "sentiment_trump": sicurezza.get("sentiment_trump"),
            "sentiment_geo":   sicurezza.get("sentiment_geo"),
            "livello_allerta": "ROSSO",
        }

    else:
        # Step 3
        decisione = await decidi_trade(dati, livello)
        decisione["sentiment_trump"] = sicurezza.get("sentiment_trump")
        decisione["sentiment_geo"]   = sicurezza.get("sentiment_geo")
        decisione["livello_allerta"] = livello

        # Step 4 — autonomous filters DISABLED
        filtro = decisione.get("nuovo_filtro_proposto")
        if filtro and str(filtro).lower() not in ("null", "none", ""):
            log.info(f"Claude suggests filter (ignored): {str(filtro)[:100]}")
            salva_evento(
                "FILTRO_SUGGERITO",
                f"Suggestion ignored: {str(filtro)[:200]}",
                trigger="autonomous_disabled"
            )

    # Step 5
    aggiorna_memoria_compressa()

    return {
        "sicurezza": sicurezza,
        "decisione": decisione,
        "livello":   livello,
        "modelli": {
            "sicurezza": MODEL_FAST,
            "trade":     MODEL_MAIN,
        },
    }


# ── Test ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    from data_collector import raccogli_tutto

    logging.basicConfig(level=logging.INFO)

    async def test():
        print(f"MAIN model: {MODEL_MAIN}")
        print(f"FAST model: {MODEL_FAST}")

        print("\nCollecting data...")
        dati = await raccogli_tutto(["SOL", "BTC", "ETH"])

        print("\n[FAST] Safety evaluation...")
        sic = await valuta_sicurezza(dati)
        print(json.dumps(sic, indent=2))

        print("\n[MAIN] Trade decision...")
        dec = await decidi_trade(dati, sic.get("livello", "VERDE"))
        print(json.dumps(dec, indent=2))

        print("\nFull cycle...")
        result = await ciclo_decisionale(dati)
        print(f"Level:    {result['livello']}")
        print(f"Action:   {result['decisione'].get('azione')}")
        print(f"Conf:     {result['decisione'].get('confidenza')}%")
        print(f"Models:   {result['modelli']}")

    asyncio.run(test())