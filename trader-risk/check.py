#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only; no network unless a
symbol's exchange rules are not cached.

Reads the proposed orders (trader/orders.json) and checks every one against your
rules (trader/config.json), your account (trader/portfolio.json), prices
(trader/prices.json), the symbol's Binance rules (exchangeInfo) and what was placed
in this session (trader/state/broker.json). Each order gets PASS or BLOCK with the
reasons. Orders are checked in file order as if each PASS had filled.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-10-19 02:01"        # pin the clock (UTC)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY is a BUY with its protective OCO. An EXIT is a SELL that
only closes coins you hold. Getting out must always be possible: an exit is checked
for format, asset, Binance filters and "do you hold it" only.

What blocks any order:
  format     unknown fields (anything margin, futures or leverage), side, type,
             quantity, a LIMIT without price / timeInForce, an OCO that is not a SELL
             with LIMIT_MAKER / TAKE_PROFIT(_LIMIT) above and STOP_LOSS(_LIMIT) below
  asset      not in exchangeInfo, not TRADING, no spot trading, another quote asset
             than "quote_asset", an order type the symbol does not allow, no OCO allowed
  filters    LOT_SIZE / MARKET_LOT_SIZE (step, min, max), PRICE_FILTER (tick, min, max)
             for every price, NOTIONAL / MIN_NOTIONAL for the entry AND both OCO legs
  short      selling more than you hold (spot cannot go short)

What blocks an entry on top:
  blocked       a symbol on your "blocked" list
  stop          no OCO stop while "require_stop" is true; a stop not under the entry
  target        a target not above the entry
  loss          the loss at the stop, fees in (Binance takes the buy's fee in coins,
                so the OCO sells less) over max_loss_per_trade. The entry is the limit
                price, else the HIGHER of the plan's entry and a fresh price
  reward:risk   gain at the target / loss at the stop, both after fees, under min_reward_risk
  size          under min_order or over max_order
  position      over max_position_pct of the budget
  cash          more than the free quote asset minus the cash buffer
  daily loss    the losses at the stops of this session's entries plus this one over max_daily_loss
  trades        max_trades_per_day entries already placed in this session
  orders        max_orders_per_day orders already placed in this session
  duplicate     the same symbol twice in this batch, or already entered this session
  window        before the opening range ends or after entry_window_minutes (config)
  plan expired  the plan's exit_by has passed

Exit code 0 when every order passes, 1 when any order is blocked. The verdicts go to
trader/state/risk.json with a fingerprint of each order, so trader-broker only treats
an order as ready if THIS exact order passed in this session.
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
    import order_math as om
    import symbol_filters as sf
    import trader_config
    import trading_session as tsess
except ImportError:                                            # pragma: no cover
    sys.exit("check.py: the shared folder common/ is missing next to this skill (copy it too)")

FRESH_MINUTES = 5
D = sf.D
ALLOWED_TOP = {"symbol", "side", "kind", "order", "oco", "cancel_order_list", "plan", "why"}
ALLOWED_ORDER = set(om.ORDER_FIELDS)
ALLOWED_OCO = set(om.OCO_FIELDS)
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price"}


class RiskError(Exception):
    pass


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def _d(x):
    try:
        v = D(x)
        return v if v.is_finite() else None
    except (sf.FilterError, TypeError):
        return None


def money(x):
    return "{:,.2f}".format(float(x))


def human(x):
    return format(D(x).normalize(), "f")


def rec_session(rec):
    return str(rec.get("session") or rec.get("date") or "")


def placed_in(state, session):
    return [r for r in (state or {}).get("orders", []) if r.get("status") == "placed" and rec_session(r) == session]


