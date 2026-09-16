# XAUUSD Multi-Horizon Signal Bot

Scans live gold data every 5 minutes and drops a signal card for any of four
horizons — **next 5 min, 15 min, 1 hour, 4 hours** — with entry, stop, TP1,
TP2, R:R, entry zone, expiry, session context and position size.

Every signal it drops is logged and later resolved against real price action,
so the win/loss record on each card is the bot's actual performance. When that
record turns bad, the circuit breaker pauses auto-drops instead of firing into
a losing run.

```
🏆 XAUUSD SIGNAL
Scanner confluence: 6/7 factors aligned · High confidence
──────────────────
Signal: BUY
Entry: 4560.43
Stop Loss: 4556.91
Take Profit 1: 4567.47
Take Profit 2: 4570.99
Risk:Reward: 1:2.0
Timeframe: ⚡ Next 5 min
──────────────────
📍 Entry 4560.43   🛑 Stop 4556.91 (3.52 risk)
🎯 TP1 4567.47 (+7.04)   🎯 TP2 4570.99 (+10.56)
🟢 ⏰ BUY on retest of 4560.43 (zone 4560.32–4560.53)
Valid ~5 min · 🇬🇧 London — deep liquidity
📈 Track record: 4W/3L (57%) · 2 open
📊 Expectancy: +0.31R/trade over 7 closed
💰 Size: 0.28 lot · risking $98.55 (0.99% of account)
⚠️ Not financial advice.
```

## Setup

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="123456:ABC-your-token"

# optional
export FINNHUB_API_KEY="..."                  # enables news blackout
export SIGNAL_STATE_PATH="/data/state.json"   # persist record across restarts
export DEFAULT_ACCOUNT_EQUITY="10000"
export DEFAULT_RISK_PCT="1"
export DEFAULT_CONTRACT_SIZE="100"            # 10 for micro gold
export GOLD_TICKER="GC=F"
export SCAN_INTERVAL_MINUTES="5"

