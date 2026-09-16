"""
live_signal.py
===============
Runs a full multi-horizon scan: one data fetch, four independent signals
(5m / 15m / 1h / 4h), each with its own stop, targets, expiry and size.

Design note worth knowing: all timeframes are fetched ONCE per scan and
shared across horizons. With a 5-minute auto-scan cadence hitting Yahoo's
free endpoint, re-fetching per horizon would mean ~4x the requests and is
the fastest way to get rate-limited into silence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import market_data as md
import tracker
from horizons import ALL_TIMEFRAMES, HORIZON_ORDER, HORIZONS
from indicators import adx, atr, compute_tf_votes, detect_structure
from market_data import DataFetchError
from signal_engine import build_json_out, compute_signal
from sizing import size_position

log = logging.getLogger("xauusd-live-bot.live")

DEFAULT_SETTINGS = {
    "equity": None,
    "risk": 1.0,
    "contractsize": 100.0,
    "lotstep": 0.01,
    "minlot": 0.01,
    "maxlot": 100.0,
    "kfactor": 0.3,
    "tp1r": 2.0,
    "tp2r": 3.0,
    "lossstreak": 3,
    "dailyloss": 3.0,
    "maxopen": 3,
}

# How stale price data may be before a horizon refuses to signal.
STALE_MIN_BY_HORIZON = {"M5": 3, "M15": 6, "H1": 15, "H4": 45}

# Minutes per bar, for the hold-time estimate.
BAR_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}


def settings_with_defaults(settings: Optional[dict]) -> dict:
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in (settings or {}).items() if v is not None})
    return merged


def fetch_market_snapshot() -> dict:
    """One fetch of everything every horizon might need."""
    ohlc, votes, errors = {}, {}, []
    for tf in ALL_TIMEFRAMES:
        try:
            df = md.get_ohlc(tf)
            ohlc[tf] = df
            votes[tf] = compute_tf_votes(df)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{tf}: {exc}")
            log.warning("Could not fetch %s: %s", tf, exc)

    adx_by_tf = {}
    for tf, df in ohlc.items():
        try:
            adx_by_tf[tf] = float(adx(df)[0].iloc[-1])
        except Exception:  # noqa: BLE001
            adx_by_tf[tf] = None

    try:
        price = md.get_last_price()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"price: {exc}")
        price = None
        if "M1" in ohlc:
            price = float(ohlc["M1"]["Close"].iloc[-1])

    try:
        macro = md.get_macro()
    except Exception:  # noqa: BLE001
        macro = {"dxyPrice": None, "dxyChangePct": None, "y10": None, "y10ChangeBps": None, "vix": None}

    event = md.get_next_high_impact_event()

    # A total fetch failure must propagate. Swallowing it here means the scan
    # quietly returns INSUFFICIENT_DATA on every horizon forever, the auto-job
    # never increments its failure counter, and the bot goes silently dead
    # during a Yahoo outage with the user assuming "no setups right now".
    if not ohlc:
        raise DataFetchError(
            "no market data could be fetched for any timeframe: "
            + "; ".join(errors[:3])
        )
    if price is None:
        raise DataFetchError("could not determine a current price: " + "; ".join(errors[:2]))

    return {
        "ohlc": ohlc, "votes": votes, "adx": adx_by_tf, "price": price,
        "macro": macro, "event": event, "errors": errors,
        "fetched_at": datetime.now(timezone.utc),
    }


def build_horizon_input(horizon_key: str, snap: dict, settings: dict) -> dict:
    h = HORIZONS[horizon_key]
    votes, ohlc = snap["votes"], snap["ohlc"]
    tf_set = {tf for tf in h["tf_set"] if tf in votes}

    atr_tf = h["atr_tf"]
    atr_stop = votes.get(atr_tf, {}).get("atr")

    # Structure + invalidation from the horizon's reference timeframe.
    struct_tf = h["structure_tf"] if h["structure_tf"] in ohlc else atr_tf
    if struct_tf in ohlc:
        structure_direction, invalidation_level = detect_structure(ohlc[struct_tf])
    else:
        structure_direction, invalidation_level = "None", None

    # Speed of the market on the reference timeframe, converted to $/minute,
    # for the hold-time estimate.
    per_min = None
    if atr_stop and BAR_MINUTES.get(atr_tf):
        per_min = atr_stop / BAR_MINUTES[atr_tf]

    d = {
        "classification": horizon_key,
        "symbol": "XAU/USD",
        "horizon": horizon_key,
        "horizonLabel": h["label"],
        "expiryMin": h["expiry_min"],
        "staleThresholdMin": STALE_MIN_BY_HORIZON.get(horizon_key, 10),
        "currentPrice": snap["price"],
        "priceTimestamp": snap["fetched_at"],
        "atrStop": atr_stop,
        "stopMult": h["stop_mult"],
        "atr1m": per_min,
        "tf_used": tf_set,
        "newsChecked": bool(snap["event"].get("eventName")) or snap["event"].get("calendarConfigured", False),
    }

    for tf in tf_set:
        v = votes[tf]
        d[f"trend-{tf}"] = v["trend"]
        d[f"mom-{tf}"] = v["momentum"]
        d[f"vol-{tf}"] = v["volatility"]

    # ADX regime read comes from the horizon's own stop timeframe.
    adx_val = snap["adx"].get(atr_tf)
    if adx_val is not None:
        d["adx-H1"] = adx_val

    d.update(snap["macro"])
    d.update(snap["event"])
    d["speakerRisk"] = False
    d["structureDirection"] = structure_direction
    d["invalidationLevel"] = invalidation_level
    d["accountEquity"] = settings.get("equity")
    d["riskPct"] = settings.get("risk")
    d["stopLossOverride"] = None
    d["kFactor"] = settings.get("kfactor")
    d["tp1R"] = settings.get("tp1r")
    d["tp2R"] = settings.get("tp2r")
    return d


def scan(horizon_keys=None, settings: Optional[dict] = None, snap: Optional[dict] = None) -> dict:
    """
    Run every requested horizon against one market snapshot.
    Returns {'snapshot':..., 'results': [...], 'settings':...}.
    """
    settings = settings_with_defaults(settings)
    horizon_keys = horizon_keys or list(HORIZON_ORDER)
    snap = snap or fetch_market_snapshot()

    results = []
    for key in sorted(horizon_keys, key=lambda k: HORIZONS[k]["order"]):
        d = build_horizon_input(key, snap, settings)
        r = compute_signal(d)
        r["horizon"] = key
        r["horizon_label"] = HORIZONS[key]["label"]
        r["expiry_min"] = HORIZONS[key]["expiry_min"]

        r["sizing"] = None
        if r["status"] == "SIGNAL" and r.get("stop_distance_usd"):
            r["sizing"] = size_position(
                equity=settings.get("equity"),
                risk_pct=settings.get("risk"),
                stop_distance_usd=r["stop_distance_usd"],
                contract_size=settings.get("contractsize"),
                lot_step=settings.get("lotstep"),
                min_lot=settings.get("minlot"),
                max_lot=settings.get("maxlot"),
            )
            if r["sizing"].ok:
                r["lots"] = r["sizing"].lots

        r["json"] = build_json_out(d, r)
        results.append(r)

    return {"snapshot": snap, "results": results, "settings": settings}


def resolve_outcomes(snap: Optional[dict] = None) -> list:
    """Close out any open trades that hit their stop/target or timed out."""
    try:
        m1 = (snap or {}).get("ohlc", {}).get("M1")
        if m1 is None:
            m1 = md.get_ohlc("M1")
        return tracker.resolve_open_trades(m1)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not resolve open trades: %s", exc)
        return []
