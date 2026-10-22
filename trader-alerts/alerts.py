#!/usr/bin/env python3
"""Price alerts and the session clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # where in the session are we (UTC)
    python3 alerts.py quote BTCUSDT --write          # Binance prices -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every symbol in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and max-hold exit from trader/plan.json
    python3 alerts.py add --symbol BTCUSDT --stop 52545.29
    python3 alerts.py add --symbol BTCUSDT --target 53007.07
    python3 alerts.py add --at 05:30 --label "check the trade"
    python3 alerts.py add --symbol ETHUSDT --pct 3
    python3 alerts.py add --session-start
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-19 06:00" --offline tests/fixtures/binance
    python3 alerts.py list
    python3 alerts.py reset btcusdt-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more against the price 24 hours ago
                  (or "ref" if you give one); "direction": "up" or "down"
  session_start   a new session started (rings once per session)
  time            the UTC clock passed "at" (HH:MM) in a session (rings once per session)
  exit_by         the UTC moment "at_utc" passed: the trade has reached max_hold_minutes and
                  should be closed (rings once). add --from-plan makes it from the plan.

A rule with "expires_utc" is no longer watched after that moment (rules from a plan
expire an hour after the plan's exit time). An above / below alert rings ONCE and then
stays quiet until you reset it, unless the rule has "rearm": true (it re-arms when the
price crosses back). A pct_move alert rings at most once per session.

Quotes: Binance GET /api/v3/ticker/24hr (type MINI, one request for all symbols:
lastPrice, openPrice 24 hours ago, closeTime). Alerts are information, never an order:
the OCO at Binance is what actually stops you out. --offline DIR reads
DIR/ticker24hr-mini.json instead. Times are UTC; --now pins the clock.
"""

import argparse
import datetime
import json
import os
import re
import sys

_COMMON = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "common"))
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)
try:
    import binance_client as bc
    import trader_config
    import trading_session as tsess
except ImportError:                                            # pragma: no cover
    sys.exit("alerts.py: the shared folder common/ is missing next to this skill (copy it too)")

HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
EXPIRE_AFTER = datetime.timedelta(hours=1)


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- time

def parse_now(text):
    try:
        return tsess.parse_now(text)
    except tsess.SessionError as e:
        raise AlertError(str(e))


def parse_iso(text):
    try:
        return tsess.parse_now(str(text)) if text else None
    except tsess.SessionError:
        raise AlertError("%r is not a UTC time like 2026-10-19T06:00:00Z" % text)


def session_status(now_utc, cfg=None):
    w = tsess.current(now_utc, cfg)
    s = w["settings"]
    return {"now_utc": tsess.label(now_utc), "session": w["date"], "start": tsess.iso(w["start"]),
            "range_end": tsess.iso(w["range_end"]), "entry_end": tsess.iso(w["entry_end"]),
            "end": tsess.iso(w["end"]), "phase": w["phase"],
            "phase_words": {"range": "the opening range is forming", "entries": "new entries allowed",
                            "late": "after the entry window: no new entries"}[w["phase"]],
            "max_hold_minutes": s["max_hold_minutes"]}


# ---------------------------------------------------------------- quotes

