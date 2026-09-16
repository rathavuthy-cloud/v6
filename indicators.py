"""
indicators.py
=============
Pure pandas/numpy technical-analysis building blocks used to turn raw OHLC
candles into the same kind of "trend / momentum / volatility vote" that the
original XAU/USD console asked a human trader to type in by hand.

No network calls happen in this file on purpose — everything here is a
deterministic function of a DataFrame you already have in memory, which
makes it trivial to unit test with synthetic data (see test_indicators.py).

Expected DataFrame shape: columns ['Open','High','Low','Close'] (Volume
optional), indexed by an increasing datetime index, oldest row first.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))


def sign(n: float) -> int:
    return 1 if n > 0 else (-1 if n < 0 else 0)


# ---------------------------------------------------------------------------
# Core indicators (Wilder-smoothed where that's the standard convention)
# ---------------------------------------------------------------------------

def true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = true_range(df)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    low_min = df["Low"].rolling(k_period).min()
    high_max = df["High"].rolling(k_period).max()
    span = (high_max - low_min).replace(0, np.nan)
    k = 100 * (df["Close"] - low_min) / span
    k = k.fillna(50.0)
    d = k.rolling(d_period).mean().fillna(50.0)
    return k, d


def adx(df: pd.DataFrame, period: int = 14):
    high, low = df["High"], df["Low"]
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    tr = true_range(df)
    atr_ = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_

    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    adx_ = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx_.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0)


# ---------------------------------------------------------------------------
# Vote builders — map raw indicator values onto the console's original
# -8..+8 (trend) / -20..+20 (momentum) / -2..+2 (volatility) scales.
# These weightings are a documented, adjustable heuristic, not a proven
# edge — tune WEIGHTS below if you want to emphasize different signals.
# ---------------------------------------------------------------------------

MIN_BARS = 60  # minimum candles we want before trusting a vote


def compute_tf_votes(df: pd.DataFrame) -> dict:
    """
    Returns {'trend': float in [-8,8], 'momentum': float in [-20,20],
             'volatility': float in [-2,2], 'atr': float, 'bars': int}
    for a single timeframe's OHLC DataFrame.
    """
    if df is None or len(df) < MIN_BARS:
        return {"trend": 0.0, "momentum": 0.0, "volatility": 0.0, "atr": None, "bars": 0 if df is None else len(df)}

    close = df["Close"]
    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    macd_line, signal_line, hist = macd(close)

    # --- trend vote (-8..+8): four binary votes worth 2 points each ---
    trend = 0.0
    trend += 2 if ema20.iloc[-1] > ema50.iloc[-1] else -2
    trend += 2 if close.iloc[-1] > ema20.iloc[-1] else -2
    trend += 2 if macd_line.iloc[-1] > signal_line.iloc[-1] else -2
    lookback = min(10, len(ema50) - 1)
    trend += 2 if ema50.iloc[-1] > ema50.iloc[-1 - lookback] else -2
    trend = clamp(trend, -8, 8)

    # --- momentum vote (-20..+20) ---
    rsi_val = rsi(close).iloc[-1]
    k, d = stochastic(df)
    atr_series = atr(df)
    atr_now = atr_series.iloc[-1]

    mom = 0.0
    mom += clamp((rsi_val - 50) / 5.0, -8, 8)
    mom += clamp((k.iloc[-1] - d.iloc[-1]) / 2.0, -6, 6)
    hist_norm = (hist.iloc[-1] / atr_now) if atr_now and not np.isnan(atr_now) and atr_now > 0 else 0
    mom += clamp(hist_norm * 6, -6, 6)
    mom = clamp(mom, -20, 20)

    # --- volatility vote (-2..+2): is ATR expanding/contracting, and in
    #     which direction does that favor (continuation vs. fade)? ---
    atr_avg = atr_series.rolling(20, min_periods=10).mean().iloc[-1]
    vol_ratio = (atr_now / atr_avg) if atr_avg and not np.isnan(atr_avg) and atr_avg > 0 else 1.0
    trend_dir = sign(trend)
    if vol_ratio >= 1.3:
        vol = 2 * (trend_dir or 1)
    elif vol_ratio >= 1.1:
        vol = 1 * (trend_dir or 1)
    elif vol_ratio <= 0.75:
        vol = -1
    else:
        vol = 0
    vol = clamp(vol, -2, 2)

    return {
        "trend": round(trend, 2),
        "momentum": round(mom, 2),
        "volatility": vol,
        "atr": None if atr_now is None or np.isnan(atr_now) else float(atr_now),
        "bars": len(df),
    }


def detect_structure(df: pd.DataFrame, lookback: int = 80, window: int = 2):
    """
    Very simplified fractal-based swing-structure read (ICT/SMC style):
    looks at the last two confirmed swing highs and swing lows.
      - higher high + higher low  -> ("Bullish", last swing low)
      - lower high + lower low    -> ("Bearish", last swing high)
      - anything else             -> ("None", None)
    The second element is a suggested invalidation level.
    """
    if df is None or len(df) < (2 * window + 10):
        return "None", None

    highs, lows = df["High"], df["Low"]
    n = len(df)
    start = max(window, n - lookback)
    swing_highs, swing_lows = [], []

    for i in range(start, n - window):
        window_high = highs.iloc[i - window : i + window + 1]
        window_low = lows.iloc[i - window : i + window + 1]
        if highs.iloc[i] == window_high.max():
            swing_highs.append(float(highs.iloc[i]))
        if lows.iloc[i] == window_low.min():
            swing_lows.append(float(lows.iloc[i]))

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        h2, h1 = swing_highs[-2], swing_highs[-1]
        l2, l1 = swing_lows[-2], swing_lows[-1]
        if h1 > h2 and l1 > l2:
            return "Bullish", l1
        if h1 < h2 and l1 < l2:
            return "Bearish", h1
    return "None", None
