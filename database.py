# database.py
import os
import json
import logging
from datetime import datetime, timezone
from contextlib import contextmanager
from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, text, Column, Integer, String,
    Numeric, Boolean, DateTime, Text
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

load_dotenv()
log = logging.getLogger(__name__)
Base = declarative_base()

# ── Models ────────────────────────────────────────────────────────────────────

class Trade(Base):
    __tablename__ = "trades"
    id               = Column(Integer, primary_key=True)
    data             = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    token            = Column(String(20), nullable=False)
    azione           = Column(String(10), nullable=False)   # buy/sell/wait
    importo_usdc     = Column(Numeric(18, 6))
    prezzo_entrata   = Column(Numeric(18, 6))
    prezzo_uscita    = Column(Numeric(18, 6))
    risultato_pct    = Column(Numeric(10, 4))
    risultato_usdc   = Column(Numeric(18, 6))
    motivazione      = Column(Text)                         # caveman-speak
    livello_allerta  = Column(String(10))                   # GREEN/YELLOW/RED
    confidenza       = Column(Integer)
    fear_greed       = Column(Integer)
    sentiment        = Column(JSONB)                        # {trump, geo, crypto}
    tx_hash          = Column(String(100))                  # Solana transaction hash
    eseguito         = Column(Boolean, default=False)

class Regole(Base):
    __tablename__ = "regole"
    id                   = Column(Integer, primary_key=True)
    data_aggiornamento   = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    confidenza_minima    = Column(Integer, default=60)
    stop_loss_pct        = Column(Numeric(5, 2), default=10.0)
    max_trade_pct        = Column(Numeric(5, 2), default=20.0)
    token_core           = Column(JSONB, default=lambda: ["SOL", "BTC", "ETH"])
    token_esclusi        = Column(JSONB, default=lambda: [])
    note_strategiche     = Column(Text)
    approvata            = Column(Boolean, default=False)

class AnalisiSettimanale(Base):
    __tablename__ = "analisi_settimanali"
    id                  = Column(Integer, primary_key=True)
    data                = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    win_rate            = Column(Numeric(5, 2))
    profit_totale       = Column(Numeric(18, 6))
    analisi_testo       = Column(Text)
    modifiche_proposte  = Column(JSONB)
    approvata           = Column(Boolean, default=False)

class Evento(Base):
    __tablename__ = "eventi"
    id          = Column(Integer, primary_key=True)
    data        = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    tipo        = Column(String(30))        # SHELTER/RETURN/EMERGENCY/INFO
    descrizione = Column(Text)
    trigger     = Column(Text)

class MemoriaCompressa(Base):
    __tablename__ = "memoria_compressa"
    id          = Column(Integer, primary_key=True)
    aggiornata  = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    contenuto   = Column(Text)              # caveman-speak, ready for Claude
    token_count = Column(Integer)

# ── Engine ────────────────────────────────────────────────────────────────────

def get_engine():
    url = (
        f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST', 'localhost')}/{os.getenv('DB_NAME')}"
    )
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)

engine = get_engine()
Session = sessionmaker(bind=engine)

@contextmanager
def get_session():
    session = Session()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        log.error(f"DB error: {e}")
        raise
    finally:
        session.close()

def init_db():
    """Create tables + default rules if they don't exist."""
    Base.metadata.create_all(engine)
    with get_session() as s:
        if not s.query(Regole).first():
            s.add(Regole(
                confidenza_minima=60,
                stop_loss_pct=10.0,
                max_trade_pct=20.0,
                token_core=["SOL", "BTC", "ETH"],
                token_esclusi=[],
                note_strategiche="Initial default rules.",
                approvata=True
            ))
    log.info("DB init OK")

# ── CRUD trades ───────────────────────────────────────────────────────────────

def salva_trade(dati: dict) -> int:
    with get_session() as s:
        t = Trade(**dati)
        s.add(t)
        s.flush()
        return t.id

def aggiorna_risultato(trade_id: int, prezzo_uscita: float, risultato_pct: float, risultato_usdc: float):
    with get_session() as s:
        t = s.query(Trade).get(trade_id)
        if t:
            t.prezzo_uscita  = prezzo_uscita
            t.risultato_pct  = risultato_pct
            t.risultato_usdc = risultato_usdc

