---
name: trader-budget
description: >-
  Budget Manager for Binance spot. Sizes every position to your budget and
  your max loss per trade, fees included. Reads the plan from trader-exit,
  finds the largest fractional quantity on the symbol's LOT_SIZE step that
  fits four limits (max loss at the stop with both fees, position cap, max
  order, free USDT after your buffer), checks Binance's tick size and minimum
  notional for the entry and both legs of the protective OCO, and writes ONE
  proposed order to trader/orders.json: the buy plus the OCO (take-profit and
  stop) that trader-broker sets as soon as the buy fills. Refuses plainly when
  the numbers do not work. Nothing is placed. Use when the user says "how much
  should I buy", "size this trade", "I have 1,000 USDT", "I only want to risk
  10 USDT a trade", "set my rules", or after /trader-exit writes a plan.
argument-hint: "[--budget USDT] [--limit PRICE]"
---

# trader-budget

The Budget Manager turns a plan ("buy BTCUSDT at 52,730, stop 52,545.29") into an
amount. It starts from the loss you accept, not from what you would like to make.
On Binance spot that loss includes two fees and one quirk: the buy's fee is taken
in the coin you buy, so you can sell a little less than you bought.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                         # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                 # -> trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 52700 --write   # a LIMIT buy (GTC) instead of MARKET
python3 "${CLAUDE_SKILL_DIR}/size.py" --budget 500
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | entry, stop, target, exit_by from trader-exit |
| `trader/config.json` | `budget`, `cash_buffer_pct`, `max_position_pct`, `max_order`, `min_order`, `max_loss_per_trade`, `min_reward_risk`, `fee_rate`, `stop_limit_offset_pct`, `blocked` |
| `trader/portfolio.json` | free USDT from `ticket.py account`; it caps the size if it is lower than the budget |
| `trader/prices.json` | a fresh price (under 5 minutes) above the plan's entry is used instead |
| `trader/cache/` | the symbol's exchangeInfo, 6 hours |
| `trader/orders.json` | the proposed order |

## Step 1: set the rules once

Copy `config.example.json` from the repository root to `trader/config.json` and
set your numbers. Amounts are in the quote asset (USDT). No keys in this file.

## Step 2: size the trade

```
loss at the stop  = Q x E - Q' x W x (1 - fee)        Q' = Q x (1 - fee), down to the step
gain at target    = Q' x T x (1 - fee) - Q x E
```

Q is the largest step multiple that keeps all four limits. W is the stop, or the
stop-limit price (`stop_limit_offset_pct` under the stop) when the symbol has no
STOP_LOSS order type. Refused, with the reason: zero quantity, the entry or an OCO
leg under the minimum notional or quantity, reward:risk after fees under
`min_reward_risk`, an expired plan, a blocked or non-trading symbol.

A very small position can lose a whole step to the fee rounding (the "dust"):
then the trade cannot win and it is refused.

## Step 3: show the user

Say the quantity, the cost, which limit decided it, the loss at the stop and the
gain at the target, both with fees, and the two orders that will follow a yes.

## Example: the test data

```
Size: 0.00948 BTC = 499.88 USDT. The buy's fee leaves 0.00947 to sell.
Max loss at the stop 52545.29, fees in: 2.77 USDT (your limit 10.00).
Gain at the target, fees in: 5.96 USDT. Reward:risk after fees 2.15.
Proposed (nothing has been placed):
  1. BUY 0.00948 BTCUSDT at market
  2. as soon as it fills: OCO SELL 0.00947: take-profit 53468.84 / stop 52545.29
```

## Rules

- Size from the loss, never from the hoped-for gain.
- Always say the fees are in, and show the after-fees reward:risk.
- A refusal is an answer: say why, never shrink the stop to make it fit.

## What it never does

It never places an order, never sizes a short or a margin position, never rounds a
quantity up.

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
