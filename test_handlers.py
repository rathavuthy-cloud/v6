"""
test_handlers.py
=================
Exercises every Telegram command handler without a bot token or network,
using the stub `telegram` package in _faketg/ and mocked market data.

    PYTHONPATH="_faketg:." python test_handlers.py
"""

import asyncio
import os
import sys
import tempfile

os.environ["SIGNAL_STATE_PATH"] = os.path.join(tempfile.gettempdir(), "handler_test_state.json")
if os.path.exists(os.environ["SIGNAL_STATE_PATH"]):
    os.remove(os.environ["SIGNAL_STATE_PATH"])

import market_data as md
import test_offline as fixtures

fixtures.install_trend_mocks(direction=1)

import formatting  # noqa: E402
import live_signal  # noqa: E402
import tracker  # noqa: E402
import xauusd_live_bot as bot  # noqa: E402
from telegram.ext import JobQueue  # noqa: E402

FAILS = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILS.append(label)


class FakeMessage:
    def __init__(self, chat=None):
        self.sent, self.edited, self.deleted = [], [], False
        self.chat = chat

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        self.sent.append(text)
        return self

    async def edit_text(self, text, parse_mode=None):
        self.edited.append(text)
        return self

    async def delete(self):
        self.deleted = True


class FakeChat:
    def __init__(self, chat_id=4242):
        self.id = chat_id
        self.sent = []

    async def send_message(self, text, parse_mode=None):
        self.sent.append(text)
        return FakeMessage(self)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


class FakeUpdate:
    def __init__(self, chat=None):
        self.effective_chat = chat or FakeChat()
        self.message = FakeMessage(self.effective_chat)


class FakeContext:
    def __init__(self, args=None, job_queue=None):
        self.args = args or []
        self.user_data = {}
        self.job_queue = job_queue
        self.bot = FakeBot()


class FakeCallbackQuery:
    def __init__(self, data, chat):
        self.data = data
        self.message = FakeMessage(chat)
        self.answered = False

    async def answer(self):
        self.answered = True


