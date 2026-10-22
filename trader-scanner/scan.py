#!/usr/bin/env python3
"""Chart Scanner for trader-scanner: scans Binance spot charts for setups. Standard library only.

    python3 scan.py universe                          # every TRADING USDT spot pair -> trader/universe.txt
    python3 scan.py daily                             # daily setups on 1-day candles -> trader/watchlist.json
    python3 scan.py daily --symbols BTCUSDT,ETHUSDT   # or --watchlist FILE, or --limit 50
    python3 scan.py orb                               # opening-range breakouts in the current session
    python3 scan.py orb --at 02:00                    # cut the clock at 02:00 UTC
    python3 scan.py orb --offline scan-5m.json.gz --now "2026-10-19 02:00"

universe  Binance exchangeInfo (permissions SPOT, status TRADING), kept when, in order:
            1. the quote asset is "quote_asset" in the config (USDT) and spot trading is allowed
            2. not a leveraged token (BTCUP, ETHBEAR ...) when exclude_leveraged_tokens is true
            3. not a stablecoin against a stablecoin (USDCUSDT) when exclude_stable_pairs is true
            4. not on your "blocked" list
            5. 24h quote volume (ticker/24hr) at least --min-quote-volume
               (config min_quote_volume_24h, 5,000,000 USDT; 0 switches it off)
          Writes one SYMBOL|BASE/QUOTE|24h quote volume a line, most traded first.

daily     1-day candles (UTC days, the last 60), one request each. Only COMPLETE days count
          (today's candle is dropped until it closes). Liquidity: the last full day's quote
          volume >= --min-quote-volume. Setups, from closes, as in the original pack:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 days before the last 5, then the
                             last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 the day before and the last day closed up

orb       Opening-range breakouts on 5-minute candles in the configured session
          (session_start_utc, opening_range_minutes). Range high = the highest high of
          the range candles; a hit = the first COMPLETED candle after the range that CLOSES
          above it, at that candle's close time. One request per symbol (klines carry real
          highs and lows, no second stage). A chart is usable only when every 5-minute candle
          from the session start to the cutoff is there. After entry_window_minutes (an
          expired setup) it prints one plain line and writes nothing.

Rate limits: one klines request weighs 2 (of 6,000 a minute). The shared client reads
X-MBX-USED-WEIGHT-1M and pauses at 70 %, waits out a 429 and stops on a 418.
Partial results are said as partial. Times are UTC; --now "2026-10-19 02:00" pins the clock.

--offline PATH reads saved Binance answers instead of the network: universe wants a folder
with exchangeInfo.json and ticker24hr-mini.json; daily and orb take a bundle file
{"BTCUSDT": [kline arrays], ...} (.json or .json.gz) or a folder of <SYMBOL>.json files.

A scan is a list of charts that match a rule, not a list of coins to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

_COMMON = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "common"))
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)
try:
    import binance_client as bc
    import trader_config
    import trading_session as tsess
except ImportError:                                            # pragma: no cover
    sys.exit("scan.py: the shared folder common/ is missing next to this skill (copy it too)")

BAR_MIN = 5
DAILY_BARS = 60
MIN_DAILY_BARS = 30
DEFAULT_UNIVERSE = "trader/universe.txt"
NOTE = "A scan is a list of charts that match a rule, not a list of coins to buy."
SLEEP = time.sleep  # tests swap this out

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time

def parse_now(text):
    try:
        return tsess.parse_now(text)
    except tsess.SessionError as e:
        raise ScanError(str(e))


def parse_hhmm(text, flag="--at"):
    try:
        return tsess.parse_hhmm(text, flag)
    except tsess.SessionError as e:
        raise ScanError(str(e))


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def utc_of_ms(ms):
    return tsess.EPOCH + datetime.timedelta(milliseconds=int(ms))


# ---------------------------------------------------------------- data

def series_of(rows):
    """Binance kline arrays -> the column series this scanner works on (times = open, UTC)."""
    ks = sorted(bc.kline_rows(rows), key=lambda k: k["open_time"])
    return {"t": [k["open_time"] // 1000 for k in ks], "o": [k["o"] for k in ks], "h": [k["h"] for k in ks],
            "l": [k["l"] for k in ks], "c": [k["c"] for k in ks], "v": [k["v"] for k in ks],
            "qv": [k["quote_volume"] for k in ks], "close_ms": [k["close_time"] for k in ks]}


def load_offline(path):
    """A bundle file {SYM: klines} or a folder of <SYM>.json(.gz) -> {SYM: series}."""
    data = {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            doc = bc.load_json(os.path.join(path, f))
            if isinstance(doc, list):
                data[re.sub(r"\.json(\.gz)?$", "", f).upper()] = series_of(doc)
            elif isinstance(doc, dict):
                for k, v in doc.items():
                    if isinstance(v, list):
                        data[k.upper()] = series_of(v)
    elif os.path.isfile(path):
        try:
            doc = bc.load_json(path)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        if not isinstance(doc, dict):
            raise ScanError("offline file %s is not a bundle {SYMBOL: [klines]}" % path)
        for k, v in doc.items():
            if isinstance(v, list):
                data[k.upper()] = series_of(v)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data


def fetch_klines(cl, symbols, interval, start_ms=None, end_ms=None, limit=1000, a=None):
    """One klines request a symbol. -> ({SYM: series}, {SYM: error}). Stops on a ban."""
    data, failed = {}, {}
    for k, sym in enumerate(symbols):
        try:
            rows = cl.klines(sym, interval, start_ms, end_ms, limit)
        except bc.BannedError as e:
            for s in symbols[k:]:
                failed[s] = str(e)
            break
        except bc.BinanceError as e:
            failed[sym] = str(e)
            continue
        if rows:
            data[sym] = series_of(rows)
        else:
            failed[sym] = "no candles from Binance"
        if a is not None and len(symbols) > 50 and (k + 1) % 50 == 0:
            progress(a, "  %d of %d charts (weight used this minute: %d)" % (k + 1, len(symbols), cl.used_weight))
    return data, failed


# ---------------------------------------------------------------- universe

def build_universe(info, tickers, cfg, min_quote_volume):
    """exchangeInfo + 24h tickers -> (kept [(SYM, base, quote, quote_volume)], counts)."""
    quote = cfg.get("quote_asset", "USDT").upper()
    blocked = {str(b).upper() for b in cfg.get("blocked") or []}
    vol = {}
    for t in tickers or []:
        try:
            vol[str(t.get("symbol", "")).upper()] = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            pass
    counts, kept = {"symbols": 0}, []
    for s in (info or {}).get("symbols") or []:
        counts["symbols"] += 1
        sym = str(s.get("symbol", "")).upper()
        why = None
        if s.get("status") != "TRADING":
            why = "not trading"
        elif str(s.get("quoteAsset", "")).upper() != quote:
            why = "other quote asset"
        elif not s.get("isSpotTradingAllowed", False):
            why = "no spot trading"
        elif cfg.get("exclude_leveraged_tokens", True) and trader_config.is_leveraged_token(s.get("baseAsset")):
            why = "leveraged token"
        elif cfg.get("exclude_stable_pairs", True) and trader_config.is_stable_pair(s.get("baseAsset"), quote):
            why = "stablecoin pair"
        elif sym in blocked:
            why = "blocked"
        elif min_quote_volume and vol.get(sym, 0.0) < min_quote_volume:
            why = "under the volume floor"
        if why:
            counts[why] = counts.get(why, 0) + 1
            continue
        kept.append((sym, s.get("baseAsset"), quote, vol.get(sym)))
    kept.sort(key=lambda x: (-(x[3] or 0), x[0]))
    counts["kept"] = len(kept)
    return kept, counts


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_bars(series, now_utc):
    """[(date, close, quote_volume)] of COMPLETE 1-day candles (closed by now_utc)."""
    now_ms = tsess.ms_of(now_utc)
    out = {}
    for i, t in enumerate(series["t"]):
        if series["close_ms"][i] >= now_ms:
            continue                      # today's candle is still open
        d = tsess.utc_of(t).date()
        out[d] = (float(series["c"][i]), float(series["qv"][i]))
    return [(d, c, q) for d, (c, q) in sorted(out.items())]


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_quote_volume):
    """Pure. data = {SYM: series of 1d candles}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_bars(s, now_utc)
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        qv = bars[-1][2]
        if min_quote_volume and qv < min_quote_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c, _ in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "setups": tags, "price": closes[-1], "close_date": d_last.isoformat(),
                         "rsi": round(nums["rsi"], 1), "quote_volume": round(qv, 2)})
    hits.sort(key=lambda h: (-h["quote_volume"], h["symbol"]))   # most traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def session_bars(series, start, cutoff):
    """5-minute candles from the session start that are complete at the cutoff, one per slot."""
    out = {}
    for i, ts in enumerate(series["t"]):
        st = tsess.utc_of(ts)
        if st < start or st >= start + tsess.DAY:
            continue
        if (st - start).total_seconds() % (BAR_MIN * 60):
            continue
        if st + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue                     # still forming at the cutoff
        out[st] = {"start": st, "o": float(series["o"][i]), "h": float(series["h"][i]),
                   "l": float(series["l"][i]), "c": float(series["c"][i]), "v": float(series["v"][i]),
                   "qv": float(series["qv"][i])}
    return [out[k] for k in sorted(out)]


