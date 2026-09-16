"""
XAU/USD Multi-Horizon Signal Bot — Telegram
============================================
Scans live gold data every 5 minutes and drops a signal card for any of
four horizons that produce a setup: next 5 min, 15 min, 1 hour, 4 hours.
Each card carries entry, stop, TP1/TP2, R:R, entry zone, expiry, session
context, position size, and the bot's running win/loss record.

Every signal it drops is logged and later resolved against real price
action, so the track record on each card is the bot's actual performance,
not a marketing number. When the record turns bad — a losing streak, a
daily loss limit, too many open ideas — the circuit breaker pauses the
auto-drops instead of continuing to fire into a losing run.

SETUP
-----
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN="123456:ABC-your-token"

    # optional
    export FINNHUB_API_KEY="..."              # enables news blackout
    export SIGNAL_STATE_PATH="/data/state.json"  # persist record across restarts
    export DEFAULT_ACCOUNT_EQUITY="10000"
    export DEFAULT_RISK_PCT="1"
    export GOLD_TICKER="GC=F"

    python xauusd_live_bot.py
"""

import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import formatting
import live_signal
import tracker
from horizons import HORIZON_ORDER, HORIZONS, parse_horizons
from market_data import DataFetchError

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
log = logging.getLogger("xauusd-live-bot")

SCAN_MINUTES = int(os.environ.get("SCAN_INTERVAL_MINUTES", "5"))
MIN_SCAN_MINUTES = 5  # below this you're just hammering a free data endpoint

SETTINGS_KEYS = {
    "equity": float, "risk": float, "contractsize": float, "lotstep": float,
    "minlot": float, "maxlot": float, "kfactor": float, "tp1r": float,
    "tp2r": float, "lossstreak": int, "dailyloss": float, "maxopen": int,
}


def _default_settings_from_env() -> dict:
    out = {}
    for key, env in (("equity", "DEFAULT_ACCOUNT_EQUITY"), ("risk", "DEFAULT_RISK_PCT"),
                     ("contractsize", "DEFAULT_CONTRACT_SIZE")):
        val = os.environ.get(env)
        if val:
            try:
                out[key] = float(val)
            except ValueError:
                pass
    return out


def _get_settings(context) -> dict:
    if "settings" not in context.user_data:
        context.user_data["settings"] = _default_settings_from_env()
    return context.user_data["settings"]


def horizon_keyboard():
    rows = [[InlineKeyboardButton(HORIZONS[k]["label"], callback_data=f"h:{k}")] for k in HORIZON_ORDER]
    rows.append([InlineKeyboardButton("\U0001f50e Scan all horizons", callback_data="h:all")])
    return InlineKeyboardMarkup(rows)


