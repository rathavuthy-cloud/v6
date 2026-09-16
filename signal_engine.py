"""
signal_engine.py
=================
This is a direct port of the original xauusd_telegram_bot.py's compute_signal()
/ build_json_out() / format_result_message() — the confluence-scoring "brain".

Nothing about the scoring logic itself has changed: same weighting of trend
(40%) / momentum (45%) / volatility (15%) per timeframe, same H1/H4 alignment
gate, same 7-point confluence checklist, same ATR-based stop/target/position-
sizing math. What changed is *where the inputs come from* — see
market_data.py for the live-fetch layer that now fills in the dict this file
consumes, instead of a human answering 30 Telegram prompts.

IMPORTANT: This is a rules-based technical/macro heuristic, not a proven
trading edge. Treat every output as a hypothesis to verify yourself, not an
instruction. See the disclaimer field on every JSON output.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

TF_ORDER = ["M1", "M5", "M15", "H1", "H4", "D1"]
TF_WEIGHT = {"M1": 5, "M5": 10, "M15": 20, "H1": 30, "H4": 35, "D1": 40}
STALE_THRESHOLD_MIN = {"SCALP": 5, "INTRADAY": 10, "SWING": 30}

# Which timeframes each classification pulls live indicator votes from.
TF_SET_BY_CLASS = {
    "SCALP": {"M1", "M5", "H1"},
    "INTRADAY": {"M5", "M15", "H1"},
    "SWING": {"M15", "H1", "H4"},
}


def clamp(n, lo, hi):
    return max(lo, min(hi, n))


def sign(n):
    return 1 if n > 0 else (-1 if n < 0 else 0)


def tf_score(trend, mom, vol):
    raw = ((trend / 8) * 0.40 + (mom / 20) * 0.45 + (vol / 2) * 0.15) * 100
    return clamp(raw, -100, 100)


def band(score):
    if score is None:
        return "N/A"
    if score <= -60:
        return "Strong Sell"
    if score <= -20:
        return "Sell"
    if score < 20:
        return "Neutral"
    if score < 60:
        return "Buy"
    return "Strong Buy"


def fmt(n, d=2):
    if n is None:
        return "\u2014"
    return f"{n:.{d}f}"


def compute_signal(d: dict) -> dict:
    classification = d.get("classification") or "SWING"
    symbol = d.get("symbol") or "XAU/USD"
    current_price = d.get("currentPrice")
    price_ts = d.get("priceTimestamp")

    age_minutes = None
    stale = False
    kill_zone_active = False
    if price_ts is not None:
        age_minutes = (datetime.now(timezone.utc) - price_ts).total_seconds() / 60
        threshold = STALE_THRESHOLD_MIN.get(classification, 10)
        stale = age_minutes > threshold
        utc_hour = price_ts.hour
        kill_zone_active = (7 <= utc_hour < 10) or (12 <= utc_hour < 15)

    used = d.get("tf_used") or set()
    scores = {}
    for tf in TF_ORDER:
        if tf in used:
            trend = d.get(f"trend-{tf}") or 0
            mom = d.get(f"mom-{tf}") or 0
            vol = d.get(f"vol-{tf}") or 0
            scores[tf] = tf_score(trend, mom, vol)
        else:
            scores[tf] = None

    sum_w = sum_ws = 0
    for tf in TF_ORDER:
        if scores[tf] is not None:
            sum_w += TF_WEIGHT[tf]
            sum_ws += TF_WEIGHT[tf] * scores[tf]
    final_score = (sum_ws / sum_w) if sum_w > 0 else None
    score_band = band(final_score)

    adx_h1 = d.get("adx-H1")
    if adx_h1 is not None:
        regime = "RANGING" if adx_h1 < 20 else ("TRENDING" if adx_h1 >= 25 else "TRANSITIONAL")
    else:
        regime = "N/A"

    # --- multi-timeframe alignment gate ---------------------------------
    # Generalized across horizons: take the two highest timeframes this
    # horizon actually reads and require them to point the same way with
    # real conviction. The old version hardcoded H4/H1, which silently
    # blocked every 5-minute signal (that horizon never reads H1 or H4).
    alignment_checked = True
    alignment_ok = False
    available = [tf for tf in TF_ORDER if scores.get(tf) is not None]
    higher_tfs = sorted(available, key=lambda tf: TF_WEIGHT[tf], reverse=True)[:2]
    align_ref = d.get("alignmentTfs") or higher_tfs

    ref_scores = [scores[tf] for tf in align_ref if scores.get(tf) is not None]
    if len(ref_scores) >= 2:
        a, b = ref_scores[0], ref_scores[1]
        alignment_ok = (
            sign(a) == sign(b) and sign(a) != 0 and abs(a) >= 20 and abs(b) >= 20
        )
    elif len(ref_scores) == 1:
        alignment_ok = abs(ref_scores[0]) >= 20

    dxy_change_pct = d.get("dxyChangePct")
    y10_change_bps = d.get("y10ChangeBps")
    dxy_trend = "N/A" if dxy_change_pct is None else ("Up" if dxy_change_pct > 0.15 else ("Down" if dxy_change_pct < -0.15 else "Flat"))
    yield_trend = "N/A" if y10_change_bps is None else ("Rising" if y10_change_bps > 2 else ("Falling" if y10_change_bps < -2 else "Flat"))
    tech_direction = "N/A" if final_score is None else ("Bullish" if final_score > 0 else ("Bearish" if final_score < 0 else "Neutral"))
    conflict_flag = tech_direction not in ("N/A", "Neutral") and (
        (tech_direction == "Bullish" and (dxy_trend == "Up" or yield_trend == "Rising"))
        or (tech_direction == "Bearish" and (dxy_trend == "Down" or yield_trend == "Falling"))
    )

    event_impact = d.get("eventImpact") or "None"
    affects_usd_gold = bool(d.get("eventAffectsUsdGold"))
    minutes_to_event = d.get("minutesToEvent")
    event_type = d.get("eventType") or "Standard"
    stab_window = 12 if event_type in ("NFP", "FOMC") else 3
    pre_blackout = event_impact != "None" and affects_usd_gold and minutes_to_event is not None and 0 <= minutes_to_event <= 10
    post_window = event_impact != "None" and affects_usd_gold and minutes_to_event is not None and minutes_to_event < 0 and abs(minutes_to_event) <= stab_window
    blackout_active = pre_blackout or post_window
    speaker_risk = bool(d.get("speakerRisk"))
    news_state = "BLACKOUT" if blackout_active else ("ELEVATED_RISK" if speaker_risk else "CLEAR")

    structure_direction = d.get("structureDirection") or "None"
    structure_agrees = structure_direction != "None" and final_score is not None and (
        (structure_direction == "Bullish" and final_score > 0) or (structure_direction == "Bearish" and final_score < 0)
    )

    invalidation_level = d.get("invalidationLevel")
    tp1_r = d.get("tp1R")
    tp1_r = tp1_r if tp1_r is not None else 1.5

    # --- stop geometry -------------------------------------------------
    # Computed here, before the checklist, because whether the invalidation
    # level is actually reachable determines two of the criteria.
    atr_stop = d.get("atrStop")
    if atr_stop is None:
        atr_stop = d.get("atr1h")
    stop_mult = d.get("stopMult")
    stop_mult = 1.5 if stop_mult is None else stop_mult
    structural_buffer = stop_mult * atr_stop if atr_stop is not None else None
    ob_distance = abs(current_price - invalidation_level) if (current_price is not None and invalidation_level is not None) else None

    # Cap the stop distance. Taking max(atr_buffer, distance_to_invalidation)
    # unbounded means that whenever the last swing point sits far from price —
    # which happens constantly in a trend — the stop balloons to something
    # untradeable while the R:R arithmetic still "works".
    max_stop_mult = d.get("maxStopAtrMult")
    max_stop_mult = 3.0 if max_stop_mult is None else max_stop_mult
    stop_cap = max_stop_mult * atr_stop if atr_stop is not None else None

    # Note the distinction: a far-away invalidation level means the stop gets
    # capped tighter than structure, NOT that the structure read is wrong.
    # Conflating the two makes c1/c7 fail on any healthy trend (the swing low
    # is usually 3-6 ATR back), which silently caps confluence at 5/7 and, via
    # the ranging gate, blocks every ranging signal forever.
    stop_tighter_than_structure = bool(
        stop_cap is not None and ob_distance is not None and ob_distance > stop_cap
    )

    stop_dist = None
    if structural_buffer is not None or ob_distance is not None:
        stop_dist = max(structural_buffer or 0, ob_distance or 0)
        if stop_cap is not None and stop_dist > stop_cap:
            stop_dist = stop_cap

    c1 = structure_agrees
    c2 = alignment_checked and alignment_ok
    c3 = not conflict_flag
    c4 = news_state == "CLEAR"
    c5 = kill_zone_active
    c6 = tp1_r >= 1.5
    c7 = invalidation_level is not None

    criteria = [
        ("Technical direction agrees with market structure", c1),
        (f"{'/'.join(align_ref) if align_ref else 'Multi-timeframe'} alignment holds", c2),
        ("Macro layer (DXY/yields) does not conflict", c3),
        ("News status is clear", c4),
        ("Setup is inside an active kill-zone window", c5),
        ("Risk:reward at TP1 \u2265 1:1.5", c6),
        ("A clear invalidation level is defined", c7),
    ]
    criteria_met = sum(1 for _, ok in criteria if ok)
    tier = "HIGH" if criteria_met >= 6 else ("MODERATE" if criteria_met >= 4 else "LOW")
    confluence_pct = round(criteria_met / 7 * 100)

    # --- data completeness ---------------------------------------------
    # `missing` blocks signal generation: without these the levels would be
    # invented. Things that only degrade the output (no macro context, no
    # account equity) go in `notices` instead, so a 5-minute setup isn't
    # thrown away because the user hasn't typed their balance in yet.
    missing, notices = [], []

    if current_price is None:
        missing.append("current_price")
    if price_ts is None:
        missing.append("data_timestamp")
    if sum_w == 0:
        missing.append("no timeframe votes could be computed (insufficient live candles)")
    if d.get("atrStop") is None and d.get("atr1h") is None:
        missing.append("ATR for the stop timeframe \u2014 cannot place a stop")
    if stale:
        thresh = d.get("staleThresholdMin") or STALE_THRESHOLD_MIN.get(classification, 10)
        missing.append(f"price data is stale ({age_minutes:.1f}min old, max {thresh:g}min for this horizon)")

    if d.get("dxyPrice") is None:
        notices.append("DXY unavailable \u2014 macro conflict check skipped")
    if d.get("y10") is None:
        notices.append("US10Y unavailable \u2014 macro conflict check skipped")
    if d.get("vix") is None:
        notices.append("VIX unavailable")
    if d.get("accountEquity") is None:
        notices.append("account equity not set \u2014 position size unavailable (/settings equity=...)")
    if d.get("riskPct") is None:
        notices.append("risk % not set \u2014 position size unavailable (/settings risk=1)")
    if not d.get("newsChecked"):
        notices.append("news calendar not configured \u2014 running news-blind (set FINNHUB_API_KEY)")
    if stop_tighter_than_structure:
        notices.append(
            f"invalidation level is >{max_stop_mult:g}x ATR away \u2014 stop capped tighter than "
            "structure, so price can stop you out without invalidating the setup"
        )

    status, action, reason = None, "NONE", ""
    if missing:
        status = "INSUFFICIENT_DATA"
        reason = "Missing or stale: " + "; ".join(missing) + "."
    elif blackout_active:
        status = "NEWS_BLACKOUT"
        reason = "Signal generation frozen \u2014 inside the pre/post-news window for " + (d.get("eventName") or "the scheduled release") + "."
    elif final_score is None or abs(final_score) < 20:
        status = "NO_SIGNAL"
        reason = "Blended technical score is inside the Neutral band (\u221219\u2026+19)."
    elif not alignment_ok:
        status = "NO_SIGNAL"
        ref_txt = " and ".join(align_ref) if align_ref else "higher timeframes"
        reason = f"{ref_txt} disagree or don't both clear \u00b120 \u2014 the multi-timeframe gate blocks the signal."
    elif tier == "LOW":
        status = "NO_SIGNAL"
        reason = f"Only {criteria_met}/7 confluence criteria met (LOW tier)."
    elif regime == "RANGING" and criteria_met < 6:
        # In a ranging market this scoring model fires readily on noise:
        # EMA crossovers and momentum swings flip constantly with no follow-
        # through, so a MODERATE score there carries much less information
        # than the same score in a trending regime. Demand HIGH tier instead.
        status = "NO_SIGNAL"
        reason = (f"Ranging regime (ADX {adx_h1:.0f}) needs 6/7 confluence, got {criteria_met}/7 \u2014 "
                  "directional signals are unreliable in chop.")
    else:
        status = "SIGNAL"
        action = "BUY" if final_score > 0 else "SELL"
        reason = f"{criteria_met}/7 confluence criteria met ({tier}). {score_band} band, {regime.lower()} regime."

    direction = 1 if action == "BUY" else (-1 if action == "SELL" else 0)
    stop_override = d.get("stopLossOverride")
    stop_loss = None
    if stop_override is not None:
        stop_loss = stop_override
    elif direction != 0 and current_price is not None and stop_dist is not None:
        stop_loss = current_price - direction * stop_dist

    stop_distance_usd = abs(current_price - stop_loss) if (current_price is not None and stop_loss is not None) else None
    # What the stop WOULD be if this horizon fired — useful for diagnostics
    # and for sanity-checking that each horizon's risk matches its duration.
    prospective_stop_distance = stop_dist

    # Position sizing now lives in sizing.py, which uses a real contract spec
    # instead of an assumed pip value. `lots` is attached by the caller.
    lots = None

    # Entry zone: a retest band around the signal price, scaled to the stop so
    # it stays proportionate across horizons (a 4h setup gets a wider zone than
    # a 5m one). ~3% of stop distance each side.
    entry_zone = (None, None)
    if current_price is not None and stop_distance_usd:
        half = max(stop_distance_usd * 0.03, 0.05)
        entry_zone = (current_price - half, current_price + half)

    tp2_r = d.get("tp2R")
    tp2_r = tp2_r if tp2_r is not None else 3
    tp1 = tp2 = None
    if direction != 0 and current_price is not None and stop_distance_usd is not None:
        tp1 = current_price + direction * stop_distance_usd * tp1_r
        tp2 = current_price + direction * stop_distance_usd * tp2_r

    # Estimated minutes to TP1, from how fast the market is actually moving
    # on the reference timeframe (ATR per bar / bar length in minutes).
    atr1m = d.get("atr1m")
    k_factor = d.get("kFactor")
    hold_time = None
    if atr1m and k_factor and tp1 is not None and current_price is not None:
        hold_time = abs(tp1 - current_price) / (atr1m * k_factor)
        # never claim a hold time longer than the horizon it's valid for
        expiry = d.get("expiryMin")
        if expiry:
            hold_time = min(hold_time, expiry)

    bull_pct = 50 if final_score is None else round(clamp((final_score + 100) / 2, 0, 100))
    bear_pct = 100 - bull_pct

    return {
        "symbol": symbol, "classification": classification, "status": status, "action": action,
        "reason": reason, "final_score": final_score, "score_band": score_band,
        "criteria": criteria, "criteria_met": criteria_met, "tier": tier, "confluence_pct": confluence_pct,
        "bull_pct": bull_pct, "bear_pct": bear_pct, "regime": regime,
        "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2, "tp1_r": tp1_r, "tp2_r": tp2_r,
        "lots": lots, "hold_time": hold_time, "current_price": current_price,
        "stop_distance_usd": stop_distance_usd,
        "prospective_stop_distance": prospective_stop_distance, "entry_zone": entry_zone,
        "horizon": d.get("horizon"), "horizon_label": d.get("horizonLabel"),
        "expiry_min": d.get("expiryMin"),
        "missing": missing, "notices": notices, "scores": scores, "dxy_trend": dxy_trend, "yield_trend": yield_trend,
        "conflict_flag": conflict_flag, "news_state": news_state, "structure_direction": structure_direction,
        "invalidation_level": invalidation_level, "price_ts": price_ts, "tech_direction": tech_direction,
        "kill_zone_active": kill_zone_active, "age_minutes": age_minutes,
    }


def build_json_out(d: dict, r: dict) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "asset": r["symbol"],
        "classification": r["classification"],
        "status": r["status"],
        "missing_fields": r["missing"],
        "market_state": {
            "current_price": r["current_price"],
            "technical_score": None if r["final_score"] is None else round(r["final_score"], 1),
            "regime": r["regime"],
            "active_trend": r["tech_direction"],
            "per_timeframe_score": {k: (None if v is None else round(v, 1)) for k, v in r["scores"].items()},
        },
        "macro_context": {"dxy_trend": r["dxy_trend"], "yield_trend": r["yield_trend"], "conflict_flag": r["conflict_flag"]},
        "news_status": {"state": r["news_state"], "next_high_impact_event": d.get("eventName") or None},
        "structure_analysis": {"pattern": r["structure_direction"], "invalidation_level": r["invalidation_level"]},
        "confluence": {"criteria_met": r["criteria_met"], "criteria_total": 7, "tier": r["tier"]},
        "signal": {
            "action": r["action"] if r["status"] == "SIGNAL" else "NONE",
            "entry_price": r["current_price"] if r["status"] == "SIGNAL" else None,
            "stop_loss": r["stop_loss"] if r["status"] == "SIGNAL" else None,
            "take_profit_1": r["tp1"] if r["status"] == "SIGNAL" else None,
            "take_profit_2": r["tp2"] if r["status"] == "SIGNAL" else None,
            "risk_reward": f"1:{r['tp1_r']} (TP1) / 1:{r['tp2_r']} (TP2)" if r["status"] == "SIGNAL" else "",
            "estimated_hold_time_minutes": round(r["hold_time"]) if (r["status"] == "SIGNAL" and r["hold_time"] is not None) else None,
            "suggested_position_size_lots": round(r["lots"], 2) if (r["status"] == "SIGNAL" and r["lots"] is not None) else None,
            "reasoning": r["reason"],
        },
        "disclaimer": "Informational only. Rules-based technical/macro heuristic \u2014 not financial advice, not a guarantee of outcome. Verify independently before risking money.",
    }


STATUS_EMOJI = {"SIGNAL": "\U0001F7E2", "NO_SIGNAL": "\u26AA\uFE0F", "NEWS_BLACKOUT": "\U0001F7E0", "INSUFFICIENT_DATA": "\U0001F7E1"}


def format_result_message(d: dict, r: dict) -> str:
    emoji = STATUS_EMOJI.get(r["status"], "\u26AA\uFE0F")
    action_line = f"<b>{r['action']}</b>" if r["status"] == "SIGNAL" else "\u2014"
    lines = [
        f"{emoji} <b>{html.escape(r['symbol'])}</b> \u2014 {r['status'].replace('_', ' ')} ({r['classification']})",
        f"Action: {action_line}    Price: <b>{fmt(r['current_price'])}</b>",
        "",
        f"Technical score: <b>{fmt(r['final_score'], 1)}</b> ({r['score_band']})",
        f"Buy/Sell split: {r['bull_pct']}% / {r['bear_pct']}%",
        f"Confluence: <b>{r['criteria_met']}/7 \u00b7 {r['tier']}</b> ({r['confluence_pct']}%)",
        f"Regime: {r['regime']}",
        "",
        f"<i>{html.escape(r['reason'])}</i>",
        "",
        "<b>Confluence checklist</b>",
    ]
    for label, ok in r["criteria"]:
        lines.append(f"{'\u2705' if ok else '\u25AB\uFE0F'} {html.escape(label)}")

    lines.append("")
    if r["status"] == "SIGNAL":
        lines.append("<b>Trade plan</b>")
        lines.append(f"Entry: {fmt(r['current_price'])}")
        lines.append(f"Stop-loss: {fmt(r['stop_loss'])}")
        lines.append(f"TP1 (1:{r['tp1_r']}): {fmt(r['tp1'])}")
        lines.append(f"TP2 (1:{r['tp2_r']}): {fmt(r['tp2'])}")
        lines.append(f"Position size: {fmt(r['lots'], 2)} lots")
        lines.append(f"Est. hold time: {fmt(r['hold_time'], 0)} min")
    else:
        lines.append("<b>Risk / sizing</b> (n/a \u2014 no active signal)")

    if r["missing"]:
        lines.append("")
        lines.append("<b>\u26A0\uFE0F Missing / stale inputs</b>")
        for m in r["missing"]:
            lines.append(f"\u2022 {html.escape(m)}")

    lines.append("")
    lines.append("<i>Automated technical/macro read-out \u2014 not financial advice. Confirm independently before trading.</i>")
    return "\n".join(lines)