def parse_tickers(rows, wanted=None):
    """ticker/24hr MINI rows -> {SYM: quote}."""
    out = {}
    want = {s.upper() for s in wanted} if wanted else None
    for t in rows or []:
        sym = str(t.get("symbol", "")).upper()
        if want is not None and sym not in want:
            continue
        try:
            price = float(t["lastPrice"])
        except (KeyError, TypeError, ValueError):
            continue
        ref = t.get("openPrice")
        ct = t.get("closeTime")
        out[sym] = {"symbol": sym, "price": price, "open_24h": float(ref) if ref not in (None, "") else None,
                    "time": tsess.iso(tsess.utc_of(int(ct) // 1000)) if ct else "", "source": "binance"}
    return out


def get_quotes(symbols, cfg, offline=None, cl=None):
    """{SYM: quote} and [errors]. One request for every symbol."""
    symbols = [s.upper() for s in symbols]
    if offline:
        p = os.path.join(offline, "ticker24hr-mini.json")
        if not os.path.exists(p):
            raise AlertError("no offline quote file %s" % p)
        rows = bc.load_json(p)
    else:
        try:
            cl = cl or bc.client(cfg)
            rows = cl.ticker_24hr(symbols, mini=True)
        except bc.BinanceError as e:
            return {}, [str(e)]
    quotes = parse_tickers(rows, symbols)
    errors = ["no Binance price for %s (check the symbol)" % s for s in symbols if s not in quotes]
    return quotes, errors


def write_prices(path, quotes, now_utc):
    old = _read(path, {}) if os.path.exists(path) else {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = tsess.iso(now_utc)
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"open_24h": q.get("open_24h"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": "binance",
           "note": "Last prices for alerts and sizing checks. The order book is what fills. "
                   "detail.time is the ticker's own time, detail.fetched when it was read.",
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


def num(x):
    x = float(x)
    if x >= 1000:
        return "{:,.2f}".format(x)
    if x >= 1:
        s = "%.4f" % x
        return s[:-2] if s.endswith("00") else s.rstrip("0")
    return ("%.10f" % x).rstrip("0")


# ---------------------------------------------------------------- rules

TYPES = {"above", "below", "stop", "target", "pct_move", "session_start", "time", "exit_by"}
NO_QUOTE = {"session_start", "time", "exit_by"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in ("session_start", "time") and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as a UTC time like \"05:30\"" % rule.get("id"))
    if t == "exit_by":
        parse_iso(rule.get("at_utc"))
        if not rule.get("at_utc"):
            raise AlertError("rule %s needs \"at_utc\" (when max_hold_minutes is reached)" % rule.get("id"))
    if rule.get("expires_utc"):
        parse_iso(rule["expires_utc"])


def active(rule, now_utc):
    exp = rule.get("expires_utc")
    return not exp or now_utc <= parse_iso(exp)


def _rang_in_session(stamp, session, cfg):
    if not stamp:
        return False
    try:
        return tsess.session_date_of(parse_iso(stamp), cfg) == session
    except AlertError:
        return False


def evaluate(rules, quotes, now_utc, state, cfg=None):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    st = session_status(now_utc, cfg)
    sess = st["session"]
    stamp = tsess.iso(now_utc)
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "session"), r["type"])
        if not active(r, now_utc):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        sym = str(r.get("symbol") or "").upper()
        if typ == "session_start":
            if not _rang_in_session(rung.get(rid), sess, cfg):
                rings.append({"id": rid, "symbol": "", "message": "Session %s started at %s; the opening range "
                              "ends at %s, new entries until %s." % (sess, st["start"][11:16] + " UTC",
                                                                     st["range_end"][11:16] + " UTC",
                                                                     st["entry_end"][11:16] + " UTC")})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = _hhmm(r["at"])
            w = tsess.current(now_utc, cfg)
            ring_at = datetime.datetime.combine(w["start"].date(), at)
            if ring_at < w["start"]:
                ring_at += tsess.DAY
            if now_utc >= ring_at and not _rang_in_session(rung.get(rid), sess, cfg):
                label = r.get("label") or "time alert"
                rings.append({"id": rid, "symbol": sym, "message": "%s%s: it is %s (set for %s UTC)."
                              % (label[:1].upper(), label[1:], tsess.label(now_utc), r["at"])})
                rung[rid] = stamp
            continue
        if typ == "exit_by":
            due = parse_iso(r["at_utc"])
            if now_utc >= due and rid not in rung:
                rings.append({"id": rid, "symbol": sym, "message": (
                    "Max hold reached for %s: the plan said out by %s (%s minutes after the entry). If it is "
                    "still open, close it: ticket.py flatten --symbol %s cancels the OCO and sells, after "
                    "trader-risk and your yes." % (sym, tsess.label(due), r.get("max_hold_minutes", "max_hold"),
                                                   sym))})
                rung[rid] = stamp
            continue
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s is %s: %s your %s %s%s." % (
                    sym, num(p), word, label, num(r["price"]), (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("open_24h")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_in_session(rung.get(rid), sess, cfg):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s)."
                              % (sym, move, num(p), num(ref))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def rules_from_plan(plan, cfg=None):
    """trader/plan.json -> a stop alert, a target alert and the max-hold exit alert."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    sym = str(plan["symbol"]).upper()
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    due = plan.get("exit_by")
    if not due:
        raise AlertError("the plan has no exit_by time (make it again with trader-exit)")
    due_dt = parse_iso(due)
    out.append({"id": "%s-exit-by" % sym.lower(), "symbol": sym, "type": "exit_by", "at_utc": tsess.iso(due_dt),
                "max_hold_minutes": plan.get("max_hold_minutes"), "label": "max hold reached"})
    for r in out:
        r["expires_utc"] = tsess.iso(due_dt + EXPIRE_AFTER)
        if plan.get("date"):
            r["plan_date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(str(p.get("symbol") or p.get("ticker") or "").upper())
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(str(plan["symbol"]).upper())
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def _cfg(a):
    try:
        return trader_config.load(getattr(a, "config", trader_config.DEFAULT_PATH))
    except trader_config.ConfigFileError as e:
        raise AlertError(str(e))


def cmd_clock(a):
    st = session_status(parse_now(a.now), _cfg(a))
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print("%s: session %s (started %s), %s." % (st["now_utc"], st["session"], st["start"][11:16] + " UTC",
                                                    st["phase_words"]))
        print("Opening range until %s, entries until %s, the session ends %s. Max hold %d minutes a trade."
              % (st["range_end"][11:16], st["entry_end"][11:16], st["end"].replace("T", " ")[:16],
                 st["max_hold_minutes"]))
        print("Crypto trades around the clock: the session is your rule (trader/config.json), not the exchange's.")
    return 0 if st["phase"] == "entries" else 1


def cmd_quote(a):
    syms = _dedupe([s.upper() for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote BTCUSDT ETHUSDT, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, _cfg(a), a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("open_24h"):
                    chg = " (%+.2f%% in 24 hours)" % (100.0 * (q["price"] / q["open_24h"] - 1))
                print("%-10s %s%s  %s" % (s, num(q["price"]), chg, q.get("time", "")))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    cfg = _cfg(a)
    rules = doc["rules"]
    now = parse_now(a.now)
    watched = [r for r in rules if active(r, now)]
    syms = _dedupe([str(r.get("symbol", "")).upper() for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, cfg, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, now, state, cfg)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now)
    if a.json:
        print(json.dumps({"session": session_status(now, cfg), "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        print("%s, session %s." % (tsess.label(now), session_status(now, cfg)["session"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched%s." % (
                len(watched), (", " + ", ".join("%s %s" % (s, num(quotes[s]["price"])) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. The OCO at Binance does the stopping: check it with "
                  "ticket.py status and record fills there. To close a position yourself: ticket.py flatten, "
                  "then trader-risk and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
        new = rules_from_plan(plan)
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        print("Run alerts.py check every minute or two while the trade is open (a scheduled task or /loop). "
              "The OCO at Binance is what actually stops you out.")
        return 0
    if a.session_start:
        rule = {"id": a.id or "session-start", "type": "session_start"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants a UTC time like 05:30")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = a.symbol.upper()
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --session-start)")
        sym = a.symbol.upper()
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], ("%g" % val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.expires:
        rule["expires_utc"] = tsess.iso(parse_iso(a.expires))
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the session clock (trader-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read DIR/ticker24hr-mini.json, no network")
        p.add_argument("--now", help="pretend it is this UTC time")
        p.add_argument("--config", default=trader_config.DEFAULT_PATH)
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="where in the session are we")
    p.add_argument("--now")
    p.add_argument("--config", default=trader_config.DEFAULT_PATH)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest Binance prices")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add symbols from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--session-start", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, UTC")
    g.add_argument("--from-plan", action="store_true", help="stop, target and max-hold exit from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--label", help="what a time alert says")
    p.add_argument("--expires", help="stop watching this rule after this UTC time")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except (AlertError, tsess.SessionError) as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