def usable(bars, start, cutoff, n_range):
    """Every 5-minute slot from the session start to the cutoff is there, and at least the range."""
    want = int((cutoff - start).total_seconds() // (BAR_MIN * 60))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == start + datetime.timedelta(minutes=BAR_MIN * k) for k, b in enumerate(bars))


def orb_hit(bars, n_range):
    """The first completed candle after the range to CLOSE above the range high."""
    rng = bars[:n_range]
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": b["c"], "at": hm(closes_at) + " UTC", "close_time": tsess.iso(closes_at),
                    "bar_start": hm(b["start"]), "bar_index": k, "or_high": oh, "or_low": ol,
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, cfg):
    """-> (session window, cutoff) or raise ScanError with the plain message."""
    w = tsess.current(now_utc, cfg)
    or_min = w["settings"]["opening_range_minutes"]
    if or_min % BAR_MIN:
        raise ScanError("opening_range_minutes must be a multiple of 5 for 5-minute candles (got %d)" % or_min)
    if at is not None:
        cutoff = datetime.datetime.combine(w["start"].date(), at)
        if cutoff < w["start"]:
            cutoff += tsess.DAY
        if cutoff > now_utc + datetime.timedelta(seconds=59):
            raise ScanError("--at %s UTC is later than now (%s)." % (hm(at), tsess.label(now_utc)))
    else:
        cutoff = now_utc.replace(second=0, microsecond=0)
    first_close = w["range_end"] + datetime.timedelta(minutes=BAR_MIN)
    if cutoff < first_close:
        raise ScanError("It is %s: the %d-minute opening range of the session that started %s ends at %s and "
                        "the first candle after it closes at %s. Run orb again after that."
                        % (tsess.label(cutoff), or_min, tsess.label(w["start"]), hm(w["range_end"]),
                           hm(first_close)))
    if cutoff > w["entry_end"]:
        raise ScanError("It is %s, after %s, the end of your entry window (entry_window_minutes %d after the "
                        "%s UTC session start): a breakout found now is expired. Run orb in the next session, or "
                        "replay this one with --at." % (tsess.label(cutoff), hm(w["entry_end"]),
                                                       w["settings"]["entry_window_minutes"],
                                                       w["settings"]["session_start_utc"]))
    return w, cutoff


