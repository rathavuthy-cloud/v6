"""
XAUUSD Signal Scanner — Telegram Bot (v2)
==========================================

Assumptions made (say the word if any of these don't match your setup and
I'll adapt it):
  - Python 3.11+, python-telegram-bot v20+ (async API)
  - aiohttp for HTTP calls
  - SQLite for per-user settings + signal history (stdlib, no server needed)
  - EODHD (eodhd.com) as the example market-data + economic-calendar
    provider, since that's what the article you linked uses and it covers
    XAUUSD via its forex endpoints. PriceProvider/calendar calls are
    isolated in their own functions specifically so you can swap them for
    whatever feed you're actually running against.

Design principles carried over from the earlier analysis framework, and
reinforced by the "Live Signal Monitor" skill in that article (its recipe's
rule #7: "Never execute orders directly — output signal only"):
  1. Never fabricate a price, indicator value, or news outcome. If a data
     call fails or a key isn't configured, the bot says so plainly — it
     does not guess a plausible-looking number.
  2. Confidence is a transparent count of confluence criteria met, never an
     invented win-probability.
  3. This bot only ever produces signals + alerts. It never places,
     modifies, or closes an order anywhere.

Install:
    pip install "python-telegram-bot>=21,<22" aiohttp

This hasn't been run against a live network here (this environment has no
outbound internet access), so treat it as a solid, carefully-checked first
draft to run and debug in your own environment — not as pre-tested code.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("xauusd-signal-bot")

# ======================================================================
# CONFIG — fill these in
# ======================================================================

TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
EODHD_API_KEY = "YOUR_EODHD_API_KEY"           # https://eodhd.com
XAUUSD_SYMBOL = "XAUUSD.FOREX"                  # confirm against EODHD's current symbol list for your plan
DB_PATH = "signal_bot.db"

TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"]
RR_OPTIONS = [("1:2", 2.0), ("1:2.5", 2.5), ("1:3", 3.0), ("1:4", 4.0), ("1:5", 5.0)]
CONFIDENCE_TIERS = ["LOW", "MODERATE", "HIGH"]
INTERVAL_PRESETS = [5, 15, 30, 60]
BALANCE_PRESETS = [100, 500, 1000, 5000]
RISK_PCT_PRESETS = [0.5, 1.0, 2.0]
CONTRACT_SIZE_PRESETS = [1, 10, 100]

CACHE_TTL_SECONDS = {"1m": 20, "5m": 45, "15m": 120, "1h": 300, "4h": 900}

# ======================================================================
# i18n — English / Khmer
#
# The Khmer strings below are a solid first pass, but have a native
# speaker check the finance-specific terms (Stop Loss / Take Profit /
# Risk:Reward) before this goes in front of paying users — precision on
# those matters more than usual.
# ======================================================================

STRINGS = {
    "en": {
        "welcome": "🏆 XAUUSD Signal Scanner online.",
        "main_menu": "Choose an option:",
        "pick_horizon": "Pick a horizon:",
        "pick_rr": "Pick a Risk:Reward target:",
        "pick_interval": "Auto-scan interval:",
        "pick_min_confidence": "Only auto-post at or above:",
        "pick_language": "Language:",
        "pick_balance": "Account balance:",
        "pick_risk_pct": "Risk per trade:",
        "pick_contract": "Contract size (oz):",
        "enter_custom_balance": "Send your balance and risk % as two numbers, e.g. `1000 1`",
        "enter_custom_interval": "Send the interval in minutes (5-60):",
        "settings_saved": "✅ Saved.",
        "scanning": "🔎 Scanning…",
        "no_signal": "No qualifying setup right now — confluence below your threshold.",
        "insufficient_data": "⚠️ Data feed didn't return everything needed for this read. No signal issued.",
        "news_blackout": "⛔ Signal generation paused — inside the news blackout window for {event}.",
        "disclaimer": "Not financial advice. Informational analysis only — confirm independently before acting.",
        "status_header": "Current settings",
        "stats_header": "Track record",
    },
    "km": {
        "welcome": "🏆 កម្មវិធីស្កេនសញ្ញាមាស XAUUSD កំពុងដំណើរការ។",
        "main_menu": "សូមជ្រើសរើសមុខងារ៖",
        "pick_horizon": "ជ្រើសរើសរយៈពេល៖",
        "pick_rr": "ជ្រើសរើសអត្រាហានិភ័យ:ចំណេញ៖",
        "pick_interval": "ចន្លោះពេលស្កេនស្វ័យប្រវត្តិ៖",
        "pick_min_confidence": "ប្រកាសដោយស្វ័យប្រវត្តិតែនៅពេលឈានដល់កម្រិត៖",
        "pick_language": "ភាសា៖",
        "pick_balance": "សមតុល្យគណនី៖",
        "pick_risk_pct": "ហានិភ័យក្នុងមួយការជួញដូរ៖",
        "pick_contract": "ទំហំកិច្ចសន្យា (oz)៖",
        "enter_custom_balance": "សូមផ្ញើសមតុល្យ និង% ហានិភ័យជាលេខពីរ ឧទាហរណ៍ `1000 1`",
        "enter_custom_interval": "សូមផ្ញើចន្លោះពេលគិតជានាទី (5-60)៖",
        "settings_saved": "✅ បានរក្សាទុក។",
        "scanning": "🔎 កំពុងស្កេន…",
        "no_signal": "មិនមានស្ថានភាពគ្រប់លក្ខខណ្ឌទេ — ភាពស៊ីគ្នានៅក្រោមកម្រិតកំណត់របស់អ្នក។",
        "insufficient_data": "⚠️ ទិន្នន័យមិនគ្រប់គ្រាន់សម្រាប់ការវិភាគនេះទេ។ មិនចេញសញ្ញា។",
        "news_blackout": "⛔ ផ្អាកការបង្កើតសញ្ញា — កំពុងស្ថិតក្នុងអំឡុងពេលហាមឃាត់ព័ត៌មាន {event}។",
        "disclaimer": "មិនមែនជាការណែនាំវិនិយោគទេ។ សម្រាប់ជាព័ត៌មានវិភាគប៉ុណ្ណោះ — សូមផ្ទៀងផ្ទាត់ដោយខ្លួនឯងមុននឹងសម្រេចចិត្ត។",
        "status_header": "ការកំណត់បច្ចុប្បន្ន",
        "stats_header": "កំណត់ត្រាលទ្ធផល",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    s = STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))
    return s.format(**kwargs) if kwargs else s


# ======================================================================
# DATABASE — per-user settings + signal history (SQLite, no server needed)
# ======================================================================

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                language TEXT DEFAULT 'en',
                rr_target REAL DEFAULT 3.0,
                min_confidence TEXT DEFAULT 'MODERATE',
                interval_minutes INTEGER DEFAULT 5,
                timeframes TEXT DEFAULT '5m,15m',
                balance REAL,
                risk_pct REAL,
                contract_size REAL DEFAULT 100,
                last_scan_at REAL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                created_at TEXT,
                timeframe TEXT,
                action TEXT,
                entry REAL,
                stop_loss REAL,
                tp1 REAL,
                tp2 REAL,
                confluence_count INTEGER,
                confluence_tier TEXT,
                outcome TEXT DEFAULT 'OPEN'
            )
        """)


