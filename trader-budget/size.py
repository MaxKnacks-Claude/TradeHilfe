#!/usr/bin/env python3
"""Position sizing for trader-budget. Standard library only, no network unless the
symbol's rules are not cached.

Sizes ONE day trade from trader/plan.json (written by trader-exit) to your budget and
your max loss per trade, FEES INCLUDED, and writes one proposed order to
trader/orders.json: the entry (a MARKET or LIMIT buy) plus the protective OCO
(take-profit and stop) that trader-broker sets right after the entry fills. It never
places anything: trader-risk checks the order and trader-broker asks you yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit                          # a LIMIT buy at the plan's entry
    python3 size.py --limit 52700 --write            # a LIMIT buy at 52,700
    python3 size.py --now "2026-10-19 02:01" --json  # pin the clock (UTC)

How much. Fractions are fine, on the symbol's LOT_SIZE step (MARKET_LOT_SIZE too for
a market buy), always rounded DOWN:

    quantity = the largest step multiple that keeps ALL of these:
      max loss      loss at the stop, fees in, <= max_loss_per_trade
      position cap  quantity x entry <= budget x max_position_pct
      max order     quantity x entry <= max_order
      cash          quantity x entry <= free cash minus the cash buffer
                    (free USDT from trader/portfolio.json if it exists, else the budget)

  loss at the stop = Q x E - Q' x W x (1 - fee), with Q' = Q x (1 - fee) down to the
  step (Binance takes the buy's fee in coins, so the OCO can only sell Q') and W the
  worst stop fill planned for: the stop for a STOP_LOSS leg, or the limit of a
  STOP_LOSS_LIMIT leg (stop_limit_offset_pct under the stop) where the symbol has no
  STOP_LOSS order type. A fast market can still fill worse.

Refused, with the reason (no order is written): zero quantity, an order or an OCO leg
under the minimum notional (NOTIONAL / MIN_NOTIONAL) or the minimum quantity,
reward:risk after fees under min_reward_risk, a plan whose exit_by has passed, a
blocked symbol.

The entry it sizes at: a limit order's limit price; for a market order the plan's
entry, or the price in trader/prices.json if that is fresh (under 5 minutes old) and
higher, because a price that ran up since the plan means a bigger loss at the same stop.
"""

import argparse
import datetime
import json
import os
import sys

_COMMON = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "common"))
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)
try:
    import binance_client as bc
    import order_math as om
    import symbol_filters as sf
    import trader_config
    import trading_session as tsess
except ImportError:                                            # pragma: no cover
    sys.exit("size.py: the shared folder common/ is missing next to this skill (copy it too)")

FRESH_MINUTES = 5
SETUP_WORDS = {"orb": "opening-range breakout", "level": "close above a level"}
D = sf.D


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, time, fresh) for sym from a prices.json doc."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    det = (doc.get("detail") or {}).get(sym) or {}
    t = None
    for key in ("fetched", "time"):
        if det.get(key):
            try:
                t = tsess.parse_now(det[key])
                break
            except tsess.SessionError:
                pass
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


def human(x, step=None):
    x = D(x)
    if step is not None:
        x = sf.D(sf.fmt(x, step))
    s = format(x.normalize(), "f") if x == x.to_integral_value() or step is None else format(x, "f")
    return s


def money(x):
    return "{:,.2f}".format(float(x))


# ---------------------------------------------------------------- the sizing

