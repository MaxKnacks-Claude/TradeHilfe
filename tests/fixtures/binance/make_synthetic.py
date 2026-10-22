#!/usr/bin/env python3
"""Builds the SYNTHETIC Binance fixtures in this folder. Standard library only, no network.

Why synthetic: the container these tools were built in could not reach any Binance host.
Every file here has exactly the shape of the documented Binance Spot REST answers
(github.com/binance/binance-spot-api-docs, rest-api.md and filters.md), but the prices are
a seeded random walk, not market history. Replace them with recorded answers by running
tests/record_fixtures.py on a machine that can reach Binance (the tests then need their
expected numbers refreshed; they say which).

    python3 tests/fixtures/binance/make_synthetic.py      # rewrites the files, same bytes every time
"""

import datetime
import gzip
import json
import math
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
EPOCH = datetime.datetime(1970, 1, 1)
DAY = "2026-10-19"          # the example session (session_start_utc 00:00)


def ms(dt):
    return int((dt - EPOCH).total_seconds()) * 1000


def fmt(x, places):
    return "%.*f" % (places, x)


def walk(seed, start_price, start, n, step_s, vol, places, qty_places=5, drift=0.0, profile=True):
    """Seeded geometric random walk -> Binance kline arrays (strings, like the API)."""
    rng = random.Random(seed)
    p = start_price
    rows = []
    for i in range(n):
        t = start + datetime.timedelta(seconds=step_s * i)
        hour = t.hour + t.minute / 60.0
        k = 1.0 + (0.6 * math.exp(-((hour - 14.5) ** 2) / 6.0) if profile else 0.0)   # busier US afternoon
        o = p
        moves = [rng.gauss(drift, vol * k / 2.0) for _ in range(4)]
        path = [o]
        for m in moves:
            path.append(path[-1] * math.exp(m))
        c = path[-1]
        h = max(path) * (1 + abs(rng.gauss(0, vol * 0.3)))
        lo = min(path) * (1 - abs(rng.gauss(0, vol * 0.3)))
        v = abs(rng.gauss(1.0, 0.35)) * k * 40.0 * (1 + 20 * abs(c / o - 1) / max(vol, 1e-9) * 0.05)
        q = round(v, qty_places)
        o_s, h_s, l_s, c_s = (fmt(x, places) for x in (o, h, lo, c))
        qv = sum(float(x) for x in (o_s, c_s)) / 2 * q
        rows.append([ms(t), o_s, h_s, l_s, c_s, fmt(q, 8), ms(t) + step_s * 1000 - 1, fmt(qv, 8),
                     rng.randint(200, 4000), fmt(q * 0.5, 8), fmt(qv * 0.5, 8), "0"])
        p = float(c_s)
    return rows


def set_close(rows, i, close, places):
    """Move one candle's close (open = the previous close), keeping high >= open/close >= low."""
    r = rows[i]
    o = float(rows[i - 1][4]) if i else float(r[1])
    r[1], r[4] = fmt(o, places), fmt(close, places)
    r[2] = fmt(max(o, close) * 1.004, places)
    r[3] = fmt(min(o, close) * 0.996, places)


def orb_breakout(rows, bars_range=3, last=72):
    """(bar index, high) of the first 5-minute close above the opening range of the day the rows start."""
    hi = max(float(r[2]) for r in rows[:bars_range])
    for i in range(bars_range, min(last, len(rows))):
        if float(rows[i][4]) > hi:
            return i, hi
    return None, hi


def pick_seed(base_seed, make, test):
    """The first seed from base_seed whose walk passes test(rows): deterministic."""
    seed = base_seed
    while True:
        rows = make(seed)
        if test(rows):
            return seed, rows
        seed += 1


