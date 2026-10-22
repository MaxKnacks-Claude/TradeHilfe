#!/usr/bin/env python3
"""Time Machine for trader-timemachine. Standard library only.

Replays a planned day trade before you risk a dollar: 10,000 simulated runs of the
next max_hold_minutes from your entry, plus the same rule on every past session in
the data.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --hold 120               # replay 2 hours instead of the plan's max hold
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol BTCUSDT --entry 52730 --stop 52545.29 --target 53007.07 \\
        --entry-time "2026-10-19 02:00"
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader/timemachine.json

How the replays work (the original pack's algorithm, on a 24/7 market):
  1. Fetch history_days (config, default 60) of 5-minute candles from Binance
     (GET /api/v3/klines, 1,000 a request, about 18 requests) up to the entry.
     Sessions follow your config: session_start_utc + 24 hours = 288 candles. Keep
     only FULL sessions before the plan's session (a maintenance gap makes a
     session short; it is left out and named).
  2. The pool: for every slot 0..287 of the session, each past session's candle as
     ratios to the candle before it: (open, high, low, close) / previous close. The
     candle before slot 0 is the last candle of the session before (crypto has no
     overnight gap); slot 0 is skipped when that session is missing.
  3. One replay starts at the entry price and walks the next --hold minutes (the
     plan's max_hold_minutes, 240 by default = 48 candles). For candle i it picks a
     slot j = i + randint(-window, window), wrapped around the 24-hour clock, then
     one past candle from pool[j] (random.choice), and scales it to the current
     price. The stop is checked before the target in every candle, so a candle
     that touches both counts as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither" when
     the trade is still open at the max hold. "green" = target first, or above the
     entry at the max hold. R = the target's R at the target, -1 at the stop,
     otherwise (price at the max hold - entry) / risk. A gap through the stop still
     counts -1R here; a real fill can be worse. R after fees takes fee_rate off
     both sides of every replay.
  5. The price at the max hold: median and the 5th to 95th percentile of all replays
     (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a second run of
     2,000 paths with the same seed. The histogram has 24 bins between the 1st and
     99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same range
     length, stop rule and target R) re-run on every one of those past full sessions:
     first close above the range inside the entry window = entry, same stop-first
     rule, held up to the same max hold on the real candles that followed.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share that
breaks even AFTER FEES: (risk + fees at the stop) / (risk + fees at the stop +
reward - fees at the target). Without fees that is 1 / (1 + R): 50 % at 1R. The gap:
  less than 5 points    "Close to break-even"
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither, and the average R
after fees when its sign goes against the lean.

5-minute candles only. The run refuses with a plain message when there are fewer
than 20 full sessions or the entry candle is not in the data. Same plan, same data
and same --seed give the same numbers on any machine. A simulation built from the
coin's own recent candles is not a prediction: news, listings, hacks and liquidation
cascades can do what the last 60 days never did.

--offline FILE reads a saved klines answer (.json or .json.gz) instead of Binance.
Times are UTC; --now "2026-10-19 02:00" pins the clock.
"""

import argparse
import datetime
import json
import math
import os
import random
import re
import statistics
import sys

_COMMON = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "common"))
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)
try:
    import binance_client as bc
    import symbol_filters as sf
    import trader_config
    import trading_session as tsess
except ImportError:                                            # pragma: no cover
    sys.exit("timemachine.py: the shared folder common/ is missing next to this skill (copy it too)")

INTERVAL = 300       # 5-minute candles, in seconds
STEP = datetime.timedelta(seconds=INTERVAL)
SESSION_BARS = 288   # 24 hours of 5-minute candles
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05
STRONG = 0.15
MANY_NEITHER = 0.40


class TMError(Exception):
    pass


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def num(x):
    x = float(x)
    if x >= 1000:
        return "{:,.2f}".format(x)
    if x >= 1:
        s = "%.4f" % x
        return s[:-2] if s.endswith("00") else s.rstrip("0")
    return ("%.10f" % x).rstrip("0")


def parse_time(text, what="a time"):
    try:
        return tsess.parse_now(text)
    except tsess.SessionError:
        raise TMError("%s wants a UTC time like \"2026-10-19 02:00\", not %r" % (what, text))


# ---------------------------------------------------------------- data

def read_offline(path, sym):
    if os.path.isdir(path):
        for name in ("%s-5m-60d.json.gz" % sym, "%s-5m-60d.json" % sym, "klines-%s-5m-60d.json.gz" % sym):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-5m-60d.json(.gz) in %s" % (sym, path))
    if not os.path.exists(path):
        raise TMError("no offline klines file %s" % path)
    try:
        rows = bc.load_json(path)
    except (OSError, ValueError) as e:
        raise TMError("could not read %s: %s" % (path, e))
    if isinstance(rows, dict):
        rows = rows.get(sym) or []
    return rows