def size_trade(plan, cfg, rules, budget=None, limit=None, price=None, cash=None, now_utc=None):
    """Pure function: one plan + your rules + the symbol's rules -> the sizing (a dict). qty may be 0."""
    c = dict(trader_config.DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = str(plan.get("symbol") or "").strip().upper()
    if not sym:
        raise SizeError("the plan has no symbol (run trader-exit)")
    if str(plan.get("side") or "buy").lower() != "buy":
        raise SizeError("spot is long only: the plan says %r" % plan.get("side"))
    if rules.get("symbol") and rules["symbol"].upper() != sym:
        raise SizeError("the exchange rules are for %s, the plan for %s" % (rules["symbol"], sym))
    if rules.get("status") != "TRADING":
        raise SizeError("%s is not trading right now (status %s)" % (sym, rules.get("status")))
    if now_utc is not None and plan.get("exit_by"):
        if now_utc >= tsess.parse_now(plan["exit_by"]):
            raise SizeError("the plan's exit time %s has passed: make a new plan with trader-exit" % plan["exit_by"])
    for name in ("entry", "stop", "target"):
        v = _f(plan.get(name))
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit)" % name)
    if sym in {str(b).upper() for b in (c.get("blocked") or [])}:
        raise SizeError("%s is on your blocked list" % sym)
    budget = D(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = D(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    fee = D(c.get("fee_rate") or 0)
    offset = D(c.get("stop_limit_offset_pct") or 0.5)
    tick = rules["tick"]

    entry_plan = D(plan["entry"])
    if limit is not None:
        ref, ref_from = sf.floor_tick(limit, tick), "limit"
    elif price is not None and D(price) > entry_plan:
        ref, ref_from = D(price), "price"
    else:
        ref, ref_from = entry_plan, "plan"
    stop = sf.floor_tick(plan["stop"], tick)
    target = sf.round_tick(plan["target"], tick)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (human(stop), human(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. Skip it or make "
                        "a new plan with trader-exit" % (human(target), human(ref)))
    market = limit is None
    step = rules["step"]
    if market and D(rules.get("market_step") or 0) > D(step):
        step = rules["market_step"]

    cash_buffer = budget * buf_pct / 100
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget), money(cash_buffer))
    if cash is not None and D(cash) - cash_buffer < available:
        available = D(cash) - cash_buffer
        cash_note = "free %s %s minus the %s buffer" % (money(cash), c["quote_asset"], money(cash_buffer))
    available = max(available, D(0))

    limits = []
    max_loss = D(c.get("max_loss_per_trade") or 0)
    if max_loss > 0:
        worst = om.worst_stop_fill(rules, stop, offset)[1]
        per = (ref - worst) + fee * (ref + worst)                       # loss a coin, fees in: the first guess
        q = sf.floor_step(max_loss / per, step) if per > 0 else D(0)
        while q > 0 and om.money(rules, q, ref, stop, target, fee, offset)["max_loss"] > max_loss:
            q = sf.floor_step(q - D(step), step)
        limits.append({"name": "max loss", "qty": q, "how": "%s max loss / %s loss a coin at the stop, fees in"
                       % (money(max_loss), human(per.quantize(D("0.00000001"))))})
    cap_pct = D(c.get("max_position_pct") or 0)
    if cap_pct > 0:
        cap = budget * cap_pct / 100
        limits.append({"name": "position cap", "qty": sf.floor_step(cap / ref, step),
                       "how": "%s%% of %s = %s / %s" % (human(cap_pct), money(budget), money(cap), human(ref))})
    max_order = D(c.get("max_order") or 0)
    if max_order > 0:
        limits.append({"name": "max order", "qty": sf.floor_step(max_order / ref, step),
                       "how": "%s / %s" % (money(max_order), human(ref))})
    limits.append({"name": "cash", "qty": sf.floor_step(available / ref, step),
                   "how": "%s = %s / %s" % (cash_note, money(available), human(ref))})
    qty = min(l["qty"] for l in limits)
    bound = [l["name"] for l in limits if l["qty"] == qty][0]
    for l in limits:
        l["binds"] = l["name"] == bound
    m = om.money(rules, qty, ref, stop, target, fee, offset)

    refusal = None
    min_rr = D(c.get("min_reward_risk") or 0)
    filt = []
    if qty > 0:
        filt = sf.check_qty(rules, qty, market) + sf.check_notional(rules, qty, ref, market)
        filt += ["take-profit leg: " + x for x in sf.check_notional(rules, m["qty_sell"], target)]
        filt += ["stop leg: " + x for x in sf.check_notional(rules, m["qty_sell"], m["worst_exit"])]
        filt += ["OCO: " + x for x in sf.check_qty(rules, m["qty_sell"])]
    if qty <= 0:
        if bound == "max loss":
            refusal = ("one step (%s %s) already risks more than your %s max loss per trade, fees in. No order. Skip "
                       "this trade, or plan a closer stop with trader-exit." % (human(step), rules.get("base") or "",
                                                                               money(max_loss)))
        else:
            refusal = "the %s limit leaves no room (%s). No order." % (bound, [l["how"] for l in limits
                                                                           if l["name"] == bound][0])
    elif filt:
        refusal = ("%s %s does not pass Binance's rules: %s. No order: the position your limits allow is too small "
                   "(or too big) for this symbol." % (human(qty), sym, "; ".join(filt)))
    elif min_rr > 0 and m["reward_risk_net"] < min_rr:
        refusal = ("reward:risk after fees %.2f (gain %s at the target, loss %s at the stop) is under your minimum %s, "
                   "so trader-risk would block it. No order. A further target or a closer stop changes that."
                   % (m["reward_risk_net"], money(m["gain_at_target"]), money(m["max_loss"]), human(min_rr)))
    elif D(c.get("min_order") or 0) > 0 and m["cost"] < D(c["min_order"]):
        refusal = "%s %s costs %s, under your %s minimum order. No order." % (
            human(qty), sym, money(m["cost"]), money(c["min_order"]))
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time"), "exit_by": plan.get("exit_by"), "entry_plan": entry_plan,
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target, "fee_rate": fee,
        "budget": budget, "cash_buffer": cash_buffer, "cash_available": available, "limits": limits,
        "qty": qty, "bound_by": bound, "money": m, "order_type": "market" if market else "limit",
        "rules": rules, "refusal": refusal, "stop_label": plan.get("stop_label", ""),
        "target_label": plan.get("target_label", ""), "quote": c["quote_asset"], "max_loss_limit": max_loss,
    }