def symbol(sym, base, quote, tick, step, min_qty, max_qty, min_notional, status="TRADING",
           order_types=None, market_step=None, use_min_notional=False, spot=True, permissions=("SPOT",)):
    order_types = order_types or ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT",
                                  "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"]
    filters = [
        {"filterType": "PRICE_FILTER", "minPrice": tick, "maxPrice": "1000000.00000000", "tickSize": tick},
        {"filterType": "LOT_SIZE", "minQty": min_qty, "maxQty": max_qty, "stepSize": step},
        {"filterType": "ICEBERG_PARTS", "limit": 10},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.00000000", "maxQty": market_step or "100.00000000",
         "stepSize": "0.00000000"},
        {"filterType": "TRAILING_DELTA", "minTrailingAboveDelta": 10, "maxTrailingAboveDelta": 2000,
         "minTrailingBelowDelta": 10, "maxTrailingBelowDelta": 2000},
        {"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierUp": "5", "bidMultiplierDown": "0.2",
         "askMultiplierUp": "5", "askMultiplierDown": "0.2", "avgPriceMins": 5},
        {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 200},
        {"filterType": "MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders": 5},
    ]
    if use_min_notional:
        filters.append({"filterType": "MIN_NOTIONAL", "minNotional": min_notional, "applyToMarket": True,
                        "avgPriceMins": 5})
    else:
        filters.append({"filterType": "NOTIONAL", "minNotional": min_notional, "applyMinToMarket": True,
                        "maxNotional": "9000000.00000000", "applyMaxToMarket": False, "avgPriceMins": 5})
    return {"symbol": sym, "status": status, "baseAsset": base, "baseAssetPrecision": 8, "quoteAsset": quote,
            "quotePrecision": 8, "quoteAssetPrecision": 8, "baseCommissionPrecision": 8,
            "quoteCommissionPrecision": 8, "orderTypes": order_types, "icebergAllowed": True,
            "ocoAllowed": True, "otoAllowed": True, "quoteOrderQtyMarketAllowed": True,
            "allowTrailingStop": True, "cancelReplaceAllowed": True, "amendAllowed": True,
            "isSpotTradingAllowed": spot, "isMarginTradingAllowed": False, "filters": filters,
            "permissions": [], "permissionSets": [list(permissions)], "defaultSelfTradePreventionMode":
                "EXPIRE_MAKER", "allowedSelfTradePreventionModes": ["NONE", "EXPIRE_TAKER", "EXPIRE_MAKER",
                                                                   "EXPIRE_BOTH"]}


SYMBOLS = [
    # sym, base, quote, tick, step, minQty, maxQty, minNotional, start price, 5m vol
    ("BTCUSDT", "BTC", "USDT", "0.01000000", "0.00001000", "0.00001000", "9000.00000000", "5.00000000", 67250.0, 0.0016),
    ("ETHUSDT", "ETH", "USDT", "0.01000000", "0.00010000", "0.00010000", "9000.00000000", "5.00000000", 2480.0, 0.0021),
    ("SOLUSDT", "SOL", "USDT", "0.01000000", "0.00100000", "0.00100000", "90000.00000000", "5.00000000", 151.3, 0.0028),
    ("XRPUSDT", "XRP", "USDT", "0.00010000", "0.10000000", "0.10000000", "9222449.00000000", "5.00000000", 0.5321, 0.0026),
    ("PEPEUSDT", "PEPE", "USDT", "0.00000001", "1.00000000", "1.00000000", "92233720368.00000000", "5.00000000", 0.00000873, 0.0045),
    ("DOGEUSDT", "DOGE", "USDT", "0.00001000", "1.00000000", "1.00000000", "9000000.00000000", "1.00000000", 0.11853, 0.0030),
]


def exchange_info():
    syms = []
    for s in SYMBOLS:
        extra = {}
        if s[0] == "DOGEUSDT":
            extra = {"use_min_notional": True,           # the older MIN_NOTIONAL filter
                     "order_types": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"]}
        syms.append(symbol(*s[:8], **extra))
    syms.append(symbol("BTCUPUSDT", "BTCUP", "USDT", "0.00100000", "0.01000000", "0.01000000", "9000.00000000",
                       "5.00000000"))                    # a leveraged token: the scanner drops it
    syms.append(symbol("USDCUSDT", "USDC", "USDT", "0.00010000", "1.00000000", "1.00000000", "9000000.00000000",
                       "5.00000000"))                    # stable against stable: dropped
    syms.append(symbol("ETHBTC", "ETH", "BTC", "0.00001000", "0.00010000", "0.00010000", "9000.00000000",
                       "0.00010000"))                    # not a USDT pair
    syms.append(symbol("LUNAUSDT", "LUNA", "USDT", "0.00010000", "0.01000000", "0.01000000", "9000.00000000",
                       "5.00000000", status="BREAK"))    # not trading
    return {"timezone": "UTC", "serverTime": ms(datetime.datetime(2026, 10, 19, 1, 0)),
            "rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000},
                           {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 100},
                           {"rateLimitType": "ORDERS", "interval": "DAY", "intervalNum": 1, "limit": 200000},
                           {"rateLimitType": "RAW_REQUESTS", "interval": "MINUTE", "intervalNum": 5, "limit": 61000}],
            "exchangeFilters": [], "symbols": syms, "_synthetic": "see make_synthetic.py"}


