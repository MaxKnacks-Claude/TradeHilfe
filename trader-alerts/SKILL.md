---
name: trader-alerts
description: >-
  Price Alerts for Binance spot. Rings when a price, a stop, a target or the
  max hold hits. Turns the trade plan into three alerts in one go (the stop,
  the target, and the exit_by moment when max_hold_minutes is reached), plus
  above / below levels, percent moves against 24 hours ago, any UTC time of the
  session and the session start, each ringing once so you are not spammed.
  Shows where you are in your configured UTC session, reads Binance 24h
  tickers in one request, and writes trader/prices.json for sizing and the
  risk check. Use when the user says "alert me when", "tell me if BTC hits",
  "watch my stop", "watch my trade", "remind me when my hold time is up", "what
  session is it", "get me a price", or right after /trader-exit writes a plan.
argument-hint: "[symbol] [price]"
---

# trader-alerts

Price Alerts watches the numbers so the user does not have to stare at a chart.
An alert is information, never an order: when a stop alert rings, nothing is
sold by this skill. The OCO at Binance does the stopping; the alerts tell the user
what happened and when it is time to be out.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                        # where in the session are we (UTC)
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan              # stop, target, exit_by from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --session-start
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol BTCUSDT --stop 52545.29
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol ETHUSDT --pct 3 --direction up
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 05:30 --label "check the trade"
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                        # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote BTCUSDT ETHUSDT --write   # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset btcusdt-plan-stop
```

`--now "2026-10-19 06:00"` pins the clock (UTC); `--offline DIR` reads a saved
`ticker24hr-mini.json`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest prices, merged in, for trader-budget and trader-risk |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | the price is at or above the level | after `reset`, or on its own with `"rearm": true` |
| `below` / `stop` | the price is at or below the level | same |
| `pct_move` | the price moved `pct` % against 24 hours ago (or `ref`) | once per session |
| `session_start` | a new session started | once per session |
| `time` | the UTC clock passed `at` in a session | once per session |
| `exit_by` | the moment `at_utc` passed: the plan's max hold is reached | once |

Rules from a plan carry `expires_utc` (an hour after the exit) so they do not ring
the next day.

## Quotes

One request for every symbol: `GET /api/v3/ticker/24hr?type=MINI&symbols=[...]`
(last price, open price 24 hours ago, close time). Binance tickers are real time,
but an alert checked every few minutes is not; the OCO is what fills.

## The session clock

`alerts.py clock` says the session date, when it started, when the opening range
ends, until when new entries are allowed and the max hold. Crypto trades around
the clock: the session is the user's rule in `trader/config.json`.

## Steps

1. After `/trader-exit`: `alerts.py add --from-plan`.
2. While the trade is open, run `alerts.py check` every minute or two (a scheduled
   task or `/loop`). Read every ALERT line to the user.
3. On `exit_by`: `ticket.py flatten --symbol SYMBOL`, then trader-risk and the
   user's yes. Never close a position without that yes.

## Example: the test data

```
python3 alerts.py check --now "2026-10-19 06:00" --offline tests/fixtures/binance
ALERT Max hold reached for BTCUSDT: the plan said out by 2026-10-19 06:00 UTC (240 minutes after the entry).
If it is still open, close it: ticket.py flatten --symbol BTCUSDT cancels the OCO and sells, after trader-risk and your yes.
```

## Rules

- An alert is information. Say what hit and what the plan says to do; never act alone.
- Always give the UTC time.

## What it never does

It never places, changes or cancels an order and never calls a signed endpoint.

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
