---
name: trader-broker
description: >-
  Order Executor for Binance spot, testnet by default, never without your yes.
  Shows each proposed order as a plain ticket (the buy, the protective OCO with
  take-profit and stop, the max loss and gain with fees, the latest exit) plus
  the exact API parameters, records your yes word for word and hands out a
  one-time code, then places the buy and, as soon as it fills, the OCO on what
  actually arrived after the fee. Reads the account (read only), checks fills,
  builds the closing ticket at the max hold (cancel the OCO, sell at market),
  and logs everything so trader-risk can hold the daily limits. Live trading
  only with "testnet": false AND --live. Use when the user says "place the
  order", "buy it", "execute", "send it to Binance", "close my position",
  "flatten", "get me out", "connect Binance", "set up the testnet", or after
  /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can send an order, so it
is the strictest. An order reaches Binance only after three gates, every time,
for every order:

1. **trader-risk passed this exact order in this session** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat. `approve`
   records it word for word and prints a one-time code.
3. **`place` is called with that code** within 5 minutes. Claude Code's own
   permission prompt for `ticket.py place` asks once more (see Setup, step 5).

No yes, no order. The code is stored only as a hash and works once.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" account                    # balances -> trader/portfolio.json (read only)
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                       # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" validate --index 1         # Binance's order/test: checks, sends nothing
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" place --index 1 --confirm <code>
python3 "${CLAUDE_SKILL_DIR}/ticket.py" status                     # the order and its OCO at Binance
python3 "${CLAUDE_SKILL_DIR}/ticket.py" protect --index 1          # set the approved OCO if place could not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol BTCUSDT   # closing ticket: cancel OCO + market sell
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposals (trader-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from this session is READY |
| `trader/state/broker.json` | every yes, no, placed, protected, unprotected, failed and closed, with each entry's max loss |
| `trader/portfolio.json` | free balances from `GET /api/v3/account`, refreshed after each order |

## Setup: Binance spot testnet

1. Log in at https://testnet.binance.vision (with a GitHub account) and create an
   HMAC API key there. Testnet keys are separate from real ones and hold test money.
2. Put the keys in the environment of the shell Claude Code runs in, never in a
   file in the repo and never in the chat:
   `export BINANCE_API_KEY=...` and `export BINANCE_API_SECRET=...`
   If the user pastes a key into the chat anyway: tell them to delete it there and
   make a new one.
3. `cp config.example.json trader/config.json` (it says `"testnet": true`).
4. `ticket.py account` must show the testnet balances.
5. In `.claude/settings.json` put the rules from `claude_settings.example.json`
   here: `ticket.py place` and `ticket.py protect` on "ask", never "allow".

**Live (real money) is the user's own decision**, never Claude's: they make a key
on binance.com with ONLY "Enable Spot & Margin Trading" ticked, "Enable
Withdrawals" OFF, restricted to their IP; set `"testnet": false`; and pass `--live`
to every ticket.py call. A yes given for a testnet ticket never places a live order.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. NOT READY or BLOCKED goes
   back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order per
   question. Never offer "yes to all".
3. `ticket.py approve --index N --reply "<the user's reply, word for word>"`. If it
   prints "Not recorded", it was not an explicit yes for this order ("ok", "sure",
   "yes to all", a yes naming another pair or amount). Ask again. Never turn a reply
   into a yes yourself.
4. Only after "Recorded your yes": `ticket.py place --index N --confirm <code>`.
   It buys, reads what arrived after the fee and sets the OCO at once.
5. Say what happened: the fill, the OCO's list id. If it prints **UNPROTECTED**, tell
   the user immediately: the coins have no stop at Binance. Offer `protect` (the
   same approved OCO) or `flatten` (a new ticket, a new yes). Nothing is retried by
   itself. A refused order is not retried without a new check and a new yes.

## Steps: while it is open, and out at the max hold

- trader-alerts watches the stop, the target and `exit_by` (`alerts.py add --from-plan`).
  The OCO at Binance does the stopping.
- `ticket.py status` shows the OCO; when one leg filled it records which.
- At `exit_by` (max hold), anything still open gets closed:
  1. `ticket.py flatten --symbol BTCUSDT` writes a ticket: cancel the open OCO, then
     SELL its quantity at market.
  2. `/trader-risk`. A sell of coins the user holds is never blocked by the entry limits.
  3. `ticket.py show`, the user's yes for this one order, `approve`, `place`.

## Example: the test data

```
ORDER 1 of 1, Binance spot, TESTNET, no real money
  BUY 0.00948 BTCUSDT at market
  then at once an OCO: SELL 0.00947, take-profit 53468.84 / stop 52545.29, whichever comes first
  max loss about 2.77 if the stop fills at 52545.29, fees in (it can fill worse in a fast market)
  gain about 5.96 at the take-profit, fees in
  out by 2026-10-19 06:00:00 UTC at the latest (max hold): trader-alerts rings, flatten closes it
  READY: trader-risk PASS, waiting for your yes
  place sends exactly:
    POST /api/v3/order {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.00948"}
    POST /api/v3/orderList/oco {"symbol": "BTCUSDT", "side": "SELL", "quantity": "0.00947", ...}
```

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores, prints or repeats API keys.
- Never switches `"testnet"` to false or adds `--live` on its own.
- Never withdraws, never trades margin or futures, never uses leverage: the client
  cannot call those endpoints at all.
- Never promises a fill price: a market order and a stop fill at whatever the market gives.

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
