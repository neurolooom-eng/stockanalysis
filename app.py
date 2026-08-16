"""
Stock Monitor - single-service backend.

Serves both the JSON API and the dashboard page, so there is only one
thing to run and one thing to deploy.

Run:  python app.py       ->  http://localhost:8000
"""

import asyncio
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests
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

# Starting caps only - both are editable at runtime from the admin panel and
# are read from the settings table on every request. See caps().
DEFAULT_MAX_PROFILES = int(os.environ.get("MAX_PROFILES", 10))
DEFAULT_MAX_STOCKS = int(os.environ.get("MAX_STOCKS", 30))
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL", 60))
INTRADAY_INTERVAL = os.environ.get("INTERVAL", "5m")  # 1m/2m/5m/15m/30m/60m
MA_FAST = int(os.environ.get("MA_FAST", 20))          # in bars, not days
MA_SLOW = int(os.environ.get("MA_SLOW", 50))

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
ALERT_TICK_SECONDS = 20      # how often the loop wakes to see if a sweep is due
TELEGRAM_TIMEOUT = 15

_cache: Dict[str, tuple] = {}  # symbol -> (timestamp, payload)
_log_lock = threading.Lock()   # guards the read-then-write in score_profile()
# Reported by /api/settings so the settings panel can show whether the
# background loop is actually running, and why it last failed if it did.
_alert_state: Dict[str, Optional[str]] = {
    "last_run": None, "last_sent": None, "last_error": None, "running": False}


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
            CREATE INDEX IF NOT EXISTS idx_log_symbol
                ON signal_log(profile_id, symbol, id);
            CREATE TABLE IF NOT EXISTS settings (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL
            );
            """
        )
        # Migration for databases created before alerts existed. SQLite has no
        # "ADD COLUMN IF NOT EXISTS", so check the table first.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(profiles)")}
        if "alerts" not in cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN alerts INTEGER NOT NULL DEFAULT 1")


# ----------------------------------------------------------------------------
# Settings (stored in the database, so nothing has to be edited in a file)
# ----------------------------------------------------------------------------

SETTING_DEFAULTS = {
    "telegram_token": "",
    "telegram_chat_ids": "",
    "alerts_enabled": "0",
    "alert_interval_minutes": "5",
    "market_hours_only": "1",
    # Caps are settings, not constants, so they can be changed from the admin
    # panel without touching code. These are the starting values only.
    "max_profiles": str(DEFAULT_MAX_PROFILES),
    "max_stocks_per_profile": str(DEFAULT_MAX_STOCKS),
}


def caps() -> Dict[str, int]:
    cfg = get_settings()
    def num(key, fallback):
        try:
            return max(1, min(500, int(cfg[key])))
        except (TypeError, ValueError):
            return fallback
    return {"max_profiles": num("max_profiles", DEFAULT_MAX_PROFILES),
            "max_stocks_per_profile": num("max_stocks_per_profile", DEFAULT_MAX_STOCKS)}


def get_settings() -> Dict[str, str]:
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = dict(SETTING_DEFAULTS)
    out.update({r["key"]: r["value"] for r in rows})
    return out


def save_settings(values: Dict[str, str]) -> None:
    with db() as conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, str(v)) for k, v in values.items()])


def market_is_open(now: Optional[datetime] = None) -> bool:
    """NSE/BSE cash session: Mon-Fri, 09:15-15:30 IST. Holidays are not
    modelled - on a holiday the loop simply finds no new bars, so no signal
    changes and no alerts."""
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() > 4:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


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
            "ma_fast": round(ma_fast, 2) if ma_fast is not None else None,
            "ma_slow": round(ma_slow, 2) if ma_slow is not None else None,
            "ma_fast_period": MA_FAST,
            "ma_slow_period": MA_SLOW,
            "vwap": round(vwap, 2) if vwap is not None else None,
            "pivots": {k: round(v, 2) for k, v in piv.items()},
            "as_of": as_of,
            "basis": basis,
        })
    except Exception as exc:  # noqa: BLE001 - surface the reason to the UI
        out["error"] = f"{type(exc).__name__}: {exc}"

    _cache[key] = (time.time(), out)
    return out


# ----------------------------------------------------------------------------
# Scoring a whole watchlist, and logging only what changed
# ----------------------------------------------------------------------------

def score_profile(profile_id: int) -> Dict:
    """
    Score every stock on a profile and record CHANGES ONLY.

    The dashboard polls, and the alert loop sweeps, so writing a row per run
    would bury the real transitions under thousands of duplicates and make the
    hit rate meaningless. A row is written when a symbol's signal differs from
    the last one logged for it - which is also exactly when an alert is due.
    """
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
    changes: List[Dict] = []
    if good:
        now = datetime.now().isoformat()
        # The dashboard and the alert loop can both land here at once; the
        # read-then-write below would otherwise log the same change twice.
        with _log_lock, db() as conn:
            for r in good:
                prev = conn.execute(
                    "SELECT signal, price, logged_at FROM signal_log"
                    " WHERE profile_id=? AND symbol=? ORDER BY id DESC LIMIT 1",
                    (profile_id, r["symbol"])).fetchone()
                if prev and prev["signal"] == r["signal"]:
                    continue
                conn.execute(
                    "INSERT INTO signal_log (profile_id, symbol, signal, score, price, logged_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (profile_id, r["symbol"], r["signal"], r["score"], r["price"], now))
                changes.append({
                    "symbol": r["symbol"], "exchange": r["exchange"],
                    "previous": prev["signal"] if prev else None,
                    "signal": r["signal"], "price": r["price"], "score": r["score"],
                    "reasons": r["reasons"], "logged_at": now,
                })

    order = {"BUY": 0, "SELL": 1, "HOLD": 2}
    good.sort(key=lambda r: (order.get(r["signal"], 3), -r["confidence"]))
    failed = [r for r in results if "error" in r]
    return {
        "generated_at": datetime.now().isoformat(),
        "signals": good + failed,
        "changes": changes,
        "counts": {
            "buy": sum(1 for r in good if r["signal"] == "BUY"),
            "sell": sum(1 for r in good if r["signal"] == "SELL"),
            "hold": sum(1 for r in good if r["signal"] == "HOLD"),
            "failed": len(failed)},
    }


# ----------------------------------------------------------------------------
# Telegram alerts
# ----------------------------------------------------------------------------

def chat_id_list(raw: str) -> List[str]:
    return [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()]


def send_telegram(token: str, chat_ids: List[str], text: str) -> Dict:
    """Returns {"sent": n, "errors": [...]}. Never raises - a dead bot token
    must not take the dashboard down with it."""
    sent, errors = 0, []
    for chat in chat_ids:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text,
                      "disable_web_page_preview": True},
                timeout=TELEGRAM_TIMEOUT)
            body = resp.json() if resp.content else {}
            if resp.ok and body.get("ok"):
                sent += 1
            else:
                errors.append(f"{chat}: {body.get('description') or resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{chat}: {type(exc).__name__}: {exc}")
    return {"sent": sent, "errors": errors}


ARROW = {"BUY": "▲", "SELL": "▼", "HOLD": "–"}


def format_alert(profile_name: str, change: Dict) -> str:
    was = change["previous"] or "new"
    lines = [
        f"{ARROW.get(change['signal'], '')} {change['symbol']} ({change['exchange']})"
        f"  {was} → {change['signal']}",
        f"₹{change['price']:,.2f}   score {change['score']:+g}/3   [{profile_name}]",
    ]
    lines += [f"  • {r['text']}" for r in change.get("reasons", [])]
    lines.append("Delayed Yahoo data. Arithmetic on past prices, not advice.")
    return "\n".join(lines)


def run_alert_sweep() -> Dict:
    """One pass over every alert-enabled profile. Blocking; called in a thread."""
    cfg = get_settings()
    token = cfg["telegram_token"].strip()
    chats = chat_id_list(cfg["telegram_chat_ids"])
    with db() as conn:
        profiles = conn.execute(
            "SELECT id, name FROM profiles WHERE alerts = 1 ORDER BY id").fetchall()

    total_changes, total_sent, errors = 0, 0, []
    for prof in profiles:
        result = score_profile(prof["id"])
        for change in result["changes"]:
            # A symbol with no history is seeded silently - the first sweep
            # after adding 30 stocks should not fire 30 messages.
            if change["previous"] is None:
                continue
            total_changes += 1
            outcome = send_telegram(token, chats, format_alert(prof["name"], change))
            total_sent += outcome["sent"]
            errors.extend(outcome["errors"])

    _alert_state["last_run"] = datetime.now(IST).isoformat()
    if total_sent:
        _alert_state["last_sent"] = _alert_state["last_run"]
    _alert_state["last_error"] = "; ".join(errors[:3]) if errors else None
    return {"changes": total_changes, "sent": total_sent, "errors": errors}


async def alert_loop():
    """Wakes every ALERT_TICK_SECONDS so interval or on/off changes made in the
    settings panel take effect without a restart."""
    last_sweep = 0.0
    while True:
        try:
            cfg = get_settings()
            interval = max(1, int(cfg.get("alert_interval_minutes") or 5)) * 60
            ready = (cfg["alerts_enabled"] == "1"
                     and cfg["telegram_token"].strip()
                     and chat_id_list(cfg["telegram_chat_ids"])
                     and (cfg["market_hours_only"] != "1" or market_is_open()))
            if ready and time.time() - last_sweep >= interval:
                last_sweep = time.time()
                await asyncio.to_thread(run_alert_sweep)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            _alert_state["last_error"] = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(ALERT_TICK_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(alert_loop())
    _alert_state["running"] = True
    try:
        yield
    finally:
        _alert_state["running"] = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------

class ProfileIn(BaseModel):
    name: str


class StockIn(BaseModel):
    symbol: str
    exchange: str = "NSE"


class SettingsIn(BaseModel):
    telegram_token: Optional[str] = None
    telegram_chat_ids: Optional[str] = None
    alerts_enabled: Optional[bool] = None
    alert_interval_minutes: Optional[int] = None
    market_hours_only: Optional[bool] = None
    max_profiles: Optional[int] = None
    max_stocks_per_profile: Optional[int] = None


class AlertsIn(BaseModel):
    alerts: bool


init_db()  # runs at import, so the tables exist however the app is started

app = FastAPI(title="Pivot Desk", version="2.1.0", lifespan=lifespan)


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
            """SELECT p.id, p.name, p.created_at, p.alerts, COUNT(w.id) AS stock_count
               FROM profiles p LEFT JOIN watchlist w ON w.profile_id = p.id
               GROUP BY p.id ORDER BY p.id"""
        ).fetchall()
    return {"profiles": [dict(r) for r in rows], **caps()}


@app.post("/api/profiles")
def create_profile(body: ProfileIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Watchlist name cannot be empty")
    limit = caps()["max_profiles"]
    with db() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        if existing >= limit:
            raise HTTPException(
                400, f"You already have {existing} watchlists, and the cap is {limit}. "
                     f"Delete one, or raise the cap in Settings.")
        try:
            cur = conn.execute(
                "INSERT INTO profiles (name, created_at) VALUES (?, ?)",
                (name, datetime.now().isoformat()))
        except sqlite3.IntegrityError:
            raise HTTPException(400, f"A watchlist named '{name}' already exists")
        return {"id": cur.lastrowid, "name": name, "stock_count": 0, "alerts": 1}


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: int):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone():
            raise HTTPException(404, "Watchlist not found")
        conn.execute("DELETE FROM watchlist WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM signal_log WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    return {"deleted": profile_id}


@app.post("/api/profiles/{profile_id}/alerts")
def set_profile_alerts(profile_id: int, body: AlertsIn):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone():
            raise HTTPException(404, "Watchlist not found")
        conn.execute("UPDATE profiles SET alerts = ? WHERE id = ?",
                     (1 if body.alerts else 0, profile_id))
    return {"id": profile_id, "alerts": body.alerts}


@app.get("/api/profiles/{profile_id}/stocks")
def get_stocks(profile_id: int):
    with db() as conn:
        prof = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if not prof:
            raise HTTPException(404, "Watchlist not found")
        rows = conn.execute(
            "SELECT symbol, exchange FROM watchlist WHERE profile_id = ? ORDER BY symbol",
            (profile_id,)).fetchall()
    return {"profile": dict(prof), "stocks": [dict(r) for r in rows],
            "limit": caps()["max_stocks_per_profile"]}


@app.post("/api/profiles/{profile_id}/stocks")
def add_stock(profile_id: int, body: StockIn):
    symbol = body.symbol.strip().upper()
    exchange = body.exchange.strip().upper()
    if not symbol:
        raise HTTPException(400, "Enter a symbol")
    if exchange not in ("NSE", "BSE"):
        raise HTTPException(400, "Exchange must be NSE or BSE")
    limit = caps()["max_stocks_per_profile"]
    with db() as conn:
        if not conn.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone():
            raise HTTPException(404, "Watchlist not found")
        count = conn.execute(
            "SELECT COUNT(*) FROM watchlist WHERE profile_id = ?", (profile_id,)).fetchone()[0]
        if count >= limit:
            raise HTTPException(
                400, f"This watchlist already holds {limit} stocks. "
                     f"Remove one, or raise the cap in Settings.")
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
    return score_profile(profile_id)


@app.get("/api/profiles/{profile_id}/history")
def history(profile_id: int, limit: int = 200):
    """
    Every logged signal change, plus how the price had moved by the time each
    one was replaced. This is a record of what the score did, on delayed data,
    ignoring brokerage, slippage and the fact that you cannot trade the close
    of a 5-minute bar. Read it as a sanity check, not a backtest.
    """
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, symbol, signal, score, price, logged_at FROM signal_log
               WHERE profile_id=? ORDER BY id ASC""", (profile_id,)).fetchall()]

    by_symbol: Dict[str, List[Dict]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r)

    resolved: Dict[int, Dict] = {}
    for series in by_symbol.values():
        for entry, nxt in zip(series, series[1:]):
            move = ((nxt["price"] - entry["price"]) / entry["price"] * 100
                    if entry["price"] else 0.0)
            hit = None
            if entry["signal"] == "BUY":
                hit = move > 0
            elif entry["signal"] == "SELL":
                hit = move < 0
            resolved[entry["id"]] = {
                "exit_price": nxt["price"], "exit_at": nxt["logged_at"],
                "move_pct": round(move, 2), "hit": hit}

    tally = {"BUY": [0, 0], "SELL": [0, 0]}  # signal -> [resolved, hits]
    for entry_id, out in resolved.items():
        sig = next(r["signal"] for r in rows if r["id"] == entry_id)
        if sig in tally:
            tally[sig][0] += 1
            tally[sig][1] += 1 if out["hit"] else 0

    def pct(pair):
        return round(pair[1] / pair[0] * 100, 1) if pair[0] else None

    out_rows = []
    for r in reversed(rows):
        out_rows.append({**r, **resolved.get(r["id"], {"exit_price": None,
                                                       "exit_at": None,
                                                       "move_pct": None,
                                                       "hit": None})})
    return {
        "history": out_rows[:min(limit, 500)],
        "total": len(rows),
        "summary": {
            "buy": {"resolved": tally["BUY"][0], "hits": tally["BUY"][1],
                    "pct": pct(tally["BUY"])},
            "sell": {"resolved": tally["SELL"][0], "hits": tally["SELL"][1],
                     "pct": pct(tally["SELL"])},
        },
    }


