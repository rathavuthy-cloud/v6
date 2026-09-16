"""
test_offline.py
================
Runs the whole pipeline against synthetic candles — no network, no Telegram
token. Use this before deploying, and after any change to the scoring or
sizing logic.

    python test_offline.py
"""

import os
import sys
import tempfile

os.environ.setdefault("SIGNAL_STATE_PATH", os.path.join(tempfile.gettempdir(), "test_signal_state.json"))
if os.path.exists(os.environ["SIGNAL_STATE_PATH"]):
    os.remove(os.environ["SIGNAL_STATE_PATH"])

import numpy as np
import pandas as pd

import market_data as md

FREQ = {"M1": "min", "M5": "5min", "M15": "15min", "H1": "h", "H4": "4h", "D1": "D"}
BAR_MIN = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}

# Per-bar volatility must scale with sqrt(time), like a real price series.
# Calibrated so ATR lands near real XAUUSD levels: M5 ~ $1.5, H1 ~ $6, H4 ~ $12.
VOL_PER_ROOT_MINUTE = 0.75


def make_df(tf, drift_per_hour, seed=0, n=300, start=4300.0):
    """Synthetic OHLC whose volatility scales realistically with timeframe."""
    rng = np.random.default_rng(seed)
    minutes = BAR_MIN[tf]
    sigma = VOL_PER_ROOT_MINUTE * np.sqrt(minutes)
    drift = drift_per_hour * (minutes / 60.0)

    idx = pd.date_range("2026-09-10", periods=n, freq=FREQ[tf], tz="UTC")
    close = start + np.cumsum(rng.normal(drift, sigma, n))
    wick = sigma * 0.6
    high = close + rng.uniform(0.1 * wick, wick, n)
    low = close - rng.uniform(0.1 * wick, wick, n)
    open_ = close + rng.normal(0, sigma * 0.4, n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)


def make_trend_df(tf, direction=1, seed=0, n=300, start=4300.0, total_move_pct=0.06):
    """
    A trending series with realistic pullbacks.

    Two things this gets right that a naive fixture doesn't:
      * Drift is scaled so every timeframe moves the same total % across its
        own visible window. Using a fixed per-hour drift instead sends D1 from
        $4,300 to $10,797 over 300 bars, which tests nothing real.
      * Noise stays high enough to produce actual pullbacks. A near-monotonic
        rise has no local minima, so the fractal swing detector finds no swing
        lows and structure comes back "None" on every timeframe — an artifact
        of the fixture, not of the detector.
    """
    rng = np.random.default_rng(seed)
    minutes = BAR_MIN[tf]
    sigma = VOL_PER_ROOT_MINUTE * np.sqrt(minutes) * 0.5
    drift = direction * (start * total_move_pct) / n

    idx = pd.date_range("2026-09-10", periods=n, freq=FREQ[tf], tz="UTC")
    close = start + np.cumsum(rng.normal(drift, sigma, n))
    wick = sigma * 0.6
    high = close + rng.uniform(0.1 * wick, wick, n)
    low = close - rng.uniform(0.1 * wick, wick, n)
    open_ = close + rng.normal(0, sigma * 0.4, n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)


def install_trend_mocks(direction=1):
    md.get_ohlc = lambda tf, ticker=None: make_trend_df(tf, direction, seed=TF_SEED[tf])
    last = float(make_trend_df("M1", direction, seed=TF_SEED["M1"])["Close"].iloc[-1])
    md.get_last_price = lambda ticker=None: last
    md.get_macro = lambda: {"dxyPrice": 101.0, "dxyChangePct": -0.35 * direction, "y10": 4.05,
                            "y10ChangeBps": -2.5 * direction, "vix": 14.0}
    md.get_next_high_impact_event = lambda: {
        "eventName": None, "eventImpact": "None", "eventType": "Standard",
        "minutesToEvent": None, "eventAffectsUsdGold": True, "calendarConfigured": False,
    }
    return last


TF_SEED = {"M1": 11, "M5": 22, "M15": 33, "H1": 44, "H4": 55, "D1": 66}


def install_mocks(drift=0.45, seed_offset=0):
    # Deterministic seeds: Python randomizes str hashing per process, so
    # hash(tf) would give a different dataset every run and the suite would
    # pass or fail depending on luck.
    md.get_ohlc = lambda tf, ticker=None: make_df(tf, drift, seed=TF_SEED[tf] + seed_offset)
    md.get_last_price = lambda ticker=None: 4323.01
    md.get_macro = lambda: {"dxyPrice": 101.0, "dxyChangePct": -0.35, "y10": 4.05,
                            "y10ChangeBps": -2.5, "vix": 14.0}
    md.get_next_high_impact_event = lambda: {
        "eventName": None, "eventImpact": "None", "eventType": "Standard",
        "minutesToEvent": None, "eventAffectsUsdGold": True, "calendarConfigured": False,
    }


