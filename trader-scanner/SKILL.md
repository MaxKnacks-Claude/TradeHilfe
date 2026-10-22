---
name: trader-scanner
description: >-
  Chart Scanner for Binance spot. Scans every TRADING USDT pair for setups:
  opening-range breakouts in your configured UTC session, 20-day breakouts,
  bull flags, reclaims of the 20-day average, oversold coins and oversold
  bounces. Builds the universe from Binance exchangeInfo (no leveraged tokens,
  no stablecoin pairs, a 24h volume floor), reads 1-day and 5-minute candles
  through the shared client that respects Binance's rate limits, and writes
  trader/watchlist.json for trader-entry. Use when the user says "scan the
  market", "scan Binance", "find me setups", "what is breaking out",
  "opening range breakouts", "which coins are oversold", "build my
  watchlist", "what should I look at today", or at the start of a session
  before /trader-entry.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade. It reads Binance charts and
lists the ones that match a rule. That is all it does. A scan is a list of charts
that match a rule, not a list of coins to buy, and a setup on a chart is not a
prediction. Most people who day trade lose money, and crypto moves faster than
stocks; the scanner does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
The shared folder `common/` must sit next to it (one level up).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                          # -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                             # 1-day setups -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols BTCUSDT,ETHUSDT   # or --watchlist FILE, --limit 50
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                               # breakouts in the current session
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 02:00                    # cut the clock at 02:00 UTC
```

Flags: `--min-quote-volume` (universe and daily: config `min_quote_volume_24h`,
5,000,000 USDT; orb: 0), `--now "2026-10-19 02:00"` (UTC) to pin the clock,
`--offline PATH` to read saved Binance answers, `--no-write`, `--show N`,
`--json`, `--live` (only with `"testnet": false`). Exit code 0 when the scan
found something, 1 when it found nothing or could not run (the reason is one
plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | `quote_asset`, `symbols` (used when there is no universe yet), the session, the filters |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|BASE/QUOTE\|24h quote volume` a line, most traded first |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `session`, `scanned`, `failed`, `hits[]`; trader-entry reads `hits[].symbol` |

## The universe

`universe` reads `GET /api/v3/exchangeInfo?permissions=SPOT&symbolStatus=TRADING`
(weight 20) and `GET /api/v3/ticker/24hr?type=MINI` (all symbols, weight 80), and
keeps a pair when, in order:

1. its quote asset is `quote_asset` (USDT) and spot trading is allowed,
2. it is not a leveraged token (BTCUP, ETHBEAR ...), unless `exclude_leveraged_tokens` is false,
3. it is not a stablecoin against a stablecoin (USDCUSDT), unless `exclude_stable_pairs` is false,
4. it is not on your `blocked` list,
5. it traded at least `--min-quote-volume` in the last 24 hours.

## The daily scan

1-day candles are UTC days. Only complete days count: today's candle is left
out until it closes at 00:00 UTC. One `klines` request a symbol (weight 2).

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 days before it |
| `bull_flag` | up more than 10 % over the 10 days before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 the day before and the last day closed up |

## The opening-range breakout scan

Crypto never closes, so the session is yours: `session_start_utc` (00:00),
`opening_range_minutes` (15) and `entry_window_minutes` (360) in
`trader/config.json`. The range is the high and low of the first 15 minutes of
the session. A hit is the first completed 5-minute candle after the range that
CLOSES above the range high, at that candle's close time. A session that starts
at 22:00 UTC runs over midnight; that works the same way.

One `klines` request per symbol from the session start to the cutoff. A chart
is usable only when every 5-minute candle is there; a missing one (maintenance,
no trades) leaves it out and says so. Before the first candle after the range,
or after the entry window, it prints one plain line and writes nothing.

## Rate limits

Binance allows 6,000 request weight a minute per IP. The shared client reads
`X-MBX-USED-WEIGHT-1M` on every answer and pauses until the next minute at 70 %,
waits out an HTTP 429 (`Retry-After`) and stops on a 418 (an IP ban) instead of
hammering it. A scan of 300 pairs costs about 600 weight.

## Steps

1. Once a day or so: `scan.py universe`.
2. After a daily candle closes (00:00 UTC): `scan.py daily`. Tell the user the counts
   (fetched, scanned, hits) and the top few by setup.
3. After the opening range: `scan.py orb`. Tell the user how many usable charts
   broke their range and when, and that a later hit is closer to the price now.
4. The user picks one chart. Hand it on: `/trader-entry SYMBOL`. Never pick for them.

## Example: the test data

The offline fixtures (synthetic Binance answers, see `tests/fixtures/binance/README.md`),
session 2026-10-19, cut at 03:00 UTC:

```
python3 scan.py orb --offline tests/fixtures/binance/scan-5m-2026-10-19.json.gz --now "2026-10-19 03:00"
Fetched 6 of 6 charts; 6 usable.
4 of 6 charts closed above their opening-range high by 03:00 UTC:
  00:50 UTC  PEPEUSDT       0.00000878  range 0.00000862 to 0.00000875  +0.34% over
  02:00 UTC  BTCUSDT         52,730.00  range 52,410.22 to 52,680.36  +0.09% over
  ...
```

The user picks BTCUSDT and Claude hands it to `/trader-entry BTCUSDT`.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data (UTC).
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which coin to buy, never
calls the data a promise, and never calls a signed endpoint.

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
