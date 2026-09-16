"""
market_data.py
===============
All network I/O lives here, isolated from the scoring logic in
signal_engine.py / indicators.py, so the data source can be swapped later
(e.g. for a paid provider) without touching the math.

Default source: Yahoo Finance via the `yfinance` package (no API key
required). Two important caveats, spelled out here rather than buried:

1. yfinance scrapes an unofficial Yahoo endpoint. Yahoo periodically
   rate-limits or blocks requests from cloud-hosted IPs (Railway, Heroku,
   AWS, etc). If you deploy this and start seeing DataFetchError a lot,
   that's almost certainly what's happening — the fix is either to add
   retry/backoff (already included), self-host it somewhere with a
   residential-ish IP, or switch GOLD_TICKER's fetch function to a paid
   provider (Twelve Data, Polygon.io, OANDA) that gives you a real SLA.
2. "GC=F" (COMEX gold futures) is used by default for OHLC/indicators
   because it has the deepest, most reliable intraday history on Yahoo.
   Spot/cash gold ("XAUUSD=X") is used as a fallback. Futures and spot
   gold track each other extremely closely but aren't identical (small
   basis due to carry cost) — fine for reading trend/momentum structure,
   worth knowing if you're pasting the "current price" into a spot CFD
   broker platform for exact order entry.

Economic-calendar / news-blackout support is optional and requires a free
Finnhub API key (FINNHUB_API_KEY env var). Without it, news_state defaults
to CLEAR and the bot simply won't gate on scheduled news.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - handled at runtime with a clear error
    yf = None


class DataFetchError(RuntimeError):
    pass


GOLD_TICKER_PRIMARY = os.environ.get("GOLD_TICKER", "GC=F")
GOLD_TICKER_FALLBACK = "XAUUSD=X"
DXY_TICKERS = ["DX-Y.NYB", "DX=F"]
US10Y_TICKER = "^TNX"  # quoted as yield% * 10
VIX_TICKER = "^VIX"

# interval -> (yfinance interval, lookback period) — kept within Yahoo's
# documented lookback limits for each granularity.
_INTERVAL_MAP = {
    "M1": ("1m", "5d"),
    "M5": ("5m", "5d"),
    "M15": ("15m", "1mo"),
    "H1": ("60m", "3mo"),
    "D1": ("1d", "2y"),
}


def _require_yfinance():
    if yf is None:
        raise DataFetchError(
            "The 'yfinance' package isn't installed. Run: pip install yfinance"
        )


def _retry(fn, attempts=3, delay=1.5):
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - we want to retry on anything network-shaped
            last_exc = exc
            if i < attempts - 1:
                time.sleep(delay * (i + 1))
    raise DataFetchError(str(last_exc)) from last_exc


def _history(ticker: str, interval: str, period: str) -> pd.DataFrame:
    _require_yfinance()

    def _fetch():
        df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
        if df is None or df.empty:
            raise DataFetchError(f"Empty response for {ticker} ({interval}, {period})")
        return df

    return _retry(_fetch)


def get_ohlc(timeframe: str, ticker: Optional[str] = None) -> pd.DataFrame:
    """
    timeframe: one of 'M1','M5','M15','H1','H4'.
    H4 isn't a native Yahoo interval, so it's built by resampling H1 candles.
    """
    ticker = ticker or GOLD_TICKER_PRIMARY
    if timeframe == "H4":
        h1 = get_ohlc("H1", ticker=ticker)
        h4 = h1.resample("4h").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        ).dropna()
        return h4

    if timeframe not in _INTERVAL_MAP:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    interval, period = _INTERVAL_MAP[timeframe]
    try:
        return _history(ticker, interval, period)
    except DataFetchError:
        if ticker != GOLD_TICKER_FALLBACK:
            return _history(GOLD_TICKER_FALLBACK, interval, period)
        raise


def get_last_price(ticker: Optional[str] = None) -> float:
    """Most current tradeable price available (faster/fresher than a closed candle)."""
    ticker = ticker or GOLD_TICKER_PRIMARY
    _require_yfinance()

    def _fetch():
        info = yf.Ticker(ticker).fast_info
        price = info.get("last_price") or info.get("lastPrice")
        if not price:
            raise DataFetchError(f"No last_price in fast_info for {ticker}")
        return float(price)

    try:
        return _retry(_fetch)
    except DataFetchError:
        # fall back to the most recent 1-minute close
        df = get_ohlc("M1", ticker=ticker)
        return float(df["Close"].iloc[-1])


def get_macro() -> dict:
    """Best-effort DXY / US10Y / VIX daily levels + 1-day change."""
    out = {"dxyPrice": None, "dxyChangePct": None, "y10": None, "y10ChangeBps": None, "vix": None}
    _require_yfinance()

    for dxy_ticker in DXY_TICKERS:
        try:
            df = _history(dxy_ticker, "1d", "5d")
            if len(df) >= 2:
                out["dxyPrice"] = float(df["Close"].iloc[-1])
                out["dxyChangePct"] = (out["dxyPrice"] / float(df["Close"].iloc[-2]) - 1) * 100
            break
        except DataFetchError:
            continue

    try:
        df = _history(US10Y_TICKER, "1d", "5d")
        if len(df) >= 2:
            tnx_now, tnx_prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
            out["y10"] = tnx_now / 10.0
            out["y10ChangeBps"] = (tnx_now - tnx_prev) * 10.0
    except DataFetchError:
        pass

    try:
        df = _history(VIX_TICKER, "1d", "5d")
        if len(df) >= 1:
            out["vix"] = float(df["Close"].iloc[-1])
    except DataFetchError:
        pass

    return out


def get_next_high_impact_event() -> dict:
    """
    Optional economic-calendar lookup via Finnhub's free tier.
    Returns dict(eventName, eventImpact, eventType, minutesToEvent,
    eventAffectsUsdGold) — all None/defaults if FINNHUB_API_KEY isn't set
    or the call fails, so the bot degrades gracefully.
    """
    defaults = {
        "eventName": None, "eventImpact": "None", "eventType": "Standard",
        "minutesToEvent": None, "eventAffectsUsdGold": True,
        "calendarConfigured": False,
    }
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return defaults
    defaults["calendarConfigured"] = True

    try:
        import requests
        today = datetime.now(timezone.utc).date()
        url = (
            "https://finnhub.io/api/v1/calendar/economic"
            f"?from={today.isoformat()}&to={(today + timedelta(days=2)).isoformat()}&token={api_key}"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        events = resp.json().get("economicCalendar", []) or []
        now = datetime.now(timezone.utc)

        candidates = []
        for ev in events:
            if (ev.get("country") or "").upper() not in ("US", "USD"):
                continue
            impact_raw = str(ev.get("impact", "")).lower()
            if impact_raw not in ("2", "3", "high", "medium"):
                continue
            try:
                ev_time = datetime.fromisoformat(ev["time"].replace("Z", "+00:00"))
            except Exception:
                continue
            candidates.append((ev_time, ev))

        if not candidates:
            return defaults

        candidates.sort(key=lambda pair: abs((pair[0] - now).total_seconds()))
        ev_time, ev = candidates[0]
        minutes_to_event = (ev_time - now).total_seconds() / 60
        name = ev.get("event", "Scheduled release")
        name_lower = name.lower()
        event_type = "NFP" if "nonfarm" in name_lower or "non-farm" in name_lower else (
            "FOMC" if "fomc" in name_lower or "fed interest rate" in name_lower else "Standard"
        )
        impact_raw = str(ev.get("impact", "")).lower()
        event_impact = "Red" if impact_raw in ("3", "high") else "Orange"

        return {
            "calendarConfigured": True,
            "eventName": name,
            "eventImpact": event_impact,
            "eventType": event_type,
            "minutesToEvent": minutes_to_event,
            "eventAffectsUsdGold": True,
        }
    except Exception:
        return defaults