python xauusd_live_bot.py
```

On Railway, set those in project settings; the `Procfile` worker picks it up.
**Mount a volume and point `SIGNAL_STATE_PATH` at it** — otherwise the track
record resets on every redeploy, which defeats the purpose.

## Commands

| Command | Does |
|---|---|
| `/auto [minutes] [horizons]` | Start auto-drops. Default 5 min, all horizons. `/auto 5 1h 4h` |
| `/stopauto` | Stop auto-drops |
| `/scan [5m\|15m\|1h\|4h\|all]` | Scan now — digest plus a card per signal |
| `/signal <horizon>` | Full card for one horizon |
| `/record` | Win/loss, expectancy, recent trades |
| `/positions` | Currently open signals with time left |
| `/pause`, `/resume` | Halt/restart auto-drops |
| `/resetrecord` | Wipe the track record |
| `/settings` | `equity`, `risk`, `tp1r`, `tp2r`, `lossstreak`, `dailyloss`, `maxopen`, … |
| `/contractsize <oz>` | Oz per lot — 100 standard, 10 micro |

## How a signal is built

Per horizon, each timeframe in its set produces three votes:

- **Trend** (±8): EMA20 vs EMA50, price vs EMA20, MACD vs signal, EMA50 slope
- **Momentum** (±20): RSI(14) from 50, stochastic %K−%D, MACD histogram / ATR
- **Volatility** (±2): ATR(14) vs its 20-period average, signed by trend

| Horizon | Timeframes | Stop | Expiry |
|---|---|---|---|
| 5 min | M1, M5, M15 | 1.2 × ATR(M5) | 5 min |
| 15 min | M5, M15, H1 | 1.3 × ATR(M15) | 15 min |
| 1 hour | M15, H1, H4 | 1.5 × ATR(H1) | 60 min |
| 4 hours | H1, H4, D1 | 1.8 × ATR(H4) | 240 min |

Votes blend by timeframe weight, then pass seven confluence checks (structure
agreement, multi-timeframe alignment, macro non-conflict, news clear,
kill-zone timing, R:R ≥ 1:1.5, invalidation defined). A signal needs ≥4/7 and
a score outside ±20 — **or ≥6/7 in a ranging regime** (ADX < 20), where this
kind of model fires readily on noise.

## Bugs found while building this

Documented because they were all silent — the bot produced confident,
well-formatted, wrong output rather than crashing:

1. **A 5-minute signal carried an $89.99 stop.** Every horizon used
   `1.5 × ATR(H1)`. Each now sizes from its own ATR: M5 ≈ $3, H4 ≈ $40.
2. **Stops of $602 and $3,263 on higher horizons.**
   `max(atr_buffer, distance_to_invalidation)` had no cap, so a distant swing
   point ballooned the stop while R:R still looked fine. Capped at 3× ATR.
3. **The 5-minute horizon could never fire.** The data gate demanded H1 votes
   and the alignment gate hardcoded H1/H4 — neither of which that horizon
   reads. Both are horizon-aware now.
4. **Confluence was permanently capped at 4/7.** I'd conflated "stop too wide
   to trade" with "structure invalid". In any healthy trend the swing low sits
   3–6 ATR back, so the structure criteria always failed — and because the
   ranging gate needs 6/7, that would have blocked every ranging signal
   forever. The stop is capped; the structure read is left alone.
5. **A Yahoo outage killed the bot silently.** Per-timeframe errors were
   caught and logged but never propagated, so the scan returned
   "insufficient data" on every horizon indefinitely while the user assumed
   there were simply no setups. Total fetch failure now raises and is reported.
6. **Missing account equity blocked signals entirely.** Now it costs you the
   position size, not the whole signal.

## Risk controls

**Sizing** uses a real contract spec, not an assumed pip value:

```
risk per lot = contract_size × stop_distance
lots         = (equity × risk%) / risk_per_lot     → floored to lot step
```

If the *smallest tradeable size* still exceeds your risk budget, it refuses
rather than rounding up:

> Too small to size safely at $10.00 balance / 1.0% risk and a 18.56 stop —
> even the smallest tradeable size (0.01 lot) would risk $18.56 (185.6% of
> your account).

**Circuit breaker** (adapted from `drawdown-circuit-breaker` in
tradermonty/claude-trading-skills): pauses after 3 straight losses, −3R on the
day, or 3 open trades. While paused it still shows the read, clearly marked,
but doesn't log it as a trade.

**Outcome tracking** resolves against M1 candle highs/lows, not a spot-price
poll — a poll misses a stop that was hit and reversed between ticks, which
flatters the record. Two deliberately conservative choices so the record errs
against the bot:

- A bar containing **both** stop and target books as a **loss**. Without tick
  data there's no way to know which came first, and assuming the win is how
  backtests lie.
- Fills are assumed at signal price. Real slippage runs against you, so live
  results should come in slightly worse than recorded.

## Testing

```bash
python test_offline.py                              # pipeline, no network
PYTHONPATH="_faketg:." python test_handlers.py      # 33 command tests
```

`test_offline.py` checks level ordering, R:R arithmetic, stop scaling across
horizons, direction correctness, structure detection, and the sizing refusal.
`test_handlers.py` covers every command, the auto-job drop/dedupe cycle, the
circuit breaker, and data-failure handling.

## Read this before risking money

- **This is a rules-based heuristic, not a proven edge.** The weights are a
  documented design choice — not backtested, not optimized. Every signal is a
  hypothesis to verify, not an instruction. Not financial advice.
- **The 5-minute horizon is where spread and slippage hurt most.** Gold's
  spread can be several times wider in thin sessions, and a 5m signal's whole
  target may be smaller than a bad fill. The 1h and 4h horizons have a far
  better ratio of signal to cost.
- **`yfinance` scrapes an unofficial Yahoo endpoint.** No SLA, and cloud IPs
  get rate-limited. For production, put a paid provider behind
  `market_data.py`'s interface.
- **The printed price is not a tradable quote.** It's the last trade on gold
  futures (`GC=F`), which tracks spot closely but isn't identical to your
  broker's feed.
- **News gating is off without `FINNHUB_API_KEY`** — the bot runs news-blind
  and will happily signal into an NFP release.
- **Fewer than ~20 decided trades tells you nothing.** Let `/record` build a
  real sample on paper before committing money. The tracker exists so that's
  a question with an answer.