def get_ultimi_trade(n: int = 20) -> list:
    with get_session() as s:
        rows = (
            s.query(Trade)
            .order_by(Trade.data.desc())
            .limit(n)
            .all()
        )
        return [_trade_to_dict(r) for r in rows]

def get_performance(giorni: int = 30) -> dict:
    giorni = int(giorni)
    with get_session() as s:
        result = s.execute(text("""
            SELECT
                COUNT(*)                                              AS totale,
                ROUND(AVG(risultato_pct)::numeric, 2)                AS media_pct,
                ROUND(SUM(risultato_usdc)::numeric, 2)               AS profit_usdc,
                ROUND(
                    SUM(CASE WHEN risultato_pct > 0 THEN 1 ELSE 0 END)
                    * 100.0 / NULLIF(COUNT(*), 0), 1
                )                                                     AS win_rate,
                ROUND(MIN(risultato_pct)::numeric, 2)                AS peggior,
                ROUND(MAX(risultato_pct)::numeric, 2)                AS miglior
            FROM trades
            WHERE data > NOW() - make_interval(days => :giorni)
              AND eseguito = true
              AND azione = 'vendi'
        """), {"giorni": giorni}).mappings().first()
        return dict(result) if result else {}

def get_performance_per_token() -> list:
    with get_session() as s:
        rows = s.execute(text("""
            SELECT
                token,
                COUNT(*)                                        AS n_trade,
                ROUND(AVG(risultato_pct)::numeric, 2)           AS media_pct,
                ROUND(SUM(risultato_usdc)::numeric, 2)          AS profit_usdc,
                ROUND(
                    SUM(CASE WHEN risultato_pct > 0 THEN 1 ELSE 0 END)
                    * 100.0 / NULLIF(COUNT(*),0), 1
                )                                               AS win_rate
            FROM trades
            WHERE eseguito = true
              AND azione = 'vendi'
            GROUP BY token
            ORDER BY media_pct DESC
        """)).mappings().all()
        return [dict(r) for r in rows]

# ── CRUD regole ───────────────────────────────────────────────────────────────

def get_regole_attive() -> dict:
    with get_session() as s:
        r = (
            s.query(Regole)
            .filter(Regole.approvata == True)
            .order_by(Regole.data_aggiornamento.desc())
            .first()
        )
        return _regole_to_dict(r) if r else {}

def salva_regole(dati: dict) -> int:
    import json as _json
    with get_session() as s:
        # Sanitize token_core and token_esclusi — must be lists
        for campo in ("token_core", "token_esclusi"):
            val = dati.get(campo)
            if isinstance(val, str):
                try:
                    dati[campo] = _json.loads(val)
                except Exception:
                    dati[campo] = ["SOL", "BTC", "ETH"] if campo == "token_core" else []
            elif isinstance(val, dict):
                dati[campo] = list(val.keys()) if val else []
            elif not isinstance(val, list):
                dati[campo] = ["SOL", "BTC", "ETH"] if campo == "token_core" else []

        r = Regole(**dati)
        s.add(r)
        s.flush()
        return r.id

def approva_regole(regole_id: int):
    with get_session() as s:
        s.query(Regole).filter(Regole.id == regole_id).update({"approvata": True})

# ── CRUD eventi ───────────────────────────────────────────────────────────────

def salva_evento(tipo: str, descrizione: str, trigger: str = ""):
    with get_session() as s:
        s.add(Evento(tipo=tipo, descrizione=descrizione, trigger=trigger))

# ── Compressed memory (caveman) ───────────────────────────────────────────────

