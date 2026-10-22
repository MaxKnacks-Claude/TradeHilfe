# Binance fixtures

**These files are synthetic.** Their shape is exactly that of the documented Binance Spot
REST answers (`github.com/binance/binance-spot-api-docs`, `rest-api.md` and `filters.md`):
kline arrays of strings, `exchangeInfo` with its filters, 24h MINI tickers. The prices are
a seeded random walk from `make_synthetic.py`, not market history: the build container
could not reach any Binance host, so nothing could be recorded there.

| file | what |
| --- | --- |
| `exchangeInfo.json` | 6 USDT pairs with realistic filters, plus a leveraged token, a stablecoin pair, a BTC-quoted pair and a pair in BREAK; DOGEUSDT carries the old `MIN_NOTIONAL` filter and no `STOP_LOSS` order type |
| `klines-BTCUSDT-5m-60d.json.gz` | 60 sessions of 5-minute candles before 2026-10-19, the session itself and 2 hours after; 2026-09-02 misses an hour (a maintenance gap) |
| `klines-BTCUSDT-5m-2026-10-19.json` | the example session alone: the 15-minute range is 52,410.22 to 52,680.36, the 01:55 candle closes at 52,730.00 |
| `scan-5m-2026-10-19.json.gz` | the session's 5-minute candles for the six pairs |
| `scan-1d-2026-10-19.json.gz` | 121 daily candles each; ETHUSDT breaks its 20-day closing high, SOLUSDT is oversold |
| `ticker24hr-mini.json` | 24h MINI tickers |

`python3 tests/fixtures/binance/make_synthetic.py` rewrites them byte for byte.
`python3 tests/record_fixtures.py` records the same files from Binance (public data, no key)
on a machine that can reach it. The tests pin numbers from the synthetic data; after a swap
they say which expectations to update.