def gz_json(name, doc):
    with gzip.GzipFile(os.path.join(HERE, name), "wb", mtime=0) as fh:
        fh.write(json.dumps(doc, separators=(",", ":")).encode("utf-8"))


def json_file(name, doc):
    with open(os.path.join(HERE, name), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")


def main():
    json_file("exchangeInfo.json", exchange_info())
    day0 = datetime.datetime(2026, 10, 19)

    # BTCUSDT 5m: 60 full UTC days before the example session, the session itself and a bit of the next.
    start = day0 - datetime.timedelta(days=60)
    n_hist = 60 * 288

    def make(seed):
        hist = walk(seed, 64200.0, start, n_hist, 300, 0.0016, 2)
        today = walk(seed + 100000, float(hist[-1][4]), day0, 288 + 24, 300, 0.0016, 2, drift=0.000008)
        return hist + today

    def has_orb(rows):
        i, _ = orb_breakout(rows[n_hist:], 3, 24)
        return i is not None and i >= 4

    seed, rows = pick_seed(19, make, has_orb)
    # one gap (an exchange maintenance window): the 2026-09-02 session misses an hour, so it is "short"
    gap_day = datetime.datetime(2026, 9, 2, 3, 0)
    rows = [r for r in rows if not (ms(gap_day) <= r[0] < ms(gap_day) + 3600 * 1000)]
    gz_json("klines-BTCUSDT-5m-60d.json.gz", rows)

    # the example session alone, plain JSON (entry, exit, alerts read it)
    sess = [r for r in rows if ms(day0) <= r[0] < ms(day0) + 86400 * 1000]
    json_file("klines-BTCUSDT-5m-%s.json" % DAY, sess)

    # a scan bundle: 5m klines of the session for every USDT symbol, and 1d klines for 120 days
    bundle5, bundle1d = {}, {}
    for k, s in enumerate(SYMBOLS):
        sym, places = s[0], max(0, len(s[3].rstrip("0").split(".")[1]) if "." in s[3].rstrip("0") else 0)
        if sym == "BTCUSDT":
            bundle5[sym] = sess
        else:
            bundle5[sym] = walk(seed + 7 * (k + 1), s[8], day0, 288, 300, s[9], places)
        bundle1d[sym] = walk(seed + 13 * (k + 1), s[8] * 0.9, day0 - datetime.timedelta(days=120), 121, 86400,
                             s[9] * 12, places, profile=False)
    # two clear daily setups, so the daily scan has something to find:
    # ETHUSDT closes 3 % over its 20-day closing high on the last full day (breakout_20d),
    # SOLUSDT falls 3 % a day for the last 14 full days (oversold)
    eth = bundle1d["ETHUSDT"]
    set_close(eth, len(eth) - 2, max(float(r[4]) for r in eth[-22:-2]) * 1.03, 2)
    sol = bundle1d["SOLUSDT"]
    for i in range(len(sol) - 15, len(sol) - 1):
        set_close(sol, i, float(sol[i - 1][4]) * 0.97, 2)
    gz_json("scan-5m-%s.json.gz" % DAY, bundle5)
    gz_json("scan-1d-%s.json.gz" % DAY, bundle1d)

    t24 = []
    for s in SYMBOLS + [("BTCUPUSDT",), ("USDCUSDT",), ("ETHBTC",), ("LUNAUSDT",)]:
        sym = s[0]
        rows5 = bundle5.get(sym)
        last = float(rows5[-1][4]) if rows5 else 1.0
        qv = sum(float(r[7]) for r in rows5) if rows5 else 1000.0
        if sym == "DOGEUSDT":
            qv = 900000.0                       # under the 5,000,000 USDT volume floor
        t24.append({"symbol": sym, "openPrice": rows5[0][1] if rows5 else "1.0", "highPrice": "0", "lowPrice": "0",
                    "lastPrice": fmt(last, 8), "volume": "0", "quoteVolume": fmt(qv, 8),
                    "openTime": ms(day0), "closeTime": ms(day0) + 86400000 - 1, "firstId": 1, "lastId": 2,
                    "count": 1000})
    json_file("ticker24hr-mini.json", t24)
    print("seed", seed, "rows", len(rows))


if __name__ == "__main__":
    main()