async def main():
    print("\n--- /start, /help ---")
    u, c = FakeUpdate(), FakeContext()
    await bot.start(u, c)
    check(len(u.message.sent) == 1 and "XAUUSD" in u.message.sent[0], "/start replies")

    u, c = FakeUpdate(), FakeContext()
    await bot.help_cmd(u, c)
    check("/auto" in u.message.sent[0], "/help lists commands")

    print("\n--- /settings ---")
    u, c = FakeUpdate(), FakeContext(args=["equity=10000", "risk=1", "tp1r=2"])
    await bot.settings_cmd(u, c)
    check(c.user_data["settings"]["equity"] == 10000, "/settings stores equity")
    check(c.user_data["settings"]["tp1r"] == 2, "/settings stores tp1r")

    u, c = FakeUpdate(), FakeContext(args=["risk=50"])
    await bot.settings_cmd(u, c)
    check("risk" not in c.user_data.get("settings", {}), "/settings refuses 50% risk")

    u, c = FakeUpdate(), FakeContext(args=["equity=-5"])
    await bot.settings_cmd(u, c)
    check("equity" not in c.user_data.get("settings", {}), "/settings refuses negative equity")

    u, c = FakeUpdate(), FakeContext(args=["nonsense", "bogus=1"])
    await bot.settings_cmd(u, c)
    check("Couldn't set" in u.message.sent[0], "/settings reports bad keys")

    u, c = FakeUpdate(), FakeContext()
    await bot.settings_cmd(u, c)
    check("equity" in u.message.sent[0], "/settings with no args shows current")

    print("\n--- /contractsize ---")
    u, c = FakeUpdate(), FakeContext(args=["10"])
    await bot.contractsize_cmd(u, c)
    check(c.user_data["settings"]["contractsize"] == 10, "/contractsize 10 sets micro")

    u, c = FakeUpdate(), FakeContext(args=["-3"])
    await bot.contractsize_cmd(u, c)
    check("positive" in u.message.sent[0], "/contractsize rejects negative")

    print("\n--- /scan and /signal ---")
    u, c = FakeUpdate(), FakeContext(args=["all"])
    c.user_data["settings"] = {"equity": 10000, "risk": 1.0}
    await bot._do_scan(u.effective_chat, c, ["M5", "M15", "H1", "H4"], detailed=False)
    msgs = u.effective_chat.sent
    check(any("multi-horizon scan" in m for m in msgs), "/scan sends a digest")
    check(any("XAUUSD SIGNAL" in m for m in msgs), "/scan sends signal cards")

    u, c = FakeUpdate(), FakeContext(args=["5m"])
    c.user_data["settings"] = {"equity": 10000, "risk": 1.0}
    await bot.signal_cmd(u, c)
    check(len(u.effective_chat.sent) >= 1, "/signal 5m replies")

    print("\n--- horizon button ---")
    chat = FakeChat()
    q = FakeCallbackQuery("h:H1", chat)
    upd = type("U", (), {"callback_query": q})()
    c = FakeContext()
    c.user_data["settings"] = {"equity": 10000, "risk": 1.0}
    await bot.on_horizon_chosen(upd, c)
    check(q.answered and len(chat.sent) >= 1, "horizon button runs a scan")

    print("\n--- /auto lifecycle ---")
    jq = JobQueue()
    u, c = FakeUpdate(), FakeContext(args=["5", "5m", "15m"], job_queue=jq)
    c.user_data["settings"] = {"equity": 10000, "risk": 1.0}
    await bot.auto_cmd(u, c)
    check(len(jq.jobs) == 1, "/auto registers one job")
    check(jq.jobs[0].data["horizons"] == ["M5", "M15"], "/auto parses horizon list")

    u2, c2 = FakeUpdate(), FakeContext(args=["1"], job_queue=jq)
    c2.user_data["settings"] = {"equity": 10000, "risk": 1.0}
    await bot.auto_cmd(u2, c2)
    live = jq.get_jobs_by_name(f"auto_{u2.effective_chat.id}")
    check(len(live) == 1, "/auto replaces the previous job")
    check(live[0].data and "5 min" in u2.message.sent[0], "/auto floors interval at 5 min")

    print("\n--- auto job fires and drops signals ---")
    job = live[0]
    job.data["settings"] = live_signal.settings_with_defaults({"equity": 10000, "risk": 1.0})
    jctx = FakeContext()
    jctx.job = job
    await bot._auto_job(jctx)
    dropped = [t for _, t in jctx.bot.sent if "XAUUSD SIGNAL" in t]
    check(len(dropped) >= 1, "auto job drops at least one card")
    check(len(tracker.open_trades()) >= 1, "dropped signals are logged as open trades")

    print("\n--- dedupe: second identical tick must not repost ---")
    before = len(jctx.bot.sent)
    await bot._auto_job(jctx)
    check(len(jctx.bot.sent) == before, "duplicate signal is suppressed")

    print("\n--- /positions, /record ---")
    u, c = FakeUpdate(), FakeContext()
    await bot.positions_cmd(u, c)
    check("Open signals" in u.message.sent[0], "/positions lists open trades")

    u, c = FakeUpdate(), FakeContext()
    await bot.record_cmd(u, c)
    check("track record" in u.message.sent[0].lower(), "/record renders")
    check("too small a sample" in u.message.sent[0], "/record warns about small sample")

    print("\n--- circuit breaker ---")
    tracker.reset_record()
    for i in range(3):
        t = tracker.record_signal(
            {"symbol": "XAU/USD", "action": "SELL", "current_price": 4300.0,
             "stop_loss": 4310.0, "tp1": 4280.0, "tp2": 4270.0, "expiry_min": 15},
            "M15")
        data = tracker._load()
        for row in data["trades"]:
            if row["id"] == t.id:
                row.update(status="LOSS", r_multiple=-1.0,
                           closed_at=tracker._now().isoformat(), exit_price=4310.0)
        tracker._save(data)

    blocked, reason = tracker.check_circuit_breaker({})
    check(blocked and "losing streak" in reason, "breaker trips after 3 losses")

    jq2 = JobQueue()
    u, c = FakeUpdate(), FakeContext(args=[], job_queue=jq2)
    c.user_data["settings"] = {"equity": 10000, "risk": 1.0}
    await bot.auto_cmd(u, c)
    job2 = jq2.jobs[0]
    job2.data["settings"] = live_signal.settings_with_defaults({"equity": 10000, "risk": 1.0})
    jctx2 = FakeContext()
    jctx2.job = job2
    await bot._auto_job(jctx2)
    texts = [t for _, t in jctx2.bot.sent]
    check(any("paused" in t.lower() for t in texts), "breaker notifies on pause")
    raw = [t for t in texts if "XAUUSD SIGNAL" in t]
    check(all("paused" in t.lower() for t in raw) if raw else True,
          "paused cards carry the pause banner")
    check(len(tracker.open_trades()) == 0, "paused signals are NOT logged as trades")

    print("\n--- /pause, /resume, /resetrecord ---")
    u, c = FakeUpdate(), FakeContext()
    await bot.pause_cmd(u, c)
    check(tracker.stats()["paused"], "/pause sets paused")

    u, c = FakeUpdate(), FakeContext()
    await bot.resume_cmd(u, c)
    check(not tracker.stats()["paused"], "/resume clears paused")
    check("still" in u.message.sent[0], "/resume warns streak is still live")

    u, c = FakeUpdate(), FakeContext()
    await bot.resetrecord_cmd(u, c)
    check(tracker.stats()["closed"] == 0, "/resetrecord clears the record")

    print("\n--- /stopauto ---")
    u, c = FakeUpdate(), FakeContext(job_queue=jq2)
    await bot.stopauto_cmd(u, c)
    check(len(jq2.get_jobs_by_name(f"auto_{u.effective_chat.id}")) == 0, "/stopauto removes the job")

    u, c = FakeUpdate(), FakeContext(job_queue=JobQueue())
    await bot.stopauto_cmd(u, c)
    check("No auto-scan" in u.message.sent[0], "/stopauto handles no job gracefully")

    print("\n--- data failure is handled, not crashed ---")
    def boom(*a, **k):
        raise md.DataFetchError("simulated Yahoo rate limit")
    orig = md.get_ohlc
    md.get_ohlc = boom
    jq3 = JobQueue()
    u, c = FakeUpdate(), FakeContext(args=[], job_queue=jq3)
    await bot.auto_cmd(u, c)
    job3 = jq3.jobs[0]
    job3.data["settings"] = live_signal.settings_with_defaults({"equity": 10000})
    jctx3 = FakeContext()
    jctx3.job = job3
    try:
        for _ in range(3):
            await bot._auto_job(jctx3)
        check(True, "auto job survives repeated fetch failures")
        check(any("failed 3 times" in t for _, t in jctx3.bot.sent),
              "user is warned after repeated failures")
    except Exception as exc:
        check(False, f"auto job crashed on fetch failure: {exc}")
    md.get_ohlc = orig

    print("\n" + "=" * 54)
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S):")
        for f in FAILS:
            print("  -", f)
        return 1
    print("ALL HANDLER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