def main():
    import formatting
    import live_signal
    import tracker

    failures = []

    for label, drift in (("UPTREND", 0.45), ("DOWNTREND", -0.45), ("CHOPPY", 0.0)):
        install_mocks(drift)
        out = live_signal.scan(settings={"equity": 10000, "risk": 1.0})
        print("=" * 62)
        print(f"{label}  (price 4323.01)")
        print("=" * 62)
        for r in out["results"]:
            status = r["status"]
            line = f"  {r['horizon']:<4} {status:<18} score={r['final_score']:+7.1f} {r['criteria_met']}/7"
            if status == "SIGNAL":
                line += (f"  {r['action']} SL={r['stop_loss']:.2f} "
                         f"TP1={r['tp1']:.2f} risk={r['stop_distance_usd']:.2f}")
            print(line)

            # invariant checks
            if status == "SIGNAL":
                sd = r["stop_distance_usd"]
                if sd is None or sd <= 0:
                    failures.append(f"{label}/{r['horizon']}: non-positive stop distance")
                d1 = abs(r["tp1"] - r["current_price"])
                if abs(d1 / sd - r["tp1_r"]) > 0.01:
                    failures.append(f"{label}/{r['horizon']}: TP1 R mismatch")
                if r["action"] == "BUY" and not (r["stop_loss"] < r["current_price"] < r["tp1"]):
                    failures.append(f"{label}/{r['horizon']}: BUY levels out of order")
                if r["action"] == "SELL" and not (r["tp1"] < r["current_price"] < r["stop_loss"]):
                    failures.append(f"{label}/{r['horizon']}: SELL levels out of order")
        print()

    # Stops must widen with horizon (a 5m stop can't exceed a 4h stop).
    install_mocks(0.45)
    out = live_signal.scan(settings={"equity": 10000, "risk": 1.0})
    stops = {r["horizon"]: r.get("prospective_stop_distance")
             for r in out["results"] if r.get("prospective_stop_distance")}
    print("Stop distance by horizon:", {k: round(v, 2) for k, v in stops.items()})
    order = [h for h in ("M5", "M15", "H1", "H4") if h in stops]
    if len(order) < 4:
        failures.append(f"expected a stop distance for all 4 horizons, got {order}")
    for a, b in zip(order, order[1:]):
        if stops[a] > stops[b]:
            failures.append(f"stop for {a} ({stops[a]:.2f}) wider than {b} ({stops[b]:.2f})")

    # A 5-minute stop should be a few dollars on gold, not tens of dollars.
    if "M5" in stops and stops["M5"] > 15:
        failures.append(f"M5 stop implausibly wide for a 5-minute trade: {stops['M5']:.2f}")
    print()

    # --- clean trend must produce signals, and exercise the full card path ---
    price = install_trend_mocks(direction=1)
    out = live_signal.scan(settings={"equity": 10000, "risk": 1.0})
    signals = [r for r in out["results"] if r["status"] == "SIGNAL"]
    print("=" * 62)
    print(f"CLEAN UPTREND (price {price:.2f}) \u2014 signals on "
          f"{len(signals)}/{len(out['results'])} horizons")
    print("=" * 62)
    for r in out["results"]:
        print(f"  {r['horizon']:<4} {r['status']:<12} score={r['final_score']:+7.1f} "
              f"{r['criteria_met']}/7  {r['reason'][:52]}")
    if not signals:
        failures.append("clean trending fixture produced no signal on any horizon")
    for r in signals:
        if r["action"] != "BUY":
            failures.append(f"{r['horizon']}: SELL signal in a clean uptrend")

    # Structure detection must actually fire on trending data. If it never
    # does, criteria 1 and 7 are dead weight, every signal is capped at 5/7,
    # and the ranging gate (which needs 6/7) would block every ranging signal
    # forever — a silent failure that still "passes" every other check.
    structured = [r for r in out["results"] if r.get("structure_direction") != "None"]
    print(f"  structure detected on {len(structured)}/{len(out['results'])} horizons")
    if not structured:
        failures.append("swing-structure detection found nothing on any horizon")
    best = max(r["criteria_met"] for r in out["results"])
    print(f"  best confluence reached: {best}/7")
    if best < 5:
        failures.append(f"no horizon exceeded {best}/7 on clean trending data")
    print()

    # Mirror it: a clean downtrend must produce SELLs, never BUYs.
    install_trend_mocks(direction=-1)
    down = live_signal.scan(settings={"equity": 10000, "risk": 1.0})
    down_sigs = [r for r in down["results"] if r["status"] == "SIGNAL"]
    print(f"CLEAN DOWNTREND \u2014 signals on {len(down_sigs)}/{len(down['results'])} horizons "
          f"({', '.join(r['action'] for r in down_sigs) or 'none'})")
    for r in down_sigs:
        if r["action"] != "SELL":
            failures.append(f"{r['horizon']}: BUY signal in a clean downtrend")
    print()

    if signals:
        install_trend_mocks(direction=1)
        out = live_signal.scan(settings={"equity": 10000, "risk": 1.0})
        r = [x for x in out["results"] if x["status"] == "SIGNAL"][0]
        tracker.record_signal(r, r["horizon"], lots=r.get("lots"))
        card = formatting.format_signal_card(r, tracker.stats(), r.get("sizing"), out["settings"])
        print("=" * 62)
        print("SAMPLE CARD")
        print("=" * 62)
        print(card)
        print()

        # Same setup, $10 account -> must refuse to size.
        small = live_signal.scan(settings={"equity": 10.0, "risk": 1.0})
        sm = [x for x in small["results"] if x["status"] == "SIGNAL"]
        if sm:
            s = sm[0]["sizing"]
            print("$10 account sizing allowed?", s.ok)
            print(" ", s.reason)
            if s.ok:
                failures.append("$10 account was allowed to size a gold trade")
        print()

    print("=" * 62)
    if failures:
        print("FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL INVARIANTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