def scan_orb(data, symbols, start, cutoff, or_minutes, min_quote_volume=0):
    """data = {SYM: series of 5-minute candles}. Returns (hits, counts)."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0}
    hits = []
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, start, cutoff)
        if not usable(bars, start, cutoff, n_range):
            counts["unusable"] += 1
            continue
        qv = sum(b["qv"] for b in bars)
        if min_quote_volume and qv < min_quote_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        h = orb_hit(bars, n_range)
        if h:
            hits.append(dict({"symbol": sym, "setups": ["orb"], "quote_volume_session": round(qv, 2)}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts


# ---------------------------------------------------------------- helpers

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def read_symbols(a, cfg, offline_syms=None):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if os.path.exists(path):
            syms = symbols_from_file(path)
        elif cfg.get("symbols"):
            syms = [str(s).upper() for s in cfg["symbols"]]
        else:
            raise ScanError("no %s yet and no \"symbols\" in the config: run `scan.py universe` first, or pass "
                            "--symbols" % path)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def load_cfg(path):
    try:
        return trader_config.load(path)
    except trader_config.ConfigFileError as e:
        raise ScanError(str(e))


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def num(x):
    """A price for people: enough digits for BTC and for PEPE."""
    x = float(x)
    if x >= 1000:
        return "{:,.2f}".format(x)
    if x >= 1:
        s = "%.4f" % x
        return s[:-2] if s.endswith("00") else s.rstrip("0")
    return ("%.10f" % x).rstrip("0")


def big(x):
    return "{:,.0f}".format(float(x or 0))


def market_client(a, cfg):
    return bc.client(cfg, live=getattr(a, "live", False), sleep=SLEEP)


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    cfg = load_cfg(a.config)
    minq = cfg.get("min_quote_volume_24h", 0) if a.min_quote_volume is None else a.min_quote_volume
    if a.offline:
        info = bc.load_json(os.path.join(a.offline, "exchangeInfo.json"))
        tp = os.path.join(a.offline, "ticker24hr-mini.json")
        tickers = bc.load_json(tp) if os.path.exists(tp) else []
        venue = "offline"
    else:
        cl = market_client(a, cfg)
        try:
            info = cl.exchange_info(permissions="SPOT", status="TRADING")
            tickers = cl.ticker_24hr(mini=True) if minq else []
        except bc.BinanceError as e:
            raise ScanError(str(e))
        venue = "Binance " + ("testnet" if cl.testnet else "live")
    kept, counts = build_universe(info, tickers, cfg, minq)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, base, quote, qv in kept:
                fh.write("%s|%s/%s|%s\n" % (sym, base, quote, "" if qv is None else "%.0f" % qv))
    dropped = {k: v for k, v in counts.items() if k not in ("kept", "symbols")}
    if a.json:
        print(json.dumps({"count": len(kept), "symbols": counts["symbols"], "dropped": dropped,
                          "source": venue, "min_quote_volume": minq,
                          "out": None if a.no_write else a.out}, indent=2))
    else:
        print("%s exchange information: %s symbols." % (venue, big(counts["symbols"])))
        print("Kept %d %s spot pairs%s." % (len(kept), cfg["quote_asset"],
                                            (" with at least %s %s traded in 24 hours" % (big(minq), cfg["quote_asset"]))
                                            if minq else ""))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % (v, k) for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|BASE/QUOTE|24h volume a line, most traded first)." % a.out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    cfg = load_cfg(a.config)
    now = parse_now(a.now)
    minq = cfg.get("min_quote_volume_24h", 0) if a.min_quote_volume is None else a.min_quote_volume
    offline_syms = None
    if a.offline:
        data = load_offline(a.offline)
        failed = {}
        offline_syms = sorted(data)
    symbols = read_symbols(a, cfg, offline_syms)
    if not a.offline:
        cl = market_client(a, cfg)
        progress(a, "Fetching 1-day candles for %d symbols (%d requests, weight %d)..."
                 % (len(symbols), len(symbols), 2 * len(symbols)))
        data, failed = fetch_klines(cl, symbols, "1d", None, tsess.ms_of(now), DAILY_BARS + 1, a)
    hits, counts, last_date = scan_daily(data, symbols, now, minq)
    nfailed = len(symbols) - counts["fetched"]
    doc = {"created_by": "trader-scanner", "created_at": tsess.iso(now), "mode": "daily",
           "as_of": ("%s UTC close" % last_date.isoformat()) if last_date else None, "universe": len(symbols),
           "scanned": counts["scanned"], "failed": nfailed, "source": "binance",
           "filters": {"min_quote_volume": minq}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Daily scan, %s, 1-day candles through %s (UTC days)." % (tsess.label(now),
                                                                  last_date.isoformat() if last_date else "nothing"))
    print("Fetched %d of %d charts%s. %d scanned%s%s." % (
        counts["fetched"], len(symbols), (" (%d failed or had no data)" % nfailed) if nfailed else "",
        counts["scanned"], (", %d under the %s %s volume floor" % (counts["illiquid"], big(minq), cfg["quote_asset"]))
        if counts["illiquid"] else "", (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS))
        if counts["short"] else ""))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (24h quote volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-10s %14s  RSI %5.1f  %s" % (h["symbol"], num(h["price"]), h["rsi"],
                                               ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-entry SYMBOL.")
    return 0 if hits else 1


def cmd_orb(a):
    t0 = time.time()
    cfg = load_cfg(a.config)
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    w, cutoff = orb_window(now, at, cfg)
    or_min = w["settings"]["opening_range_minutes"]
    offline_syms = None
    if a.offline:
        data = load_offline(a.offline)
        failed = {}
        offline_syms = sorted(data)
    symbols = read_symbols(a, cfg, offline_syms)
    if not a.offline:
        cl = market_client(a, cfg)
        need = int((cutoff - w["start"]).total_seconds() // (BAR_MIN * 60)) + 1
        progress(a, "5-minute candles of the session for %d symbols (%d requests, weight %d)..."
                 % (len(symbols), len(symbols), 2 * len(symbols)))
        data, failed = fetch_klines(cl, symbols, "%dm" % BAR_MIN, tsess.ms_of(w["start"]), tsess.ms_of(cutoff),
                                    min(1000, need), a)
    hits, counts = scan_orb(data, symbols, w["start"], cutoff, or_min, a.min_quote_volume or 0)
    nfailed = len(symbols) - counts["fetched"]
    doc = {"created_by": "trader-scanner", "created_at": tsess.iso(now), "mode": "orb",
           "as_of": "%s UTC" % cutoff.strftime("%Y-%m-%d %H:%M"), "session": w["date"],
           "session_start": tsess.iso(w["start"]), "universe": len(symbols), "scanned": counts["scanned"],
           "failed": nfailed, "source": "binance", "or_minutes": or_min, "interval": "%dm" % BAR_MIN,
           "filters": {"min_quote_volume_session": a.min_quote_volume or 0}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Opening-range breakout scan, session %s (from %s), cut at %s: %d-minute range (%s to %s UTC), "
          "5-minute candles, Binance." % (w["date"], hm(w["start"]), tsess.label(cutoff), or_min,
                                         hm(w["start"]), hm(w["range_end"])))
    print("Fetched %d of %d charts%s; %d usable%s." % (
        counts["fetched"], len(symbols), (" (%d failed or had no data)" % nfailed) if nfailed else "",
        counts["scanned"], (" (%d under the volume floor)" % counts["illiquid"]) if counts["illiquid"] else ""))
    if counts["unusable"]:
        print("%d chart%s had a missing candle (a halt, maintenance or no trades) and were left out."
              % (counts["unusable"], "" if counts["unusable"] == 1 else "s"))
    if hits:
        print("%d of %d charts closed above their opening-range high by %s UTC:"
              % (len(hits), counts["scanned"], hm(cutoff)))
        for h in hits[:a.show]:
            print("  %9s  %-10s %14s  range %s to %s  %+.2f%% over" % (
                h["at"], h["symbol"], num(h["price"]), num(h["or_low"]), num(h["or_high"]), h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s UTC." % hm(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-entry SYMBOL.")
    return 0 if hits else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (trader-scanner): Binance spot setups and "
                                             "opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="every TRADING spot pair in your quote asset -> trader/universe.txt")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--offline", metavar="DIR", help="a folder with exchangeInfo.json and ticker24hr-mini.json")
    p.add_argument("--min-quote-volume", type=float, help="24h quote volume floor (config min_quote_volume_24h)")
    p.add_argument("--config", default=trader_config.DEFAULT_PATH)
    p.add_argument("--live", action="store_true", help="read the live API (needs \"testnet\": false)")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")

    def common(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated symbols instead of the universe")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of symbols")
        p.add_argument("--universe", help="symbol list, one SYMBOL|... a line (default %s, else config symbols)"
                       % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--offline", metavar="PATH", help="a bundle {SYMBOL: [klines]} or a folder, no network")
        p.add_argument("--now", help='pretend it is this UTC time, e.g. "2026-10-19 02:00"')
        p.add_argument("--config", default=trader_config.DEFAULT_PATH)
        p.add_argument("--live", action="store_true", help="read the live API (needs \"testnet\": false)")
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="setups on complete 1-day candles")
    common(p)
    p.add_argument("--min-quote-volume", type=float, help="last day's quote volume floor (config "
                                                          "min_quote_volume_24h)")
    p = sub.add_parser("orb", help="opening-range breakouts in the current session")
    common(p)
    p.add_argument("--at", help="cut the clock at this UTC time, HH:MM")
    p.add_argument("--min-quote-volume", type=float, default=0, help="session quote volume floor (default 0)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except (ScanError, bc.BinanceError, tsess.SessionError) as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
