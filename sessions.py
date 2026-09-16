"""
sessions.py
============
Which FX/metals session is live, and what that means for a gold trade.

The liquidity notes aren't decoration — session matters more for short
horizons than almost anything else in the signal. A 5-minute gold setup
during the Asia lunch lull and the same setup during the London/NY overlap
are not the same trade: the spread can be several times wider and stops get
wicked out by moves that carry no information. The scanner surfaces this so
you can discount a signal that fires in thin conditions.

All windows are UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone

SESSIONS = [
    {
        "name": "Sydney",
        "start": 21, "end": 24,
        "emoji": "\U0001f1e6\U0001f1fa",
        "liquidity": "thin",
        "note": "Sydney session \u2014 thinnest liquidity of the day, wide spreads, expect slippage \u26a0\ufe0f",
    },
    {
        "name": "Asia",
        "start": 0, "end": 7,
        "emoji": "\U0001f5fe",
        "liquidity": "thin",
        "note": "Asia session \u2014 thinner liquidity, expect slippage \u26a0\ufe0f",
    },
    {
        "name": "London",
        "start": 7, "end": 12,
        "emoji": "\U0001f1ec\U0001f1e7",
        "liquidity": "deep",
        "note": "London session \u2014 deep liquidity, gold's most reliable directional window",
    },
    {
        "name": "London/NY overlap",
        "start": 12, "end": 16,
        "emoji": "\U0001f525",
        "liquidity": "deepest",
        "note": "London/NY overlap \u2014 deepest liquidity and the widest real ranges of the day",
    },
    {
        "name": "New York",
        "start": 16, "end": 21,
        "emoji": "\U0001f1fa\U0001f1f8",
        "liquidity": "moderate",
        "note": "New York afternoon \u2014 liquidity fades after the London close, trends can stall",
    },
]

# Kill zones: the windows where institutional order flow concentrates.
KILL_ZONES = [(7, 10, "London open"), (12, 15, "NY open")]


def get_session(ts: datetime | None = None) -> dict:
    ts = ts or datetime.now(timezone.utc)
    hour = ts.hour
    for s in SESSIONS:
        if s["start"] <= hour < s["end"]:
            return s
    return SESSIONS[0]  # 21:00-24:00 wrap


def in_kill_zone(ts: datetime | None = None):
    ts = ts or datetime.now(timezone.utc)
    for start, end, name in KILL_ZONES:
        if start <= ts.hour < end:
            return True, name
    return False, None


def session_is_thin(ts: datetime | None = None) -> bool:
    return get_session(ts)["liquidity"] in ("thin",)
