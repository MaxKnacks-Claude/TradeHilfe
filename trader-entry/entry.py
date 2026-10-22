#!/usr/bin/env python3
"""Entry Timer for trader-entry: watches a setup candle by candle and says the
moment to get in. Standard library only. Long side only. It never places an order.

    python3 entry.py check BTCUSDT                         # opening-range breakout, 5-minute candles
    python3 entry.py check BTCUSDT --now "2026-10-19 02:00" --offline klines.json
    python3 entry.py check BTCUSDT --setup level --level 53100   # first close above a price
    python3 entry.py check BTCUSDT --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch BTCUSDT --every 15 --until 05:30   # one line per closed candle
    python3 entry.py watchlist                                 # every hit in trader/watchlist.json

Setups (long only):
  orb    opening-range breakout. The range is the high and low of the first
         opening_range_minutes of the session (config, default 15: the 00:00, 00:05
         and 00:10 five-minute candles when session_start_utc is 00:00). It triggers
         on the first COMPLETED candle after the range that CLOSES above the range
         high. A wick above the high is not a trigger.
  level  the first completed candle that closes above --level (for example a
         20-day high from trader-scanner).
  --min-volume-x X  the trigger candle also needs at least X times the average
         volume of the opening-range candles.

The session (trader/config.json): session_start_utc (00:00), opening_range_minutes
(15), entry_window_minutes (360). Crypto trades 24/7, so the "session" is your rule,
not the exchange's. A trigger's time is the trigger candle's CLOSE time (the 01:55
candle closes at 02:00 UTC) and its price is that close, exactly as Binance reports it.
A candle counts only once it has closed: now >= its open time + the interval.

Statuses: triggered; waiting (range still forming, or no close above the level
yet); expired (no trigger by the end of the entry window); no-data (Binance sent
no candles for the session).

watch wakes just after each candle closes, re-polls every --every seconds while
Binance has not posted it, prints one line per closed candle and stops on a
trigger (exit 0, writes trader/entry.json), on expiry or at --until (exit 1).
With --now or --offline it runs on a simulated clock and replays without sleeping.

Data: GET /api/v3/klines (weight 2) on the venue of your config (the testnet by
default; "market_data": "live" reads the live market). --offline PATH reads a saved
klines answer (.json or .json.gz) or PATH/<SYMBOL>.json when PATH is a folder.
Times are UTC; --now "2026-10-19 02:00" pins the clock.

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMMON = next((p for p in (os.path.join(_HERE, "common"), os.path.join(_HERE, os.pardir, "common"))
                if os.path.isfile(os.path.join(p, "binance_client.py"))), os.path.join(_HERE, os.pardir, "common"))
_COMMON = os.path.abspath(_COMMON)
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)
try:
    import binance_client as bc
    import trader_config
    import trading_session as tsess
except ImportError:                                            # pragma: no cover
    sys.exit("entry.py: the shared folder common/ is missing next to this skill (copy it too)")

PAUSE = 0.2      # seconds between symbols in watchlist
GRACE = 2        # seconds after a candle closes before watch asks for it
SETTLE = 30      # seconds a closed candle may wait for Binance's data to catch up
INTERVALS = {"1m": 60, "5m": 300}
EPOCH = datetime.datetime(1970, 1, 1)


class EntryError(Exception):
    pass


# ---------------------------------------------------------------- time

utc_now = tsess.utc_now
utc_of = tsess.utc_of
ts_of = tsess.ts_of
iso = tsess.iso


def parse_now(text):
    try:
        return tsess.parse_now(text)
    except tsess.SessionError as e:
        raise EntryError(str(e))


def parse_hhmm(text, what):
    try:
        return tsess.parse_hhmm(text, what)
    except tsess.SessionError as e:
        raise EntryError(str(e))


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def num(x):
    """A price for people: enough digits for BTC and for PEPE."""
    if x is None:
        return "-"
    x = float(x)
    if x >= 1000:
        return "{:,.2f}".format(x)
    if x >= 1:
        s = "%.4f" % x
        return s[:-2] if s.endswith("00") else s.rstrip("0")
    return ("%.10f" % x).rstrip("0")


def px(x):
    return None if x is None else float(x)


# ---------------------------------------------------------------- data

def parse_chart(text, sym):
    """Chart JSON (the Yahoo v8 shape klines_to_chart builds) -> {symbol, meta, bars, data_time}."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EntryError("the chart data for %s is not in the expected shape" % sym)
    if not res:
        raise EntryError("no chart data for %s" % sym)
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": float(at("volume", i) or 0)})
    return {"symbol": str(meta.get("symbol") or sym).upper(), "meta": meta, "bars": bars,
            "data_time": meta.get("regularMarketTime")}


