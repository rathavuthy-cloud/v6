"""
tracker.py
===========
Records every signal the scanner drops, then resolves it against real price
action and keeps an honest running record.

Why this module is the most valuable one in the repo: a signal generator
without outcome tracking is unfalsifiable. It can print "PREMIUM SIGNAL" a
hundred times a day and you'd never know it was wrong. The track record
line (0W/4L etc.) is what turns this from a confidence-generator into
something you can actually evaluate and switch off.

Resolution uses M1 candle highs/lows after the signal opened, not a spot
price check on a 5-minute poll — a poll would miss a stop that was hit and
reversed between ticks, which flatters the record.

Two deliberately conservative choices, so the record errs against the bot
rather than for it:
  * If a bar's range covers BOTH the stop and the target, it's booked as a
    loss. Without tick data there's no way to know which came first, and
    assuming the win is how backtests lie.
  * Entry is assumed filled at the signal price. Real fills slip, usually
    against you, so live results should be expected to run slightly worse
    than what's recorded here.

State is a JSON file (SIGNAL_STATE_PATH, default ./signal_state.json).
On Railway and similar ephemeral filesystems this resets on redeploy —
mount a volume if you want the record to survive.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("xauusd-live-bot.tracker")

STATE_PATH = os.environ.get("SIGNAL_STATE_PATH", "signal_state.json")
_LOCK = threading.Lock()

# --- circuit-breaker defaults (all overridable via /settings) ---
DEFAULT_LOSS_STREAK_LIMIT = 3      # consecutive losses before auto-drops pause
DEFAULT_DAILY_LOSS_LIMIT_R = 3.0   # stop after -3R on the day
DEFAULT_MAX_OPEN = 3               # don't stack more than this many live ideas


@dataclass
class Trade:
    id: str
    symbol: str
    horizon: str
    direction: str            # BUY | SELL
    entry: float
    stop: float
    tp1: float
    tp2: float
    risk_usd: float           # stop distance in USD (1R)
    opened_at: str            # ISO
    expires_at: str           # ISO
    status: str = "OPEN"      # OPEN | WIN | LOSS | EXPIRED | AMBIGUOUS
    closed_at: Optional[str] = None
    exit_price: Optional[float] = None
    r_multiple: Optional[float] = None
    note: str = ""
    lots: Optional[float] = None
    chat_id: Optional[int] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"trades": [], "paused": False, "paused_reason": ""}
    try:
        with open(STATE_PATH, "r") as fh:
            data = json.load(fh)
        data.setdefault("trades", [])
        data.setdefault("paused", False)
        data.setdefault("paused_reason", "")
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s (%s) \u2014 starting a fresh record.", STATE_PATH, exc)
        return {"trades": [], "paused": False, "paused_reason": ""}


def _save(data: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        log.error("Could not persist signal state: %s", exc)


def record_signal(r: dict, horizon: str, lots: Optional[float] = None, chat_id: Optional[int] = None) -> Trade:
    """Log a freshly-dropped signal as an OPEN trade."""
    h_expiry = r.get("expiry_min")
    h_expiry = 60 if h_expiry is None else max(float(h_expiry), 0.0)
    opened = _now()
    trade = Trade(
        id=uuid.uuid4().hex[:10],
        symbol=r.get("symbol", "XAU/USD"),
        horizon=horizon,
        direction=r["action"],
        entry=float(r["current_price"]),
        stop=float(r["stop_loss"]),
        tp1=float(r["tp1"]),
        tp2=float(r["tp2"]),
        risk_usd=abs(float(r["current_price"]) - float(r["stop_loss"])),
        opened_at=opened.isoformat(),
        expires_at=(opened + timedelta(minutes=h_expiry)).isoformat(),
        lots=lots,
        chat_id=chat_id,
    )
    with _LOCK:
        data = _load()
        data["trades"].append(asdict(trade))
        _save(data)
    return trade


def open_trades() -> list:
    return [t for t in _load()["trades"] if t["status"] == "OPEN"]


def _resolve_one(t: dict, m1_df) -> Optional[dict]:
    """
    Walk the M1 bars after the trade opened and decide the outcome.
    Returns the mutated trade dict if it closed, else None.
    """
    import pandas as pd  # local import keeps this module importable without pandas

    opened_at = datetime.fromisoformat(t["opened_at"])
    expires_at = datetime.fromisoformat(t["expires_at"])

    idx = m1_df.index
    if getattr(idx, "tz", None) is None:
        bars = m1_df[idx.to_series().apply(lambda x: x.replace(tzinfo=timezone.utc)) > opened_at]
    else:
        bars = m1_df[idx > opened_at]

    is_buy = t["direction"] == "BUY"
    stop, tp1 = t["stop"], t["tp1"]

    for ts, bar in bars.iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])
        hit_stop = (lo <= stop) if is_buy else (hi >= stop)
        hit_tp = (hi >= tp1) if is_buy else (lo <= tp1)

        if hit_stop and hit_tp:
            # Both inside one bar — no tick data, so book the loss.
            t.update(status="LOSS", exit_price=stop, r_multiple=-1.0,
                     closed_at=str(ts), note="Stop and target both inside one M1 bar \u2014 booked as a loss (conservative).")
            return t
        if hit_stop:
            t.update(status="LOSS", exit_price=stop, r_multiple=-1.0, closed_at=str(ts))
            return t
        if hit_tp:
            r_mult = abs(tp1 - t["entry"]) / t["risk_usd"] if t["risk_usd"] else 1.0
            t.update(status="WIN", exit_price=tp1, r_multiple=round(r_mult, 2), closed_at=str(ts))
            return t

    # Neither level touched — has it timed out?
    if _now() > expires_at:
        if len(bars):
            last_close = float(bars["Close"].iloc[-1])
        else:
            last_close = t["entry"]
        direction = 1 if is_buy else -1
        r_mult = (last_close - t["entry"]) * direction / t["risk_usd"] if t["risk_usd"] else 0.0
        t.update(status="EXPIRED", exit_price=last_close, r_multiple=round(r_mult, 2),
                 closed_at=_now().isoformat(), note="Horizon elapsed without hitting stop or target.")
        return t

    return None


def resolve_open_trades(m1_df) -> list:
    """Resolve every OPEN trade against fresh M1 candles. Returns newly-closed trades."""
    closed = []
    with _LOCK:
        data = _load()
        for t in data["trades"]:
            if t["status"] != "OPEN":
                continue
            try:
                result = _resolve_one(t, m1_df)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not resolve trade %s: %s", t.get("id"), exc)
                continue
            if result:
                closed.append(dict(result))
        if closed:
            _save(data)
    return closed


def stats() -> dict:
    data = _load()
    trades = data["trades"]
    closed = [t for t in trades if t["status"] in ("WIN", "LOSS", "EXPIRED")]
    wins = [t for t in closed if t["status"] == "WIN"]
    losses = [t for t in closed if t["status"] == "LOSS"]
    expired = [t for t in closed if t["status"] == "EXPIRED"]
    open_n = len([t for t in trades if t["status"] == "OPEN"])

    decided = len(wins) + len(losses)
    win_rate = (len(wins) / decided * 100) if decided else 0.0
    r_values = [t["r_multiple"] for t in closed if t.get("r_multiple") is not None]
    total_r = sum(r_values) if r_values else 0.0
    expectancy = (total_r / len(r_values)) if r_values else 0.0

    # consecutive losses, most recent first
    streak = 0
    for t in sorted(closed, key=lambda x: x.get("closed_at") or "", reverse=True):
        if t["status"] == "LOSS":
            streak += 1
        elif t["status"] == "WIN":
            break

    today = _now().date().isoformat()
    today_r = sum(
        t.get("r_multiple") or 0.0
        for t in closed
        if (t.get("closed_at") or "").startswith(today)
    )

    return {
        "wins": len(wins), "losses": len(losses), "expired": len(expired),
        "open": open_n, "closed": len(closed), "decided": decided,
        "win_rate": win_rate, "total_r": total_r, "expectancy": expectancy,
        "loss_streak": streak, "today_r": today_r,
        "paused": data.get("paused", False), "paused_reason": data.get("paused_reason", ""),
    }


def check_circuit_breaker(settings: dict) -> tuple:
    """
    Returns (blocked: bool, reason: str). Mirrors the drawdown-circuit-breaker
    idea from claude-trading-skills: losing-streak cooldown, daily loss limit,
    and a cap on concurrent open risk.
    """
    s = stats()
    if s["paused"]:
        return True, s["paused_reason"] or "Auto-signals manually paused."

    streak_limit = settings.get("lossstreak") or DEFAULT_LOSS_STREAK_LIMIT
    daily_limit = settings.get("dailyloss") or DEFAULT_DAILY_LOSS_LIMIT_R
    max_open = settings.get("maxopen") or DEFAULT_MAX_OPEN

    if s["loss_streak"] >= streak_limit:
        return True, f"Auto-signals are paused after a losing streak ({s['loss_streak']} in a row, limit {streak_limit})."
    if s["today_r"] <= -abs(daily_limit):
        return True, f"Daily loss limit hit ({s['today_r']:+.1f}R today, limit -{abs(daily_limit):.1f}R)."
    if s["open"] >= max_open:
        return True, f"{s['open']} trades already open (max {max_open}) \u2014 not stacking more risk."
    return False, ""


def set_paused(paused: bool, reason: str = "") -> None:
    with _LOCK:
        data = _load()
        data["paused"] = paused
        data["paused_reason"] = reason
        _save(data)


def recent_closed(limit: int = 10) -> list:
    closed = [t for t in _load()["trades"] if t["status"] != "OPEN"]
    return sorted(closed, key=lambda x: x.get("closed_at") or "", reverse=True)[:limit]


def reset_record() -> int:
    with _LOCK:
        data = _load()
        n = len(data["trades"])
        data["trades"] = []
        data["paused"] = False
        data["paused_reason"] = ""
        _save(data)
    return n


def has_live_signal(horizon: str, direction: str) -> bool:
    """Dedupe guard: is an identical idea already open on this horizon?"""
    return any(
        t["horizon"] == horizon and t["direction"] == direction
        for t in open_trades()
    )
