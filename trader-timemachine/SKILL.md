---
name: trader-timemachine
description: >-
  Time Machine for Binance spot. Replays a planned trade before you risk a
  dollar: 10,000 simulated runs of the next max_hold_minutes from your entry,
  each 5-minute candle drawn from the coin's own candles at the same time of
  day over the last 60 days (by UTC time of day, wrapping over midnight), plus
  the same rule re-run on every one of those real sessions. Reports target
  first, stop first, neither, the price at the max hold, average R before AND
  after fees, and a verdict against the break-even share after fees. Seeded,
  so anyone can repeat it. Use when the user says "what are the odds", "is
  this a good trade", "replay it", "backtest this setup", "how often does
  this work", "simulate", or after /trader-exit writes a plan.
argument-hint: "[--replays 10000] [--seed 19] [--hold MINUTES]"
---

# trader-timemachine

Before you risk money, the Time Machine shows how often this exact plan would
have hit the target or the stop, with the coin's own recent candles. It is a
reality check, not a forecast: it is built from the last 60 days, and crypto can
do things those days never did (a listing, a hack, a liquidation cascade).

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                          # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write                  # -> trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --hold 120 --seed 7
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from trader/timemachine.json   # ASCII fan chart
```

`--now` pins the clock (UTC); `--offline FILE` reads a saved klines answer.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit (entry, entry time, stop, target, max hold, tick size) |
| `trader/config.json` | `history_days` (60), `fee_rate`, the session settings |
| `trader/timemachine.json` | the result: odds, price bands, history trades, verdict |

## How it works

1. Data: `history_days` of 5-minute candles up to the entry, about 18 `klines`
   requests (1,000 candles each, weight 2). Nothing after the entry is read.
2. Sessions follow your config: `session_start_utc` + 24 hours = 288 candles.
   Only full sessions before the plan's session go into the pool; a session with a
   maintenance gap is left out and named.
3. Each replay walks the next `max_hold_minutes` from the entry. Each candle is a
   real past candle from the same time of day (plus or minus 2 candles), as ratios,
   scaled to the current price. The clock wraps at the end of the session, so a
   hold over midnight works. Stop is checked before target in each candle (the
   conservative call).
4. History: the plan's own rule (same range, stop rule, target R, max hold) on every
   past session, held on the real candles that followed.
5. Fees: R after fees takes `fee_rate` off both sides. The verdict compares the
   target's share of the decided replays with the share that breaks even AFTER fees.

## Steps

1. `timemachine.py run --write`.
2. Tell the user: target first, stop first, neither, the average R after fees, the
   history line and the verdict, word for word. Say the seed.
3. If the verdict leans toward the stop, say so plainly. The user decides.
4. Then `/trader-budget`.

## Example: the test data

The 4R plan from trader-exit on the synthetic BTCUSDT candles:

```
  target first   14.6 %
  stop first     75.5 %
  neither        9.9 %    still open at the max hold (06:00 UTC)
  Average R: -0.039 before fees, -0.610 after 0.10 % fees a side
History: ... fired 48 times: 4 target, 40 stop, 4 neither. Average R -0.357, after fees -1.109.
Strongly toward the stop: after fees the target has to come first in 31 % of the replays that hit
one side; it did in 16 %.
```

That is the point of looking first: this plan loses money in its own history.

## Rules

- Report the odds as they come out. Never round a 16 % up to "decent".
- Always show the after-fees numbers next to the plain ones.
- Say "a simulation, not a prediction" every time.

## What it never does

It never places, sizes or checks an order, never reads data from after the entry,
and never hides a losing history.

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