def proposed_order(s, offset):
    """The one proposed order in the shared shape (common/order_math.py)."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    r = s["rules"]
    m = s["money"]
    o = {"type": "MARKET", "quantity": sf.fmt(s["qty"], r["step"])}
    if s["order_type"] == "limit":
        o = {"type": "LIMIT", "timeInForce": "GTC", "quantity": sf.fmt(s["qty"], r["step"]),
             "price": sf.fmt(s["entry"], r["tick"])}
    oco = om.build_oco(r, m["qty_sell"], s["stop"], s["target"], offset)
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    why = ("%s on %s: %s, bound by your %s (loss at the stop %s %s, fees in)" % (
        setup, s["symbol"], human(s["qty"]), {"max loss": "max loss per trade", "position cap": "position cap",
                                              "max order": "max order", "cash": "cash after the buffer"}[s["bound_by"]],
        money(m["max_loss"]), s["quote"]))
    return {"symbol": s["symbol"], "side": "BUY", "kind": "entry", "order": o, "oco": oco, "why": why,
            "plan": {"entry": float(s["entry"]), "entry_from": s["entry_from"], "stop": float(s["stop"]),
                     "target": float(s["target"]), "worst_stop_fill": float(m["worst_exit"]),
                     "qty": sf.fmt(s["qty"], r["step"]), "qty_sell": sf.fmt(m["qty_sell"], r["step"]),
                     "cost": round(float(m["cost"]), 2), "max_loss": round(float(m["max_loss"]), 2),
                     "gain_at_target": round(float(m["gain_at_target"]), 2),
                     "fees_at_stop": round(float(m["fees_at_stop"]), 4),
                     "reward_risk_net": round(float(m["reward_risk_net"]), 2), "fee_rate": float(s["fee_rate"]),
                     "setup": s["setup"], "date": s["date"], "entry_time": s["entry_time"], "exit_by": s["exit_by"]}}


def report(s, order, notes=()):
    m, r = s["money"], s["rules"]
    q = s["quote"]
    lines = ["%s buy, %s, planned entry %s%s" % (s["symbol"], SETUP_WORDS.get(s["setup"], s["setup"] or "setup"),
                                                 human(s["entry_plan"]),
                                                 (" at " + s["entry_time"]) if s.get("entry_time") else "")]
    lines.append("Stop %s%s. Target %s%s. Exit by %s at the latest." % (
        human(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "", human(s["target"]),
        (": " + s["target_label"]) if s["target_label"] else "", s.get("exit_by") or "-"))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s), fee %s%% a side, %s step %s, tick %s." % (
        human(s["entry"]), src, human(s["fee_rate"] * 100), r.get("base") or "", human(D(r["step"]).normalize()),
        human(D(r["tick"]).normalize())))
    lines.append("")
    lines.append("Limits (the smallest one wins, rounded down to the step):")
    for l in s["limits"]:
        lines.append("  %-13s %-66s %s%s" % (l["name"], l["how"], human(l["qty"]), "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    stop_words = "the stop %s" % human(s["stop"]) if m["below_type"] == "STOP_LOSS" else \
        "the stop-limit %s (stop %s, limit %s%% under it: %s has no STOP_LOSS orders)" % (
            human(m["worst_exit"]), human(s["stop"]), human(D(order["oco"].get("belowPrice") and
                                                              (1 - m["worst_exit"] / s["stop"]) * 100).quantize(D("0.01"))),
            s["symbol"])
    lines.append("Size: %s %s = %s %s. The buy's fee leaves %s to sell." % (
        human(s["qty"]), r.get("base") or "", money(m["cost"]), q, human(m["qty_sell"])))
    lines.append("Max loss at %s, fees in: %s %s (your limit %s)." % (stop_words, money(m["max_loss"]), q,
                                                                      money(s["max_loss_limit"])))
    lines.append("Gain at the target, fees in: %s %s. Reward:risk after fees %.2f." % (
        money(m["gain_at_target"]), q, m["reward_risk_net"]))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % human(m["worst_exit"]))
    lines.append("")
    lines.append("Proposed (nothing has been placed):")
    lines.append("  1. " + om.describe(dict(order, oco=None)))
    lines.append("  2. as soon as it fills: " + om.describe({"symbol": order["symbol"], "side": "SELL",
                                                            "order": {"type": "OCO", "quantity": order["oco"]["quantity"]},
                                                            "oco": order["oco"]}).split(", then ")[1])
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {"created_by": "trader-budget", "created_at": tsess.iso(now_utc or tsess.utc_now()), "status": "proposed",
           "testnet": bool(cfg.get("testnet", True)),
           "note": "Proposals only. Nothing is placed until trader-risk passes each order and you say yes to it.",
           "source": source, "orders": orders}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def symbol_rules(a, cfg, sym):
    try:
        cl = None if a.exchange_info else bc.client(cfg, live=a.live)
        return sf.rules_for(sym, a.exchange_info, a.cache, cl)
    except (sf.FilterError, bc.BinanceError, OSError, ValueError) as e:
        raise SizeError("exchange rules for %s: %s" % (sym, e))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size a day trade to your budget and max loss, fees in (trader-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default=trader_config.DEFAULT_PATH)
    ap.add_argument("--budget", type=float, help="override the budget in the config")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a LIMIT buy instead of MARKET, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account (ticket.py account writes it); its free cash caps the size")
    ap.add_argument("--exchange-info", metavar="FILE", help="a saved exchangeInfo answer, no network")
    ap.add_argument("--cache", default="trader/cache")
    ap.add_argument("--live", action="store_true", help="read the live API (needs \"testnet\": false)")
    ap.add_argument("--now", help="pretend it is this UTC time, e.g. \"2026-10-19 02:01\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = trader_config.load(a.config)
        now = tsess.parse_now(a.now)
        plan = read_json(a.plan, "plan (run trader-exit first)")
        sym = str((plan or {}).get("symbol") or "").upper()
        rules = symbol_rules(a, cfg, sym)
        limit = None
        if a.limit is not None:
            limit = _f(plan.get("entry")) if a.limit == "plan" else _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 52700")
        notes, price = [], None
        ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
        if ppath and limit is None and sym:
            p, t, fresh = fresh_price(read_json(ppath, "prices"), sym, now)
            if p is not None and fresh:
                price = p
                if p > float(plan.get("entry") or 0):
                    notes.append("%s last %s (%s) is above the plan's entry %s, so the risk is bigger: sized at %s."
                                 % (sym, human(p), tsess.label(t), human(plan.get("entry")), human(p)))
            elif p is not None:
                notes.append("%s in %s is older than %d minutes: not used. Sized at the plan's entry."
                             % (sym, ppath, FRESH_MINUTES))
        cash = None
        if a.portfolio and os.path.exists(a.portfolio):
            pf = read_json(a.portfolio, "portfolio")
            cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
        s = size_trade(plan, cfg, rules, a.budget, limit, price, cash, now)
        order = proposed_order(s, cfg.get("stop_limit_offset_pct", 0.5))
        if order and a.write:
            write_orders(a.write, [order], cfg, "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan",
                                                                               s["date"]), now)
        if a.json:
            out = {k: v for k, v in s.items() if k not in ("rules", "money", "limits")}
            out["limits"] = [dict(l, qty=str(l["qty"])) for l in s["limits"]]
            print(json.dumps({"sizing": out, "orders": [order] if order else [], "notes": notes}, indent=2,
                             default=str))
        else:
            print(report(s, order, notes))
            if order and a.write:
                print("Wrote 1 proposed order (entry + OCO) to %s" % a.write)
        return 0 if order else 1
    except (SizeError, trader_config.ConfigFileError, tsess.SessionError, om.OrderError) as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
