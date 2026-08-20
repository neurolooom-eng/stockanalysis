"""
Stock Monitor - single-service backend.

Serves both the JSON API and the dashboard page, so there is only one
thing to run and one thing to deploy.

Run:  python app.py       ->  http://localhost:8000
"""

import asyncio
import hashlib
import hmac
import json
import os
import secrets
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
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
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

# Which pivot system the app runs on unless the setting says otherwise.
# Declared here because SETTING_DEFAULTS below needs it.
DEFAULT_PIVOT = "camarilla"

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
# Build stamp
#
# Shown in the footer so you can tell at a glance whether the page you are
# looking at is the latest deploy, or a cached copy of an older one.
# ----------------------------------------------------------------------------

STARTED_AT = datetime.now(ZoneInfo("Asia/Kolkata"))


def build_id() -> str:
    """
    Short identifier for the running code.

    Render sets RENDER_GIT_COMMIT, so on the hosted copy this is the real
    commit. Locally it falls back to asking git, and if that fails (no git, or
    a copied folder) to the modification time of app.py, which still changes
    whenever the code does.
    """
    commit = (os.environ.get("RENDER_GIT_COMMIT")
              or os.environ.get("SOURCE_VERSION")   # some hosts use this name
              or os.environ.get("GIT_COMMIT"))
    if commit:
        return commit[:7]
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(BASE_DIR), capture_output=True,
                             text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001 - a missing git must not break startup
        pass
    try:
        stamp = datetime.fromtimestamp(
            (BASE_DIR / "app.py").stat().st_mtime, ZoneInfo("Asia/Kolkata"))
        return "f" + stamp.strftime("%m%d%H%M")
    except OSError:
        return "unknown"


def code_changed_at() -> Optional[str]:
    """When app.py was last modified - the honest 'version' of the code."""
    try:
        return datetime.fromtimestamp(
            (BASE_DIR / "app.py").stat().st_mtime,
            ZoneInfo("Asia/Kolkata")).isoformat()
    except OSError:
        return None


def build_info() -> Dict:
    return {
        "build": build_id(),
        "code_changed_at": code_changed_at(),
        "started_at": STARTED_AT.isoformat(),
    }


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
            CREATE TABLE IF NOT EXISTS candidates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT NOT NULL,
                exchange    TEXT NOT NULL,
                band_pct    REAL NOT NULL,
                level_name  TEXT NOT NULL,
                level_price REAL NOT NULL,
                price       REAL NOT NULL,
                status      TEXT NOT NULL DEFAULT 'WATCHING',
                scanned_at  TEXT NOT NULL,
                UNIQUE(symbol, exchange, scanned_at)
            );
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT NOT NULL,
                exchange      TEXT NOT NULL,
                level_name    TEXT NOT NULL,
                level_price   REAL NOT NULL,
                entry_price   REAL NOT NULL,
                entry_at      TEXT NOT NULL,
                entry_reason  TEXT NOT NULL,
                stop_price    REAL NOT NULL,
                target_price  REAL NOT NULL,
                high_price    REAL NOT NULL,
                trail_step    REAL NOT NULL DEFAULT -1,
                status        TEXT NOT NULL DEFAULT 'OPEN',
                exit_price    REAL,
                exit_at       TEXT,
                exit_reason   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(status, symbol);
            CREATE TABLE IF NOT EXISTS scan_hits (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT NOT NULL,
                exchange    TEXT NOT NULL,
                timeframe   TEXT NOT NULL,
                scanned_at  TEXT NOT NULL,
                bar_at      TEXT NOT NULL,
                price       REAL NOT NULL,
                h3          REAL NOT NULL,
                l3          REAL NOT NULL,
                prev_h3     REAL NOT NULL,
                prev_l3     REAL NOT NULL,
                band_pct    REAL NOT NULL,
                depth_pct   REAL NOT NULL,
                streak      INTEGER NOT NULL,
                rank_score  REAL NOT NULL,
                rules       TEXT NOT NULL,
                UNIQUE(symbol, exchange, timeframe, bar_at)
            );
            CREATE INDEX IF NOT EXISTS idx_scan_hits
                ON scan_hits(timeframe, scanned_at, rank_score);
            CREATE TABLE IF NOT EXISTS breakouts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT NOT NULL,
                exchange    TEXT NOT NULL,
                scanned_at  TEXT NOT NULL,
                price       REAL NOT NULL,
                level_name  TEXT NOT NULL,
                level_price REAL NOT NULL,
                above_pct   REAL NOT NULL,
                h3          REAL NOT NULL,
                h4          REAL NOT NULL,
                pp          REAL NOT NULL,
                coiled      INTEGER NOT NULL DEFAULT 0,
                turnover_cr REAL,
                UNIQUE(symbol, exchange, scanned_at)
            );
            CREATE INDEX IF NOT EXISTS idx_breakouts
                ON breakouts(scanned_at, above_pct);
            CREATE TABLE IF NOT EXISTS users (
                username    TEXT PRIMARY KEY,
                salt        TEXT NOT NULL,
                pw_hash     TEXT NOT NULL,
                created_at  TEXT NOT NULL
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
    # ---- breakout strategy (see STRATEGY section) ----
    "strategy_enabled": "0",
    "strategy_level": "h3",        # which Camarilla level is the trigger line
    "band_max_pct": "1.5",         # H3-L3 width, as % of price, to count as contracted
    "entry_buffer_pct": "0.1",     # trigger this far above the level
    "entry_mode": "either",        # buffer | candle | either
    "sl_pct": "0.3",               # stop this far BELOW the level
    "target_pct": "1.5",           # book this far above entry
    "trail_steps": "0.5:0,1.0:0.5,1.5:1.0",   # gain% : move stop to entry+this%
    "scan_universe": "",           # blank = STARTER_SYMBOLS
    # ---- contraction scanner (see SCANNER section) ----
    "scan_source": "fno",          # fno | starter | custom
    "fno_universe": "",            # blank = FNO_SYMBOLS
    "min_turnover_cr": "50",       # skip names thinner than this (Rs crore/day)
    "rank_min_score": "0",         # hide hits ranking below this
    # Which pivot system everything runs on. Changing it changes the score,
    # the Scanner and the Buy list together - see PIVOT_SYSTEMS.
    "pivot_system": DEFAULT_PIVOT,
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


# ----------------------------------------------------------------------------
# Login
#
# A gate, not an identity system: both users see the same watchlists. Passwords
# are salted and hashed rather than stored as typed, and the session cookie is
# signed, but there is no password-change screen, no lockout and no per-user
# data yet. Those are the backlog item.
# ----------------------------------------------------------------------------

SEED_USERS = {"pnk": "123", "kau": "123"}
SESSION_COOKIE = "pivotdesk_session"
SESSION_DAYS = 30
OPEN_PATHS = {"/api/health", "/api/login", "/api/me"}


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                               120_000).hex()


def seed_users() -> None:
    with db() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        now = datetime.now().isoformat()
        for name, password in SEED_USERS.items():
            salt = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO users (username, salt, pw_hash, created_at) VALUES (?,?,?,?)",
                (name, salt, hash_password(password, salt), now))


def check_password(username: str, password: str) -> bool:
    with db() as conn:
        row = conn.execute("SELECT salt, pw_hash FROM users WHERE username = ?",
                           (username.strip().lower(),)).fetchone()
    if not row:
        return False
    return hmac.compare_digest(hash_password(password, row["salt"]), row["pw_hash"])


def session_secret() -> str:
    """Generated once and kept in the database, so restarting the app does not
    sign everyone out."""
    secret = get_settings().get("session_secret", "")
    if not secret:
        secret = secrets.token_hex(32)
        save_settings({"session_secret": secret})
    return secret