def offline_file(path, sym):
    if os.path.isdir(path):
        for name in (sym + ".json", sym + ".json.gz"):
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def get_chart(sym, interval, now_utc, cfg, offline=None, cl=None, or_minutes=None):
    """Candles of the session around now_utc for one symbol, from Binance or a saved klines file."""
    sym = sym.upper()
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    now_ms = tsess.ms_of(now_utc) if now_utc is not None else None
    if offline:
        f = offline_file(offline, sym)
        try:
            rows = bc.load_json(f)
        except (OSError, ValueError) as e:
            raise EntryError("could not read %s: %s" % (f, e))
        if isinstance(rows, dict):
            if sym not in rows:
                raise EntryError("the bundle %s has no %s" % (f, sym))
            rows = rows[sym]
        if now_ms is not None:
            rows = [r for r in rows if int(r[0]) <= now_ms]
    else:
        w = tsess.current(now_utc, cfg)
        need = int((now_utc - w["start"]).total_seconds() // INTERVALS[interval]) + 2
        try:
            cl = cl or bc.client(cfg)
            rows = cl.klines(sym, interval, tsess.ms_of(w["start"]), now_ms, min(1000, max(need, 1)))
        except bc.BinanceError as e:
            raise EntryError(str(e))
    return parse_chart(bc.chart_text(rows, sym, interval, now_ms), sym)


# ---------------------------------------------------------------- the rule

def read_config(path):
    try:
        cfg = trader_config.load(path)
        tsess.settings(cfg)
    except (trader_config.ConfigFileError, tsess.SessionError) as e:
        raise EntryError(str(e))
    return cfg


def evaluate(chart, now_utc, setup="orb", or_minutes=None, interval="5m", level=None, min_volume_x=None, cfg=None):
    """Pure function: the setup's status at now_utc from one session's candles."""
    cfg = cfg or {}
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    w = tsess.current(now_utc, cfg)
    s = w["settings"]
    or_minutes = int(or_minutes or s["opening_range_minutes"])
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("the opening range (%d minutes) must be a whole number of %s candles" % (or_minutes, interval))
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    n_or = or_minutes * 60 // step
    start, end = w["start"], w["end"]
    or_end = start + datetime.timedelta(minutes=or_minutes)
    cutoff = w["entry_end"]
    res = {"symbol": chart["symbol"], "setup": setup, "side": "buy", "date": w["date"],
           "session_start": iso(start), "interval": interval, "or_minutes": or_minutes,
           "now": now_utc.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting", "phase": "", "reason": "",
           "opening_range": None, "level": None, "trigger": None, "last": None, "forming": None,
           "min_volume_x": min_volume_x, "entry_window_end": hm(cutoff), "_bars": []}
    dt = chart.get("data_time")
    res["data_time"] = utc_of(dt).strftime("%Y-%m-%d %H:%M:%S") if dt else None

    by_slot = {}
    for b in chart["bars"]:
        st = utc_of(b["t"])
        if not (start <= st < end) or (st - start).total_seconds() % step:
            continue
        slot = int((st - start).total_seconds()) // step
        by_slot[slot] = dict(b, et=st, end=st + datetime.timedelta(seconds=step), slot=slot)
    bars, pending = [], None
    now_ts = ts_of(now_utc)
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        end_ts = b["t"] + step
        if end_ts <= now_ts:
            if dt is None or dt >= end_ts or slot < top or now_ts >= end_ts + SETTLE:
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["et"]), "closes": hm(b["end"]), "price": px(b["c"])}
        elif b["et"] <= now_utc:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": hm(b["end"]), "price": px(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    or_bars = [b for b in bars if b["slot"] < n_or]
    avg_vol = (sum(b["v"] for b in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        res["opening_range"] = {"high": max(b["h"] for b in or_bars), "low": min(b["l"] for b in or_bars),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": now_utc >= or_end, "avg_volume": avg_vol}
    if setup == "orb":
        if now_utc < or_end:
            res.update(phase="forming", reason="%d of %d range candles closed; the range is set at %s UTC" % (
                len(or_bars), n_or, hm(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="Binance sent no candles for the opening range")
            return res
        lvl = max(b["h"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = lvl

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] > cutoff:
            continue
        if b["c"] > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": last["c"],
                       "vs_level_pct": round(100.0 * (last["c"] / lvl - 1), 2)}
    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open",
                   reason="the %s candle closed above %s" % (hm(trig["et"]), "the range high" if setup == "orb"
                                                             else "the level"))
        res["trigger"] = {"time": hm(trig["end"]) + " UTC", "close_time": iso(trig["end"]),
                          "bar_start": hm(trig["et"]), "bar_index": trig["slot"], "price": trig["c"],
                          "volume": trig["v"], "volume_x": round(vx, 2) if vx is not None else None,
                          "pct_over": round(100.0 * (trig["c"] / lvl - 1), 2)}
        res["minutes_since"] = int((now_utc - trig["end"]).total_seconds() // 60)
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="Binance sent no candles for this session yet")
        return res
    what = "close above the range high %s" % num(lvl) if setup == "orb" else "close above %s" % num(lvl)
    if now_utc >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-window",
                   reason="no %s by %s UTC, the end of the entry window (entry_window_minutes %d)" % (
                       what, hm(cutoff), s["entry_window_minutes"]))
        return res
    res["phase"] = "open"
    if last["c"] > lvl and last in thin:
        res["reason"] = "the %s candle closed above at %s but on %.2fx the range's volume, under your %.2fx" % (
            hm(last["et"]), num(last["c"]), last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close %s (%s candle), %s %s %s (%+.2f%%)" % (
            num(last["c"]), hm(last["et"]), num(abs(gap)), "under" if gap >= 0 else "over",
            "the range high %s" % num(lvl) if setup == "orb" else "the level %s" % num(lvl),
            100.0 * (last["c"] / lvl - 1))
    return res


def public(res):
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now_utc):
    """trader/entry.json for trader-exit."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "trader-entry", "symbol": res["symbol"], "setup": res["setup"], "side": "buy",
            "date": res["date"], "session_start": res.get("session_start"), "status": res["status"],
            "time": t.get("time"), "close_time": t.get("close_time"), "bar_start": t.get("bar_start"),
            "bar_index": t.get("bar_index"), "price": t.get("price"), "level": res.get("level"),
            "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "source": "binance", "checked_at": iso(now_utc), "reason": res.get("reason")}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"forming": "RANGE FORMING"}
SHORT = {"forming": "forming"}


def setup_name(res):
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    return "close above %s" % num(res["level"]) if res["level"] is not None else "close above a level"


def describe(res):
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    head = "%s  %s UTC  session %s  %s on %s candles (Binance)" % (
        res["symbol"], when.strftime("%Y-%m-%d %H:%M"), res["date"], setup_name(res), bars)
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["session_start"], "%Y-%m-%dT%H:%M:%SZ")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s UTC: high %s, low %s (%d of %d candles%s)." % (
            hm(start), hm(end), num(orr["high"]), num(orr["low"]), orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        out.append("TRIGGERED %s at %s: the %s candle (candle %d) closed above %s %s (%+.2f%%%s)." % (
            t["time"], num(t["price"]), t["bar_start"], t["bar_index"],
            "the range high" if res["setup"] == "orb" else "the level", num(res["level"]), t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is %s (%s candle). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], num(res["last"]["close"]), res["last"]["bar_start"]))
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you buy: /trader-exit "
                   "(exit.py plan --from trader/entry.json).")
    else:
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s candle is still forming (%s so far, closes %s UTC); it does not count until it "
                       "closes." % (f["bar_start"], num(f["price"]), f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup in this session. Nothing was placed.")
    return out


def bar_line(b, res):
    start = hm(b["et"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %s to %s" % (num(min(x["l"] for x in seen)), num(max(x["h"] for x in seen)))
        if b["slot"] == n_or - 1:
            note = "opening range set: high %s, low %s" % (num(orr["high"]), num(orr["low"]))
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed above %s (%+.2f%%). Get in now if you take it: %s, %s" % (
            num(lvl), t["pct_over"], t["time"], num(t["price"]))
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "above %s" % num(lvl)
        avg = orr.get("avg_volume")
        if res.get("min_volume_x") and avg:
            note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
    else:
        note = "%s under %s" % (num(lvl - b["c"]), num(lvl))
    return "%5s candle  closed %s  %s" % (start, num(b["c"]), note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a session without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=15, until=None, max_errors=5, on_trigger=None):
    """Poll candle by candle until a trigger (0), expiry or --until (1)."""
    step = INTERVALS[opts.get("interval", "5m")]
    printed, errors, said_wait, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        w = tsess.current(now, opts.get("cfg"))
        if now < w["start"] + datetime.timedelta(seconds=step):
            if not said_wait:
                emit("%s UTC  the session just started: watching from the first candle's close at %s." % (
                    hm(now), hm(w["start"] + datetime.timedelta(seconds=step))))
                said_wait = True
            next_fetch = w["start"] + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s UTC  could not read candles: %s" % (now.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired":
            emit("EXPIRED  %s." % res["reason"])
            return 1, res
        if until and now >= until:
            emit("Stopped at --until %s UTC without a trigger. %s." % (hm(until), res["reason"]))
            return 1, res
        cur_slot = int((now - w["start"]).total_seconds()) // step
        behind = (not bars) or bars[-1]["slot"] < cur_slot - 1
        if behind:
            wait = every
        else:
            nxt = w["start"] + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - now).total_seconds()
        for stop_at in (until, w["entry_end"]):
            if stop_at and stop_at > now:
                wait = min(wait, (stop_at - now).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg):
    if a.side in ("sell", "short"):
        raise EntryError("long only: no short entries (spot cannot short). Watch for a buy instead.")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg}


def _load(a, sym, now, cfg):
    """(chart, now). An offline file without --now is read as of its last candle's close."""
    if a.offline and now is None:
        ch = get_chart(sym, a.interval, None, cfg, offline=a.offline)
        return ch, (utc_of(ch["data_time"]) if ch.get("data_time") else utc_now())
    now = now or utc_now()
    return get_chart(sym, a.interval, now, cfg, a.offline), now


def cmd_check(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = a.symbol.upper()
    chart, now = _load(a, sym, parse_now(a.now) if a.now else None, cfg)
    res = evaluate(chart, now, **opts)
    if a.write:
        write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = a.symbol.upper()
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None, cfg)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, time.sleep
    cl = None if a.offline else bc.client(cfg)

    def provider(now):
        return get_chart(sym, a.interval, now, cfg, a.offline, cl)

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until")
        n = now_fn()
        until = datetime.datetime.combine(n.date(), t)
        if until < n:
            until += tsess.DAY

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until, on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /trader-scanner first (it writes the hits there)" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        sym = str(h["symbol"]).upper()
        o = dict(opts)
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                time.sleep(PAUSE)
            chart, when = _load(a, sym, now, cfg)
            rows.append((sym, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows], indent=2))
    else:
        print("%-10s  %-9s  %-9s  %-14s  %-14s  %-14s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST",
                                                               "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-10s  error: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print("%-10s  %-9s  %-9s  %-14s  %-14s  %-14s  %s" % (
                s, status[:9], t.get("time") or "-", num(t.get("price")) if t else "-",
                num(r.get("level")) if r.get("level") is not None else "-",
                num(last.get("close")) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <SYMBOL> --write, and /trader-exit." % (n, len(rows)))
        print("Binance candles, not every trade. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (trader-entry): watches a setup candle by candle.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, help="opening range length (default: config opening_range_minutes)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger candle volume vs the range candles' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
        p.add_argument("--now", help='pretend it is this UTC time, e.g. "2026-10-19 02:00"')
        p.add_argument("--offline", metavar="PATH", help="saved klines JSON (.json/.json.gz) or a folder")
        p.add_argument("--config", default=trader_config.DEFAULT_PATH)

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll candle by candle until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=15, help="seconds between polls while Binance is behind")
    p.add_argument("--until", help="stop at this UTC time (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except (EntryError, tsess.SessionError) as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
