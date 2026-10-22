#!/usr/bin/env python3
"""Records REAL Binance answers to replace the synthetic fixtures in tests/fixtures/binance/.

Public market data only: no key, no order, nothing signed. Run it on a machine that can reach
Binance (the build container could not), from the repository root:

    python3 tests/record_fixtures.py --out tests/fixtures/binance-recorded
    python3 tests/record_fixtures.py --base https://data-api.binance.vision --day 2026-10-19

It writes the same file names as make_synthetic.py. The tests pin numbers that come from the
synthetic data (the BTCUSDT breakout at 02:00 UTC, the time machine's odds); after swapping
in recorded data, run the suite and update those expectations where it says so. The unittest
run itself never touches the network.
"""

import argparse
import datetime
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "PEPEUSDT", "DOGEUSDT"]
EXTRA_INFO = ["BTCUPUSDT", "USDCUSDT", "ETHBTC", "LUNAUSDT"]


def get(base, path, params):
    url = base + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "day-trader-skill fixture recorder"})
    with urllib.request.urlopen(req, timeout=30) as r:
        used = r.headers.get("X-MBX-USED-WEIGHT-1M")
        body = json.loads(r.read().decode("utf-8"))
    if used and int(used) > 3000:
        time.sleep(61 - time.time() % 60)
    return body


def klines(base, sym, interval, start_ms, end_ms):
    out, cur = [], start_ms
    while cur <= end_ms:
        rows = get(base, "/api/v3/klines", {"symbol": sym, "interval": interval, "startTime": cur,
                                            "endTime": end_ms, "limit": 1000})
        if not rows:
            break
        out.extend(rows)
        cur = rows[-1][0] + 1
        if len(rows) < 1000:
            break
        time.sleep(0.2)
    return out


def ms(dt):
    return int((dt - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="https://api.binance.com", help="or https://data-api.binance.vision")
    ap.add_argument("--day", default="2026-10-19", help="the example session (UTC date)")
    ap.add_argument("--out", default="tests/fixtures/binance-recorded")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    day = datetime.datetime.strptime(a.day, "%Y-%m-%d")
    info = get(a.base, "/api/v3/exchangeInfo", {"symbols": json.dumps(SYMBOLS + EXTRA_INFO, separators=(",", ":"))})
    json.dump(info, open(os.path.join(a.out, "exchangeInfo.json"), "w"), indent=1)
    hist = klines(a.base, "BTCUSDT", "5m", ms(day - datetime.timedelta(days=60)), ms(day + datetime.timedelta(hours=26)))
    with gzip.open(os.path.join(a.out, "klines-BTCUSDT-5m-60d.json.gz"), "wt") as fh:
        json.dump(hist, fh)
    sess = [r for r in hist if ms(day) <= r[0] < ms(day + datetime.timedelta(days=1))]
    json.dump(sess, open(os.path.join(a.out, "klines-BTCUSDT-5m-%s.json" % a.day), "w"))
    b5, b1 = {}, {}
    for s in SYMBOLS:
        b5[s] = klines(a.base, s, "5m", ms(day), ms(day + datetime.timedelta(days=1)) - 1)
        b1[s] = klines(a.base, s, "1d", ms(day - datetime.timedelta(days=120)), ms(day))
    with gzip.open(os.path.join(a.out, "scan-5m-%s.json.gz" % a.day), "wt") as fh:
        json.dump(b5, fh)
    with gzip.open(os.path.join(a.out, "scan-1d-%s.json.gz" % a.day), "wt") as fh:
        json.dump(b1, fh)
    t24 = get(a.base, "/api/v3/ticker/24hr", {"symbols": json.dumps(SYMBOLS, separators=(",", ":")), "type": "MINI"})
    json.dump(t24, open(os.path.join(a.out, "ticker24hr-mini.json"), "w"), indent=1)
    print("Recorded %d BTCUSDT 5m candles, %d symbols, into %s." % (len(hist), len(SYMBOLS), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