def make_token(username: str) -> str:
    expires = int(time.time()) + SESSION_DAYS * 86400
    body = f"{username}|{expires}"
    sig = hmac.new(session_secret().encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{sig}"


def read_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        username, expires, sig = token.split("|")
    except ValueError:
        return None
    expected = hmac.new(session_secret().encode(), f"{username}|{expires}".encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected) or int(expires) < time.time():
        return None
    return username


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


# NSE derivatives ("futures segment") stocks - the universe the Chartink scan
# runs over.
#
# IMPORTANT: this list is typed in, not fetched. NSE publishes the official F&O
# list and revises it every few months (names are added, dropped, renamed after
# mergers). Nothing here reaches nseindia.com to refresh it, so treat this as a
# starting point and check it against NSE's own list periodically. It is
# editable in Settings without touching this file, and a symbol that no longer
# exists simply fails its own fetch and is reported - it does not break a scan.
FNO_SYMBOLS = [
    "ABB", "ABBOTINDIA", "ABCAPITAL", "ABFRL", "ACC", "ADANIENSOL", "ADANIENT",
    "ADANIGREEN", "ADANIPORTS", "ALKEM", "AMBUJACEM", "ANGELONE", "APLAPOLLO",
    "APOLLOHOSP", "APOLLOTYRE", "ASHOKLEY", "ASIANPAINT", "ASTRAL", "ATUL",
    "AUBANK", "AUROPHARMA", "AXISBANK", "BAJAJ-AUTO", "BAJAJFINSV",
    "BAJFINANCE", "BALKRISIND", "BANDHANBNK", "BANKBARODA", "BATAINDIA", "BEL",
    "BERGEPAINT", "BHARATFORG", "BHARTIARTL", "BHEL", "BIOCON", "BOSCHLTD",
    "BPCL", "BRITANNIA", "BSOFT", "CANBK", "CANFINHOME", "CHAMBLFERT",
    "CHOLAFIN", "CIPLA", "COALINDIA", "COFORGE", "COLPAL", "CONCOR",
    "COROMANDEL", "CROMPTON", "CUB", "CUMMINSIND", "DABUR", "DALBHARAT",
    "DEEPAKNTR", "DIVISLAB", "DIXON", "DLF", "DRREDDY", "EICHERMOT", "ESCORTS",
    "EXIDEIND", "FEDERALBNK", "GAIL", "GLENMARK", "GMRAIRPORT", "GNFC",
    "GODREJCP", "GODREJPROP", "GRANULES", "GRASIM", "GUJGASLTD", "HAL",
    "HAVELLS", "HCLTECH", "HDFCAMC", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDCOPPER", "HINDPETRO", "HINDUNILVR", "ICICIBANK",
    "ICICIGI", "ICICIPRULI", "IDEA", "IDFCFIRSTB", "IEX", "IGL", "INDHOTEL",
    "INDIACEM", "INDIAMART", "INDIGO", "INDUSINDBK", "INDUSTOWER", "INFY",
    "IOC", "IPCALAB", "IRCTC", "ITC", "JINDALSTEL", "JKCEMENT", "JSWSTEEL",
    "JUBLFOOD", "KOTAKBANK", "LALPATHLAB", "LAURUSLABS", "LICHSGFIN", "LT",
    "LTF", "LTIM", "LTTS", "LUPIN", "M&M", "M&MFIN", "MANAPPURAM", "MARICO",
    "MARUTI", "MCX", "METROPOLIS", "MFSL", "MGL", "MOTHERSON", "MPHASIS",
    "MRF", "MUTHOOTFIN", "NATIONALUM", "NAUKRI", "NAVINFLUOR", "NESTLEIND",
    "NMDC", "NTPC", "OBEROIRLTY", "OFSS", "ONGC", "PAGEIND", "PEL",
    "PERSISTENT", "PETRONET", "PFC", "PIDILITIND", "PIIND", "PNB", "POLYCAB",
    "POWERGRID", "PVRINOX", "RAMCOCEM", "RBLBANK", "RECLTD", "RELIANCE",
    "SAIL", "SBICARD", "SBILIFE", "SBIN", "SHREECEM", "SHRIRAMFIN", "SIEMENS",
    "SRF", "SUNPHARMA", "SUNTV", "SYNGENE", "TATACHEM", "TATACOMM",
    "TATACONSUM", "TATAMOTORS", "TATAPOWER", "TATASTEEL", "TCS", "TECHM",
    "TITAN", "TORNTPHARM", "TRENT", "TVSMOTOR", "UBL", "ULTRACEMCO",
    "UNITDSPR", "UPL", "VEDL", "VOLTAS", "WIPRO", "ZYDUSLIFE",
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


# ----------------------------------------------------------------------------
# Pivot systems
#
# All six take the PREVIOUS completed session's H/L/C (DeMark also needs the
# open) and return a flat dict of level name -> price.
#
# Every system is normalised onto the same two ideas so the rest of the app
# doesn't have to know which one is selected:
#
#   band     - the two levels the contraction test compares between bars.
#   breakout - the levels the Buy list checks price against, low to high.
#
# For Camarilla the band is H3/L3, which is what the Chartink filter uses.
# For CPR it is TC/BC, the central range, whose width is read exactly the same
# way - narrow means a trending day is expected. For the rest it is R1/S1.
# ----------------------------------------------------------------------------

PIVOT_SYSTEMS = {
    "camarilla": {
        "label": "Camarilla",
        "band": ("h3", "l3"),
        "breakout": ("h3", "h4"),
        "order": ["l4", "l3", "l2", "l1", "pp", "h1", "h2", "h3", "h4"],
        "note": "Chartink's 0.275 is this system's H3/L3 coefficient.",
    },
    "classic": {
        "label": "Classic",
        "band": ("r1", "s1"),
        "breakout": ("r2", "r3"),
        "order": ["s3", "s2", "s1", "pp", "r1", "r2", "r3"],
        "note": "The most widely used system. Levels sit wider than Camarilla's.",
    },
    "fibonacci": {
        "label": "Fibonacci",
        "band": ("r1", "s1"),
        "breakout": ("r2", "r3"),
        "order": ["s3", "s2", "s1", "pp", "r1", "r2", "r3"],
        "note": "Levels at 38.2%, 61.8% and 100% of the previous range.",
    },
    "woodie": {
        "label": "Woodie",
        "band": ("r1", "s1"),
        "breakout": ("r1", "r2"),
        "order": ["s2", "s1", "pp", "r1", "r2"],
        "note": "Weights the previous close more heavily in the central pivot.",
    },
    "demark": {
        "label": "DeMark",
        "band": ("r1", "s1"),
        "breakout": ("r1",),
        "order": ["s1", "pp", "r1"],
        "note": "Branches on whether the session closed above or below its open.",
    },
    "cpr": {
        "label": "CPR",
        "band": ("tc", "bc"),
        "breakout": ("tc",),
        "order": ["bc", "pp", "tc"],
        "note": "A narrow CPR is read as a trending day, a wide one as sideways.",
    },
}


def pretty_level(key: str) -> str:
    """
    What to call a level on screen.

    Camarilla's levels are H1-H4 and L1-L4 internally, but everyone - Kite,
    Chartink, the owner - says R and S. One naming scheme across all six
    systems, so "R3" means the same thing wherever it appears.
    """
    if key.startswith("h") and key[1:].isdigit():
        return "R" + key[1:]
    if key.startswith("l") and key[1:].isdigit():
        return "S" + key[1:]
    return key.upper()


def pivot_system() -> str:
    chosen = get_settings().get("pivot_system", DEFAULT_PIVOT)
    return chosen if chosen in PIVOT_SYSTEMS else DEFAULT_PIVOT


def classic_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    pp = (high + low + close) / 3
    rng = high - low
    return {
        "pp": pp,
        "r1": 2 * pp - low,
        "r2": pp + rng,
        "r3": high + 2 * (pp - low),
        "s1": 2 * pp - high,
        "s2": pp - rng,
        "s3": low - 2 * (high - pp),
    }


def fibonacci_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    pp = (high + low + close) / 3
    rng = high - low
    return {
        "pp": pp,
        "r1": pp + 0.382 * rng,
        "r2": pp + 0.618 * rng,
        "r3": pp + 1.000 * rng,
        "s1": pp - 0.382 * rng,
        "s2": pp - 0.618 * rng,
        "s3": pp - 1.000 * rng,
    }


def woodie_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    # The close counts twice here - that is the whole point of Woodie.
    pp = (high + low + 2 * close) / 4
    rng = high - low
    return {
        "pp": pp,
        "r1": 2 * pp - low,
        "r2": pp + rng,
        "s1": 2 * pp - high,
        "s2": pp - rng,
    }


def demark_pivots(high: float, low: float, close: float,
                  open_: float) -> Dict[str, float]:
    """
    DeMark picks its X differently depending on how the session finished, so
    it needs the OPEN as well as H/L/C. Without an open it cannot be computed
    and the caller falls back.
    """
    if close < open_:
        x = high + 2 * low + close
    elif close > open_:
        x = 2 * high + low + close
    else:
        x = high + low + 2 * close
    return {"pp": x / 4, "r1": x / 2 - low, "s1": x / 2 - high}


def cpr_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    """
    Central Pivot Range. TC = 2P - BC can come out BELOW BC; by convention the
    higher of the two is plotted as the top, so they are swapped if needed.
    """
    pp = (high + low + close) / 3
    bc = (high + low) / 2
    tc = 2 * pp - bc
    if tc < bc:
        tc, bc = bc, tc
    return {"pp": pp, "tc": tc, "bc": bc}


def pivot_levels(high: float, low: float, close: float,
                 open_: Optional[float] = None,
                 system: Optional[str] = None) -> Dict[str, float]:
    """Levels for the chosen system, from the previous session's bar."""
    system = system if system in PIVOT_SYSTEMS else (system or DEFAULT_PIVOT)
    if system == "camarilla":
        return camarilla(high, low, close)
    if system == "classic":
        return classic_pivots(high, low, close)
    if system == "fibonacci":
        return fibonacci_pivots(high, low, close)
    if system == "woodie":
        return woodie_pivots(high, low, close)
    if system == "cpr":
        return cpr_pivots(high, low, close)
    if system == "demark":
        if open_ is None:
            # No open available - Classic is the closest thing that only needs
            # H/L/C, and the caller is told which system actually ran.
            return classic_pivots(high, low, close)
        return demark_pivots(high, low, close, open_)
    return camarilla(high, low, close)


def band_of(levels: Dict[str, float], system: str) -> Optional[tuple]:
    """The (upper, lower) prices the contraction test compares."""
    up, dn = PIVOT_SYSTEMS[system]["band"]
    if up not in levels or dn not in levels:
        return None
    return levels[up], levels[dn]


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


def score_signal(price, ma_fast, ma_slow, vwap, piv, system=None):
    """
    Transparent additive score in [-3, +3]. Every component is reported so
    you can see WHY a signal fired instead of trusting a bare number.

    The pivot test uses whichever system is selected: its band's upper level
    is the resistance and the lower one the support. On Camarilla that is
    H3/L3, on CPR it is TC/BC, elsewhere R1/S1.
    """
    system = system if system in PIVOT_SYSTEMS else DEFAULT_PIVOT
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
    up_key, dn_key = PIVOT_SYSTEMS[system]["band"]
    up_name, dn_name = pretty_level(up_key), pretty_level(dn_key)
    band = band_of(piv, system)
    if band is not None:
        upper, lower = band
        if price > upper:
            score += 1
            reasons.append(("pivot", 1, f"Broken above {up_name} resistance"))
        elif price < lower:
            score -= 1
            reasons.append(("pivot", -1, f"Broken below {dn_name} support"))
        elif price > piv["pp"]:
            score += 0.5
            reasons.append(("pivot", 0.5, f"Above central pivot, inside {up_name}"))
        else:
            score -= 0.5
            reasons.append(("pivot", -0.5, f"Below central pivot, inside {dn_name}"))

    if score >= 2:
        signal = "BUY"
    elif score <= -2:
        signal = "SELL"
    else:
        signal = "HOLD"

    return signal, round(score, 2), round(abs(score) / 3, 2), reasons


def analyse(symbol: str, exchange: str) -> Dict:
    ticker = yahoo_symbol(symbol, exchange)
    # The pivot system is part of the key: switching it must not serve back a
    # result computed against the old levels.
    key = f"{ticker}|{INTRADAY_INTERVAL}|{pivot_system()}"
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
        system = pivot_system()
        prev_open = float(prev["Open"]) if "Open" in daily.columns else None
        piv = pivot_levels(float(prev["High"]), float(prev["Low"]),
                           float(prev["Close"]), prev_open, system)

        last_close = None
        if intra is not None and len(intra) >= 2:
            close = intra["Close"].dropna()
            price = float(close.iloc[-1])
            # The bar behind the live one is the last COMPLETED bar. The
            # strategy's "candle closed above the level" test needs that, not
            # the bar still forming.
            last_close = float(close.iloc[-2]) if len(close) >= 2 else None
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

        signal, score, confidence, reasons = score_signal(
            price, ma_fast, ma_slow, vwap, piv, system)
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
            "last_close": round(last_close, 2) if last_close is not None else None,
            "band_pct": round(band_pct(piv, price, system), 2) if price else None,
            "pivots": {k: round(v, 2) for k, v in piv.items()},
            "pivot_system": system,
            "pivot_label": PIVOT_SYSTEMS[system]["label"],
            # Low to high, so the UI can draw the ladder without knowing which
            # system produced it.
            "pivot_order": [k for k in PIVOT_SYSTEMS[system]["order"] if k in piv],
            "pivot_band": list(PIVOT_SYSTEMS[system]["band"]),
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


# ----------------------------------------------------------------------------
# Breakout strategy
#
# Scan for Camarilla contraction, watch the candidates for a break above the
# trigger level, then track stop, target and a stepped trailing stop until the
# position closes. Every number below is a setting.
#
# This ALERTS. It does not place orders - there is no broker connection, and
# Yahoo's data is delayed, so a fill at these prices is not something the app
# can promise. Treat the trade rows as a journal of what the rules said.
# ----------------------------------------------------------------------------

def strategy_config() -> Dict:
    cfg = get_settings()

    def num(key, fallback):
        try:
            return float(cfg[key])
        except (TypeError, ValueError, KeyError):
            return fallback

    steps = []
    for chunk in (cfg.get("trail_steps") or "").split(","):
        if ":" not in chunk:
            continue
        gain, stop_at = chunk.split(":", 1)
        try:
            steps.append((float(gain), float(stop_at)))
        except ValueError:
            continue
    steps.sort()

    level = cfg.get("strategy_level", "h3")
    return {
        "enabled": cfg.get("strategy_enabled") == "1",
        "level": level if level in ("h3", "h4") else "h3",
        "band_max_pct": num("band_max_pct", 1.5),
        "entry_buffer_pct": num("entry_buffer_pct", 0.1),
        "entry_mode": cfg.get("entry_mode", "either"),
        "sl_pct": num("sl_pct", 0.3),
        "target_pct": num("target_pct", 1.5),
        "trail_steps": steps,
        "universe": [s.strip().upper() for s in
                     (cfg.get("scan_universe") or "").replace("\n", ",").split(",")
                     if s.strip()] or list(STARTER_SYMBOLS),
    }


def band_pct(pivots: Dict[str, float], price: float,
             system: Optional[str] = None) -> Optional[float]:
    """Width of the selected system's band as a percentage of price. The
    narrower it is, the more the stock has coiled up against yesterday's
    range. On CPR this is the classic narrow-vs-wide CPR read."""
    if not price:
        return None
    band = band_of(pivots, system if system in PIVOT_SYSTEMS else DEFAULT_PIVOT)
    if band is None:
        return None
    upper, lower = band
    return (upper - lower) / price * 100


def run_scan(exchange: str = "NSE") -> Dict:
    """Score the universe and record today's contracted names as candidates."""
    cfg = strategy_config()
    universe = cfg["universe"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: analyse(s, exchange), universe))

    today = datetime.now(IST).date().isoformat()
    found, failed = [], []
    for r in results:
        if "error" in r:
            failed.append({"symbol": r["symbol"], "error": r["error"]})
            continue
        system = r.get("pivot_system", DEFAULT_PIVOT)
        width = band_pct(r["pivots"], r["price"], system)
        if width is None or width > cfg["band_max_pct"]:
            continue
        # The strategy's trigger is named h3/h4, which only exist on Camarilla.
        # On any other system fall back to that system's top breakout level so
        # the strategy keeps working rather than raising.
        level_key = cfg["level"]
        if level_key not in r["pivots"]:
            level_key = PIVOT_SYSTEMS[system]["breakout"][-1]
        if level_key not in r["pivots"]:
            continue
        found.append({
            "symbol": r["symbol"], "exchange": exchange,
            "band_pct": round(width, 2),
            "level_name": pretty_level(level_key),
            "level_price": r["pivots"][level_key],
            "price": r["price"],
        })

    found.sort(key=lambda c: c["band_pct"])
    with db() as conn:
        for c in found:
            conn.execute(
                "INSERT OR IGNORE INTO candidates"
                " (symbol, exchange, band_pct, level_name, level_price, price,"
                "  status, scanned_at) VALUES (?,?,?,?,?,?, 'WATCHING', ?)",
                (c["symbol"], c["exchange"], c["band_pct"], c["level_name"],
                 c["level_price"], c["price"], today))
    return {"scanned": len(universe), "candidates": found,
            "failed": failed, "scanned_at": today}


# ----------------------------------------------------------------------------
# SCANNER - Camarilla contraction across the F&O universe
#
# This is the Chartink scan, written out. Chartink's filter reads:
#
#     0.275 * (prevH - prevL) + prevC   >   0.275 * (H - L) + C
#     prevC - 0.275 * (prevH - prevL)   <   C - 0.275 * (H - L)
#
# 0.275 is 1.1/4 - the Camarilla H3 and L3 coefficient. So the two lines say
# "this bar's H3 is lower than the previous bar's H3" and "this bar's L3 is
# higher than the previous bar's L3": the H3-L3 band has closed in on BOTH
# sides and now sits entirely inside the previous band. The stock has coiled.
#
# That is a different test from band_pct() above, which asks whether the band
# is narrow in absolute terms (as a % of price). A stock can have a permanently
# narrow band without ever contracting, and a wide-range stock can contract
# hard. This scan uses the contraction test; band_pct is still reported so you
# can see the width too.
# ----------------------------------------------------------------------------

# period and interval to pull for each timeframe. Yahoo caps intraday history
# at 60 days for 5m/15m, so a month is comfortably inside what it will serve.
TIMEFRAMES = {
    "1d": ("6mo", "1d"),
    "15m": ("1mo", "15m"),
    "5m": ("1mo", "5m"),
}


def scanner_config() -> Dict:
    cfg = get_settings()

    def num(key, fallback):
        try:
            return float(cfg[key])
        except (TypeError, ValueError, KeyError):
            return fallback

    source = cfg.get("scan_source", "fno")
    custom = [s.strip().upper() for s in
              (cfg.get("fno_universe") or "").replace("\n", ",").split(",")
              if s.strip()]
    if source == "custom" and custom:
        universe = custom
    elif source == "starter":
        universe = list(STARTER_SYMBOLS)
    else:
        universe = custom or list(FNO_SYMBOLS)

    return {
        "source": source,
        "universe": universe,
        "min_turnover_cr": num("min_turnover_cr", 50.0),
        "rank_min_score": num("rank_min_score", 0.0),
    }


def completed_bars(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """
    Drop the bar that is still forming.

    While the market is open the final row - daily or intraday - is a partial
    bar whose high, low and close are still moving. A contraction test against
    a half-built bar flips as the day goes on, so the scan only ever reads
    completed bars. Once the market is shut every row is final and all of them
    count.
    """
    if df is None or df.empty:
        return df
    return df.iloc[:-1] if market_is_open() else df


def bar_pivots(bar, system: Optional[str] = None) -> Dict[str, float]:
    """Pivot levels from one bar, in whichever system is selected."""
    open_ = float(bar["Open"]) if "Open" in bar.index else None
    return pivot_levels(float(bar["High"]), float(bar["Low"]),
                        float(bar["Close"]), open_, system or pivot_system())


def is_contraction(cur: Dict[str, float], prev: Dict[str, float],
                   system: Optional[str] = None) -> bool:
    """
    The Chartink test: this band sits strictly inside the previous one.

    On Camarilla the band is H3/L3, which is exactly what Chartink compares.
    On any other system it is that system's band, so the scan still means
    "the band closed in on both sides" - but it stops matching Chartink.
    """
    system = system if system in PIVOT_SYSTEMS else DEFAULT_PIVOT
    a, b = band_of(cur, system), band_of(prev, system)
    if a is None or b is None:
        return False
    return a[0] < b[0] and a[1] > b[1]


def contraction_depth(cur: Dict[str, float], prev: Dict[str, float],
                      system: Optional[str] = None) -> float:
    """How much tighter the band got, as a % of the previous band's width."""
    system = system if system in PIVOT_SYSTEMS else DEFAULT_PIVOT
    a, b = band_of(cur, system), band_of(prev, system)
    if a is None or b is None:
        return 0.0
    prev_width = b[0] - b[1]
    if prev_width <= 0:
        return 0.0
    return (prev_width - (a[0] - a[1])) / prev_width * 100


def contraction_streak(df: pd.DataFrame, system: Optional[str] = None) -> int:
    """
    How many bars in a row, counting back from the last one, contracted.

    A band that has narrowed three sessions running is a tighter spring than
    one that only narrowed today, so this feeds the rank.
    """
    system = system or pivot_system()
    streak = 0
    for i in range(len(df) - 1, 0, -1):
        cur = bar_pivots(df.iloc[i], system)
        prev = bar_pivots(df.iloc[i - 1], system)
        if not is_contraction(cur, prev, system):
            break
        streak += 1
    return streak


def narrow_range(df: pd.DataFrame, n: int) -> bool:
    """True if the last bar's range is the narrowest of the last n bars."""
    if len(df) < n:
        return False
    rng = (df["High"] - df["Low"]).tail(n)
    return bool(rng.iloc[-1] <= rng.min())


def volume_ratio(df: pd.DataFrame, lookback: int = 20) -> Optional[float]:
    """Last bar's volume against the average of the bars before it."""
    vol = df["Volume"].dropna()
    if len(vol) < 3:
        return None
    base = vol.iloc[-(lookback + 1):-1]
    if base.empty or base.mean() <= 0:
        return None
    return float(vol.iloc[-1] / base.mean())


def avg_turnover_cr(df: pd.DataFrame, days: int = 20) -> Optional[float]:
    """
    Average traded value per DAY in Rs crore.

    Intraday bars are summed within each date first, so this means the same
    thing whichever timeframe is being scanned.
    """
    if df is None or df.empty or "Volume" not in df:
        return None
    value = (df["Close"] * df["Volume"]).groupby(df.index.date).sum()
    value = value.tail(days)
    if value.empty:
        return None
    return float(value.mean()) / 1e7


def rank_hit(df: pd.DataFrame, depth_pct: float, streak: int) -> tuple:
    """
    Additive, transparent rank - same idea as score_signal(): every point
    added is reported with the reason, so a high-ranked name can be argued
    with rather than taken on faith.

    Liquidity is not scored here; it is a filter applied before ranking.
    """
    score, rules = 0.0, []

    if depth_pct >= 25:
        score += 1
        rules.append(("depth", 1, f"Band {depth_pct:.0f}% tighter than the previous bar"))
    elif depth_pct >= 10:
        score += 0.5
        rules.append(("depth", 0.5, f"Band {depth_pct:.0f}% tighter than the previous bar"))

    if streak >= 3:
        score += 1
        rules.append(("streak", 1, f"Contracting {streak} bars in a row"))
    elif streak == 2:
        score += 0.5
        rules.append(("streak", 0.5, "Contracting 2 bars in a row"))

    vr = volume_ratio(df)
    if vr is not None and vr < 0.8:
        score += 1
        rules.append(("volume", 1, f"Volume dried up to {vr:.0%} of its average"))

    if narrow_range(df, 7):
        score += 1
        rules.append(("range", 1, "Narrowest range of the last 7 bars (NR7)"))
    elif narrow_range(df, 4):
        score += 0.5
        rules.append(("range", 0.5, "Narrowest range of the last 4 bars (NR4)"))

    close = df["Close"].dropna()
    if len(close) >= 20:
        ma20 = float(close.rolling(20).mean().iloc[-1])
        if float(close.iloc[-1]) > ma20:
            score += 0.5
            rules.append(("trend", 0.5, "Holding above its 20-bar average"))

    return round(score, 2), rules


def batch_history(symbols: List[str], exchange: str, period: str,
                  interval: str, chunk: int = 40) -> Dict[str, pd.DataFrame]:
    """
    Fetch many symbols per Yahoo request instead of one at a time.

    The old per-symbol scan was fine for 60 names; ~190 would be 190 separate
    requests every run and Yahoo starts refusing well before that. yfinance
    accepts a list and batches internally, so this is a handful of calls.
    """
    out: Dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), chunk):
        block = symbols[i:i + chunk]
        tickers = [yahoo_symbol(s, exchange) for s in block]
        try:
            raw = yf.download(tickers, period=period, interval=interval,
                              progress=False, auto_adjust=False,
                              group_by="ticker", threads=True)
        except Exception:  # noqa: BLE001 - one bad block must not kill the scan
            continue
        if raw is None or raw.empty:
            continue
        for sym, tk in zip(block, tickers):
            try:
                df = raw[tk] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.dropna(how="all")
                if df is not None and not df.empty:
                    out[sym] = df
            except (KeyError, IndexError):
                continue
    return out