async def _send(chat, text):
    await chat.send_message(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = tracker.stats()
    await update.message.reply_text(
        "\U0001f3c6 <b>XAUUSD Multi-Horizon Signal Bot</b>\n\n"
        "I scan live gold data and drop signals across four horizons "
        "(5m / 15m / 1h / 4h) with entry, stop, TP1, TP2 and position size.\n\n"
        f"Current record: <b>{s['wins']}W/{s['losses']}L</b> \u00b7 {s['open']} open\n\n"
        "Pick a horizon for an instant read, or <code>/auto</code> to start "
        "5-minute auto-drops.\n\n"
        "<i>Set your account first: <code>/settings equity=10000 risk=1</code> \u2014 "
        "without it I can't size anything. Not financial advice.</i>",
        parse_mode="HTML",
        reply_markup=horizon_keyboard(),
    )


HELP_TEXT = (
    "<b>Signals</b>\n"
    "/scan [5m|15m|1h|4h|all] \u2014 run a live scan now\n"
    "/signal &lt;horizon&gt; \u2014 full card for one horizon\n"
    "/auto [minutes] [horizons] \u2014 start auto-drops (default 5 min, all horizons)\n"
    "/stopauto \u2014 stop auto-drops\n\n"
    "<b>Record &amp; risk</b>\n"
    "/record \u2014 win/loss, expectancy, recent trades\n"
    "/positions \u2014 currently open signals\n"
    "/pause, /resume \u2014 halt or restart auto-drops\n"
    "/resetrecord \u2014 wipe the track record\n\n"
    "<b>Setup</b>\n"
    "/settings \u2014 view or change settings\n"
    "   <code>/settings equity=10000 risk=1 tp1r=2 tp2r=3</code>\n"
    "/contractsize &lt;oz&gt; \u2014 contract size per lot (100 standard, 10 micro)\n\n"
    "<i>Rules-based technical analysis. It reports, never executes. "
    "Not financial advice.</i>"
)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")


async def _do_scan(chat, context, horizon_keys, detailed: bool):
    settings = _get_settings(context)
    status = await chat.send_message("\U0001f50e Pulling live data\u2026")
    try:
        out = await asyncio.to_thread(live_signal.scan, horizon_keys, settings)
    except DataFetchError as exc:
        await status.edit_text(f"\u26a0\ufe0f Data fetch failed: {html.escape(str(exc))}")
        return None
    except Exception as exc:  # noqa: BLE001
        log.exception("Scan failed")
        await status.edit_text(f"\u26a0\ufe0f Unexpected error: {html.escape(str(exc))}")
        return None

    await status.delete()

    # Resolve anything that closed since the last look.
    closed = await asyncio.to_thread(live_signal.resolve_outcomes, out["snapshot"])
    for t in closed:
        await _send(chat, formatting.format_outcome(t))

    stats = tracker.stats()
    results = out["results"]

    if not detailed and len(results) > 1:
        await _send(chat, formatting.format_scan_digest(results, stats, out["snapshot"]["price"]))
        signals = [r for r in results if r["status"] == "SIGNAL"]
        for r in signals:
            await _send(chat, formatting.format_signal_card(r, stats, r.get("sizing"), out["settings"]))
    else:
        for r in results:
            if r["status"] == "SIGNAL":
                await _send(chat, formatting.format_signal_card(r, stats, r.get("sizing"), out["settings"]))
            else:
                await _send(chat, formatting.format_no_signal(r, stats))

    errors = out["snapshot"].get("errors")
    if errors:
        await _send(chat, "\u26a0\ufe0f Some data was unavailable: " + html.escape("; ".join(errors[:3])))
    return out


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keys = parse_horizons(context.args)
    await _do_scan(update.effective_chat, context, keys, detailed=False)


async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keys = parse_horizons(context.args)
    await _do_scan(update.effective_chat, context, keys[:1], detailed=True)


async def on_horizon_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, key = q.data.split(":", 1)
    if key == "all":
        await _do_scan(q.message.chat, context, list(HORIZON_ORDER), detailed=False)
    else:
        await _do_scan(q.message.chat, context, [key], detailed=True)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = live_signal.settings_with_defaults(_get_settings(context))
    if not context.args:
        lines = ["<b>Settings</b>"]
        for k in SETTINGS_KEYS:
            v = settings.get(k)
            lines.append(f"\u2022 <code>{k}</code> = {v if v is not None else '<i>not set</i>'}")
        lines.append(
            "\nChange with e.g. <code>/settings equity=10000 risk=1 tp1r=2</code>\n"
            "<i>contractsize is oz per lot (100 standard, 10 micro). "
            "lossstreak / dailyloss / maxopen drive the circuit breaker.</i>"
        )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    store = _get_settings(context)
    updated, errors = [], []
    for token in context.args:
        key, _, raw = token.partition("=")
        key = key.strip().lower()
        if not raw or key not in SETTINGS_KEYS:
            errors.append(token)
            continue
        try:
            val = SETTINGS_KEYS[key](raw)
        except ValueError:
            errors.append(token)
            continue
        if key in ("equity", "risk", "contractsize", "minlot", "lotstep") and val <= 0:
            errors.append(f"{token} (must be > 0)")
            continue
        if key == "risk" and val > 10:
            errors.append(f"{token} (risking >10% per trade is not something I'll set for you)")
            continue
        store[key] = val
        updated.append(f"{key}={val:g}")

    msg = []
    if updated:
        msg.append("\u2705 Updated: " + ", ".join(updated))
    if errors:
        msg.append("\u26a0\ufe0f Couldn't set: " + ", ".join(errors))
        msg.append("Valid keys: " + ", ".join(SETTINGS_KEYS))
    await update.message.reply_text("\n".join(msg) or "Nothing changed.")


async def contractsize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        cur = live_signal.settings_with_defaults(_get_settings(context))["contractsize"]
        await update.message.reply_text(
            f"Contract size is <b>{cur:g} oz</b> per 1.00 lot.\n"
            "Standard XAUUSD is 100 oz; micro accounts are often 10 oz.\n"
            "Set with <code>/contractsize 10</code>.",
            parse_mode="HTML",
        )
        return
    try:
        val = float(context.args[0])
        if val <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Give a positive number, e.g. /contractsize 10")
        return
    _get_settings(context)["contractsize"] = val
    await update.message.reply_text(f"\u2705 Contract size set to {val:g} oz per lot.")


async def record_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        formatting.format_record(tracker.stats(), tracker.recent_closed(8)),
        parse_mode="HTML",
    )


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    open_t = tracker.open_trades()
    if not open_t:
        await update.message.reply_text("No open signals right now.")
        return
    lines = ["\U0001f4cb <b>Open signals</b>"]
    for t in open_t:
        expires = datetime.fromisoformat(t["expires_at"])
        left = (expires - datetime.now(timezone.utc)).total_seconds() / 60
        lines.append(
            f"\u2022 {t['horizon']} {t['direction']} @ {t['entry']:.2f} \u00b7 "
            f"SL {t['stop']:.2f} \u00b7 TP1 {t['tp1']:.2f} \u00b7 "
            f"{'expired' if left < 0 else f'{left:.0f}m left'}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker.set_paused(True, "Auto-signals manually paused.")
    await update.message.reply_text("\u23f8\ufe0f Auto-drops paused. /resume to restart.")


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker.set_paused(False, "")
    s = tracker.stats()
    note = ""
    if s["loss_streak"] >= 3:
        note = (f"\n\u26a0\ufe0f Note: you're still {s['loss_streak']} losses deep. "
                "The breaker will trip again on the next loss.")
    await update.message.reply_text("\u25b6\ufe0f Auto-drops resumed." + note)


async def resetrecord_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = tracker.reset_record()
    await update.message.reply_text(
        f"\U0001f5d1\ufe0f Cleared {n} logged trades. The record starts from zero \u2014 "
        "note that wiping a losing record doesn't change whether the strategy works."
    )


# ---------------------------------------------------------------------------
# Auto-scan
# ---------------------------------------------------------------------------

async def _auto_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id
    keys = job.data["horizons"]
    settings = job.data["settings"]
    bot = context.bot

    try:
        out = await asyncio.to_thread(live_signal.scan, keys, settings)
    except DataFetchError as exc:
        job.data["fail_count"] = job.data.get("fail_count", 0) + 1
        if job.data["fail_count"] in (3, 12):
            await bot.send_message(
                chat_id,
                f"\u26a0\ufe0f Data fetch has failed {job.data['fail_count']} times in a row "
                f"({html.escape(str(exc))}). Yahoo often rate-limits cloud IPs \u2014 "
                "scanning continues, but signals are paused until data returns.",
            )
        return
    except Exception:  # noqa: BLE001
        log.exception("Auto-scan error")
        return

    job.data["fail_count"] = 0

    # 1. Resolve and report anything that closed.
    closed = await asyncio.to_thread(live_signal.resolve_outcomes, out["snapshot"])
    for t in closed:
        try:
            await bot.send_message(chat_id, formatting.format_outcome(t), parse_mode="HTML")
        except Exception:  # noqa: BLE001
            log.warning("Could not send outcome message")

    stats = tracker.stats()

    # 2. Circuit breaker.
    blocked, reason = tracker.check_circuit_breaker(settings)
    if blocked and not job.data.get("breaker_notified"):
        await bot.send_message(
            chat_id,
            f"\u26a0\ufe0f <b>Auto-drops paused</b>\n{html.escape(reason)}\n"
            "Use /record to review, /resume when you've decided to continue.",
            parse_mode="HTML",
        )
        job.data["breaker_notified"] = True
    if not blocked:
        job.data["breaker_notified"] = False

    # 3. Drop new signals.
    now = datetime.now(timezone.utc)
    last_drop = job.data.setdefault("last_drop", {})

    for r in out["results"]:
        if r["status"] != "SIGNAL":
            continue
        h = r["horizon"]

        # Dedupe: don't repost the same idea while it's still live.
        prev = last_drop.get(h)
        if prev and prev["direction"] == r["action"]:
            age = (now - datetime.fromisoformat(prev["at"])).total_seconds() / 60
            if age < HORIZONS[h]["expiry_min"]:
                continue
        if tracker.has_live_signal(h, r["action"]):
            continue

        if blocked:
            # Still show the read, clearly marked, but don't log it as a
            # tracked trade — a paused bot isn't taking the signal.
            card = formatting.format_signal_card(
                r, stats, r.get("sizing"), settings, paused_reason=reason
            )
            await bot.send_message(chat_id, card, parse_mode="HTML")
            last_drop[h] = {"direction": r["action"], "at": now.isoformat()}
            continue

        lots = r["sizing"].lots if (r.get("sizing") and r["sizing"].ok) else None
        tracker.record_signal(r, h, lots=lots, chat_id=chat_id)
        stats = tracker.stats()
        card = formatting.format_signal_card(r, stats, r.get("sizing"), settings)
        await bot.send_message(chat_id, card, parse_mode="HTML")
        last_drop[h] = {"direction": r["action"], "at": now.isoformat()}


async def auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.job_queue is None:
        await update.message.reply_text(
            "\u26a0\ufe0f Job queue unavailable. Install with:\n"
            '<code>pip install "python-telegram-bot[job-queue]"</code>',
            parse_mode="HTML",
        )
        return

    args = list(context.args or [])
    minutes = SCAN_MINUTES
    if args and args[0].isdigit():
        minutes = max(int(args.pop(0)), MIN_SCAN_MINUTES)
    keys = parse_horizons(args)

    chat_id = update.effective_chat.id
    name = f"auto_{chat_id}"
    for j in context.job_queue.get_jobs_by_name(name):
        j.schedule_removal()

    context.job_queue.run_repeating(
        _auto_job,
        interval=minutes * 60,
        first=3,
        chat_id=chat_id,
        name=name,
        data={
            "horizons": keys,
            "settings": live_signal.settings_with_defaults(_get_settings(context)),
            "last_drop": {},
            "fail_count": 0,
            "breaker_notified": False,
        },
    )

    labels = ", ".join(HORIZONS[k]["short"] for k in keys)
    settings = live_signal.settings_with_defaults(_get_settings(context))
    warn = ""
    if not settings.get("equity"):
        warn = "\n\u26a0\ufe0f No account equity set \u2014 cards will show levels but no position size."
    await update.message.reply_text(
        f"\u2705 Auto-scanning every <b>{minutes} min</b> across <b>{labels}</b>.\n"
        f"I'll drop a card when a setup appears and report the outcome when it closes.\n"
        f"Breaker: pause after {settings['lossstreak']:g} straight losses, "
        f"-{settings['dailyloss']:g}R on the day, or {settings['maxopen']:g} open.\n"
        f"/stopauto to stop." + warn,
        parse_mode="HTML",
    )


async def stopauto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.job_queue is None:
        await update.message.reply_text("No scheduler running.")
        return
    jobs = context.job_queue.get_jobs_by_name(f"auto_{update.effective_chat.id}")
    if not jobs:
        await update.message.reply_text("No auto-scan running in this chat.")
        return
    for j in jobs:
        j.schedule_removal()
    await update.message.reply_text("\U0001f6d1 Auto-scan stopped.")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first.")

    app = ApplicationBuilder().token(token).build()
    for cmd, fn in (
        ("start", start), ("help", help_cmd), ("scan", scan_cmd), ("signal", signal_cmd),
        ("auto", auto_cmd), ("stopauto", stopauto_cmd), ("settings", settings_cmd),
        ("contractsize", contractsize_cmd), ("record", record_cmd),
        ("positions", positions_cmd), ("pause", pause_cmd), ("resume", resume_cmd),
        ("resetrecord", resetrecord_cmd),
    ):
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(on_horizon_chosen, pattern=r"^h:"))

    log.info("Multi-horizon bot starting (scan every %s min)\u2026", SCAN_MINUTES)
    app.run_polling()


if __name__ == "__main__":
    main()
