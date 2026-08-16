"""
Stock Monitor - single-service backend.

Serves both the JSON API and the dashboard page, so there is only one
thing to run and one thing to deploy.

Run:  python app.py       ->  http://localhost:8000
"""

import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
# On hosts with an ephemeral disk you can point DB_PATH at a mounted volume.
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "stock_monitor.db"))
PORT = int(os.environ.get("PORT", 8000))

MAX_STOCKS_PER_PROFILE = 30
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL", 60))
INTRADAY_INTERVAL = os.environ.get("INTERVAL", "5m")  # 1m/2m/5m/15m/30m/60m
MA_FAST = int(os.environ.get("MA_FAST", 20))          # in bars, not days
MA_SLOW = int(os.environ.get("MA_SLOW", 50))

app = FastAPI(title="Stock Monitor", version="2.0.0")

_cache: Dict[str, tuple] = {}  # symbol -> (timestamp, payload)


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT UNIQUE NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id  INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                symbol      TEXT NOT NULL,
                exchange    TEXT NOT NULL DEFAULT 'NSE',
                added_at    TEXT NOT NULL,
                UNIQUE(profile_id, symbol, exchange)
            );
            CREATE TABLE IF NOT EXISTS signal_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id  INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                signal      TEXT NOT NULL,
                score       REAL NOT NULL,
                price       REAL NOT NULL,
                logged_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_log ON signal_log(profile_id, logged_at);
            """
        )


# ----------------------------------------------------------------------------
# Symbols
# ----------------------------------------------------------------------------

# A starter list. Add your own freely - the app accepts any symbol you type.
# NSE symbols get ".NS", BSE symbols get ".BO" when sent to Yahoo Finance.
STARTER_SYMBOLS = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "HINDUNILVR",
    "SBIN", "BHARTIARTL", "ITC", "LT", "KOTAKBANK", "AXISBANK",
    "MARUTI", "WIPRO", "HCLTECH", "SUNPHARMA", "ASIANPAINT", "TITAN",
    "BAJFINANCE", "BAJAJFINSV", "TATAMOTORS", "TATASTEEL", "JSWSTEEL",
    "ULTRACEMCO", "NTPC", "POWERGRID", "ONGC", "COALINDIA", "GRASIM",
    "ADANIPORTS", "ADANIENT", "NESTLEIND", "TECHM", "DRREDDY", "CIPLA",
    "EICHERMOT", "HEROMOTOCO", "BRITANNIA", "DIVISLAB", "APOLLOHOSP",
    "DMART", "INDUSINDBK", "SBILIFE", "HDFCLIFE", "BPCL", "IOC", "GAIL",
    "VEDL", "HINDALCO", "PIDILITIND", "DABUR", "GODREJCP", "MARICO",
    "TRENT", "ZOMATO", "PAYTM", "IRCTC", "IRFC", "PFC", "RECLTD",
]


def yahoo_symbol(symbol: str, exchange: str) -> str:
    return f"{symbol.upper()}.{'BO' if exchange.upper() == 'BSE' else 'NS'}"


# ----------------------------------------------------------------------------
# Indicators
# ----------------------------------------------------------------------------

def _flatten(df: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    """yfinance returns MultiIndex columns in some versions. Normalise both."""
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        lv0 = df.columns.get_level_values(0)
        if ticker in set(lv0):
            df = df.xs(ticker, axis=1, level=0)
        else:
            df = df.xs(ticker, axis=1, level=1) if ticker in set(
                df.columns.get_level_values(1)) else df.droplevel(1, axis=1)
    return df.dropna(how="all")


def camarilla(high: float, low: float, close: float) -> Dict[str, float]:
    """
    Camarilla levels from the PREVIOUS session's high/low/close.

    Standard published formula - verify against your own source, minor
    variants exist:
        H4 = C + (H-L) * 1.1/2      L1 = C - (H-L) * 1.1/12
        H3 = C + (H-L) * 1.1/4      L2 = C - (H-L) * 1.1/6
        H2 = C + (H-L) * 1.1/6      L3 = C - (H-L) * 1.1/4
        H1 = C + (H-L) * 1.1/12     L4 = C - (H-L) * 1.1/2
        PP = (H + L + C) / 3
    """
    r = high - low
    return {
        "h4": close + r * 1.1 / 2,
        "h3": close + r * 1.1 / 4,
        "h2": close + r * 1.1 / 6,
        "h1": close + r * 1.1 / 12,
        "pp": (high + low + close) / 3,
        "l1": close - r * 1.1 / 12,
        "l2": close - r * 1.1 / 6,
        "l3": close - r * 1.1 / 4,
        "l4": close - r * 1.1 / 2,
    }


def session_vwap(intraday: pd.DataFrame) -> Optional[float]:
    """
    True VWAP: cumulative within the CURRENT session only. VWAP resets each
    trading day, so a rolling multi-day average is not VWAP.
    """
    if intraday is None or intraday.empty:
        return None
    day = intraday.index[-1].date()
    today = intraday[intraday.index.map(lambda t: t.date() == day)]
    if today.empty:
        return None
    tp = (today["High"] + today["Low"] + today["Close"]) / 3
    vol = today["Volume"].fillna(0)
    if vol.sum() <= 0:
        return float(tp.iloc[-1])
    return float((tp * vol).sum() / vol.sum())


def score_signal(price, ma_fast, ma_slow, vwap, piv):
    """
    Transparent additive score in [-3, +3]. Every component is reported so
    you can see WHY a signal fired instead of trusting a bare number.
    """
    score, reasons = 0.0, []

    # 1. Trend (moving averages)
    if ma_fast is not None and ma_slow is not None:
        if price > ma_fast > ma_slow:
            score += 1
            reasons.append(("trend", 1, f"Price above MA{MA_FAST} above MA{MA_SLOW}"))
        elif price < ma_fast < ma_slow:
            score -= 1
            reasons.append(("trend", -1, f"Price below MA{MA_FAST} below MA{MA_SLOW}"))
        elif price > ma_fast:
            score += 0.5
            reasons.append(("trend", 0.5, f"Price above MA{MA_FAST}, MAs not aligned"))
        elif price < ma_fast:
            score -= 0.5
            reasons.append(("trend", -0.5, f"Price below MA{MA_FAST}, MAs not aligned"))
        else:
            reasons.append(("trend", 0, "No clear trend"))

    # 2. Volume (VWAP)
    if vwap is not None:
        if price > vwap:
            score += 1
            reasons.append(("vwap", 1, "Trading above session VWAP"))
        else:
            score -= 1
            reasons.append(("vwap", -1, "Trading below session VWAP"))

    # 3. Pivot position
    if price > piv["h3"]:
        score += 1
        reasons.append(("pivot", 1, "Broken above H3 resistance"))
    elif price < piv["l3"]:
        score -= 1
        reasons.append(("pivot", -1, "Broken below L3 support"))
    elif price > piv["pp"]:
        score += 0.5
        reasons.append(("pivot", 0.5, "Above central pivot, inside H3"))
    else:
        score -= 0.5
        reasons.append(("pivot", -0.5, "Below central pivot, inside L3"))

    if score >= 2:
        signal = "BUY"
    elif score <= -2:
        signal = "SELL"
    else:
        signal = "HOLD"

    return signal, round(score, 2), round(abs(score) / 3, 2), reasons


def analyse(symbol: str, exchange: str) -> Dict:
    ticker = yahoo_symbol(symbol, exchange)
    key = f"{ticker}|{INTRADAY_INTERVAL}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]

    out = {"symbol": symbol, "exchange": exchange, "ticker": ticker}
    try:
        daily = _flatten(
            yf.download(ticker, period="1mo", interval="1d",
                        progress=False, auto_adjust=False), ticker)
        intra = _flatten(
            yf.download(ticker, period="5d", interval=INTRADAY_INTERVAL,
                        progress=False, auto_adjust=False), ticker)

        if daily is None or len(daily) < 2:
            out["error"] = "No daily data returned"
            return out

        # Previous completed session drives the pivots.
        prev = daily.iloc[-2]
        piv = camarilla(float(prev["High"]), float(prev["Low"]), float(prev["Close"]))

        if intra is not None and len(intra) >= 2:
            close = intra["Close"].dropna()
            price = float(close.iloc[-1])
            ma_fast = float(close.rolling(MA_FAST).mean().iloc[-1]) if len(close) >= MA_FAST else None
            ma_slow = float(close.rolling(MA_SLOW).mean().iloc[-1]) if len(close) >= MA_SLOW else None
            vwap = session_vwap(intra)
            as_of = intra.index[-1].isoformat()
            basis = f"{INTRADAY_INTERVAL} bars"
        else:
            # Market closed / no intraday available - fall back to daily.
            close = daily["Close"].dropna()
            price = float(close.iloc[-1])
            ma_fast = float(close.rolling(MA_FAST).mean().iloc[-1]) if len(close) >= MA_FAST else None
            ma_slow = float(close.rolling(MA_SLOW).mean().iloc[-1]) if len(close) >= MA_SLOW else None
            vwap = None
            as_of = daily.index[-1].isoformat()
            basis = "daily bars (no intraday data)"

        signal, score, confidence, reasons = score_signal(price, ma_fast, ma_slow, vwap, piv)
        prev_close = float(daily["Close"].iloc[-2])

        out.update({
            "price": round(price, 2),
            "prev_close": round(prev_close, 2),
            "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
            "signal": signal,
            "score": score,
            "confidence": confidence,
            "reasons": [{"kind": k, "weight": w, "text": t} for k, w, t in reasons],
            "ma_fast": round(ma_fast, 2) if ma_fast else None,
            "ma_slow": round(ma_slow, 2) if ma_slow else None,
            "ma_fast_period": MA_FAST,
            "ma_slow_period": MA_SLOW,
            "vwap": round(vwap, 2) if vwap else None,
            "pivots": {k: round(v, 2) for k, v in piv.items()},
            "as_of": as_of,
            "basis": basis,
        })
    except Exception as exc:  # noqa: BLE001 - surface the reason to the UI
        out["error"] = f"{type(exc).__name__}: {exc}"

    _cache[key] = (time.time(), out)
    return out


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------

class ProfileIn(BaseModel):
    name: str


class StockIn(BaseModel):
    symbol: str
    exchange: str = "NSE"


init_db()  # runs at import, so the tables exist however the app is started


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat(),
            "interval": INTRADAY_INTERVAL, "cache_ttl": CACHE_TTL_SECONDS}


@app.get("/api/symbols")
def symbols():
    return {"symbols": STARTER_SYMBOLS}


@app.get("/api/profiles")
def list_profiles():
    with db() as conn:
        rows = conn.execute(
            """SELECT p.id, p.name, p.created_at, COUNT(w.id) AS stock_count
               FROM profiles p LEFT JOIN watchlist w ON w.profile_id = p.id
               GROUP BY p.id ORDER BY p.id"""
        ).fetchall()
    return {"profiles": [dict(r) for r in rows]}


@app.post("/api/profiles")
def create_profile(body: ProfileIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Profile name cannot be empty")
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO profiles (name, created_at) VALUES (?, ?)",
                (name, datetime.now().isoformat()))
        except sqlite3.IntegrityError:
            raise HTTPException(400, f"A profile named '{name}' already exists")
        return {"id": cur.lastrowid, "name": name, "stock_count": 0}


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: int):
    with db() as conn:
        conn.execute("DELETE FROM watchlist WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM signal_log WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    return {"deleted": profile_id}


@app.get("/api/profiles/{profile_id}/stocks")
def get_stocks(profile_id: int):
    with db() as conn:
        prof = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if not prof:
            raise HTTPException(404, "Profile not found")
        rows = conn.execute(
            "SELECT symbol, exchange FROM watchlist WHERE profile_id = ? ORDER BY symbol",
            (profile_id,)).fetchall()
    return {"profile": dict(prof), "stocks": [dict(r) for r in rows],
            "limit": MAX_STOCKS_PER_PROFILE}


@app.post("/api/profiles/{profile_id}/stocks")
def add_stock(profile_id: int, body: StockIn):
    symbol = body.symbol.strip().upper()
    exchange = body.exchange.strip().upper()
    if not symbol:
        raise HTTPException(400, "Enter a symbol")
    if exchange not in ("NSE", "BSE"):
        raise HTTPException(400, "Exchange must be NSE or BSE")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone():
            raise HTTPException(404, "Profile not found")
        count = conn.execute(
            "SELECT COUNT(*) FROM watchlist WHERE profile_id = ?", (profile_id,)).fetchone()[0]
        if count >= MAX_STOCKS_PER_PROFILE:
            raise HTTPException(
                400, f"This profile already holds {MAX_STOCKS_PER_PROFILE} stocks. "
                     f"Remove one to add another.")
        try:
            conn.execute(
                "INSERT INTO watchlist (profile_id, symbol, exchange, added_at) VALUES (?,?,?,?)",
                (profile_id, symbol, exchange, datetime.now().isoformat()))
        except sqlite3.IntegrityError:
            raise HTTPException(400, f"{symbol} is already on this watchlist")
    return {"symbol": symbol, "exchange": exchange}


@app.delete("/api/profiles/{profile_id}/stocks/{symbol}")
def remove_stock(profile_id: int, symbol: str, exchange: str = "NSE"):
    with db() as conn:
        conn.execute(
            "DELETE FROM watchlist WHERE profile_id=? AND symbol=? AND exchange=?",
            (profile_id, symbol.upper(), exchange.upper()))
    return {"removed": symbol.upper()}


@app.get("/api/profiles/{profile_id}/signals")
def signals(profile_id: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT symbol, exchange FROM watchlist WHERE profile_id=? ORDER BY symbol",
            (profile_id,)).fetchall()
    # Fetch in parallel. Sequentially, 30 stocks takes a minute or more.
    if rows:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda r: analyse(r["symbol"], r["exchange"]), rows))
    else:
        results = []

    good = [r for r in results if "error" not in r]
    if good:
        with db() as conn:
            conn.executemany(
                "INSERT INTO signal_log (profile_id, symbol, signal, score, price, logged_at)"
                " VALUES (?,?,?,?,?,?)",
                [(profile_id, r["symbol"], r["signal"], r["score"], r["price"],
                  datetime.now().isoformat()) for r in good])

    order = {"BUY": 0, "SELL": 1, "HOLD": 2}
    good.sort(key=lambda r: (order.get(r["signal"], 3), -r["confidence"]))
    failed = [r for r in results if "error" in r]
    return {"generated_at": datetime.now().isoformat(),
            "signals": good + failed,
            "counts": {
                "buy": sum(1 for r in good if r["signal"] == "BUY"),
                "sell": sum(1 for r in good if r["signal"] == "SELL"),
                "hold": sum(1 for r in good if r["signal"] == "HOLD"),
                "failed": len(failed)}}


@app.get("/api/profiles/{profile_id}/history")
def history(profile_id: int, limit: int = 100):
    with db() as conn:
        rows = conn.execute(
            """SELECT symbol, signal, score, price, logged_at FROM signal_log
               WHERE profile_id=? ORDER BY id DESC LIMIT ?""",
            (profile_id, min(limit, 500))).fetchall()
    return {"history": [dict(r) for r in rows]}


if __name__ == "__main__":
    import uvicorn
    print(f"\n  Stock Monitor running at  http://localhost:{PORT}\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
