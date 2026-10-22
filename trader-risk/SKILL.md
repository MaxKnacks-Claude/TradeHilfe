---
name: trader-risk
description: >-
  Risk Manager for Binance spot. Checks every proposed order against your
  limits and Binance's own rules and stops anything over them: the loss at the
  stop with both fees, reward:risk after fees, the daily loss limit, trades
  and orders per session, position size, free USDT after your buffer,
  duplicates, the entry window of your UTC session, expired plans, and every
  exchange filter (LOT_SIZE, MARKET_LOT_SIZE, PRICE_FILTER, NOTIONAL /
  MIN_NOTIONAL for the entry and both OCO legs). Blocks anything margin,
  futures or leverage, and short sells. PASS or BLOCK with every reason,
  recorded with a fingerprint so trader-broker only sends that exact order. Use
  when the user says "check this trade", "is this within my limits", "risk
  check", "can I place this", or after /trader-budget, before /trader-broker.
argument-hint: "[--orders trader/orders.json]"
---

# trader-risk

The Risk Manager is the rulebook between a plan and an order. It does not judge
whether a trade is good; it checks whether it breaks one of your rules or one of
Binance's. Any rule broken is a BLOCK, and a blocked order never reaches Binance.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                    # trader/orders.json -> trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write          # check without recording
```

Exit code 0 when every order passes, 1 when any is blocked. `--now` pins the clock (UTC).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposals from trader-budget (or a flatten ticket) |
| `trader/config.json` | your limits and the session |
| `trader/portfolio.json` | free USDT and coins from `ticket.py account` (else a fresh account with the budget) |
| `trader/prices.json` | fresh prices from trader-alerts |
| `trader/state/broker.json` | what was placed in this session: trades, orders, losses at risk |
| `trader/state/risk.json` | the verdicts with each order's fingerprint |

## What blocks

| check | blocks when |
| --- | --- |
| format | unknown fields (margin, futures, leverage, `quoteOrderQty`), not MARKET / LIMIT, a LIMIT without price or timeInForce, an OCO that does not SELL with LIMIT_MAKER / TAKE_PROFIT above and STOP_LOSS(_LIMIT) below |
| asset | not in exchangeInfo, not TRADING, no spot trading, another quote asset, an order type the symbol does not take, no OCO allowed |
| filters | quantity off the step or outside min / max, a price off the tick, the entry or either OCO leg under the minimum notional |
| short | a sell of more than the account holds (spot cannot go short) |
| stop / target | no OCO while `require_stop` is true; stop not under or target not above the entry; an OCO selling more than the buy delivers after its fee |
| loss | loss at the stop, both fees in, over `max_loss_per_trade` |
| reward:risk | gain at the target / loss at the stop, after fees, under `min_reward_risk` |
| size, position, cash | under `min_order` / over `max_order`, over `max_position_pct` of the budget, more than free USDT minus the buffer |
| daily loss | this session's losses at risk plus this one over `max_daily_loss` |
| trades, orders | `max_trades_per_day` entries / `max_orders_per_day` orders already placed in this session |
| duplicate | the symbol already entered in this session, or twice in the batch |
| window | before the opening range is set, or after `entry_window_minutes` |
| plan expired | the plan's `exit_by` has passed |

An EXIT (a sell of coins you hold) is checked for format, asset, filters and
holdings only: getting out must always be possible. "Today" is your UTC session.
There is no pattern-day-trader rule in crypto, so there is no PDT check.

## Steps

1. `check.py`. Read every BLOCK reason to the user, word for word.
2. On a BLOCK: fix the plan or the rules, run trader-budget again, check again.
3. On PASS: `/trader-broker` shows the ticket and asks.

## Example: the test data

```
PASS  #1 BUY BTCUSDT, entry
          BUY 0.00948 BTCUSDT at market, then OCO SELL 0.00947: take-profit 53468.84 / stop 52545.29
          entry 52730 (the plan's entry), stop 52545.29, target 53468.84: loss at the stop 2.77, gain at the target 5.96, fees in
```

The same order at 07:00 UTC is a BLOCK: after the entry window, and its plan expired at 06:00.

## Rules

- A PASS is a rules check, not a recommendation. Say so.
- Never relax a limit to make an order pass; the user changes their own config.

## What it never does

It never places an order, never edits the orders file, never calls a signed endpoint.

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
