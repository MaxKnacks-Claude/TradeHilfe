#!/usr/bin/env python3
"""Exit Planner for trader-exit: sets the stop, the target and the latest exit time
BEFORE you enter. Standard library only. Long side only. It never places an order.

    python3 exit.py plan                                     # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1.5R --write # -> trader/plan.json
    python3 exit.py plan --symbol BTCUSDT --entry 52730 --or-high 52680.36 --or-low 52410.22 \\
        --time "2026-10-19 02:00"
    python3 exit.py plan --stop atr:1.5 --offline klines.json  # ATR needs the session's candles
    python3 exit.py plan --symbol ETHUSDT --entry 2484.49 --setup level --stop pct:1
    python3 exit.py show                                     # the current trader/plan.json

Stops (a long's stop rounds DOWN to the symbol's tick size, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-candle ATR under the entry, on the session's candles up
             to the entry candle (--offline FILE or Binance). With fewer than 14
             candles by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (round to the nearest tick, half up):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

Tick size: from Binance exchangeInfo for the symbol (PRICE_FILTER.tickSize), cached
for 6 hours in trader/cache/, or --exchange-info FILE.

Max hold (max_hold_minutes in trader/config.json, default 240, or --max-hold): the
trade is closed at the latest that many minutes after the entry, win or lose:
exit_by = the entry candle's close time + max_hold_minutes (UTC). trader-alerts rings
then and trader-broker builds the closing ticket.

Fees (fee_rate, default 0.001 = 0.1 % a side): the plan also shows reward:risk after
fees, (target - entry - fees at the target) / (entry - stop + fees at the stop).
min_reward_risk (default 1.5) only warns here; trader-risk enforces it after fees.

Refused, with the reason: a stop at or above the entry, a target at or below it, a
risk under 0.05 % of the price (inside the spread), an entry.json that has not
triggered (unless you give --entry), a short (spot is long only).

A stop is a plan, not a guarantee: a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import datetime
import json
import os
import re
import sys
from decimal import Decimal

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMMON = next((p for p in (os.path.join(_HERE, "common"), os.path.join(_HERE, os.pardir, "common"))
                if os.path.isfile(os.path.join(p, "binance_client.py"))), os.path.join(_HERE, os.pardir, "common"))
_COMMON = os.path.abspath(_COMMON)
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)
try:
    import binance_client as bc
    import symbol_filters as sf
    import trader_config
    import trading_session as tsess
except ImportError:                                            # pragma: no cover
    sys.exit("exit.py: the shared folder common/ is missing next to this skill (copy it too)")

INTERVALS = {"1m": 60, "5m": 300}
ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05 % of the price is inside the spread's noise
NOTE = "A stop is a plan, not a guarantee: a fast market can fill past it."
WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- numbers

def D(x, what="a number"):
    try:
        return sf.D(x, what)
    except sf.FilterError as e:
        raise PlanError(str(e))


def num(x):
    """A Decimal without trailing zeros: 1, 1.5, 52545.29."""
    s = format(D(x).normalize(), "f")
    return s


def price(x, tick=None):
    """A price for people, with the tick's decimals."""
    x = D(x)
    places = sf.decimals(tick) if tick is not None else None
    if places is None:
        return num(x)
    s = format(x.quantize(Decimal(1).scaleb(-places)), ",f") if x >= 1000 else \
        format(x.quantize(Decimal(1).scaleb(-places)), "f")
    return s