def aggiorna_memoria_compressa():
    """
    Builds a caveman snapshot of the trade history and saves it.
    Claude will read this instead of the entire trades table.
    """
    trade_list = get_ultimi_trade(20)
    perf       = get_performance(30)
    per_token  = get_performance_per_token()
    regole     = get_regole_attive()

    lines = ["=MEMORY="]

    # Global performance
    if perf:
        lines.append(
            f"PERF30d: trades={perf.get('totale',0)} "
            f"wr={perf.get('win_rate',0)}% "
            f"profit=${perf.get('profit_usdc',0)} "
            f"best={perf.get('miglior',0)}% "
            f"worst={perf.get('peggior',0)}%"
        )

    # Top token
    if per_token:
        token_line = " | ".join(
            f"{r['token']}:wr={r['win_rate']}%,avg={r['media_pct']}%"
            for r in per_token[:5]
        )
        lines.append(f"TOKEN_PERF: {token_line}")

    # Last 20 trades in compact format
    lines.append("RECENT_TRADES:")
    for t in trade_list:
        lines.append(
            f"  {t['data'][:10]} {t['token']} {t['azione']} "
            f"${t['importo_usdc']} conf={t['confidenza']} "
            f"res={t['risultato_pct']}% alert={t['livello_allerta']}"
        )

    # Active rules
    if regole:
        lines.append(
            f"RULES: conf>={regole.get('confidenza_minima')} "
            f"sl={regole.get('stop_loss_pct')}% "
            f"max={regole.get('max_trade_pct')}% "
            f"core={regole.get('token_core')} "
            f"excl={regole.get('token_esclusi')}"
        )

    contenuto   = "\n".join(lines)
    token_count = len(contenuto.split())   # approximation

    with get_session() as s:
        # Keep only the latest snapshot
        s.query(MemoriaCompressa).delete()
        s.add(MemoriaCompressa(contenuto=contenuto, token_count=token_count))

    log.info(f"Memory updated: ~{token_count} tokens")
    return contenuto

def get_memoria_compressa() -> str:
    with get_session() as s:
        m = s.query(MemoriaCompressa).order_by(MemoriaCompressa.aggiornata.desc()).first()
        return m.contenuto if m else ""


def get_posizioni_aperte() -> list:
    """
    Returns tokens actually held in the portfolio: for each token the last
    executed trade must be a buy (if it's a sell, the position is closed).
    """
    with get_session() as s:
        token_tradati = [
            row[0] for row in
            s.query(Trade.token).filter(
                Trade.azione.in_(["compra", "vendi"]),
                Trade.eseguito == True,
                Trade.token.notin_(["USDC", "USDT"]),
            ).distinct().all()
        ]
        posizioni = []
        for token in token_tradati:
            ultimo = (
                s.query(Trade)
                .filter(
                    Trade.token == token,
                    Trade.azione.in_(["compra", "vendi"]),
                    Trade.eseguito == True,
                )
                .order_by(Trade.data.desc())
                .first()
            )
            # Open position only if the last trade is a buy
            if ultimo and ultimo.azione == "compra" and float(ultimo.prezzo_entrata or 0) > 0:
                posizioni.append({
                    "token":          token,
                    "prezzo_entrata": float(ultimo.prezzo_entrata),
                    "pnl_pct":        float(ultimo.risultato_pct or 0),
                    "importo_usdc":   float(ultimo.importo_usdc or 0),
                    "data":           ultimo.data.isoformat() if ultimo.data else None,
                })
        return posizioni


# ── Helpers ───────────────────────────────────────────────────────────────────

def _trade_to_dict(t: Trade) -> dict:
    return {
        "id":             t.id,
        "data":           t.data.isoformat() if t.data else None,
        "token":          t.token,
        "azione":         t.azione,
        "importo_usdc":   float(t.importo_usdc or 0),
        "prezzo_entrata": float(t.prezzo_entrata or 0),
        "prezzo_uscita":  float(t.prezzo_uscita or 0),
        "risultato_pct":  float(t.risultato_pct or 0),
        "risultato_usdc": float(t.risultato_usdc or 0),
        "motivazione":    t.motivazione,
        "livello_allerta":t.livello_allerta,
        "confidenza":     t.confidenza,
        "fear_greed":     t.fear_greed,
        "sentiment":      t.sentiment,
        "tx_hash":        t.tx_hash,
        "eseguito":       t.eseguito,
    }

def _regole_to_dict(r: Regole) -> dict:
    return {
        "id":                 r.id,
        "confidenza_minima":  r.confidenza_minima,
        "stop_loss_pct":      float(r.stop_loss_pct or 10),
        "max_trade_pct":      float(r.max_trade_pct or 20),
        "token_core":         r.token_core,
        "token_esclusi":      r.token_esclusi,
        "note_strategiche":   r.note_strategiche,
        "approvata":          r.approvata,
    }

# ── Test ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print("Tables created OK")
    print("Default rules:", get_regole_attive())