def scan_contraction(timeframe: str = "1d", exchange: str = "NSE") -> Dict:
    """
    Run the Chartink contraction test across the universe on one timeframe
    and store the ranked hits.

    On "1d" the hits are the list for the NEXT session: the levels come from
    the last completed daily bar, which is exactly what tomorrow trades against.
    On "15m" and "5m" they are contractions forming within the current session.
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe {timeframe}")
    period, interval = TIMEFRAMES[timeframe]
    cfg = scanner_config()
    universe = cfg["universe"]

    system = pivot_system()
    frames = batch_history(universe, exchange, period, interval)
    scanned_at = datetime.now(IST).isoformat()
    hits, skipped, failed = [], 0, []

    for sym in universe:
        df = frames.get(sym)
        if df is None:
            failed.append(sym)
            continue
        df = completed_bars(df)
        if df is None or len(df) < 2:
            failed.append(sym)
            continue

        try:
            cur = bar_pivots(df.iloc[-1], system)
            prev = bar_pivots(df.iloc[-2], system)
            price = float(df["Close"].iloc[-1])
        except (KeyError, ValueError, TypeError):
            failed.append(sym)
            continue

        if not is_contraction(cur, prev, system):
            continue

        turnover = avg_turnover_cr(df)
        if turnover is not None and turnover < cfg["min_turnover_cr"]:
            skipped += 1
            continue

        depth = contraction_depth(cur, prev, system)
        streak = contraction_streak(df, system)
        score, rules = rank_hit(df, depth, streak)
        if score < cfg["rank_min_score"]:
            continue

        # h3/l3 are the stored column names for the band, whatever the system
        # actually calls those two levels.
        (cur_up, cur_dn) = band_of(cur, system)
        (prev_up, prev_dn) = band_of(prev, system)
        hits.append({
            "symbol": sym, "exchange": exchange, "timeframe": timeframe,
            "bar_at": df.index[-1].isoformat(),
            "price": round(price, 2),
            "h3": round(cur_up, 2), "l3": round(cur_dn, 2),
            "prev_h3": round(prev_up, 2), "prev_l3": round(prev_dn, 2),
            "band_pct": round((cur_up - cur_dn) / price * 100, 2) if price else 0,
            "depth_pct": round(depth, 2),
            "streak": streak,
            "turnover_cr": round(turnover, 1) if turnover is not None else None,
            "rank_score": score,
            "rules": [{"kind": k, "weight": w, "text": t} for k, w, t in rules],
        })

    # Best-ranked first; a tighter band breaks a tie.
    hits.sort(key=lambda h: (-h["rank_score"], h["band_pct"]))

    with db() as conn:
        for h in hits:
            conn.execute(
                "INSERT OR REPLACE INTO scan_hits (symbol, exchange, timeframe,"
                " scanned_at, bar_at, price, h3, l3, prev_h3, prev_l3, band_pct,"
                " depth_pct, streak, rank_score, rules)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (h["symbol"], h["exchange"], h["timeframe"], scanned_at,
                 h["bar_at"], h["price"], h["h3"], h["l3"], h["prev_h3"],
                 h["prev_l3"], h["band_pct"], h["depth_pct"], h["streak"],
                 h["rank_score"], json.dumps(h["rules"])))

    return {
        "timeframe": timeframe,
        "scanned": len(universe),
        "fetched": len(frames),
        "hits": hits,
        "skipped_illiquid": skipped,
        "failed": failed,
        "scanned_at": scanned_at,
        "bars_complete_only": market_is_open(),
        "pivot_system": system,
        "pivot_label": PIVOT_SYSTEMS[system]["label"],
        "band_label": "/".join(pretty_level(k) for k in PIVOT_SYSTEMS[system]["band"]),
    }


# ----------------------------------------------------------------------------
# BUY LIST - F&O stocks trading above R3 / R4
#
# "R3" and "R4" are the Camarilla H3 and H4. Price above H3 is the standard
# Camarilla long trigger; above H4 is the stronger version of the same idea.
#
# Levels come from the PREVIOUS completed session, the same convention
# analyse() uses, so this list and the Signals cards always agree about where
# a level sits.
#
# This deliberately does NOT filter to contracting stocks - the owner asked for
# any F&O name that crosses R3 or R4. Names that are ALSO coiled are flagged,
# because a break out of a contraction is the more interesting of the two.
# ----------------------------------------------------------------------------

def scan_breakouts(exchange: str = "NSE") -> Dict:
    cfg = scanner_config()
    universe = cfg["universe"]

    system = pivot_system()
    # Levels this system treats as a break, low to high. Camarilla has two
    # (H3 then H4); DeMark and CPR have only one.
    breakout_keys = PIVOT_SYSTEMS[system]["breakout"]

    daily = batch_history(universe, exchange, "3mo", "1d")
    intra = batch_history(universe, exchange, "5d", INTRADAY_INTERVAL)

    scanned_at = datetime.now(IST).isoformat()
    hits, skipped, failed = [], 0, []

    for sym in universe:
        d = daily.get(sym)
        if d is None or len(d) < 2:
            failed.append(sym)
            continue
        try:
            # Same convention as analyse(): the bar behind the latest daily one
            # is the previous completed session, and it sets today's levels.
            prev = d.iloc[-2]
            piv = bar_pivots(prev, system)

            live = intra.get(sym)
            if live is not None and not live.empty:
                price = float(live["Close"].dropna().iloc[-1])
                basis = f"{INTRADAY_INTERVAL} bars"
            else:
                price = float(d["Close"].dropna().iloc[-1])
                basis = "daily close"
        except (KeyError, ValueError, TypeError, IndexError):
            failed.append(sym)
            continue

        # The highest breakout level price has cleared, if any.
        cleared = [k for k in breakout_keys if k in piv and price > piv[k]]
        if not cleared:
            continue

        turnover = avg_turnover_cr(d)
        if turnover is not None and turnover < cfg["min_turnover_cr"]:
            skipped += 1
            continue

        top = cleared[-1]
        level_name = pretty_level(top)
        level_price = piv[top]
        # The lowest breakout level, reported as context on every row.
        lo_key = breakout_keys[0]
        hi_key = breakout_keys[-1]

        # Is it also coiled? Cheap to answer - the daily bars are already here.
        coiled = False
        bars = completed_bars(d)
        if bars is not None and len(bars) >= 2:
            coiled = is_contraction(bar_pivots(bars.iloc[-1], system),
                                    bar_pivots(bars.iloc[-2], system), system)

        hits.append({
            "symbol": sym, "exchange": exchange,
            "price": round(price, 2),
            "level_name": level_name,
            "level_price": round(level_price, 2),
            "above_pct": round((price - level_price) / level_price * 100, 2),
            # Stored as h3/h4 whatever this system calls the two levels.
            "h3": round(piv[lo_key], 2), "h4": round(piv[hi_key], 2),
            "h3_name": pretty_level(lo_key), "h4_name": pretty_level(hi_key),
            "pp": round(piv["pp"], 2),
            "coiled": coiled,
            "turnover_cr": round(turnover, 1) if turnover is not None else None,
            "basis": basis,
        })

    # The higher level first, and within each the freshest break - a stock
    # barely over the level has not already made the move you would be buying.
    top_name = pretty_level(breakout_keys[-1])
    hits.sort(key=lambda h: (h["level_name"] != top_name, h["above_pct"]))

    with db() as conn:
        for h in hits:
            conn.execute(
                "INSERT OR REPLACE INTO breakouts (symbol, exchange, scanned_at,"
                " price, level_name, level_price, above_pct, h3, h4, pp, coiled,"
                " turnover_cr) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (h["symbol"], h["exchange"], scanned_at, h["price"],
                 h["level_name"], h["level_price"], h["above_pct"], h["h3"],
                 h["h4"], h["pp"], int(h["coiled"]), h["turnover_cr"]))

    return {"scanned": len(universe), "buys": hits, "skipped_illiquid": skipped,
            "failed": failed, "scanned_at": scanned_at,
            "market_open": market_is_open(),
            "pivot_system": system,
            "pivot_label": PIVOT_SYSTEMS[system]["label"],
            "levels": [pretty_level(k) for k in breakout_keys]}


def open_trade(cand: Dict, quote: Dict, reason: str, cfg: Dict) -> Dict:
    level = cand["level_price"]
    price = quote["price"]
    trade = {
        "symbol": cand["symbol"], "exchange": cand["exchange"],
        "level_name": cand["level_name"], "level_price": level,
        "entry_price": price, "entry_at": datetime.now(IST).isoformat(),
        "entry_reason": reason,
        "stop_price": round(level * (1 - cfg["sl_pct"] / 100), 2),
        "target_price": round(price * (1 + cfg["target_pct"] / 100), 2),
        "high_price": price,
    }
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO trades (symbol, exchange, level_name, level_price,"
            " entry_price, entry_at, entry_reason, stop_price, target_price,"
            " high_price) VALUES (?,?,?,?,?,?,?,?,?,?)",
            tuple(trade[k] for k in ("symbol", "exchange", "level_name",
                                     "level_price", "entry_price", "entry_at",
                                     "entry_reason", "stop_price", "target_price",
                                     "high_price")))
        conn.execute("UPDATE candidates SET status='ENTERED'"
                     " WHERE symbol=? AND exchange=? AND status='WATCHING'",
                     (cand["symbol"], cand["exchange"]))
    trade["id"] = cur.lastrowid
    return trade


def manage_trade(trade: Dict, price: float, cfg: Dict) -> List[Dict]:
    """Update high water mark, step the stop up, and close on stop or target.
    Returns the events worth messaging about."""
    events: List[Dict] = []
    high = max(trade["high_price"], price)
    stop = trade["stop_price"]
    step = trade["trail_step"]
    entry = trade["entry_price"]

    gain_pct = (high - entry) / entry * 100 if entry else 0
    for gain_at, stop_at in cfg["trail_steps"]:
        if gain_pct >= gain_at > step:
            new_stop = round(entry * (1 + stop_at / 100), 2)
            if new_stop > stop:
                stop, step = new_stop, gain_at
                events.append({"kind": "TRAIL", "trade": trade, "price": price,
                               "stop": stop, "note": f"+{gain_at:g}% reached"})

    status, exit_reason = trade["status"], None
    if price <= stop:
        status, exit_reason = "STOPPED", "Stop hit"
    elif price >= trade["target_price"]:
        status, exit_reason = "TARGET", "Target hit"

    with db() as conn:
        if exit_reason:
            conn.execute(
                "UPDATE trades SET high_price=?, stop_price=?, trail_step=?,"
                " status=?, exit_price=?, exit_at=?, exit_reason=? WHERE id=?",
                (high, stop, step, status, price, datetime.now(IST).isoformat(),
                 exit_reason, trade["id"]))
            events.append({"kind": status, "trade": trade, "price": price,
                           "stop": stop, "note": exit_reason})
        else:
            conn.execute(
                "UPDATE trades SET high_price=?, stop_price=?, trail_step=? WHERE id=?",
                (high, stop, step, trade["id"]))
    return events


def format_strategy_alert(event: Dict) -> str:
    t, price = event["trade"], event["price"]
    entry = t["entry_price"]
    move = (price - entry) / entry * 100 if entry else 0
    head = {
        "ENTRY": f"▲ ENTRY  {t['symbol']} ({t['exchange']})",
        "TRAIL": f"↗ TRAIL  {t['symbol']} ({t['exchange']})",
        "TARGET": f"★ TARGET {t['symbol']} ({t['exchange']})",
        "STOPPED": f"✖ STOP   {t['symbol']} ({t['exchange']})",
    }.get(event["kind"], t["symbol"])
    lines = [head, f"₹{price:,.2f}   {move:+.2f}% from entry ₹{entry:,.2f}"]
    if event["kind"] == "ENTRY":
        lines.append(f"{t['level_name']} ₹{t['level_price']:,.2f} · {event['note']}")
        lines.append(f"Stop ₹{t['stop_price']:,.2f}   Target ₹{t['target_price']:,.2f}")
    else:
        lines.append(f"{event['note']} · stop now ₹{event['stop']:,.2f}")
    lines.append("Delayed Yahoo data, no order placed. Not advice.")
    return "\n".join(lines)


def run_strategy_sweep() -> Dict:
    """Check today's candidates for entry triggers, then manage anything open."""
    cfg = strategy_config()
    today = datetime.now(IST).date().isoformat()
    with db() as conn:
        cands = [dict(r) for r in conn.execute(
            "SELECT * FROM candidates WHERE scanned_at=? AND status='WATCHING'",
            (today,)).fetchall()]
        open_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE status='OPEN'").fetchall()]

    symbols = {(c["symbol"], c["exchange"]) for c in cands}
    symbols |= {(t["symbol"], t["exchange"]) for t in open_rows}
    quotes: Dict[tuple, Dict] = {}
    if symbols:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for q in pool.map(lambda s: analyse(s[0], s[1]), symbols):
                quotes[(q["symbol"], q["exchange"])] = q

    events: List[Dict] = []

    for cand in cands:
        q = quotes.get((cand["symbol"], cand["exchange"]))
        if not q or "error" in q:
            continue
        level = cand["level_price"]
        trigger = level * (1 + cfg["entry_buffer_pct"] / 100)
        by_buffer = q["price"] >= trigger
        # The last COMPLETED bar, not the one still forming.
        by_candle = q.get("last_close") is not None and q["last_close"] > level
        mode = cfg["entry_mode"]
        hit = (by_buffer if mode == "buffer" else
               by_candle if mode == "candle" else (by_buffer or by_candle))
        if not hit:
            continue
        reason = (f"{cfg['entry_buffer_pct']:g}% above {cand['level_name']}"
                  if by_buffer else
                  f"{INTRADAY_INTERVAL} candle closed above {cand['level_name']}")
        trade = open_trade(cand, q, reason, cfg)
        events.append({"kind": "ENTRY", "trade": trade, "price": q["price"],
                       "stop": trade["stop_price"], "note": reason})

    for trade in open_rows:
        q = quotes.get((trade["symbol"], trade["exchange"]))
        if not q or "error" in q:
            continue
        events.extend(manage_trade(trade, q["price"], cfg))

    sent, errors = 0, []
    if events:
        settings = get_settings()
        token = settings["telegram_token"].strip()
        chats = chat_id_list(settings["telegram_chat_ids"])
        if token and chats:
            for event in events:
                out = send_telegram(token, chats, format_strategy_alert(event))
                sent += out["sent"]
                errors.extend(out["errors"])
    return {"candidates": len(cands), "open": len(open_rows),
            "events": [{"kind": e["kind"], "symbol": e["trade"]["symbol"],
                        "price": e["price"], "note": e["note"]} for e in events],
            "sent": sent, "errors": errors}


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
                if strategy_config()["enabled"]:
                    await asyncio.to_thread(run_strategy_sweep)
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
    strategy_enabled: Optional[bool] = None
    strategy_level: Optional[str] = None
    band_max_pct: Optional[float] = None
    entry_buffer_pct: Optional[float] = None
    entry_mode: Optional[str] = None
    sl_pct: Optional[float] = None
    target_pct: Optional[float] = None
    trail_steps: Optional[str] = None
    scan_universe: Optional[str] = None
    scan_source: Optional[str] = None
    fno_universe: Optional[str] = None
    min_turnover_cr: Optional[float] = None
    rank_min_score: Optional[float] = None
    pivot_system: Optional[str] = None