def fetch_rows(sym, start, end, cfg, live=False):
    try:
        cl = bc.client(cfg, live=live)
        return cl.klines_range(sym, "5m", tsess.ms_of(start), tsess.ms_of(end))
    except bc.BinanceError as e:
        raise TMError(str(e))


def parse_bars(rows, as_of, session_start):
    """Binance klines -> (bars by open time, dropped counts). A candle counts only when it
    closed by as_of (the entry) and sits on the 5-minute grid of the session."""
    dropped = {"off_grid": 0, "not_complete": 0}
    bars = {}
    for k in bc.kline_rows(rows):
        st = tsess.utc_of(k["open_time"] // 1000)
        if k["open_time"] % (INTERVAL * 1000) or (st - session_start).total_seconds() % INTERVAL:
            dropped["off_grid"] += 1
            continue
        if st + STEP > as_of:
            dropped["not_complete"] += 1
            continue
        bars[st] = {"t": st, "o": k["o"], "h": k["h"], "l": k["l"], "c": k["c"], "v": k["v"]}
    return bars, dropped


def sessions_of(bars, start_time, first_day, last_day):
    """{session date: [288 candles] or None if short}, for each session date in the range."""
    out, short = {}, []
    d = first_day
    while d <= last_day:
        s0 = datetime.datetime.combine(d, start_time)
        rows = [bars.get(s0 + STEP * i) for i in range(SESSION_BARS)]
        n = sum(1 for r in rows if r is not None)
        if n == SESSION_BARS:
            out[d.isoformat()] = rows
        elif n:
            short.append({"date": d.isoformat(), "bars": n})
        d += datetime.timedelta(days=1)
    return out, short


# ---------------------------------------------------------------- the simulation

def build_pool(sessions):
    """pool[i] = each past session's candle i as (o, h, l, c) / the previous candle's close."""
    pool = [[] for _ in range(SESSION_BARS)]
    dates = sorted(sessions)
    for n, d in enumerate(dates):
        D = sessions[d]
        prev_day = (datetime.date.fromisoformat(d) - datetime.timedelta(days=1)).isoformat()
        if prev_day in sessions:
            pc = sessions[prev_day][-1]["c"]
            pool[0].append((D[0]["o"] / pc, D[0]["h"] / pc, D[0]["l"] / pc, D[0]["c"] / pc))
        for i in range(1, SESSION_BARS):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    if not pool[0]:
        pool[0] = list(pool[1])
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def r_after_fees(exit_price, entry, stop, fee):
    risk = entry - stop
    return ((exit_price - entry) - fee * (entry + exit_price)) / risk


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, fee=0.0):
    """Monte Carlo replays of candles k+1..last (session slots, may run past 287: they wrap)."""
    risk = entry - stop
    r_target = round((target - entry) / risk, 4)

    def slot(rng, i):
        return i % SESSION_BARS if window == 0 else (i + rng.randint(-window, window)) % SESSION_BARS

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if lo <= stop:
                    outcome = "stop"        # both inside one candle: count the loss (conservative)
                elif hi >= target:
                    outcome = "target"
            p = cl
            if keep:
                path.append(p)
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes) if o == "target" or (o == "close" and p > entry))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else (p - entry) / risk
          for o, p in zip(outcomes, closes)]
    Rn = [r_after_fees(target if o == "target" else stop if o == "stop" else p, entry, stop, fee)
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([pct(col, q) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": pct(srt, 0.5), "p5_close": pct(srt, 0.05), "p95_close": pct(srt, 0.95),
        "avg_R": round(statistics.mean(Rs), 3), "avg_R_after_fees": round(statistics.mean(Rn), 3),
        "r_at_target": r_target, "fee_rate": fee, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": lo_, "hi": hi_, "bins": bins},
    }


# ---------------------------------------------------------------- the history replay

def rule_stop(rule, oh, ol, entry, tick):
    if rule["stop_rule"] == "or_mid":
        return float(sf.floor_tick((sf.D(oh) + sf.D(ol)) / 2, tick))
    if rule["stop_rule"] == "or_low":
        return float(sf.floor_tick(ol, tick))
    return float(sf.floor_tick(sf.D(entry) * (1 - sf.D(rule["stop_pct"]) / 100), tick))


def history_replay(sessions, bars, rule, start_time, hold_bars, tick, fee=0.0):
    """The plan's rule on every past full session, held on the real candles that followed."""
    n = rule["or_minutes"] // 5
    last_entry = rule["entry_window_minutes"] // 5        # candles that END inside the window
    trades = []
    for d in sorted(sessions):
        D = sessions[d]
        oh = max(b["h"] for b in D[:n])
        ol = min(b["l"] for b in D[:n])
        kk = next((i for i in range(n, last_entry) if D[i]["c"] > oh), None)
        if kk is None:
            continue
        e = D[kk]["c"]
        s = rule_stop(rule, oh, ol, e, tick)
        risk = e - s
        if risk <= 0:
            continue
        t = float(sf.round_tick(sf.D(e) + sf.D(rule["target_r"]) * sf.D(risk), tick))
        oc, cl = None, e
        t0 = D[kk]["t"]
        for j in range(1, hold_bars + 1):
            b = bars.get(t0 + STEP * j)
            if b is None:
                continue
            cl = b["c"]
            if b["l"] <= s:
                oc = "stop"
                break
            if b["h"] >= t:
                oc = "target"
                break
        ex = t if oc == "target" else s if oc == "stop" else cl
        R = (t - e) / risk if oc == "target" else -1.0 if oc == "stop" else (cl - e) / risk
        trades.append({"date": d, "time": hm(t0 + STEP) + " UTC", "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2),
                       "R_after_fees": round(r_after_fees(ex, e, s, fee), 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "avg_R_after_fees": round(statistics.mean(x["R_after_fees"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, cfg):
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": "buy", "entry": a.entry, "stop": a.stop, "target": a.target,
               "entry_time": a.entry_time, "setup": a.setup, "opening_range": {"minutes": a.or_minutes},
               "stop_rule": a.stop_rule, "interval": "5m", "tick_size": a.tick}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: run /trader-exit first, or pass --symbol --entry --stop --target "
                          "--entry-time" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        src = a.plan
    return normalize_plan(raw, cfg, src)


def normalize_plan(raw, cfg=None, src="flags"):
    sym = str(raw.get("symbol") or "").strip().upper()
    if not re.match(r"^[A-Z0-9]{2,20}$", sym):
        raise TMError("the plan needs a Binance symbol like BTCUSDT, got %r" % raw.get("symbol"))
    if str(raw.get("side") or "buy").lower() != "buy":
        raise TMError("spot is long only: the plan's side must be buy")
    if str(raw.get("interval") or "5m") != "5m":
        raise TMError("5-minute candles only (the plan says %s)" % raw.get("interval"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    if not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not raw.get("entry_time"):
        raise TMError("the plan needs entry_time (the entry candle's close, UTC)")
    et = parse_time(raw["entry_time"], "entry_time")
    if et.minute % 5 or et.second:
        raise TMError("entry_time %s is not on the 5-minute grid (use the candle's close)" % raw["entry_time"])
    s = tsess.settings(cfg or {})
    w = tsess.current(et - STEP, cfg)                     # the session of the entry CANDLE
    k = int((et - STEP - w["start"]).total_seconds() // INTERVAL)
    orng = raw.get("opening_range") or {}
    or_min = int((orng.get("minutes") if isinstance(orng, dict) else None) or s["opening_range_minutes"])
    if or_min < 5 or or_min % 5:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    hold = int(raw.get("max_hold_minutes") or s["max_hold_minutes"])
    risk = entry - stop
    return {"symbol": sym, "side": "buy", "date": w["date"], "session_start": tsess.iso(w["start"]),
            "setup": str(raw.get("setup") or "orb"), "entry": entry, "entry_time": tsess.iso(et), "entry_bar": k,
            "stop": stop, "target": target, "risk_per_unit": risk, "target_r": round((target - entry) / risk, 2),
            "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
            "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m", "max_hold_minutes": hold,
            "tick_size": str(raw.get("tick_size") or "0.01"), "from": src}


def resolve_rule(plan, entry_day, cfg):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule (or_mid,
    or_low, or 'pct' = the plan's stop distance as a percent of the entry) and its target R.
    'auto' works the stop rule out from the plan session's own opening range."""
    if plan["setup"].lower() != "orb":
        return None
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"], "stop_pct": round(100.0 * plan["risk_per_unit"] / plan["entry"], 4),
            "entry_window_minutes": tsess.settings(cfg)["entry_window_minutes"]}
    tick = plan["tick_size"]
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if entry_day and all(entry_day[i] is not None for i in range(n)):
            oh = max(entry_day[i]["h"] for i in range(n))
            ol = min(entry_day[i]["l"] for i in range(n))
            if abs(float(sf.floor_tick((sf.D(oh) + sf.D(ol)) / 2, tick)) - plan["stop"]) < 1e-9:
                rule["stop_rule"] = "or_mid"
            elif abs(float(sf.floor_tick(ol, tick)) - plan["stop"]) < 1e-9:
                rule["stop_rule"] = "or_low"
    elif rule["stop_rule"] not in ("or_mid", "or_low"):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the verdict

def lean(mc, plan):
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    e, s, t, fee = plan["entry"], plan["stop"], plan["target"], mc.get("fee_rate", 0.0)
    loss = (e - s) + fee * (e + s)
    win = (t - e) - fee * (e + t)
    even = loss / (loss + win) if (loss + win) > 0 else 1.0
    share = pt / (pt + ps)
    return share - even, share, even


def verdict(mc, plan):
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc, plan)
    hold = "%d minutes" % plan["max_hold_minutes"]
    if lv is None:
        return "No replay reached the target or the stop within %s: the price at the max hold decides every one." % hold
    d, share, even = lv
    if abs(d) < COIN_FLIP:
        head = "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    return ("%s: after fees the target has to come first in %.0f %% of the replays that hit one side; it did in "
            "%.0f %% (target first %.1f %% vs stop first %.1f %%)." % (head, 100 * even, 100 * share, 100 * pt,
                                                                       100 * ps))


def verdict_notes(mc, plan):
    notes = []
    lv = lean(mc, plan)
    if mc.get("p_neither", 0) > MANY_NEITHER and lv is not None:
        notes.append("%.1f %% of the replays hit neither within %d minutes; the price at the max hold decides those."
                     % (100 * mc["p_neither"], plan["max_hold_minutes"]))
    avg = mc.get("avg_R_after_fees")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R after fees is %+.3f all the same: the replays that hit neither pull it the other way."
                     % avg)
    return notes


# ---------------------------------------------------------------- the run

def time_machine(rows, plan, now_utc, cfg, replays=10000, seed=19, window=2, hold=None, source="binance"):
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 12:
        raise TMError("--window must be between 0 and 12 candles")
    s = tsess.settings(cfg)
    hold = int(hold or plan["max_hold_minutes"])
    if hold < 5 or hold % 5:
        raise TMError("--hold must be a multiple of 5 minutes")
    plan = dict(plan, max_hold_minutes=hold)
    et = tsess.parse_now(plan["entry_time"])
    as_of = min(now_utc, et)
    sess_start = tsess.parse_now(plan["session_start"])
    bars, dropped = parse_bars(rows, as_of, sess_start)
    if not bars:
        raise TMError("no complete 5-minute candles for %s up to the entry" % plan["symbol"])
    k = plan["entry_bar"]
    eb = bars.get(et - STEP)
    if eb is None:
        raise TMError("the entry is not in the data: no complete candle closing %s for %s"
                      % (tsess.label(et), plan["symbol"]))
    warn = []
    if not eb["l"] * 0.999 <= plan["entry"] <= eb["h"] * 1.001:
        warn.append("the plan's entry %s is outside that candle's range %s to %s"
                    % (num(plan["entry"]), num(eb["l"]), num(eb["h"])))
    first = min(bars).date() - datetime.timedelta(days=1)
    last_day = datetime.date.fromisoformat(plan["date"]) - datetime.timedelta(days=1)
    sessions, short = sessions_of(bars, s["start"], first, last_day)
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), plan["symbol"], plan["date"], MIN_SESSIONS))
    pool = build_pool(sessions)
    hold_bars = hold // 5
    last = k + hold_bars
    fee = float(cfg.get("fee_rate", 0.001))
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window, fee)
    dates = sorted(sessions)
    mc = dict(mc, hold_minutes=hold, exit_at=tsess.iso(et + STEP * hold_bars), pool_sessions=len(sessions),
              pool_from=dates[0], pool_to=dates[-1],
              band_times=[hm(et + STEP * j) for j in range(0, hold_bars + 1)])
    entry_day = [bars.get(sess_start + STEP * i) for i in range(SESSION_BARS)]
    rule = resolve_rule(plan, entry_day, cfg)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, bars, rule, s["start"], hold_bars, plan["tick_size"], fee)
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, this plan's "
                           "setup is %r" % plan["setup"]}
    last_bar = max(bars)
    return {
        "created_by": "trader-timemachine", "created_at": tsess.iso(now_utc), "source": source,
        "data": {"symbol": plan["symbol"], "interval": "5m", "last_candle": tsess.label(last_bar),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out, "mc": mc, "history": history,
        "verdict": verdict(mc, plan), "verdict_notes": verdict_notes(mc, plan), "warnings": warn,
        "note": ("A simulation built from %s's own last %d full sessions. It is not a prediction: news, listings, "
                 "hacks and liquidation cascades can do things those sessions never did. Same seed, same numbers."
                 % (plan["symbol"], len(sessions))),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    exit_at = m["exit_at"][11:16] + " UTC"
    lines = [
        "%s buy, session %s: entry %s at %s (candle %d), stop %s, target %s (%gR, risk %s a unit)."
        % (p["symbol"], p["date"], num(p["entry"]), p["entry_time"][11:16] + " UTC", p["entry_bar"], num(p["stop"]),
           num(p["target"]), p["target_r"], num(p["risk_per_unit"])),
        "%s replays of the next %d minutes, to %s (%d five-minute candles), seed %d."
        % ("{:,}".format(m["replays"]), m["hold_minutes"], exit_at, m["bars_left"], m["seed"]),
        "Each candle is drawn whole from %s's own 5-minute candles at the same time of day (+-%d candles) over its "
        "last %d full sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"],
                                               m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at the max hold (%s)" % (pc(m["p_neither"]), exit_at),
        "  green          %-8s target first, or above the entry at the max hold" % pc(m["p_green"]),
        "",
        "  Price at %s: median %s, 90 %% of replays between %s and %s."
        % (exit_at, num(m["median_close"]), num(m["p5_close"]), num(m["p95_close"])),
        "  Average R: %+.3f before fees, %+.3f after %.2f %% fees a side (R = the %s risk: +%gR at the target, "
        "-1R at the stop)." % (m["avg_R"], m["avg_R_after_fees"], 100 * m["fee_rate"], num(p["risk_per_unit"]),
                               m["r_at_target"]),
        "",
    ]
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low"}.get(
            ru["stop_rule"], "%.2f %% under the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target, held up to %d "
                         "minutes) on each of those %d real sessions fired %d times: %d target, %d stop, %d neither. "
                         "Average R %+.3f, after fees %+.3f."
                         % (ru["or_minutes"], stop_words, ru["target_r"], m["hold_minutes"], h["sessions"],
                            h["triggered"], h["target"], h["stop"], h["close"], h["avg_R"], h["avg_R_after_fees"]))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (missing candles): %s."
                     % (len(doc["data"]["short_sessions_left_out"]),
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or hi * 0.0001
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"][11:16] + " UTC",
        m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%12s |" % num(price)) if (label or r in (0, height - 1)) else "             |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("             +" + "-" * len(bands))
    out.append("              " + "".join(axis) + "  UTC")
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")


def compute(a):
    try:
        cfg = trader_config.load(a.config)
        tsess.settings(cfg)
    except (trader_config.ConfigFileError, tsess.SessionError) as e:
        raise TMError(str(e))
    now = parse_time(a.now, "--now") if a.now else tsess.utc_now()
    plan = load_plan(a, cfg)
    et = tsess.parse_now(plan["entry_time"])
    if a.offline:
        rows, source = read_offline(a.offline, plan["symbol"]), "offline"
    else:
        days = int(cfg.get("history_days") or 60)
        start = tsess.parse_now(plan["session_start"]) - datetime.timedelta(days=days + 1)
        rows, source = fetch_rows(plan["symbol"], start, et, cfg, getattr(a, "live", False)), "binance"
    return time_machine(rows, plan, now, cfg, a.replays, a.seed, a.window, a.hold, source)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /trader-budget sizes it to your budget and your max loss per trade, fees included.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned day trade (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="the plan from trader-exit")
        p.add_argument("--symbol", help="or give the plan as flags: symbol")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help='the entry candle\'s close, UTC, e.g. "2026-10-19 02:00"')
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "pct"])
        p.add_argument("--tick", default="0.01", help="tick size for the history replay (with --symbol)")
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many candles")
        p.add_argument("--hold", type=int, help="minutes to replay (default: the plan's max_hold_minutes)")
        p.add_argument("--config", default=trader_config.DEFAULT_PATH)
        p.add_argument("--live", action="store_true", help="read the live API (needs \"testnet\": false)")
        p.add_argument("--offline", metavar="FILE", help="a saved klines answer (.json or .json.gz), no network")
        p.add_argument("--now", help='pretend it is this UTC time, e.g. "2026-10-19 02:00"')

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated runs plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except (TMError, tsess.SessionError, bc.BinanceError) as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