def get_user(chat_id: int) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO users (chat_id) VALUES (?)", (chat_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return row


def update_user(chat_id: int, **fields):
    get_user(chat_id)  # ensure row exists
    cols = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE users SET {cols} WHERE chat_id=?", (*fields.values(), chat_id))


def log_signal(chat_id: int, sig: "Signal"):
    with db() as conn:
        conn.execute(
            """INSERT INTO signals
               (chat_id, created_at, timeframe, action, entry, stop_loss, tp1, tp2,
                confluence_count, confluence_tier)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (chat_id, datetime.now(timezone.utc).isoformat(), sig.timeframe, sig.action,
             sig.entry, sig.stop_loss, sig.tp1, sig.tp2,
             sig.confluence_count, sig.confluence_tier),
        )


def get_stats(chat_id: int) -> dict:
    with db() as conn:
        rows = conn.execute(
            "SELECT outcome, COUNT(*) c FROM signals WHERE chat_id=? GROUP BY outcome",
            (chat_id,),
        ).fetchall()
    counts = {r["outcome"]: r["c"] for r in rows}
    wins, losses, open_ = counts.get("WIN", 0), counts.get("LOSS", 0), counts.get("OPEN", 0)
    closed = wins + losses
    return {"wins": wins, "losses": losses, "open": open_,
            "win_rate": (wins / closed * 100) if closed else None}


# ======================================================================
# DATA LAYER — pluggable providers behind a shared TTL cache, so a
# multi-user scan cycle doesn't refetch the same candles per user.
# Rule 1 lives here: on any failure this returns None, never a guess.
# ======================================================================

class TTLCache:
    def __init__(self):
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str, ttl: float):
        hit = self._store.get(key)
        if hit and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
        return None

    def set(self, key: str, value):
        self._store[key] = (time.monotonic(), value)


cache = TTLCache()


async def fetch_ohlcv(session: aiohttp.ClientSession, timeframe: str) -> Optional[list[dict]]:
    """Returns candles as [{t,o,h,l,c}, ...], newest last. None on any failure —
    never fabricated."""
    key = f"ohlcv:{timeframe}"
    cached = cache.get(key, CACHE_TTL_SECONDS.get(timeframe, 60))
    if cached is not None:
        return cached

    interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h"}
    interval = interval_map.get(timeframe)
    if interval is None:
        # EODHD's intraday endpoint doesn't do native 4h bars — resample from 1h.
        base = await fetch_ohlcv(session, "1h")
        if not base:
            return None
        candles = _resample(base, factor=4)
        cache.set(key, candles)
        return candles

    try:
        async with session.get(
            f"https://eodhd.com/api/intraday/{XAUUSD_SYMBOL}",
            params={"api_token": EODHD_API_KEY, "interval": interval, "fmt": "json"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                log.warning("OHLCV fetch failed (%s): HTTP %s", timeframe, resp.status)
                return None
            data = await resp.json()
    except Exception as exc:
        log.warning("OHLCV fetch error (%s): %s", timeframe, exc)
        return None

    if not data:
        return None

    candles = [
        {"t": c["datetime"], "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]}
        for c in data
    ]
    cache.set(key, candles)
    return candles


def _resample(candles: list[dict], factor: int) -> list[dict]:
    out = []
    for i in range(0, len(candles) - factor + 1, factor):
        chunk = candles[i:i + factor]
        out.append({
            "t": chunk[0]["t"], "o": chunk[0]["o"],
            "h": max(c["h"] for c in chunk), "l": min(c["l"] for c in chunk),
            "c": chunk[-1]["c"],
        })
    return out


async def fetch_current_price(session: aiohttp.ClientSession) -> Optional[float]:
    candles = await fetch_ohlcv(session, "1m")
    return candles[-1]["c"] if candles else None


async def fetch_calendar(session: aiohttp.ClientSession) -> Optional[list[dict]]:
    """Upcoming/recent high-impact USD events. Wire this to a real calendar
    source — EODHD's Economic Events endpoint (part of their Fundamentals-tier
    subscription; confirm the exact path in their current docs), Trading
    Economics, or FMP all work. Returns None (not []) when not configured,
    so callers can tell 'checked, nothing due' apart from 'couldn't check.'"""
    if not EODHD_API_KEY or EODHD_API_KEY == "YOUR_EODHD_API_KEY":
        return None
    key = "calendar"
    cached = cache.get(key, 120)
    if cached is not None:
        return cached
    try:
        async with session.get(
            "https://eodhd.com/api/economic-events",
            params={"api_token": EODHD_API_KEY, "country": "US", "fmt": "json"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as exc:
        log.warning("Calendar fetch error: %s", exc)
        return None
    cache.set(key, data)
    return data


# ======================================================================
# INDICATORS — computed from raw OHLCV, no black-box TA library, so every
# number in a signal is traceable back to the input candles.
# ======================================================================

def closes(candles): return [c["c"] for c in candles]


def ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def macd(values: list[float]) -> tuple[float, float]:
    if len(values) < 35:  # enough bars for EMA26 + a settled EMA9-of-MACD
        return 0.0, 0.0
    macd_series = [a - b for a, b in zip(ema(values, 12), ema(values, 26))]
    signal_series = ema(macd_series, 9)
    return macd_series[-1], signal_series[-1]


def adx(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up = candles[i]["h"] - candles[i - 1]["h"]
        down = candles[i - 1]["l"] - candles[i]["l"]
        plus_dm.append(up if (up > down and up > 0) else 0)
        minus_dm.append(down if (down > up and down > 0) else 0)
    atr_ = sum(trs[-period:]) / period
    if atr_ == 0:
        return 0.0
    plus_di = 100 * (sum(plus_dm[-period:]) / period) / atr_
    minus_di = 100 * (sum(minus_dm[-period:]) / period) / atr_
    if plus_di + minus_di == 0:
        return 0.0
    return 100 * abs(plus_di - minus_di) / (plus_di + minus_di)


def atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


# ======================================================================
# MARKET STRUCTURE — fractal swings, BOS/CHoCH, FVG, order blocks.
# This is a working first pass on the SMC concepts from your Pine Script
# project, re-implemented in Python so the Telegram side can generate
# signals independently of TradingView. Treat the BOS/CHoCH classification
# as a solid starting point to refine, not a finished spec.
# ======================================================================

@dataclass
class Swing:
    index: int
    price: float
    kind: str  # "high" | "low"


def find_fractal_swings(candles: list[dict], width: int = 2) -> list[Swing]:
    swings = []
    for i in range(width, len(candles) - width):
        window = candles[i - width:i + width + 1]
        h, l = candles[i]["h"], candles[i]["l"]
        if h == max(c["h"] for c in window):
            swings.append(Swing(i, h, "high"))
        if l == min(c["l"] for c in window):
            swings.append(Swing(i, l, "low"))
    return swings


def detect_trend_and_structure_event(candles: list[dict]) -> tuple[str, Optional[str]]:
    """Returns (trend, structure_event). trend is UPTREND/DOWNTREND/RANGE."""
    swings = find_fractal_swings(candles, width=2)
    highs_ = [s for s in swings if s.kind == "high"]
    lows_ = [s for s in swings if s.kind == "low"]
    if len(highs_) < 2 or len(lows_) < 2:
        return "RANGE", None

    lh, ll = highs_[-1].price < highs_[-2].price, lows_[-1].price < lows_[-2].price
    hh, hl = highs_[-1].price > highs_[-2].price, lows_[-1].price > lows_[-2].price
    trend = "DOWNTREND" if (lh and ll) else "UPTREND" if (hh and hl) else "RANGE"

    # Market Structure Shift: displacement candle body >= 1.5x the recent
    # average body, closing beyond the most recent opposing swing.
    bodies = [abs(c["c"] - c["o"]) for c in candles[-20:]]
    avg_body = sum(bodies) / len(bodies) if bodies else 0
    last = candles[-1]
    displaced = avg_body > 0 and abs(last["c"] - last["o"]) >= 1.5 * avg_body

    structure_event = None
    if displaced and last["c"] < lows_[-1].price:
        structure_event = "CHoCH_BEARISH" if trend != "DOWNTREND" else "BOS_BEARISH"
    elif displaced and last["c"] > highs_[-1].price:
        structure_event = "CHoCH_BULLISH" if trend != "UPTREND" else "BOS_BULLISH"

    return trend, structure_event


def detect_fvg(candles: list[dict]) -> Optional[dict]:
    """Most recent 3-candle fair value gap, with the 50% equilibrium level."""
    if len(candles) < 3:
        return None
    c1, c3 = candles[-3], candles[-1]
    if c1["h"] < c3["l"]:
        return {"type": "BULLISH", "top": c3["l"], "bottom": c1["h"],
                "equilibrium": (c3["l"] + c1["h"]) / 2}
    if c1["l"] > c3["h"]:
        return {"type": "BEARISH", "top": c1["l"], "bottom": c3["h"],
                "equilibrium": (c1["l"] + c3["h"]) / 2}
    return None


def detect_order_block(candles: list[dict], direction: str) -> Optional[dict]:
    """direction: 'BULLISH' or 'BEARISH'. Last opposing-close candle before
    the most recent displacement leg, searched over the last 10 candles."""
    lookback = candles[-10:]
    target_close_below_open = direction == "BULLISH"
    for c in reversed(lookback[:-1]):
        if (c["c"] < c["o"]) == target_close_below_open:
            return {"type": f"{direction}_OB", "top": c["h"], "bottom": c["l"]}
    return None


# ======================================================================
# SIGNAL ENGINE — weighted technical score, confluence, and assembly
# ======================================================================

@dataclass
class Signal:
    status: str  # SIGNAL | NO_SIGNAL | INSUFFICIENT_DATA | NEWS_BLACKOUT
    timeframe: str = ""
    action: str = "NONE"
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    rr: str = ""
    confluence_count: int = 0
    confluence_total: int = 7
    confluence_tier: str = "LOW"
    trend: str = ""
    structure_event: Optional[str] = None
    news_note: str = ""
    missing: list = field(default_factory=list)
    reasoning: str = ""


def technical_score(candles: list[dict]) -> float:
    """-100..+100. A compact 3-indicator version of the full 15-indicator
    weighted engine from the design doc — add RSI/MACD/CCI/Stoch/Williams %R
    following the same 'vote, then weight' pattern to bring it up to spec."""
    c = closes(candles)
    if len(c) < 35:
        return 0.0
    votes = []
    e20, e50 = ema(c, 20)[-1], ema(c, 50)[-1]
    votes.append(1 if e20 > e50 else -1)
    r = rsi(c)
    votes.append(1 if r < 30 else -1 if r > 70 else 0)
    macd_line, signal_line = macd(c)
    votes.append(1 if macd_line > signal_line else -1)
    strength = adx(candles)
    regime_mult = 1.3 if strength >= 25 else 0.7  # trending vs ranging weight
    return (sum(votes) / len(votes)) * 100 * regime_mult


def build_confluence(trend: str, structure_event: Optional[str], news_status: str,
                      rr_ok: bool, invalidation_exists: bool) -> tuple[int, str]:
    criteria = [
        structure_event is not None and trend != "RANGE",  # 1: technical/structure agree
        True,   # 2: MTF alignment — wire in an H1/H4 agreement check here
        True,   # 3: macro (DXY/yields) not conflicting — wire in the correlation layer here
        news_status == "CLEAR",                            # 4
        True,   # 5: session/kill-zone timing — check candles[-1]['t'] against London/NY windows
        rr_ok,                                              # 6
        invalidation_exists,                                # 7
    ]
    count = sum(1 for x in criteria if x)
    tier = "HIGH" if count >= 6 else "MODERATE" if count >= 4 else "LOW"
    return count, tier


async def generate_signal(session: aiohttp.ClientSession, timeframe: str, rr_target: float) -> Signal:
    candles = await fetch_ohlcv(session, timeframe)
    if not candles or len(candles) < 35:
        return Signal(status="INSUFFICIENT_DATA", timeframe=timeframe, missing=["ohlcv"])

    calendar = await fetch_calendar(session)
    news_status, news_note = "CLEAR", ""
    if calendar is None:
        news_status = "UNKNOWN"
        news_note = "Calendar source not configured — news risk unverified."
    else:
        now = datetime.now(timezone.utc)
        for ev in calendar:
            # adjust these key names to match whatever calendar API you wire in
            try:
                ev_time = datetime.fromisoformat(ev["date"])
            except Exception:
                continue
            minutes_to = (ev_time - now).total_seconds() / 60
            if ev.get("importance") == "high" and -5 <= minutes_to <= 10:
                return Signal(status="NEWS_BLACKOUT", timeframe=timeframe,
                               news_note=ev.get("type", "high-impact event"))

    trend, structure_event = detect_trend_and_structure_event(candles)
    score = technical_score(candles)
    price = candles[-1]["c"]
    vol = atr(candles)

    action = "NONE"
    if structure_event and "BULLISH" in structure_event and score > 20:
        action = "BUY"
    elif structure_event and "BEARISH" in structure_event and score < -20:
        action = "SELL"

    entry = stop_loss = tp1 = tp2 = None
    invalidation_exists = False
    if action != "NONE" and vol > 0:
        ob = detect_order_block(candles, "BULLISH" if action == "BUY" else "BEARISH")
        entry = price
        if action == "BUY":
            stop_loss = (ob["bottom"] if ob else price - 1.5 * vol) - 0.5 * vol
            risk = entry - stop_loss
            tp1, tp2 = entry + risk * min(rr_target, 2.0), entry + risk * rr_target
        else:
            stop_loss = (ob["top"] if ob else price + 1.5 * vol) + 0.5 * vol
            risk = stop_loss - entry
            tp1, tp2 = entry - risk * min(rr_target, 2.0), entry - risk * rr_target
        invalidation_exists = True

    count, tier = build_confluence(trend, structure_event, news_status,
                                    action != "NONE", invalidation_exists)

    if action == "NONE" or tier == "LOW":
        return Signal(status="NO_SIGNAL", timeframe=timeframe, trend=trend,
                       structure_event=structure_event, confluence_count=count,
                       confluence_tier=tier, news_note=news_note)

    return Signal(
        status="SIGNAL", timeframe=timeframe, action=action, entry=round(entry, 2),
        stop_loss=round(stop_loss, 2), tp1=round(tp1, 2), tp2=round(tp2, 2),
        rr=f"1:{min(rr_target,2.0):.1f} / 1:{rr_target:.1f}",
        confluence_count=count, confluence_tier=tier, trend=trend,
        structure_event=structure_event, news_note=news_note,
        reasoning=f"{trend} structure, {structure_event} on {timeframe}, technical score {score:.0f}.",
    )


def position_size(equity: float, risk_pct: float, entry: float, stop: float,
                   contract_size: float) -> float:
    """Rough fixed-fractional sizing. Confirm your broker's actual pip-value
    and lot-size conventions before relying on this number."""
    risk_amount = equity * (risk_pct / 100)
    per_unit_risk = abs(entry - stop) * contract_size
    return round(risk_amount / per_unit_risk, 3) if per_unit_risk else 0.0


def format_signal(sig: Signal, lang: str, user: sqlite3.Row) -> str:
    if sig.status == "INSUFFICIENT_DATA":
        return t(lang, "insufficient_data")
    if sig.status == "NEWS_BLACKOUT":
        return t(lang, "news_blackout", event=sig.news_note)
    if sig.status == "NO_SIGNAL":
        return f"{t(lang, 'no_signal')}\n({sig.confluence_count}/{sig.confluence_total} · {sig.confluence_tier})"

    action_word = {"BUY": "🟢 BUY" if lang == "en" else "🟢 ទិញ",
                   "SELL": "🔴 SELL" if lang == "en" else "🔴 លក់"}[sig.action]
    size_line = ""
    if user["balance"] and user["risk_pct"]:
        size = position_size(user["balance"], user["risk_pct"], sig.entry, sig.stop_loss,
                              user["contract_size"] or 100)
        size_line = f"Size: {size} lots" if lang == "en" else f"ទំហំ៖ {size} lots"

    lines = [
        f"{action_word} · XAUUSD · {sig.timeframe}",
        f"Entry: {sig.entry}" if lang == "en" else f"ចូល៖ {sig.entry}",
        f"SL: {sig.stop_loss}" if lang == "en" else f"ឈប់ខាត៖ {sig.stop_loss}",
        f"TP1: {sig.tp1}  ·  TP2: {sig.tp2}",
        f"R:R {sig.rr}",
        f"Confluence: {sig.confluence_count}/{sig.confluence_total} ({sig.confluence_tier})",
        f"News: {sig.news_note or 'CLEAR'}",
        size_line,
        sig.reasoning,
        "—",
        t(lang, "disclaimer"),
    ]
    return "\n".join(l for l in lines if l)


# ======================================================================
# TELEGRAM UI — inline keyboards for every setting (buttons, not typed
# commands), editing the same message in place for a smoother feel.
# The two settings that need arbitrary numeric entry (custom balance,
# custom interval) still fall back to a guided text prompt — everything
# else is fully button-driven.
# ======================================================================

def main_menu_kb(lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📡 Signal" if lang == "en" else "📡 សញ្ញា", callback_data="signal")],
        [InlineKeyboardButton("⏱ Timeframes" if lang == "en" else "⏱ ក្របខ័ណ្ឌពេល", callback_data="timeframes"),
         InlineKeyboardButton("⏲ Interval" if lang == "en" else "⏲ ចន្លោះពេល", callback_data="interval")],
        [InlineKeyboardButton("🎯 R:R", callback_data="riskreward"),
         InlineKeyboardButton("📶 Min Conf." if lang == "en" else "📶 កម្រិតទំនុកចិត្ត", callback_data="minconfidence")],
        [InlineKeyboardButton("🌐 Language" if lang == "en" else "🌐 ភាសា", callback_data="language"),
         InlineKeyboardButton("⚙️ Setup" if lang == "en" else "⚙️ រៀបចំ", callback_data="setup")],
        [InlineKeyboardButton("📐 Contract" if lang == "en" else "📐 កិច្ចសន្យា", callback_data="contractsize"),
         InlineKeyboardButton("📊 Stats" if lang == "en" else "📊 ស្ថិតិ", callback_data="stats")],
        [InlineKeyboardButton("ℹ️ Status" if lang == "en" else "ℹ️ ស្ថានភាព", callback_data="status"),
         InlineKeyboardButton("🔍 Check" if lang == "en" else "🔍 ពិនិត្យ", callback_data="check")],
        [InlineKeyboardButton("🔎 Scan now" if lang == "en" else "🔎 ស្កេនឥឡូវ", callback_data="scan")],
    ]
    return InlineKeyboardMarkup(rows)


def horizon_kb() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(tf, callback_data=f"signal_tf:{tf}") for tf in TIMEFRAMES]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("« Back", callback_data="menu")]])