@app.delete("/api/profiles/{profile_id}/history")
def clear_history(profile_id: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM signal_log WHERE profile_id=?", (profile_id,))
    return {"cleared": cur.rowcount}


# ---------------------------- settings / admin ------------------------------

@app.get("/api/settings")
def read_settings():
    cfg = get_settings()
    token = cfg["telegram_token"].strip()
    return {
        # The token is never sent back in full - only enough to recognise it.
        "telegram_configured": bool(token),
        "telegram_token_hint": f"…{token[-4:]}" if len(token) >= 4 else "",
        "telegram_chat_ids": cfg["telegram_chat_ids"],
        "alerts_enabled": cfg["alerts_enabled"] == "1",
        "alert_interval_minutes": int(cfg["alert_interval_minutes"] or 5),
        "market_hours_only": cfg["market_hours_only"] == "1",
        **caps(),
        "market_open_now": market_is_open(),
        "loop": dict(_alert_state),
    }


@app.post("/api/settings")
def write_settings(body: SettingsIn):
    updates: Dict[str, str] = {}
    if body.telegram_token is not None:
        # An empty string means "leave it alone"; clearing is explicit.
        token = body.telegram_token.strip()
        if token:
            updates["telegram_token"] = token
    if body.telegram_chat_ids is not None:
        updates["telegram_chat_ids"] = ",".join(chat_id_list(body.telegram_chat_ids))
    if body.alerts_enabled is not None:
        updates["alerts_enabled"] = "1" if body.alerts_enabled else "0"
    if body.market_hours_only is not None:
        updates["market_hours_only"] = "1" if body.market_hours_only else "0"
    if body.alert_interval_minutes is not None:
        updates["alert_interval_minutes"] = str(max(1, min(180, body.alert_interval_minutes)))
    if body.max_profiles is not None:
        updates["max_profiles"] = str(max(1, min(500, body.max_profiles)))
    if body.max_stocks_per_profile is not None:
        updates["max_stocks_per_profile"] = str(max(1, min(500, body.max_stocks_per_profile)))
    if updates:
        save_settings(updates)
    return read_settings()


@app.delete("/api/settings/telegram")
def clear_telegram():
    save_settings({"telegram_token": "", "telegram_chat_ids": "", "alerts_enabled": "0"})
    return read_settings()


@app.post("/api/telegram/test")
def telegram_test():
    cfg = get_settings()
    token, chats = cfg["telegram_token"].strip(), chat_id_list(cfg["telegram_chat_ids"])
    if not token:
        raise HTTPException(400, "Save a bot token first")
    if not chats:
        raise HTTPException(400, "Add at least one chat ID first")
    result = send_telegram(
        token, chats,
        "Pivot Desk is connected. Alerts will arrive here when a stock's "
        "signal changes.")
    if not result["sent"]:
        raise HTTPException(400, "; ".join(result["errors"]) or "Telegram refused the message")
    return result


@app.post("/api/alerts/run")
def alerts_run_now():
    """Force one sweep regardless of schedule or market hours - the honest way
    to check the alert path works end to end."""
    cfg = get_settings()
    if not cfg["telegram_token"].strip() or not chat_id_list(cfg["telegram_chat_ids"]):
        raise HTTPException(400, "Set up Telegram first")
    return run_alert_sweep()


if __name__ == "__main__":
    import uvicorn
    print(f"\n  Stock Monitor running at  http://localhost:{PORT}\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
