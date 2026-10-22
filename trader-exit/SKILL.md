---
name: trader-exit
description: >-
  Exit Planner for Binance spot. Sets your stop, your target and your latest
  exit time before you enter, so you know when to get out. Reads the entry from
  trader-entry (or a price you give), puts the stop at the opening-range
  midpoint or low, a percent, a price or an ATR multiple, the target at an R
  multiple or a price, rounds both to the symbol's tick size from Binance's
  exchangeInfo, and sets exit_by = entry + max_hold_minutes (crypto has no
  closing bell). Shows reward:risk before and after fees and warns when fees
  eat the risk. Writes trader/plan.json. Long only. Use when the user says
  "where should my stop be", "set my stop", "what's my target", "plan the
  exit", "how long do I hold", "risk reward", or after /trader-entry
  triggers, before /trader-budget.
argument-hint: "[--stop or-mid|or-low|pct:X|price:X|atr:K] [--target 2R|price:X]"
---

# trader-exit

The Exit Planner decides how the trade ends before it begins: where you are
wrong (the stop), where you take the money (the target) and when you are out
anyway (the max hold). Set before the entry and never moved further away.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                                 # from trader/entry.json: or-mid stop, 2R
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --target 4R --write             # -> trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5                  # ATR on the session's candles
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol ETHUSDT --entry 2484.49 --setup level --stop pct:1
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --max-hold 120                  # out after 2 hours at the latest
python3 "${CLAUDE_SKILL_DIR}/exit.py" show
```

`--now` pins the clock (UTC). `--exchange-info FILE` reads saved symbol rules;
otherwise they come from Binance (weight 20) and are cached 6 hours in `trader/cache/`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from trader-entry |
| `trader/config.json` | `max_hold_minutes` (240), `fee_rate` (0.001), `min_reward_risk` (1.5) |
| `trader/plan.json` | entry, entry time, stop, target, risk a unit, reward:risk before and after fees, tick size, `exit_by` |

## The rules

| stop | where |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default after an orb trigger) |
| `or-low` | the opening-range low |
| `pct:X` | X percent under the entry |
| `price:X` | your price |
| `atr:K` | K times the 14-candle ATR under the entry, on the session's candles up to the entry |

A stop rounds DOWN to the tick (away from the entry); a target rounds to the
nearest tick. Refused, with the reason: a stop at or above the entry, a target
at or below it, a risk under 0.05 % of the price, an entry that has not triggered,
a symbol that is not trading.

**Fees.** Binance charges `fee_rate` (0.1 %) on each side. A tight stop makes the
fees a big part of the loss: with BTC at 52,730 and a 0.35 % stop, the fees are
56 % of the risk, and a 1.5R target is only 0.59R after fees. The plan shows both
numbers; trader-risk enforces `min_reward_risk` AFTER fees.

**Max hold.** `exit_by` = the entry candle's close + `max_hold_minutes`. It may be
after midnight UTC; that is fine. trader-alerts rings at that moment and
trader-broker builds the closing ticket.

## Steps

1. `exit.py plan` (add `--target` / `--stop` if the user wants other rules).
2. Show the plan: entry, stop, target, risk a unit, reward:risk before AND after
   fees, exit by. Read the notes out loud, especially the fee note.
3. `exit.py plan ... --write`, then `/trader-timemachine` replays it and
   `/trader-budget` sizes it.

## Example: the test data

```
python3 exit.py plan --from trader/entry.json --target 4R
  Entry          52,730.00 at 2026-10-19 02:00:00 UTC (candle 23)
  Stop           52,545.29  opening-range midpoint: (52680.36 + 52410.22) / 2 = 52545.29
  Target         53,468.84  4R (four times the risk)
  Risk a unit    184.71 (0.35% of the entry)
  Reward:risk    4.0, after 0.1% fees a side 2.18
  Exit by        2026-10-19 06:00:00 UTC at the latest (max hold 240 minutes), win or lose
Note: Fees eat 56 % of the risk at the stop: a stop this close to the entry costs a lot in fees.
```

## Rules

- The stop is set before the entry and never moved further away.
- Always show reward:risk after fees next to the plain one.
- Say "a stop is a plan, not a guarantee": a fast market can fill past it.

## What it never does

It never places an order, never sizes one, never plans a short, and never drops
the max hold.

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
