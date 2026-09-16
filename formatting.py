"""
formatting.py
==============
Builds the signal card. Layout follows the format you asked for: header,
confluence line, a monospace block with the levels, the emoji level strip,
the entry trigger with its zone, session context, expiry, track record,
and the sizing verdict.

One rule this module sticks to: it never prints a level it didn't compute.
No placeholder prices, no "TP3 coming soon", no confidence label that isn't
derived from the actual criteria count. If a number is missing the line is
omitted, because a signal card that looks complete but contains invented
levels is worse than one that admits a gap.
"""

from __future__ import annotations

import html

from sessions import get_session
from sizing import too_small_advice

DIV = "\u2500" * 18

CONFIDENCE_FROM_TIER = {"HIGH": "High", "MODERATE": "Medium", "LOW": "Low"}
CONF_EMOJI = {"High": "\U0001f7e2", "Medium": "\U0001f7e1", "Low": "\U0001f534"}


def fmt(n, d=2):
    return "\u2014" if n is None else f"{n:,.{d}f}"


def _track_record_line(stats: dict) -> str:
    w, l = stats["wins"], stats["losses"]
    decided = stats["decided"]
    rate = f"{stats['win_rate']:.0f}%" if decided else "n/a"
    line = f"\U0001f4c8 Track record: {w}W/{l}L ({rate})"
    if stats["expired"]:
        line += f" \u00b7 {stats['expired']} expired"
    if stats["open"]:
        line += f" \u00b7 {stats['open']} open"
    if stats["closed"]:
        line += f"\n\U0001f4ca Expectancy: {stats['expectancy']:+.2f}R/trade over {stats['closed']} closed"
    return line


def _sizing_block(sizing, settings: dict) -> str:
    if sizing is None:
        return ""
    if sizing.ok:
        out = (
            f"\U0001f4b0 Size: <code>{sizing.lots:g}</code> lot \u00b7 risking "
            f"${sizing.actual_risk:,.2f} ({sizing.actual_risk_pct:.2f}% of account)"
        )
        for w in sizing.warnings:
            out += f"\n\u26a0\ufe0f {html.escape(w)}"
        return out
    msg = f"\u26a0\ufe0f {html.escape(sizing.reason)}"
    if sizing.min_lot_risk is not None:
        msg += " " + too_small_advice()
    return msg


def format_signal_card(r: dict, stats: dict, sizing=None, settings: dict | None = None,
                       paused_reason: str = "") -> str:
    settings = settings or {}
    horizon_label = r.get("horizon_label", "")
    conf = CONFIDENCE_FROM_TIER.get(r["tier"], "Low")
    conf_emoji = CONF_EMOJI[conf]
    action = r["action"]
    side_emoji = "\U0001f7e2" if action == "BUY" else "\U0001f534"
    sess = get_session(r.get("price_ts"))

    lines = []

    if paused_reason:
        lines.append(f"\u26a0\ufe0f {html.escape(paused_reason)} \u2014 here's the raw read anyway:")

    lines.append("\U0001f3c6 <b>XAUUSD SIGNAL</b>")
    lines.append(
        f"Scanner confluence: {r['criteria_met']}/7 factors aligned \u00b7 {conf} confidence"
    )
    lines.append(DIV)

    # monospace block — easy to copy into a platform
    block = [
        f"Signal: {action}",
        f"Entry: {r['current_price']:.2f}",
        f"Stop Loss: {r['stop_loss']:.2f}",
        f"Take Profit 1: {r['tp1']:.2f}",
        f"Take Profit 2: {r['tp2']:.2f}",
        f"Confidence: {conf}",
        f"Risk:Reward: 1:{r['tp1_r']:.1f}",
        f"Timeframe: {horizon_label}",
    ]
    lines.append("<pre>" + html.escape("\n".join(block)) + "</pre>")

    lines.append(DIV)
    lines.append(f"\U0001f4cd Entry <code>{r['current_price']:.2f}</code>")
    lines.append(f"\U0001f6d1 Stop <code>{r['stop_loss']:.2f}</code>  ({r['stop_distance_usd']:.2f} risk)")
    lines.append(f"\U0001f3af TP1 <code>{r['tp1']:.2f}</code>  (+{abs(r['tp1']-r['current_price']):.2f})")
    lines.append(f"\U0001f3af TP2 <code>{r['tp2']:.2f}</code>  (+{abs(r['tp2']-r['current_price']):.2f})")
    lines.append(
        f"\U0001f6e1\ufe0f R:R <code>1:{r['tp1_r']:.1f}</code> \u00b7 \u23f3 Hold {horizon_label}"
    )
    lines.append(DIV)

    zone_lo, zone_hi = r["entry_zone"]
    lines.append(
        f"{side_emoji} \u23f0 Entry trigger \u00b7 {action} on retest of "
        f"<code>{r['current_price']:.2f}</code> (zone <code>{zone_lo:.2f}\u2013{zone_hi:.2f}</code>)"
    )
    lines.append(f"Valid for next ~{r['expiry_min']:g} min \u00b7 {sess['emoji']} {html.escape(sess['note'])}")
    lines.append("If price moves through the zone without filling \u2014 setup expired, don't chase.")
    lines.append(DIV)

    # what actually drove the read
    lines.append(f"{conf_emoji} Score {r['final_score']:+.1f} ({r['score_band']}) \u00b7 {r['regime']} regime")
    failed = [label for label, ok in r["criteria"] if not ok]
    if failed:
        lines.append("\u25ab\ufe0f Not aligned: " + html.escape("; ".join(failed)))

    lines.append(DIV)
    lines.append(_track_record_line(stats))

    sizing_block = _sizing_block(sizing, settings)
    if sizing_block:
        lines.append(sizing_block)

    lines.append(
        "\u26a0\ufe0f Automated technical analysis \u2014 not financial advice. "
        "Never risk more than you can afford to lose."
    )
    return "\n".join(lines)