def account(cfg, portfolio):
    """(free quote cash, {asset: free amount}). No portfolio = a fresh account funded with the budget."""
    if portfolio is None:
        b = _d(cfg.get("budget"))
        if not b:
            raise RiskError("no trader/portfolio.json and no budget in the config: nothing to check against")
        return b, {}
    cash = _d(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number (ticket.py account writes it)")
    bal = {str(k).upper(): _d(v) or D(0) for k, v in (portfolio.get("balances") or {}).items()}
    return cash, bal


def format_reasons(o):
    """Reasons an order's shape is wrong (before any money math)."""
    out = []
    extra = sorted(set(o) - ALLOWED_TOP)
    if extra:
        out.append("format: unknown field(s) %s: this toolkit sends plain spot orders only (no margin, futures or "
                   "leverage)" % ", ".join(extra))
    if not o.get("symbol"):
        out.append("format: no symbol")
    side = str(o.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        out.append("format: side must be BUY or SELL, got %r" % o.get("side"))
    kind = o.get("kind")
    if kind not in ("entry", "exit"):
        out.append("format: kind must be entry or exit, got %r" % kind)
    elif (kind == "entry") != (side == "BUY"):
        out.append("format: an entry is a BUY and an exit is a SELL (spot is long only)")
    od = o.get("order")
    if not isinstance(od, dict):
        out.append("format: no \"order\"")
        return out
    extra = sorted(set(od) - ALLOWED_ORDER)
    if extra:
        out.append("format: unknown order field(s) %s" % ", ".join(extra))
    t = str(od.get("type") or "").upper()
    if t not in om.ORDER_TYPES:
        out.append("format: order type must be MARKET or LIMIT, got %r" % od.get("type"))
    q = _d(od.get("quantity"))
    if q is None or q <= 0:
        out.append("format: order quantity must be a positive number, got %r" % od.get("quantity"))
    if od.get("quoteOrderQty") is not None:
        out.append("format: quoteOrderQty is not used here: size in coins (quantity) so the stop math is exact")
    if t == "LIMIT":
        if _d(od.get("price")) is None:
            out.append("format: a LIMIT order needs a price")
        if str(od.get("timeInForce") or "").upper() not in om.TIFS:
            out.append("format: a LIMIT order needs timeInForce GTC, IOC or FOK")
    oco = o.get("oco")
    if oco is not None:
        if not isinstance(oco, dict):
            out.append("format: \"oco\" must be an object")
            return out
        extra = sorted(set(oco) - ALLOWED_OCO)
        if extra:
            out.append("format: unknown OCO field(s) %s" % ", ".join(extra))
        if str(oco.get("side") or "").upper() != "SELL":
            out.append("format: the protective OCO must SELL")
        if oco.get("aboveType") not in om.ABOVE_TYPES:
            out.append("format: OCO aboveType must be LIMIT_MAKER, TAKE_PROFIT or TAKE_PROFIT_LIMIT")
        if oco.get("belowType") not in om.BELOW_TYPES:
            out.append("format: OCO belowType must be STOP_LOSS or STOP_LOSS_LIMIT")
        if _d(oco.get("quantity")) is None:
            out.append("format: OCO quantity missing")
        if oco.get("aboveType") in ("LIMIT_MAKER", "TAKE_PROFIT_LIMIT") and _d(oco.get("abovePrice")) is None:
            out.append("format: OCO %s needs abovePrice" % oco.get("aboveType"))
        if _d(oco.get("belowStopPrice")) is None:
            out.append("format: OCO needs belowStopPrice")
        if oco.get("belowType") == "STOP_LOSS_LIMIT" and (_d(oco.get("belowPrice")) is None or
                                                          oco.get("belowTimeInForce") not in om.TIFS):
            out.append("format: a STOP_LOSS_LIMIT leg needs belowPrice and belowTimeInForce")
    if o.get("cancel_order_list") is not None and not isinstance(o["cancel_order_list"], dict):
        out.append("format: cancel_order_list must be {\"orderListId\": N}")
    return out


def asset_reasons(o, rules, cfg):
    out = []
    if rules is None:
        return ["asset: %s is not a Binance spot symbol (not in exchangeInfo)" % o.get("symbol")]
    if rules["status"] != "TRADING":
        out.append("asset: %s is not trading right now (status %s)" % (rules["symbol"], rules["status"]))
    if not rules["spot"]:
        out.append("asset: %s has no spot trading" % rules["symbol"])
    if str(rules["quote"]).upper() != cfg["quote_asset"]:
        out.append("asset: %s is quoted in %s, your quote_asset is %s" % (rules["symbol"], rules["quote"],
                                                                          cfg["quote_asset"]))
    t = str((o.get("order") or {}).get("type") or "").upper()
    if t and t not in rules["order_types"]:
        out.append("asset: %s does not take %s orders" % (rules["symbol"], t))
    oco = o.get("oco")
    if oco:
        if not rules["oco_allowed"]:
            out.append("asset: %s does not allow OCO orders" % rules["symbol"])
        for k in ("aboveType", "belowType"):
            if oco.get(k) and oco[k] not in rules["order_types"]:
                out.append("asset: %s does not take %s orders (the OCO's %s)" % (rules["symbol"], oco[k], k))
    return out


def filter_reasons(o, rules, ref):
    od = o.get("order") or {}
    market = str(od.get("type") or "").upper() == "MARKET"
    q = _d(od.get("quantity"))
    out = []
    if q is not None and ref is not None:
        prices = [("limit price", od["price"])] if od.get("price") is not None else []
        out += sf.check_order(rules, q, ref, market, prices)
    oco = o.get("oco")
    if oco and _d(oco.get("quantity")) is not None:
        qs = D(oco["quantity"])
        out += ["OCO: " + x for x in sf.check_qty(rules, qs)]
        for k, name in (("abovePrice", "take-profit"), ("aboveStopPrice", "take-profit trigger"),
                        ("belowStopPrice", "stop"), ("belowPrice", "stop limit")):
            if oco.get(k) is not None:
                out += ["OCO %s: %s" % (name, x) for x in sf.check_price(rules, oco[k], name)]
                if k in ("abovePrice", "belowStopPrice", "belowPrice"):
                    out += ["OCO %s leg: %s" % (name, x) for x in sf.check_notional(rules, qs, oco[k])]
    return ["filter: " + x for x in out]


def check(orders, cfg, rules_of, portfolio=None, prices=None, price_times=None, state=None, now=None):
    """Pure function. rules_of(symbol) -> rules dict or None.
    Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}."""
    c = dict(trader_config.DEFAULTS)
    c.update(cfg or {})
    c["quote_asset"] = str(c.get("quote_asset") or "USDT").upper()
    now = now or tsess.utc_now()
    w = tsess.current(now, c)
    session = w["date"]
    fee = D(c.get("fee_rate") or 0)
    prices = {str(k).upper(): D(v) for k, v in (prices or {}).items() if _d(v)}
    cash, balances = account(c, portfolio)
    budget = _d(c.get("budget")) or cash
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    done = placed_in(state, session)
    entries = [r for r in done if r.get("kind") == "entry"]
    loss_today = sum((_d(r.get("max_loss")) or D(0)) for r in entries)
    entered = {str(r.get("symbol", "")).upper() for r in entries}
    top_warn = []
    if c.get("testnet", True) is False:
        top_warn.append("LIVE trading (\"testnet\": false): these orders use real money")
    for r in entries:
        if r.get("max_loss") is None:
            top_warn.append("an entry placed in this session (%s) has no max loss in the log; not counted in the "
                            "daily loss" % r.get("symbol"))
    window = []
    if w["phase"] == "range":
        window.append("window: the opening range of session %s runs until %s UTC: no new entries before it is set"
                      % (session, w["range_end"].strftime("%H:%M")))
    elif w["phase"] == "late":
        window.append("window: after %s UTC, the end of the entry window (entry_window_minutes %d): no new entries in "
                      "session %s" % (w["entry_end"].strftime("%H:%M"), w["settings"]["entry_window_minutes"], session))

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    results, seen = [], set()
    batch_entries, batch_loss, batch_orders = 0, D(0), 0
    for i, o in enumerate(orders, 1):
        reasons, warns, info, later = [], [], [], []
        sym = str(o.get("symbol", "")).strip().upper()
        side = str(o.get("side", "")).upper()
        kind = o.get("kind")
        od = o.get("order") if isinstance(o.get("order"), dict) else {}
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        reasons += format_reasons(o)
        rules = rules_of(sym) if sym else None
        reasons += asset_reasons(o, rules, c)
        q = _d(od.get("quantity"))

        ref, ref_from = None, None
        if str(od.get("type") or "").upper() == "LIMIT" and _d(od.get("price")):
            ref, ref_from = D(od["price"]), "limit"
        else:
            cands = [(p, w_) for p, w_ in ((_d(plan.get("entry")), "plan"), (fresh(sym), "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                if ref_from == "price" and _d(plan.get("entry")) and ref > D(plan["entry"]):
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, human(ref), human(plan["entry"]), human(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "price"
                warns.append("the %s price in trader/prices.json is old: used anyway for the size checks" % sym)
        if rules is not None and not reasons:
            reasons += filter_reasons(o, rules, ref)

        value = (q * ref) if (q is not None and ref is not None) else None
        max_loss = rr = None
        if kind == "exit":
            base = (rules or {}).get("base")
            held = balances.get(str(base).upper(), D(0)) if base else D(0)
            if q is not None and q > held:
                reasons.append("short: selling %s %s but the account holds %s; spot cannot go short. Refresh it with "
                               "ticket.py account" % (human(q), base or sym, human(held)))
        elif kind == "entry":
            if value is None:
                later.append("price: no price to size %s; the plan's entry or a fresh trader/prices.json is needed" % sym)
            if sym in blocked:
                later.append("blocked: %s is on your blocked list" % sym)
            oco = o.get("oco")
            if not oco:
                if c.get("require_stop", True):
                    later.append("stop: no protective OCO. Every entry needs one (\"require_stop\": true): trader-budget "
                                 "writes it")
                else:
                    warns.append("no stop: the loss is not capped and not counted in the daily loss")
            elif ref is not None and q is not None and rules is not None and not reasons:
                stop = D(oco["belowStopPrice"])
                target = _d(oco.get("abovePrice")) or _d(oco.get("aboveStopPrice"))
                if stop >= ref:
                    later.append("stop: the stop %s must be under the entry %s" % (human(stop), human(ref)))
                elif target is None or target <= ref:
                    later.append("target: the take-profit %s must be above the entry %s" % (human(target or 0),
                                                                                         human(ref)))
                else:
                    worst = D(oco["belowPrice"]) if oco.get("belowType") == "STOP_LOSS_LIMIT" else stop
                    qs = D(oco["quantity"])
                    can_sell = om.sell_quantity(rules, q, fee)
                    if qs > can_sell:
                        later.append("oco: it sells %s but the buy delivers only %s after the %s%% fee (paid in %s)"
                                     % (human(qs), human(can_sell), human(fee * 100), rules["base"]))
                    cost = q * ref
                    max_loss = cost - qs * worst * (1 - fee)
                    gain = qs * target * (1 - fee) - cost
                    rr = (gain / max_loss) if max_loss > 0 else D(0)
                    cap = _d(c.get("max_loss_per_trade"))
                    if cap and max_loss > cap + D("0.005"):
                        later.append("loss: %s at the stop, fees in, over your %s max loss per trade"
                                     % (money(max_loss), money(cap)))
                    min_rr = _d(c.get("min_reward_risk"))
                    if min_rr and rr < min_rr:
                        later.append("reward:risk: %.2f after fees (gain %s at the target, loss %s at the stop) is under "
                                     "your minimum %s" % (rr, money(gain), money(max_loss), human(min_rr)))
                    info.append("entry %s (%s), stop %s%s, target %s: loss at the stop %s, gain at the target %s, "
                                "fees in" % (human(ref), REF_WORDS.get(ref_from, ref_from), human(stop),
                                             (" (fills down to %s)" % human(worst)) if worst != stop else "",
                                             human(target), money(max_loss), money(gain)))
            if value is not None:
                if _d(c.get("min_order")) and value < D(c["min_order"]):
                    later.append("size: %s %s is under your %s minimum order" % (money(value), c["quote_asset"],
                                                                                money(c["min_order"])))
                if _d(c.get("max_order")) and value > D(c["max_order"]) + D("0.005"):
                    later.append("size: %s %s is over your %s max order" % (money(value), c["quote_asset"],
                                                                           money(c["max_order"])))
                cap_pos = budget * D(c.get("max_position_pct") or 100) / 100
                if value > cap_pos + D("0.005"):
                    later.append("position: %s is %s, over your %s%% cap (%s of the %s budget)"
                                 % (sym, money(value), human(D(c.get("max_position_pct") or 100)), money(cap_pos),
                                    money(budget)))
                buffer = budget * D(c.get("cash_buffer_pct") or 0) / 100
                if value > cash - buffer + D("0.005"):
                    later.append("cash: needs %s %s but only %s is free above your %s buffer"
                                 % (money(value), c["quote_asset"], money(max(cash - buffer, D(0))), money(buffer)))
            daily = _d(c.get("max_daily_loss"))
            if daily and max_loss is not None and loss_today + batch_loss + max_loss > daily + D("0.005"):
                later.append("daily loss: %s already at risk in this session plus %s = %s, over your %s daily limit"
                             % (money(loss_today + batch_loss), money(max_loss),
                                money(loss_today + batch_loss + max_loss), money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries) + batch_entries >= cap_t:
                later.append("trades: %d entries in this session already, your limit is %d"
                             % (len(entries) + batch_entries, cap_t))
            if sym in entered or sym in seen:
                later.append("duplicate: %s was already entered in this session or is twice in this batch" % sym)
            later.extend(window)
            if plan.get("exit_by"):
                try:
                    if now >= tsess.parse_now(plan["exit_by"]):
                        later.append("plan expired: its exit time %s has passed" % plan["exit_by"])
                except tsess.SessionError:
                    later.append("plan: exit_by %r is not a time" % plan["exit_by"])
            info.append("this session before this: %d of %s trades, %s of %s daily loss"
                        % (len(entries) + batch_entries, cap_t or "unlimited", money(loss_today + batch_loss),
                           money(c["max_daily_loss"]) if _d(c.get("max_daily_loss")) else "no limit on"))
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and len(done) + batch_orders >= cap_n:
            (later if kind == "entry" else warns).append("orders: %d placed in this session already, your limit is %d"
                                                         % (len(done) + batch_orders, cap_n))
        reasons += later
        seen.add(sym)
        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            batch_orders += 1
            if kind == "entry":
                batch_entries += 1
                batch_loss += max_loss or D(0)
                if value is not None:
                    cash -= value
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind, "plain": om.describe(o) if not format_reasons(o)
            else None, "value": round(float(value), 2) if value is not None else None,
            "entry_ref": float(ref) if ref is not None else None, "entry_ref_from": ref_from,
            "max_loss": round(float(max_loss), 4) if max_loss is not None else None,
            "reward_risk_net": round(float(rr), 2) if rr is not None else None,
            "fingerprint": om.fingerprint(o), "verdict": verdict, "reasons": reasons, "warnings": warns, "info": info,
        })
    return {"session": session, "now_utc": tsess.label(now), "testnet": bool(c.get("testnet", True)),
            "results": results, "pass": sum(1 for r in results if r["verdict"] == "PASS"),
            "block": sum(1 for r in results if r["verdict"] == "BLOCK"), "warnings": top_warn,
            "cash_after": round(float(cash), 2),
            "today": {"orders_placed": len(done), "entries_placed": len(entries),
                      "loss_at_risk": round(float(loss_today), 2)}}


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["kind"]))
        if r.get("plain"):
            lines.append("          " + r["plain"])
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s (session %s). Free cash after the passed orders: %s (%s)."
                 % (res["pass"], res["block"], res["now_utc"], res["session"], money(res["cash_after"]),
                    "testnet" if res["testnet"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to Binance. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", {})
    out, times = {}, {}
    detail = pdoc.get("detail") or {}
    for s, p in (prices or {}).items():
        if _d(p) is None:
            continue
        s = str(s).upper()
        out[s] = p
        d = detail.get(s) or {}
        t = None
        for key in ("fetched", "time"):
            if d.get(key):
                try:
                    t = tsess.parse_now(d[key])
                    break
                except tsess.SessionError:
                    pass
        if t is None and pdoc.get("as_of"):
            try:
                t = tsess.parse_now(pdoc["as_of"])
            except tsess.SessionError:
                t = None
        times[s] = t
    return out, times


def rules_loader(a, cfg):
    cache = {}

    def get(sym):
        if sym in cache:
            return cache[sym]
        try:
            cl = None if a.exchange_info else bc.client(cfg, live=a.live)
            cache[sym] = sf.rules_for(sym, a.exchange_info, a.cache, cl)
        except sf.FilterError:
            cache[sym] = None
        except (bc.BinanceError, OSError, ValueError) as e:
            raise RiskError("exchange rules for %s: %s" % (sym, e))
        return cache[sym]
    return get


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits and Binance's rules "
                                             "(trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default=trader_config.DEFAULT_PATH)
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account (ticket.py account); if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed")
    ap.add_argument("--exchange-info", metavar="FILE", help="a saved exchangeInfo answer, no network")
    ap.add_argument("--cache", default="trader/cache")
    ap.add_argument("--live", action="store_true", help="check against the live API (needs \"testnet\": false)")
    ap.add_argument("--now", help="pretend it is this UTC time, e.g. \"2026-10-19 02:01\"")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = trader_config.load(a.config)
        now = tsess.parse_now(a.now)
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        prices, times = load_prices(read_json(a.prices, "prices", required=False) or {})
        state = read_json(a.state, "broker state", required=False)
        res = check(orders, cfg, rules_loader(a, cfg), portfolio, prices, times, state, now)
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res, checked_at=tsess.iso(now), orders_file=a.orders)
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, a.out)
    except (RiskError, trader_config.ConfigFileError, tsess.SessionError) as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