class AlertsIn(BaseModel):
    alerts: bool


class LoginIn(BaseModel):
    username: str
    password: str


init_db()   # runs at import, so the tables exist however the app is started
seed_users()

app = FastAPI(title="Pivot Desk", version="2.2.0", lifespan=lifespan)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Everything under /api/ needs a session except the handful of paths that
    have to work before you have one. index.html is served to anyone - it holds
    no data, and shows the sign-in form until the API answers."""
    path = request.url.path
    if path.startswith("/api/") and path not in OPEN_PATHS:
        if not read_token(request.cookies.get(SESSION_COOKIE)):
            return JSONResponse({"detail": "Sign in to continue"}, status_code=401)
    return await call_next(request)


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    username = body.username.strip().lower()
    if not check_password(username, body.password):
        raise HTTPException(401, "Wrong user name or password")
    response.set_cookie(
        SESSION_COOKIE, make_token(username), max_age=SESSION_DAYS * 86400,
        httponly=True, samesite="lax",
        # Sent over plain HTTP on localhost, HTTPS-only once hosted.
        secure=os.environ.get("HTTPS_ONLY", "0") == "1")
    return {"user": username}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    user = read_token(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(401, "Not signed in")
    return {"user": user}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat(),
            "interval": INTRADAY_INTERVAL, "cache_ttl": CACHE_TTL_SECONDS,
            **build_info()}


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
        "strategy": {
            "enabled": cfg["strategy_enabled"] == "1",
            "strategy_level": cfg["strategy_level"],
            "band_max_pct": float(cfg["band_max_pct"]),
            "entry_buffer_pct": float(cfg["entry_buffer_pct"]),
            "entry_mode": cfg["entry_mode"],
            "sl_pct": float(cfg["sl_pct"]),
            "target_pct": float(cfg["target_pct"]),
            "trail_steps": cfg["trail_steps"],
            "scan_universe": cfg["scan_universe"],
            "universe_size": len(strategy_config()["universe"]),
        },
        "pivots": {
            "selected": pivot_system(),
            "systems": [{"key": k, "label": v["label"], "note": v["note"],
                         "band": "/".join(pretty_level(x) for x in v["band"]),
                         "breakout": [pretty_level(x) for x in v["breakout"]]}
                        for k, v in PIVOT_SYSTEMS.items()],
        },
        "scanner": {
            "scan_source": cfg.get("scan_source", "fno"),
            "fno_universe": cfg.get("fno_universe", ""),
            "min_turnover_cr": float(cfg.get("min_turnover_cr") or 50),
            "rank_min_score": float(cfg.get("rank_min_score") or 0),
            "universe_size": len(scanner_config()["universe"]),
            "fno_builtin": len(FNO_SYMBOLS),
        },
    }


# ------------------------------- strategy -----------------------------------

@app.post("/api/strategy/scan")
def strategy_scan(exchange: str = "NSE"):
    return run_scan(exchange.upper())


@app.post("/api/strategy/sweep")
def strategy_sweep():
    """Check candidates for triggers and manage open trades, now."""
    return run_strategy_sweep()


@app.get("/api/strategy")
def strategy_state():
    today = datetime.now(IST).date().isoformat()
    with db() as conn:
        cands = [dict(r) for r in conn.execute(
            "SELECT * FROM candidates WHERE scanned_at=? ORDER BY band_pct",
            (today,)).fetchall()]
        open_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE status='OPEN' ORDER BY id DESC").fetchall()]
        closed = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE status!='OPEN' ORDER BY id DESC LIMIT 100"
        ).fetchall()]

    wins = [t for t in closed if t["status"] == "TARGET"]
    moves = [((t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100)
             for t in closed if t["entry_price"]]
    return {
        "scanned_at": today,
        "candidates": cands,
        "open": open_rows,
        "closed": closed,
        "summary": {
            "closed": len(closed),
            "targets": len(wins),
            "stops": sum(1 for t in closed if t["status"] == "STOPPED"),
            "hit_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "avg_move_pct": round(sum(moves) / len(moves), 2) if moves else None,
        },
        "config": strategy_config(),
    }


@app.delete("/api/strategy/trades/{trade_id}")
def close_trade(trade_id: int, price: Optional[float] = None):
    """Close a trade by hand - you exited on your own judgement, or the alert
    was stale."""
    with db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Trade not found")
        conn.execute(
            "UPDATE trades SET status='CLOSED', exit_price=?, exit_at=?,"
            " exit_reason='Closed by hand' WHERE id=?",
            (price if price is not None else row["entry_price"],
             datetime.now(IST).isoformat(), trade_id))
    return {"closed": trade_id}


@app.post("/api/scan/run")
def scan_run(timeframe: str = "1d", exchange: str = "NSE"):
    """Run the contraction scan now. This is the 'Sync' the dashboard calls."""
    if timeframe not in TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAMES)}")
    return scan_contraction(timeframe, exchange)


@app.get("/api/scan")
def scan_read(timeframe: str = "1d", limit: int = 100):
    """The most recent stored hits for a timeframe, best-ranked first."""
    if timeframe not in TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAMES)}")
    cfg = scanner_config()
    system = pivot_system()
    with db() as conn:
        last = conn.execute(
            "SELECT MAX(scanned_at) AS t FROM scan_hits WHERE timeframe=?",
            (timeframe,)).fetchone()["t"]
        rows = conn.execute(
            "SELECT * FROM scan_hits WHERE timeframe=? AND scanned_at=?"
            " ORDER BY rank_score DESC, band_pct ASC LIMIT ?",
            (timeframe, last, limit)).fetchall() if last else []

    hits = []
    for r in rows:
        h = dict(r)
        try:
            h["rules"] = json.loads(h["rules"])
        except (TypeError, ValueError):
            h["rules"] = []
        hits.append(h)
    return {
        "timeframe": timeframe,
        "scanned_at": last,
        "hits": hits,
        "universe_size": len(cfg["universe"]),
        "source": cfg["source"],
        "pivot_system": system,
        "pivot_label": PIVOT_SYSTEMS[system]["label"],
        "band_label": "/".join(pretty_level(k) for k in PIVOT_SYSTEMS[system]["band"]),
    }


CHART_INTERVALS = {
    "5m": ("5d", "5m"),
    "15m": ("1mo", "15m"),
    "1d": ("6mo", "1d"),
}


@app.get("/api/chart")
def chart(symbol: str, exchange: str = "NSE", interval: str = "5m",
          bars: int = 120):
    """
    Bars plus the pivot levels to draw across them.

    The levels come from the previous completed SESSION regardless of the bar
    interval, which is how pivots work and how Kite plots them - the lines do
    not move as you change timeframe.
    """
    if interval not in CHART_INTERVALS:
        raise HTTPException(400, f"interval must be one of {list(CHART_INTERVALS)}")
    period, yf_interval = CHART_INTERVALS[interval]
    ticker = yahoo_symbol(symbol, exchange)
    system = pivot_system()

    try:
        frame = _flatten(yf.download(ticker, period=period, interval=yf_interval,
                                     progress=False, auto_adjust=False), ticker)
        daily = _flatten(yf.download(ticker, period="1mo", interval="1d",
                                     progress=False, auto_adjust=False), ticker)
    except Exception as exc:  # noqa: BLE001 - report, don't 500
        raise HTTPException(502, f"{type(exc).__name__}: {exc}")

    if frame is None or frame.empty or daily is None or len(daily) < 2:
        raise HTTPException(404, f"No data for {symbol}")

    prev = daily.iloc[-2]
    piv = bar_pivots(prev, system)

    frame = frame.tail(max(10, min(bars, 400)))
    out = []
    for ts, row in frame.iterrows():
        try:
            out.append({
                "t": ts.isoformat(),
                "o": round(float(row["Open"]), 2),
                "h": round(float(row["High"]), 2),
                "l": round(float(row["Low"]), 2),
                "c": round(float(row["Close"]), 2),
            })
        except (KeyError, ValueError, TypeError):
            continue

    if not out:
        raise HTTPException(404, f"No usable bars for {symbol}")

    return {
        "symbol": symbol.upper(), "exchange": exchange.upper(),
        "interval": interval,
        "bars": out,
        "price": out[-1]["c"],
        "pivots": {k: round(v, 2) for k, v in piv.items()},
        "pivot_order": [k for k in PIVOT_SYSTEMS[system]["order"] if k in piv],
        "pivot_band": list(PIVOT_SYSTEMS[system]["band"]),
        "pivot_label": PIVOT_SYSTEMS[system]["label"],
        "level_names": {k: pretty_level(k) for k in piv},
    }


@app.post("/api/buylist/run")
def buylist_run(exchange: str = "NSE"):
    """Scan the F&O list for names trading above R3 / R4."""
    return scan_breakouts(exchange.upper())


@app.get("/api/buylist")
def buylist_read(limit: int = 200):
    system = pivot_system()
    breakout_keys = PIVOT_SYSTEMS[system]["breakout"]
    top_name = pretty_level(breakout_keys[-1])
    with db() as conn:
        last = conn.execute(
            "SELECT MAX(scanned_at) AS t FROM breakouts").fetchone()["t"]
        rows = conn.execute(
            "SELECT * FROM breakouts WHERE scanned_at=?"
            " ORDER BY (level_name=?) DESC, above_pct ASC LIMIT ?",
            (last, top_name, limit)).fetchall() if last else []
    buys = [dict(r) for r in rows]
    for b in buys:
        b["coiled"] = bool(b["coiled"])
        b["h3_name"] = pretty_level(breakout_keys[0])
        b["h4_name"] = pretty_level(breakout_keys[-1])
    return {"scanned_at": last, "buys": buys,
            "universe_size": len(scanner_config()["universe"]),
            "market_open": market_is_open(),
            "pivot_system": system,
            "pivot_label": PIVOT_SYSTEMS[system]["label"],
            "levels": [pretty_level(k) for k in breakout_keys]}


class BuyListToWatchlist(BaseModel):
    profile_id: Optional[int] = None   # None means "make a new one"
    name: Optional[str] = None         # only used when creating


@app.post("/api/buylist/to-watchlist")
def buylist_to_watchlist(body: BuyListToWatchlist):
    """
    Put the current Buy list onto a watchlist in one go.

    Existing names are skipped rather than erroring, and the caps are still
    enforced - if the Buy list is longer than a watchlist can hold, the names
    highest up the list go on and the rest are reported back rather than
    silently dropped.
    """
    listing = buylist_read(limit=500)
    buys = listing["buys"]
    if not buys:
        raise HTTPException(400, "The Buy list is empty. Run a sync first.")

    stock_cap = caps()["max_stocks_per_profile"]
    now = datetime.now().isoformat()

    with db() as conn:
        if body.profile_id is None:
            name = (body.name or "").strip() or (
                f"Buy list {datetime.now(IST).strftime('%d %b %H:%M')}")
            profile_cap = caps()["max_profiles"]
            existing = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
            if existing >= profile_cap:
                raise HTTPException(
                    400, f"You already have {existing} watchlists, and the cap is "
                         f"{profile_cap}. Delete one, raise the cap in Settings, "
                         f"or add these to a watchlist you already have.")
            try:
                cur = conn.execute(
                    "INSERT INTO profiles (name, created_at) VALUES (?, ?)",
                    (name, now))
            except sqlite3.IntegrityError:
                raise HTTPException(400, f"A watchlist named '{name}' already exists")
            profile_id = cur.lastrowid
            created = True
        else:
            row = conn.execute("SELECT name FROM profiles WHERE id = ?",
                               (body.profile_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Watchlist not found")
            profile_id, name, created = body.profile_id, row["name"], False

        held = {(r["symbol"], r["exchange"]) for r in conn.execute(
            "SELECT symbol, exchange FROM watchlist WHERE profile_id = ?",
            (profile_id,))}
        count = len(held)

        added, already, no_room = [], [], []
        for b in buys:
            pair = (b["symbol"], b["exchange"])
            if pair in held:
                already.append(b["symbol"])
                continue
            if count >= stock_cap:
                no_room.append(b["symbol"])
                continue
            conn.execute(
                "INSERT INTO watchlist (profile_id, symbol, exchange, added_at)"
                " VALUES (?,?,?,?)", (profile_id, b["symbol"], b["exchange"], now))
            held.add(pair)
            count += 1
            added.append(b["symbol"])

    return {"profile_id": profile_id, "name": name, "created": created,
            "added": added, "already_there": already, "no_room": no_room,
            "cap": stock_cap, "total": len(buys)}


@app.get("/api/scan/universe")
def scan_universe():
    cfg = scanner_config()
    return {"source": cfg["source"], "symbols": cfg["universe"],
            "count": len(cfg["universe"]), "fno_builtin": len(FNO_SYMBOLS)}


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
    if body.strategy_enabled is not None:
        updates["strategy_enabled"] = "1" if body.strategy_enabled else "0"
    if body.strategy_level is not None and body.strategy_level in ("h3", "h4"):
        updates["strategy_level"] = body.strategy_level
    if body.entry_mode is not None and body.entry_mode in ("buffer", "candle", "either"):
        updates["entry_mode"] = body.entry_mode
    for key, lo, hi in (("band_max_pct", 0.05, 25), ("entry_buffer_pct", 0, 5),
                        ("sl_pct", 0.05, 25), ("target_pct", 0.1, 50)):
        value = getattr(body, key)
        if value is not None:
            updates[key] = str(round(max(lo, min(hi, float(value))), 3))
    if body.trail_steps is not None:
        updates["trail_steps"] = body.trail_steps.strip()
    if body.scan_universe is not None:
        updates["scan_universe"] = ",".join(
            s.strip().upper() for s in
            body.scan_universe.replace("\n", ",").split(",") if s.strip())
    if body.pivot_system is not None and body.pivot_system in PIVOT_SYSTEMS:
        updates["pivot_system"] = body.pivot_system
        _cache.clear()   # levels change, so every cached quote is now stale
    if body.scan_source is not None and body.scan_source in ("fno", "starter", "custom"):
        updates["scan_source"] = body.scan_source
    if body.fno_universe is not None:
        updates["fno_universe"] = ",".join(
            s.strip().upper() for s in
            body.fno_universe.replace("\n", ",").split(",") if s.strip())
    for key, lo, hi in (("min_turnover_cr", 0, 100000), ("rank_min_score", 0, 5)):
        value = getattr(body, key)
        if value is not None:
            updates[key] = str(round(max(lo, min(hi, float(value))), 3))
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