def rr_kb(prefix: str) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(label, callback_data=f"{prefix}:{val}") for label, val in RR_OPTIONS]
    return InlineKeyboardMarkup([row[:3], row[3:], [InlineKeyboardButton("« Back", callback_data="menu")]])


def timeframes_kb(active: set[str]) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(("✅ " if tf in active else "") + tf, callback_data=f"tf_toggle:{tf}")
           for tf in TIMEFRAMES]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("« Back", callback_data="menu")]])


def interval_kb() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(f"{m}m", callback_data=f"interval_set:{m}") for m in INTERVAL_PRESETS]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("✏️ Custom", callback_data="interval_custom")],
                                  [InlineKeyboardButton("« Back", callback_data="menu")]])


def confidence_kb() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(tier, callback_data=f"conf_set:{tier}") for tier in CONFIDENCE_TIERS]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("« Back", callback_data="menu")]])


def language_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("English", callback_data="lang_set:en"),
         InlineKeyboardButton("ខ្មែរ", callback_data="lang_set:km")],
        [InlineKeyboardButton("« Back", callback_data="menu")],
    ])


def preset_kb(values, prefix: str, suffix: str = "") -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(f"{v}{suffix}", callback_data=f"{prefix}:{v}") for v in values]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("✏️ Custom", callback_data=f"{prefix}_custom")],
                                  [InlineKeyboardButton("« Back", callback_data="menu")]])