def format_no_signal(r: dict, stats: dict) -> str:
    horizon_label = r.get("horizon_label", "")
    lines = [
        f"\u26aa\ufe0f <b>XAUUSD \u00b7 {horizon_label}</b> \u2014 no setup",
        f"{r['criteria_met']}/7 factors aligned \u00b7 score {fmt(r['final_score'], 1)} ({r['score_band']})",
        f"<i>{html.escape(r['reason'])}</i>",
    ]
    if r.get("missing"):
        lines.append("\u26a0\ufe0f " + html.escape("; ".join(r["missing"][:4])))
    lines.append(_track_record_line(stats))
    return "\n".join(lines)


def format_scan_digest(results: list, stats: dict, price: float | None) -> str:
    """One compact line per horizon — used for the on-demand /scan view."""
    lines = [f"\U0001f50e <b>XAUUSD multi-horizon scan</b> \u00b7 price <code>{fmt(price)}</code>", DIV]
    for r in results:
        label = r.get("horizon_label", r.get("horizon", ""))
        if r["status"] == "SIGNAL":
            emoji = "\U0001f7e2" if r["action"] == "BUY" else "\U0001f534"
            lines.append(
                f"{emoji} <b>{label}</b> \u2014 {r['action']} @ {r['current_price']:.2f} "
                f"\u00b7 SL {r['stop_loss']:.2f} \u00b7 TP1 {r['tp1']:.2f} "
                f"\u00b7 {r['criteria_met']}/7"
            )
        else:
            short = r["status"].replace("_", " ").lower()
            score = fmt(r["final_score"], 1)
            lines.append(f"\u25ab\ufe0f <b>{label}</b> \u2014 {short} \u00b7 score {score} \u00b7 {r['criteria_met']}/7")
    lines.append(DIV)
    lines.append(_track_record_line(stats))
    lines.append("\u26a0\ufe0f Not financial advice.")
    return "\n".join(lines)


def format_outcome(trade: dict) -> str:
    emoji = {"WIN": "\u2705", "LOSS": "\u274c", "EXPIRED": "\u23f1\ufe0f"}.get(trade["status"], "\u2139\ufe0f")
    r_mult = trade.get("r_multiple")
    r_txt = f"{r_mult:+.2f}R" if r_mult is not None else "\u2014"
    lines = [
        f"{emoji} <b>{trade['status']}</b> \u00b7 {trade['direction']} {trade['symbol']} ({trade['horizon']})",
        f"Entry {trade['entry']:.2f} \u2192 exit {trade['exit_price']:.2f} \u00b7 <b>{r_txt}</b>",
    ]
    if trade.get("note"):
        lines.append(f"<i>{html.escape(trade['note'])}</i>")
    return "\n".join(lines)


def format_record(stats: dict, recent: list) -> str:
    lines = [
        "\U0001f4d2 <b>Signal track record</b>",
        DIV,
        f"Decided trades: {stats['decided']}  \u00b7  {stats['wins']}W / {stats['losses']}L",
        f"Win rate: {stats['win_rate']:.1f}%" if stats["decided"] else "Win rate: n/a (no decided trades yet)",
        f"Expired (no fill of either level): {stats['expired']}",
        f"Total: {stats['total_r']:+.2f}R  \u00b7  Expectancy: {stats['expectancy']:+.2f}R/trade",
        f"Open right now: {stats['open']}",
        f"Current losing streak: {stats['loss_streak']}",
        f"Today: {stats['today_r']:+.2f}R",
    ]
    if stats["paused"]:
        lines.append(f"\u23f8\ufe0f <b>Auto-drops paused</b> \u2014 {html.escape(stats['paused_reason'])}")
    if recent:
        lines.append(DIV)
        lines.append("<b>Recent</b>")
        for t in recent:
            mark = {"WIN": "\u2705", "LOSS": "\u274c", "EXPIRED": "\u23f1\ufe0f"}.get(t["status"], "\u2022")
            rm = t.get("r_multiple")
            lines.append(
                f"{mark} {t['horizon']} {t['direction']} {t['entry']:.2f} \u00b7 "
                f"{(f'{rm:+.2f}R' if rm is not None else '\u2014')}"
            )
    if stats["decided"] < 20:
        lines.append(DIV)
        lines.append(
            "<i>Note: fewer than 20 decided trades is far too small a sample to judge an edge. "
            "Treat this record as incomplete, not as evidence either way.</i>"
        )
    return "\n".join(lines)
