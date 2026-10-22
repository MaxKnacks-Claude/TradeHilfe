---
name: trader-entry
description: >-
  Entry Timer for Binance spot. Watches a setup candle by candle and tells you
  the moment to get in. Reads Binance 5-minute or 1-minute candles, builds the
  opening range of your configured UTC session (the first 15 minutes by
  default) and calls the entry on the first completed candle that CLOSES above
  the range high, or above a level you give it (like a 20-day high from the
  scanner), with an optional volume check. Says waiting and how far,
  triggered with the time and the price, or expired after your entry window,
  and writes trader/entry.json for /trader-exit. Long only. It never places an
  order. Use when the user says "tell me when to get in", "watch BTC", "has
  it broken out", "did it break the opening range", "entry signal", "time my
  entry", "check my watchlist", or after /trader-scanner, before /trader-exit.
argument-hint: "[SYMBOL] [--setup orb|level] [--interval 5m|1m]"
---

# trader-entry

The Entry Timer turns a setup into one moment: the candle that confirms it. It
waits for the close, because a wick above the high that falls back is not a
breakout. It says what it sees and nothing more: "triggered" is a rule that
fired, not a promise that the price keeps going.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check BTCUSDT                       # opening-range breakout, 5m candles
python3 "${CLAUDE_SKILL_DIR}/entry.py" check BTCUSDT --write               # -> trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check ETHUSDT --setup level --level 2500
python3 "${CLAUDE_SKILL_DIR}/entry.py" check BTCUSDT --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch BTCUSDT --every 15 --until 05:30   # one line per closed candle
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                           # every hit in trader/watchlist.json
```

`--now "2026-10-19 02:00"` pins the clock (UTC); `--offline FILE` reads a saved
klines answer. Exit code 0 = triggered.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | `session_start_utc`, `opening_range_minutes`, `entry_window_minutes` |
| `trader/watchlist.json` | the scanner's hits, for `watchlist` |
| `trader/entry.json` | written by `check --write` and by `watch` on a trigger: symbol, setup, session, status, the trigger candle's close time (UTC) and price, the opening range |

## The rule

1. The session starts at `session_start_utc` every day and lasts 24 hours. The
   opening range is the high and low of its first `opening_range_minutes`.
2. A candle counts only once it has closed (its open time + the interval). The
   one still forming is shown, never used.
3. Triggered: the first closed candle after the range whose CLOSE is above the
   range high (or above `--level`). Its time is the candle's close time; its
   price is that close, exactly as Binance reports it, no rounding.
4. `--min-volume-x X`: the trigger candle also needs X times the average volume of
   the range candles.
5. Expired: no trigger by `entry_window_minutes` after the session start (360 by
   default, so 06:00 UTC). A trade found later is a different trade.

## Steps

1. `entry.py check SYMBOL`. Say the status in one line: waiting and how far,
   triggered at what time and price, or expired.
2. To wait for it: `entry.py watch SYMBOL`. It sleeps until each candle closes.
3. On a trigger: `entry.py check SYMBOL --write`, then `/trader-exit` sets the
   stop, the target and the latest exit BEFORE anything is bought.
4. A trigger more than one candle old is stale: say so, the price has moved.

## Example: the test data

Synthetic BTCUSDT candles, session 2026-10-19:

```
python3 entry.py check BTCUSDT --offline tests/fixtures/binance/klines-BTCUSDT-5m-2026-10-19.json --now "2026-10-19 02:00"
Opening range 00:00-00:15 UTC: high 52,680.36, low 52,410.22 (3 of 3 candles).
TRIGGERED 02:00 UTC at 52,730.00: the 01:55 candle (candle 23) closed above the range high 52,680.36 (+0.09%).
This is the moment to get in, if you take the trade. Nothing was placed.
```

At 01:58 the same command says WAITING: the 01:55 candle was above the high, but
it had not closed yet.

## Rules

- Say "the rule fired", never "buy now" as advice.
- Always give the UTC time and the price of the trigger candle.
- Never call a forming candle a trigger.
- A trigger after the entry window is "expired", whatever the price did.

## What it never does

It never places, sizes or checks an order, never moves a level to make a trigger
happen, and never calls a signed endpoint.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Testnet by default (`"testnet": true`). Live trading needs `"testnet": false` in the config AND `--live`,
  and is the user's own decision.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order in this session, the user typed an explicit yes for that one order, and
  `ticket.py place` is called with the one-time code within 5 minutes. Keep
  `Bash(python3 *ticket.py place*)` on "ask" in Claude Code; never "allow".
- Spot only: no margin, futures, leverage, short selling or withdrawals. The client cannot call those endpoints.
- API keys live in `BINANCE_API_KEY` / `BINANCE_API_SECRET` in the environment, never in a file, the chat or a log.