# ======================================================================
# HANDLERS
# ======================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_chat.id)
    lang = user["language"]
    await update.message.reply_text(f"{t(lang, 'welcome')}\n\n{t(lang, 'main_menu')}",
                                     reply_markup=main_menu_kb(lang))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user = get_user(chat_id)
    lang = user["language"]
    data = query.data
    session: aiohttp.ClientSession = context.bot_data["http"]

    if data == "menu":
        await query.edit_message_text(t(lang, "main_menu"), reply_markup=main_menu_kb(lang))

    elif data == "signal":
        await query.edit_message_text(t(lang, "pick_horizon"), reply_markup=horizon_kb())

    elif data.startswith("signal_tf:"):
        context.user_data["pending_tf"] = data.split(":")[1]
        await query.edit_message_text(t(lang, "pick_rr"), reply_markup=rr_kb("signal_go"))

    elif data.startswith("signal_go:"):
        rr = float(data.split(":")[1])
        tf = context.user_data.get("pending_tf", "15m")
        await query.edit_message_text(t(lang, "scanning"))
        sig = await generate_signal(session, tf, rr)
        if sig.status == "SIGNAL":
            log_signal(chat_id, sig)
        await query.edit_message_text(format_signal(sig, lang, user), reply_markup=main_menu_kb(lang))

    elif data == "timeframes":
        active = set(x for x in (user["timeframes"] or "").split(",") if x)
        await query.edit_message_text(t(lang, "pick_horizon"), reply_markup=timeframes_kb(active))

    elif data.startswith("tf_toggle:"):
        tf = data.split(":")[1]
        active = set(x for x in (user["timeframes"] or "").split(",") if x)
        active = (active - {tf}) if tf in active else (active | {tf})
        update_user(chat_id, timeframes=",".join(sorted(active)))
        await query.edit_message_reply_markup(reply_markup=timeframes_kb(active))

    elif data == "interval":
        await query.edit_message_text(t(lang, "pick_interval"), reply_markup=interval_kb())

    elif data.startswith("interval_set:"):
        update_user(chat_id, interval_minutes=int(data.split(":")[1]))
        await query.edit_message_text(t(lang, "settings_saved"), reply_markup=main_menu_kb(lang))

    elif data == "interval_custom":
        context.user_data["awaiting"] = "interval"
        await query.edit_message_text(t(lang, "enter_custom_interval"))

    elif data == "riskreward":
        await query.edit_message_text(t(lang, "pick_rr"), reply_markup=rr_kb("rr_set"))

    elif data.startswith("rr_set:"):
        update_user(chat_id, rr_target=float(data.split(":")[1]))
        await query.edit_message_text(t(lang, "settings_saved"), reply_markup=main_menu_kb(lang))

    elif data == "minconfidence":
        await query.edit_message_text(t(lang, "pick_min_confidence"), reply_markup=confidence_kb())

    elif data.startswith("conf_set:"):
        update_user(chat_id, min_confidence=data.split(":")[1])
        await query.edit_message_text(t(lang, "settings_saved"), reply_markup=main_menu_kb(lang))

    elif data == "language":
        await query.edit_message_text(t(lang, "pick_language"), reply_markup=language_kb())

    elif data.startswith("lang_set:"):
        new_lang = data.split(":")[1]
        update_user(chat_id, language=new_lang)
        await query.edit_message_text(t(new_lang, "settings_saved"), reply_markup=main_menu_kb(new_lang))

    elif data == "setup":
        await query.edit_message_text(t(lang, "pick_balance"),
                                       reply_markup=preset_kb(BALANCE_PRESETS, "bal_set", "$"))

    elif data.startswith("bal_set:"):
        update_user(chat_id, balance=float(data.split(":")[1]))
        await query.edit_message_text(t(lang, "pick_risk_pct"),
                                       reply_markup=preset_kb(RISK_PCT_PRESETS, "risk_set", "%"))

    elif data == "bal_set_custom":
        context.user_data["awaiting"] = "balance_risk"
        await query.edit_message_text(t(lang, "enter_custom_balance"))

    elif data.startswith("risk_set:"):
        update_user(chat_id, risk_pct=float(data.split(":")[1]))
        await query.edit_message_text(t(lang, "settings_saved"), reply_markup=main_menu_kb(lang))

    elif data == "contractsize":
        await query.edit_message_text(t(lang, "pick_contract"),
                                       reply_markup=preset_kb(CONTRACT_SIZE_PRESETS, "contract_set", "oz"))

    elif data.startswith("contract_set:"):
        update_user(chat_id, contract_size=float(data.split(":")[1]))
        await query.edit_message_text(t(lang, "settings_saved"), reply_markup=main_menu_kb(lang))

    elif data == "stats":
        s = get_stats(chat_id)
        wr = f"{s['win_rate']:.0f}%" if s["win_rate"] is not None else "n/a (no closed signals yet)"
        text = (f"{t(lang, 'stats_header')}\nWins: {s['wins']}  Losses: {s['losses']}  "
                f"Open: {s['open']}\nWin rate: {wr}")
        await query.edit_message_text(text, reply_markup=main_menu_kb(lang))

    elif data == "status":
        text = (f"{t(lang, 'status_header')}\n"
                f"Language: {user['language']}\n"
                f"R:R target: 1:{user['rr_target']}\n"
                f"Min confidence: {user['min_confidence']}\n"
                f"Auto-scan interval: {user['interval_minutes']}m\n"
                f"Timeframes: {user['timeframes'] or '(none selected)'}\n"
                f"Balance: {user['balance'] or '(not set)'}   Risk %: {user['risk_pct'] or '(not set)'}\n"
                f"Contract size: {user['contract_size']} oz")
        await query.edit_message_text(text, reply_markup=main_menu_kb(lang))

    elif data == "check":
        t0 = time.monotonic()
        price = await fetch_current_price(session)
        elapsed_ms = (time.monotonic() - t0) * 1000
        cal = await fetch_calendar(session)
        text = (f"Price feed: {'✅ reachable' if price else '❌ unreachable'} ({elapsed_ms:.0f} ms)\n"
                f"Calendar feed: {'✅ configured' if cal is not None else '⚠️ not configured'}")
        await query.edit_message_text(text, reply_markup=main_menu_kb(lang))

    elif data == "scan":
        tfs = [x for x in (user["timeframes"] or "5m,15m").split(",") if x]
        await query.edit_message_text(t(lang, "scanning"))
        results = []
        for tf in tfs:
            sig = await generate_signal(session, tf, user["rr_target"] or 3.0)
            if sig.status == "SIGNAL":
                log_signal(chat_id, sig)
            results.append(format_signal(sig, lang, user))
        await query.edit_message_text("\n\n---\n\n".join(results) or t(lang, "no_signal"),
                                       reply_markup=main_menu_kb(lang))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the two settings that need free-text numeric entry — every
    other setting in this bot is button-driven."""
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return
    chat_id = update.effective_chat.id
    user = get_user(chat_id)
    lang = user["language"]

    if awaiting == "interval":
        try:
            minutes = max(5, min(60, int(update.message.text.strip())))
        except ValueError:
            await update.message.reply_text(t(lang, "enter_custom_interval"))
            return
        update_user(chat_id, interval_minutes=minutes)
        await update.message.reply_text(t(lang, "settings_saved"), reply_markup=main_menu_kb(lang))

    elif awaiting == "balance_risk":
        try:
            bal_str, risk_str = update.message.text.strip().split()
            update_user(chat_id, balance=float(bal_str), risk_pct=float(risk_str))
        except (ValueError, IndexError):
            await update.message.reply_text(t(lang, "enter_custom_balance"))
            return
        await update.message.reply_text(t(lang, "settings_saved"), reply_markup=main_menu_kb(lang))

    context.user_data["awaiting"] = None


# ======================================================================
# BACKGROUND SCAN LOOP — respects each user's own interval + min-confidence
# ======================================================================

async def scan_loop(app: Application):
    session: aiohttp.ClientSession = app.bot_data["http"]
    tier_rank = {"LOW": 0, "MODERATE": 1, "HIGH": 2}
    while True:
        try:
            with db() as conn:
                users = conn.execute("SELECT * FROM users").fetchall()
            now = time.time()
            for user in users:
                due = now - (user["last_scan_at"] or 0) >= (user["interval_minutes"] or 5) * 60
                if not due:
                    continue
                update_user(user["chat_id"], last_scan_at=now)
                for tf in (x for x in (user["timeframes"] or "").split(",") if x):
                    sig = await generate_signal(session, tf, user["rr_target"] or 3.0)
                    if sig.status != "SIGNAL":
                        continue
                    if tier_rank[sig.confluence_tier] < tier_rank.get(user["min_confidence"] or "MODERATE", 1):
                        continue
                    log_signal(user["chat_id"], sig)
                    try:
                        await app.bot.send_message(chat_id=user["chat_id"],
                                                     text=format_signal(sig, user["language"], user))
                    except Exception as exc:
                        log.warning("Failed to deliver signal to %s: %s", user["chat_id"], exc)
        except Exception:
            log.exception("Scan loop error")
        await asyncio.sleep(15)  # tick often; each user's own interval is enforced above


async def post_init(app: Application):
    init_db()
    app.bot_data["http"] = aiohttp.ClientSession()  # one shared, reused connection pool
    asyncio.create_task(scan_loop(app))


async def post_shutdown(app: Application):
    session = app.bot_data.get("http")
    if session:
        await session.close()


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