def ratio(x):
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), D(m.group(2).replace(",", ""), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", D(m.group(1).replace(",", ""), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, tick, or_high=None, or_low=None,
            atr=None, atr_bars=None, fee_rate=0):
    """The stop, the target and the risk, on the tick grid. Raises PlanError with the reason."""
    e = D(entry)
    tick = D(tick)
    if e <= 0:
        raise PlanError("the entry price must be above 0")
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = D(or_high), D(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (num(oh), num(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s" % (num(oh), num(ol), num(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = %s" % (num(e), num(stop_param), num(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's candles")
        a = D(round(atr, 8))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR %s over the %d candles to the entry%s" % (
            num(a.quantize(Decimal("0.00000001"))), atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    stop = sf.floor_tick(raw, tick)
    if stop != D(raw):
        detail = (detail + ", rounded down to the tick %s" % num(tick) if detail else
                  "%s rounded down to the tick %s" % (num(raw), num(tick)))
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (num(stop), label, num(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above 0" % num(stop))
    risk = e - stop
    floor_risk = e * MIN_RISK_PCT / 100
    if risk < floor_risk:
        raise PlanError("the stop is %s under the entry, less than 0.05%% of the price (%s): too tight to "
                        "survive the spread, normal wiggle would stop you out. Widen it."
                        % (num(risk), num(sf.round_tick(floor_risk, tick))))
    if target_rule == "r":
        target = sf.round_tick(e + target_param * risk, tick)
        t_label = r_label(target_param)
    else:
        target = sf.round_tick(target_param, tick)
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (num(target), num(e)))
    rr = ((target - e) / risk).quantize(Decimal("0.01"))
    fee = D(fee_rate)
    net_reward = (target - e) - fee * (e + target)
    net_risk = risk + fee * (e + stop)
    rr_net = (net_reward / net_risk).quantize(Decimal("0.01")) if net_risk > 0 else Decimal(0)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr, "reward_risk_net": rr_net,
            "fees_per_unit_at_stop": fee * (e + stop), "fees_per_unit_at_target": fee * (e + target)}


# ---------------------------------------------------------------- candles for ATR

def load_bars(sym, interval, start, upto, offline=None, cfg=None, cl=None):
    """Closed candles of the session from `start` to `upto` (the entry candle's close), oldest first."""
    if offline:
        rows = bc.load_json(offline)
        if isinstance(rows, dict):
            rows = rows.get(sym.upper()) or []
    else:
        try:
            cl = cl or bc.client(cfg or {})
            rows = cl.klines(sym, interval, tsess.ms_of(start), tsess.ms_of(upto), 1000)
        except bc.BinanceError as e:
            raise PlanError(str(e))
    step = INTERVALS[interval]
    out = []
    for k in sorted(bc.kline_rows(rows), key=lambda k: k["open_time"]):
        st = tsess.utc_of(k["open_time"] // 1000)
        if st < start or st + datetime.timedelta(seconds=step) > upto:
            continue
        out.append({"h": k["h"], "l": k["l"], "c": k["c"]})
    return out


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n candles (all of them if fewer). The first candle uses its own range."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path):
    try:
        cfg = trader_config.load(path)
        tsess.settings(cfg)
        return cfg
    except (trader_config.ConfigFileError, tsess.SessionError) as e:
        raise PlanError(str(e))


def parse_time(text, now_utc):
    """'2026-10-19 02:00', '...Z', or '02:00' / '02:00 UTC' (the session day of now) -> naive UTC."""
    t = str(text).strip()
    if t.upper().endswith(" UTC"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        hh, mm = map(int, t.split(":"))
        cand = now_utc.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return cand if cand <= now_utc + datetime.timedelta(minutes=1) else cand - tsess.DAY
    try:
        return tsess.parse_now(t)
    except tsess.SessionError:
        raise PlanError("the entry time wants a UTC time like \"2026-10-19 02:00\", not %r" % text)


def gather(a, now_utc):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("spot is long only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": a.symbol.upper(), "entry": a.entry, "setup": a.setup or ("orb" if a.or_high is not None
                                                                                   else "level"),
                "entry_time": parse_time(a.time, now_utc) if a.time else now_utc.replace(second=0),
                "entry_bar": a.entry_bar, "interval": a.interval or "5m", "or_high": a.or_high, "or_low": a.or_low,
                "or_minutes": a.or_minutes, "session_start": None, "source": "flags"}
    else:
        path = a.from_path or "trader/entry.json"
        doc = read_json(path, "entry (run /trader-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: spot is long only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan "
                            "ahead" % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        if a.time:
            t = parse_time(a.time, now_utc)
        elif a.entry is None and doc.get("close_time"):
            t = parse_time(doc["close_time"], now_utc)
        else:
            t = now_utc.replace(second=0)
        info = {"symbol": str(doc.get("symbol") or "").upper(),
                "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "entry_time": t,
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None
                                                                          else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "session_start": doc.get("session_start"),
                "source": path}
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def build_plan(a, now_utc):
    cfg = read_config(a.config)
    info = gather(a, now_utc)
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    try:
        rules = sf.rules_for(info["symbol"], a.exchange_info, a.cache, None if a.exchange_info else
                             bc.client(cfg, live=a.live))
    except (sf.FilterError, bc.BinanceError, OSError, ValueError) as e:
        raise PlanError("tick size for %s: %s" % (info["symbol"], e))
    if rules["status"] != "TRADING":
        raise PlanError("%s is not trading on Binance right now (status %s)" % (info["symbol"], rules["status"]))
    tick = rules["tick"]
    t = info["entry_time"]
    sess = tsess.current(t, cfg)
    start = tsess.parse_now(info["session_start"]) if info["session_start"] else sess["start"]
    atr = atr_n = None
    if stop_rule == "atr":
        bars = load_bars(info["symbol"], info["interval"], start, t, a.offline, cfg)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s candles of %s by the entry; found %d"
                            % (info["interval"], info["symbol"], len(bars)))
        atr, atr_n = atr_of(bars)
    fee = cfg.get("fee_rate", 0.001)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, tick, info["or_high"],
                info["or_low"], atr, atr_n, fee)
    hold = int(a.max_hold if a.max_hold is not None else sess["settings"]["max_hold_minutes"])
    if not 1 <= hold <= 7 * 24 * 60:
        raise PlanError("--max-hold must be between 1 minute and 7 days")
    exit_by = tsess.exit_by(t, minutes=hold)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(info["or_high"]), "low": float(info["or_low"]),
               "minutes": int(info["or_minutes"] or sess["settings"]["opening_range_minutes"])}
    plan = {"created_by": "trader-exit", "symbol": info["symbol"], "side": "buy", "date": sess["date"],
            "session_start": tsess.iso(start), "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": tsess.iso(t), "entry_bar": info["entry_bar"], "interval": info["interval"],
            "opening_range": orr, "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_unit": float(c["risk"]), "reward_risk": float(c["reward_risk"]),
            "reward_risk_net": float(c["reward_risk_net"]), "fee_rate": float(fee), "tick_size": num(tick),
            "max_hold_minutes": hold, "exit_by": tsess.iso(exit_by), "stop_detail": c["stop_detail"],
            "created_at": tsess.iso(now_utc), "note": NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 8)
        plan["atr_bars"] = atr_n
    warnings = []
    mrr = cfg.get("min_reward_risk")
    if mrr is not None and c["reward_risk_net"] < D(mrr):
        warnings.append("Reward:risk after fees %s is under your min_reward_risk %s: trader-risk will block it."
                        % (ratio(c["reward_risk_net"]), num(D(mrr))))
    fee_share = (c["fees_per_unit_at_stop"] / c["risk"]) if c["risk"] else 0
    if fee_share >= Decimal("0.25"):
        warnings.append("Fees eat %d %% of the risk at the stop: a stop this close to the entry costs a lot in fees."
                        % int(fee_share * 100))
    if (now_utc - t) > datetime.timedelta(minutes=INTERVALS[info["interval"]] // 60 * 2):
        warnings.append("The entry was at %s, %d minutes ago: the price has moved since." % (
            tsess.label(t), int((now_utc - t).total_seconds() // 60)))
    return plan, warnings


def describe(plan):
    e = D(plan["entry"])
    risk = D(plan["risk_per_unit"])
    tick = plan.get("tick_size")
    pct = (risk / e * 100).quantize(Decimal("0.01"))
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    when = " at %s" % plan["entry_time"].replace("T", " ").replace("Z", " UTC") if plan.get("entry_time") else ""
    if plan.get("entry_bar") is not None:
        when += " (candle %d)" % plan["entry_bar"]
    out = ["Exit plan: %s long, session %s, %s on %s candles" % (
        plan["symbol"], plan["date"], setup, "5-minute" if plan["interval"] == "5m" else "1-minute")]
    out.append("  Entry          %s%s" % (price(plan["entry"], tick), when))
    out.append("  Stop           %s  %s%s" % (price(plan["stop"], tick), plan["stop_label"],
                                              (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target         %s  %s" % (price(plan["target"], tick), plan["target_label"]))
    out.append("  Risk a unit    %s (%s%% of the entry)" % (price(plan["risk_per_unit"], tick), pct))
    out.append("  Reward:risk    %s, after %s%% fees a side %s" % (ratio(plan["reward_risk"]),
                                                                  num(D(plan["fee_rate"]) * 100),
                                                                  ratio(plan["reward_risk_net"])))
    out.append("  Exit by        %s at the latest (max hold %d minutes), win or lose" % (
        plan["exit_by"].replace("T", " ").replace("Z", " UTC"), plan["max_hold_minutes"]))
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(a):
    try:
        now = tsess.parse_now(a.now)
    except tsess.SessionError as e:
        raise PlanError(str(e))
    plan, warnings = build_plan(a, now)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /trader-timemachine replays it, then /trader-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_unit", "reward_risk", "exit_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    base = {"setup": "orb", "interval": "5m", "stop_label": "", "target_label": "", "fee_rate": 0,
            "reward_risk_net": plan["reward_risk"], "max_hold_minutes": 0}
    for line in describe(dict(base, **plan)):
        print(line)
    now = tsess.parse_now(a.now) if a.now else tsess.utc_now()
    if now > tsess.parse_now(plan["exit_by"]):
        print("Note: this plan's exit time %s has passed." % plan["exit_by"])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (trader-exit): the stop, target and exit time before "
                                             "the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop, the target and the latest exit")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol")
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--time", help="entry time (the entry candle's close), UTC: \"2026-10-19 02:00\"")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--max-hold", type=int, help="minutes (default: config max_hold_minutes, 240)")
    p.add_argument("--config", default=trader_config.DEFAULT_PATH)
    p.add_argument("--exchange-info", metavar="FILE", help="a saved exchangeInfo answer, no network")
    p.add_argument("--cache", default="trader/cache", help="where exchangeInfo is cached (6 hours)")
    p.add_argument("--live", action="store_true", help="read the live API (needs \"testnet\": false)")
    p.add_argument("--offline", metavar="PATH", help="saved klines JSON for an atr stop")
    p.add_argument("--now", help='pretend it is this UTC time, e.g. "2026-10-19 02:00"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except (PlanError, tsess.SessionError, bc.BinanceError) as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
