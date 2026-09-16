"""
horizons.py
============
Defines the four signal horizons the scanner drops: next 5 min, 15 min,
1 hour, 4 hours.

Each horizon reads a different set of timeframes and — importantly — sizes
its stop from an ATR on a timeframe that actually matches the holding
period. The previous version used 1.5 x ATR(H1) for every signal, which is
far too wide a stop for a 5-minute trade: you'd be risking an hour's worth
of range on a five-minute idea, which quietly destroys the R:R the message
claims. Each horizon now has its own ATR reference and multiplier.

`expiry_min` is how long the setup stays valid. After that the entry is
considered stale — if price hasn't filled the zone, the setup is dead and
you don't chase it.
"""

from __future__ import annotations

HORIZONS = {
    "M5": {
        "key": "M5",
        "label": "\u26a1 Next 5 min",
        "short": "5m",
        "tf_set": {"M1", "M5", "M15"},
        "atr_tf": "M5",        # stop is sized from M5 ATR
        "stop_mult": 1.2,
        "structure_tf": "M15",
        "expiry_min": 5,
        "order": 0,
    },
    "M15": {
        "key": "M15",
        "label": "\u23f1\ufe0f Next 15 min",
        "short": "15m",
        "tf_set": {"M5", "M15", "H1"},
        "atr_tf": "M15",
        "stop_mult": 1.3,
        "structure_tf": "H1",
        "expiry_min": 15,
        "order": 1,
    },
    "H1": {
        "key": "H1",
        "label": "\U0001f552 Next 1 hour",
        "short": "1h",
        "tf_set": {"M15", "H1", "H4"},
        "atr_tf": "H1",
        "stop_mult": 1.5,
        "structure_tf": "H4",
        "expiry_min": 60,
        "order": 2,
    },
    "H4": {
        "key": "H4",
        "label": "\U0001f4c5 Next 4 hours",
        "short": "4h",
        "tf_set": {"H1", "H4", "D1"},
        "atr_tf": "H4",
        "stop_mult": 1.8,
        "structure_tf": "D1",
        "expiry_min": 240,
        "order": 3,
    },
}

HORIZON_ORDER = ["M5", "M15", "H1", "H4"]

# Every timeframe any horizon might need — fetched once per scan and shared.
ALL_TIMEFRAMES = sorted({tf for h in HORIZONS.values() for tf in h["tf_set"]} | {"M1"})


def get_horizon(key: str) -> dict:
    return HORIZONS[key.upper()]


def parse_horizons(tokens) -> list:
    """Turn user input like ['5m','1h'] or ['all'] into a list of horizon keys."""
    alias = {
        "5m": "M5", "m5": "M5", "5": "M5",
        "15m": "M15", "m15": "M15", "15": "M15",
        "1h": "H1", "h1": "H1", "60m": "H1",
        "4h": "H4", "h4": "H4", "240m": "H4",
    }
    if not tokens:
        return list(HORIZON_ORDER)
    out = []
    for t in tokens:
        t = str(t).strip().lower()
        if t == "all":
            return list(HORIZON_ORDER)
        key = alias.get(t)
        if key and key not in out:
            out.append(key)
    return out or list(HORIZON_ORDER)
