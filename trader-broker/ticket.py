#!/usr/bin/env python3
"""Order tickets, your yes, and the Binance spot calls for trader-broker. Standard library only.

This is the only tool in the pack that can send an order, and only through three gates,
every time, for every order:

  1. trader-risk passed THIS exact order in this session (matched by fingerprint),
  2. you typed an explicit yes for this one order (approve records it word for word and
     hands out a one-time confirmation code), and
  3. place is called with that code within APPROVAL_MINUTES of the yes.

Testnet (https://testnet.binance.vision) by default. Live only with "testnet": false in
trader/config.json AND --live on the command line. Keys from BINANCE_API_KEY and
BINANCE_API_SECRET in the environment, never printed. No withdrawals, no margin, no
futures: the shared client cannot call those endpoints.

    python3 ticket.py account                    # balances -> trader/portfolio.json (read only)
    python3 ticket.py show                       # every ticket, READY or not
    python3 ticket.py validate --index 1         # Binance's own check (POST /api/v3/order/test), sends nothing
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py place --index 1 --confirm 3f9a1c
    python3 ticket.py protect --index 1          # set the approved OCO if place could not (no second order)
    python3 ticket.py status                     # the entry and its OCO at Binance; records fills
    python3 ticket.py flatten --symbol BTCUSDT   # a closing ticket: cancel the OCO, sell at market
    python3 ticket.py decline --index 1
    python3 ticket.py log

What place does for an entry (Binance has no bracket order):
  a. POST /api/v3/order: the BUY (MARKET, or LIMIT GTC), newOrderRespType FULL.
  b. From the answer: executedQty and the fills' commission. Binance takes the fee in
     the coin you buy, so what can be sold is executedQty minus that commission.
  c. POST /api/v3/orderList/oco at once: SELL that quantity (down to the step, never
     more than the ticket says), take-profit LIMIT_MAKER above, STOP_LOSS (or
     STOP_LOSS_LIMIT) below. If the OCO fails, the position is UNPROTECTED: it says so
     loudly, records it, and never retries by itself (protect, or flatten).
  A LIMIT buy that has not filled yet waits: status shows when it filled, protect sets the OCO.

Everything is written to trader/state/broker.json (append only). Times are UTC; the
session is the one in trader/config.json. --now pins the clock.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMMON = next((p for p in (os.path.join(_HERE, "common"), os.path.join(_HERE, os.pardir, "common"))
                if os.path.isfile(os.path.join(p, "binance_client.py"))), os.path.join(_HERE, os.pardir, "common"))
_COMMON = os.path.abspath(_COMMON)
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)
try:
    import binance_client as bc
    import order_math as om
    import symbol_filters as sf
    import trader_config
    import trading_session as tsess
except ImportError:                                            # pragma: no cover
    sys.exit("ticket.py: the shared folder common/ is missing next to this skill (copy it too)")

APPROVAL_MINUTES = 5
YES_WORDS = ("yes", "y", "ja")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each", "alle", "beide"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later", "hold", "unless",
               "if", "nein", "nicht", "warte", "aber", "vielleicht", "später", "spater", "wenn", "falls"}
D = sf.D
CLIENT = None   # tests put a client here


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def append(path, rec):
    st = load_state(path)
    st.setdefault("orders", []).append(rec)
    write_json(path, st)
    return st


# ---------------------------------------------------------------- the ticket

def num(x):
    return format(D(x).normalize(), "f")


def money(x):
    return "{:,.2f}".format(float(x))


def plain_parts(order, venue="testnet"):
    p = order.get("plan") or {}
    o = order.get("order") or {}
    parts = []
    if order.get("cancel_order_list"):
        parts.append("CANCEL the open OCO %s on %s first" % (order["cancel_order_list"].get("orderListId"),
                                                               order.get("symbol")))
    how = "at market" if o.get("type") == "MARKET" else "limit %s, good till canceled" % o.get("price")
    parts.append("%s %s %s %s" % (order.get("side"), o.get("quantity"), order.get("symbol"), how))
    oco = order.get("oco")
    if oco:
        stop = "stop %s" % oco.get("belowStopPrice")
        if oco.get("belowType") == "STOP_LOSS_LIMIT":
            stop += " (stop-limit, sells down to %s)" % oco.get("belowPrice")
        parts.append("then at once an OCO: SELL %s, take-profit %s / %s, whichever comes first" % (
            oco.get("quantity"), oco.get("abovePrice") or oco.get("aboveStopPrice"), stop))
        if p.get("max_loss") is not None:
            parts.append("max loss about %s if the stop fills at %s, fees in (it can fill worse in a fast market)"
                         % (money(p["max_loss"]), p.get("worst_stop_fill", oco.get("belowStopPrice"))))
        if p.get("gain_at_target") is not None:
            parts.append("gain about %s at the take-profit, fees in" % money(p["gain_at_target"]))
        if p.get("exit_by"):
            parts.append("out by %s at the latest (max hold): trader-alerts rings, flatten closes it"
                         % p["exit_by"].replace("T", " ").replace("Z", " UTC"))
    return parts


def plain(order):
    return " / ".join(plain_parts(order))


def amount_numbers(order):
    nums = []
    o, oco, p = order.get("order") or {}, order.get("oco") or {}, order.get("plan") or {}
    for v in (o.get("quantity"), o.get("price"), oco.get("quantity"), oco.get("abovePrice"),
              oco.get("belowStopPrice"), oco.get("belowPrice"), p.get("entry"), p.get("max_loss"), p.get("cost")):
        try:
            if v is not None:
                nums.append(D(v))
        except sf.FilterError:
            pass
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-zäöüß']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("symbol", "")).upper()
    pairs = set(re.findall(r"\b[A-Z0-9]{2,15}?(?:USDT|USDC|FDUSD|BTC|ETH|BNB|EUR|TRY|BRL|JPY)\b", raw.upper()))
    words_up = set(re.findall(r"\b[A-Z0-9]{2,20}\b", raw.upper()))
    others = {s.upper() for s in batch_symbols} - {sym}
    named = (pairs - {sym}) | (words_up & others)
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [D(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) <= D("0.00000001") for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (", ".join(num(n) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state checks

def latest(state, fp, session, statuses=None):
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec.get("session") == session and \
                (statuses is None or rec.get("status") in statuses):
            found = rec
    return found


def risk_verdict(risk, fp, session):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("session") != session:
        return None, "the risk check is from session %s, not %s; run check.py again" % (risk.get("session"), session)
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, session):
    """(ready, label, risk_result)"""
    fp = om.fingerprint(order)
    rv, why = risk_verdict(risk, fp, session)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, session)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for place --confirm" % rec.get("at"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


def venue_words(cfg, live):
    return "LIVE ACCOUNT, REAL MONEY" if (cfg.get("testnet") is False and live) else "TESTNET, no real money"


def render(order, idx, total, cfg, risk, state, session, live=False):
    ready, label, rv = status_of(order, risk, state, session)
    lines = ["ORDER %d of %d, Binance spot, %s" % (idx, total, venue_words(cfg, live))]
    for part in plain_parts(order):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    lines.append("  place sends exactly:")
    if order.get("cancel_order_list"):
        lines.append("    DELETE /api/v3/orderList " + json.dumps(dict({"symbol": order["symbol"]},
                                                                      **order["cancel_order_list"])))
    lines.append("    POST /api/v3/order " + json.dumps(om.order_params(order)))
    if order.get("oco"):
        lines.append("    POST /api/v3/orderList/oco " + json.dumps(om.oco_params(order)))
        lines.append("    (the OCO quantity is what actually arrived after the buy's fee, never more than this)")
    if ready:
        lines.append("  Reply \"yes\" to place this one order (entry and its OCO), or \"no\" to skip it.")
    return "\n".join(lines), ready


# ---------------------------------------------------------------- Binance calls

def client_for(cfg, live):
    if CLIENT is not None:
        return CLIENT
    try:
        return bc.client(cfg, live=live, signed=True)
    except bc.BinanceError as e:
        raise TicketError(str(e))


def rules_for(a, cfg, cl, sym):
    try:
        return sf.rules_for(sym, getattr(a, "exchange_info", None), getattr(a, "cache", "trader/cache"), cl)
    except (sf.FilterError, bc.BinanceError, OSError, ValueError) as e:
        raise TicketError("exchange rules for %s: %s" % (sym, e))


def received_qty(resp, base):
    """executedQty minus the commission Binance took in the base asset."""
    got = D(resp.get("executedQty") or 0)
    for f in resp.get("fills") or []:
        if str(f.get("commissionAsset", "")).upper() == str(base).upper():
            got -= D(f.get("commission") or 0)
    return got


def avg_price(resp):
    q = D(resp.get("executedQty") or 0)
    quote = D(resp.get("cummulativeQuoteQty") or 0)
    return (quote / q) if q > 0 else None


def write_portfolio(path, acct, cfg, now):
    quote = cfg["quote_asset"]
    bal = {}
    locked = {}
    for b in acct.get("balances") or []:
        a, f, l = str(b.get("asset", "")).upper(), D(b.get("free") or 0), D(b.get("locked") or 0)
        if f > 0 or l > 0:
            bal[a] = format(f.normalize(), "f")
            if l > 0:
                locked[a] = format(l.normalize(), "f")
    doc = {"cash": bal.get(quote, "0"), "quote_asset": quote, "balances": bal, "locked": locked,
           "account_type": acct.get("accountType"), "can_trade": acct.get("canTrade"),
           "updated": tsess.iso(now), "venue": "testnet" if cfg.get("testnet", True) else "live",
           "note": "Free balances from GET /api/v3/account. Coins held by an open OCO show under locked."}
    write_json(path, doc)
    return doc


# ---------------------------------------------------------------- commands

def _clock(a, cfg):
    now = tsess.parse_now(getattr(a, "now", None)) if getattr(a, "now", None) else tsess.utc_now()
    return now, tsess.current(now, cfg)["date"]


def _cfg(a):
    try:
        return trader_config.load(a.config)
    except trader_config.ConfigFileError as e:
        raise TicketError(str(e))


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = _cfg(a)
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, session = _clock(a, cfg)
    return orders, cfg, risk, state, session, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_account(a):
    cfg = _cfg(a)
    now, _ = _clock(a, cfg)
    cl = client_for(cfg, a.live)
    try:
        acct = cl.account()
    except bc.BinanceError as e:
        raise TicketError(str(e))
    doc = write_portfolio(a.portfolio, acct, cfg, now)
    print("Binance %s account: %s %s free%s." % (cl.venue, doc["cash"], cfg["quote_asset"],
                                                 (", locked in orders: " + ", ".join("%s %s" % (v, k) for k, v in
                                                                                     doc["locked"].items()))
                                                 if doc["locked"] else ""))
    others = {k: v for k, v in doc["balances"].items() if k != cfg["quote_asset"]}
    if others:
        print("Coins: " + ", ".join("%s %s" % (v, k) for k, v in sorted(others.items())))
    print("Wrote %s. Read only: nothing was placed." % a.portfolio)
    return 0


def cmd_show(a):
    orders, cfg, risk, state, session, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = []
        for i, o in picks:
            ready, label, rv = status_of(o, risk, state, session)
            out.append({"index": i, "ready": ready, "status": label, "plain": plain(o), "fingerprint": om.fingerprint(o),
                        "testnet": venue_words(cfg, a.live).startswith("TESTNET"), "order": om.order_params(o),
                        "oco": om.oco_params(o) if o.get("oco") else None})
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, session, a.live)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, not financial advice.")
    return 0 if any_ready else 1


def cmd_validate(a):
    orders, cfg, risk, state, session, now = _load(a)
    order = _pick(orders, a.index)
    cl = client_for(cfg, a.live)
    try:
        cl.new_order(om.order_params(order), test=True)
    except bc.BinanceError as e:
        print("Binance (%s) would reject order %d: %s" % (cl.venue, a.index, e))
        return 1
    print("Binance (%s) accepts order %d as written (POST /api/v3/order/test: nothing was placed). Binance has no "
          "test call for the OCO; trader-risk checked its filters." % (cl.venue, a.index))
    return 0


def cmd_approve(a):
    orders, cfg, risk, state, session, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, session)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    code = secrets.token_hex(3)
    plan = order.get("plan") or {}
    rec = {"at": tsess.iso(now), "session": session, "index": a.index, "fingerprint": om.fingerprint(order),
           "symbol": order["symbol"], "side": order["side"], "kind": order.get("kind"),
           "max_loss": plan.get("max_loss"), "plain": plain(order), "status": "approved", "reply": a.reply,
           "code_sha256": hashlib.sha256(code.encode()).hexdigest(), "venue": venue_words(cfg, a.live)}
    append(a.state, rec)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order)))
    print("Confirmation code: %s (valid %d minutes, for this one order)." % (code, APPROVAL_MINUTES))
    print("Now: ticket.py place --index %d --confirm %s%s" % (a.index, code, " --live" if a.live else ""))
    return 0


def _gate(a, orders, cfg, risk, state, session, now):
    """All three gates for place. Returns (order, approval record) or raises."""
    order = _pick(orders, a.index)
    fp = om.fingerprint(order)
    rv, why = risk_verdict(risk, fp, session)
    if rv is None:
        raise TicketError("not placed: %s" % why)
    if rv.get("verdict") != "PASS":
        raise TicketError("not placed: trader-risk blocked this order: %s" % "; ".join(rv.get("reasons", [])))
    rec = latest(state, fp, session)
    if not rec or rec.get("status") != "approved":
        raise TicketError("not placed: there is no open yes for order %d in this session (show, then approve with "
                          "the user's own words)" % a.index)
    if not a.confirm or hashlib.sha256(a.confirm.strip().encode()).hexdigest() != rec.get("code_sha256"):
        raise TicketError("not placed: the confirmation code does not match the yes for order %d" % a.index)
    age = (now - tsess.parse_now(rec["at"])).total_seconds()
    if age > APPROVAL_MINUTES * 60 or age < -60:
        raise TicketError("not placed: the yes is %d minutes old (valid %d). Prices move: show the ticket again and "
                          "ask again" % (age // 60, APPROVAL_MINUTES))
    if rec.get("venue") != venue_words(cfg, a.live):
        raise TicketError("not placed: the yes was for the %s, this call goes to the %s" % (rec.get("venue"),
                                                                                           venue_words(cfg, a.live)))
    return order, rec


def _place_oco(cl, order, rules, sell_qty, a, session, now, base_rec):
    qty = min(sf.floor_step(sell_qty, rules["step"]), D(order["oco"]["quantity"]))
    params = om.oco_params(order, sf.fmt(qty, rules["step"]))
    params["listClientOrderId"] = "dts-%s-oco" % base_rec["fingerprint"]
    params["newOrderRespType"] = "RESULT"
    try:
        resp = cl.new_oco(params)
    except bc.BinanceError as e:
        append(a.state, dict(base_rec, at=tsess.iso(now), status="unprotected", note=str(e)))
        print("!!! THE POSITION IS UNPROTECTED: the OCO was refused: %s" % e)
        print("!!! %s %s are held with NO stop at Binance. Tell the user now. Options: ticket.py protect --index %d "
              "(the same approved OCO, after fixing the cause) or ticket.py flatten --symbol %s (a new ticket, a new "
              "yes). Nothing is retried by itself." % (sf.fmt(qty, rules["step"]), rules["base"], a.index,
                                                       order["symbol"]))
        return None
    append(a.state, dict(base_rec, at=tsess.iso(now), status="protected", oco_list_id=resp.get("orderListId"),
                         oco_quantity=sf.fmt(qty, rules["step"]),
                         oco_order_ids=[x.get("orderId") for x in resp.get("orders") or []]))
    print("OCO set: SELL %s %s, take-profit %s / stop %s (order list %s)." % (
        sf.fmt(qty, rules["step"]), order["symbol"], params.get("abovePrice"), params.get("belowStopPrice"),
        resp.get("orderListId")))
    return resp


def cmd_place(a):
    orders, cfg, risk, state, session, now = _load(a)
    order, rec = _gate(a, orders, cfg, risk, state, session, now)
    cl = client_for(cfg, a.live)
    fp = rec["fingerprint"]
    base_rec = {"session": session, "index": a.index, "fingerprint": fp, "symbol": order["symbol"],
                "side": order["side"], "kind": order.get("kind"), "plain": rec["plain"], "venue": rec["venue"]}
    rules = rules_for(a, cfg, cl, order["symbol"])
    if order.get("cancel_order_list"):
        try:
            cl.cancel_order_list(order["symbol"], order_list_id=order["cancel_order_list"].get("orderListId"))
            append(a.state, dict(base_rec, at=tsess.iso(now), status="oco_canceled",
                                 oco_list_id=order["cancel_order_list"].get("orderListId")))
            print("Canceled OCO %s." % order["cancel_order_list"].get("orderListId"))
        except bc.BinanceError as e:
            append(a.state, dict(base_rec, at=tsess.iso(now), status="failed", note="cancel: %s" % e))
            raise TicketError("not placed: canceling the OCO failed (%s). Check ticket.py status; the coins may be "
                              "sold by the OCO already" % e)
    params = om.order_params(order)
    params["newClientOrderId"] = "dts-%s-%s" % (fp, "e" if order.get("kind") == "entry" else "x")
    params["newOrderRespType"] = "FULL"
    try:
        resp = cl.new_order(params)
    except bc.BinanceError as e:
        append(a.state, dict(base_rec, at=tsess.iso(now), status="failed", note=str(e)))
        raise TicketError("Binance refused order %d: %s. Nothing was bought or sold. A failed order is not retried "
                          "without a new risk check and a new yes." % (a.index, e))
    got = received_qty(resp, rules["base"])
    px = avg_price(resp)
    placed = dict(base_rec, at=tsess.iso(now), status="placed", order_id=resp.get("orderId"),
                  client_order_id=resp.get("clientOrderId"), order_status=resp.get("status"),
                  executed_qty=resp.get("executedQty"), avg_price=format(px, "f") if px else None,
                  received_qty=format(got.normalize(), "f"), max_loss=(order.get("plan") or {}).get("max_loss"),
                  exit_by=(order.get("plan") or {}).get("exit_by"))
    append(a.state, placed)
    print("Placed on Binance %s: %s %s %s, status %s%s (order %s)." % (
        cl.venue, order["side"], resp.get("executedQty"), order["symbol"], resp.get("status"),
        (" at about %s" % num(px.quantize(D(rules["tick"])))) if px else "", resp.get("orderId")))
    if order.get("kind") == "entry" and order.get("oco"):
        if resp.get("status") == "FILLED":
            print("Received %s %s after the fee." % (num(got), rules["base"]))
            _place_oco(cl, order, rules, got, a, session, now, base_rec)
        else:
            print("The buy is %s, not filled yet: no OCO until it fills. ticket.py status shows it; then ticket.py "
                  "protect --index %d sets the approved OCO." % (resp.get("status"), a.index))
    _refresh(cl, a, cfg, now)
    return 0


def cmd_protect(a):
    orders, cfg, risk, state, session, now = _load(a)
    order = _pick(orders, a.index)
    fp = om.fingerprint(order)
    placed = latest(state, fp, session, ("placed",))
    if not placed or order.get("kind") != "entry" or not order.get("oco"):
        raise TicketError("order %d has no placed entry in this session to protect" % a.index)
    if latest(state, fp, session, ("protected",)):
        raise TicketError("order %d already has its OCO (ticket.py status shows it)" % a.index)
    cl = client_for(cfg, a.live)
    rules = rules_for(a, cfg, cl, order["symbol"])
    try:
        resp = cl.get_order(order["symbol"], order_id=placed.get("order_id"))
    except bc.BinanceError as e:
        raise TicketError("could not read the entry order: %s" % e)
    if resp.get("status") not in ("FILLED", "PARTIALLY_FILLED"):
        print("The entry is %s: nothing to protect yet." % resp.get("status"))
        return 1
    got = D(placed.get("received_qty") or 0)
    if resp.get("status") != "FILLED" or not got:
        got = D(resp.get("executedQty") or 0) * (1 - D(cfg.get("fee_rate", 0.001)))
    base_rec = {"session": session, "index": a.index, "fingerprint": fp, "symbol": order["symbol"],
                "side": order["side"], "kind": "entry", "plain": placed.get("plain"), "venue": placed.get("venue")}
    return 0 if _place_oco(cl, order, rules, got, a, session, now, base_rec) else 1


def _refresh(cl, a, cfg, now):
    try:
        write_portfolio(a.portfolio, cl.account(), cfg, now)
    except bc.BinanceError as e:
        print("Could not refresh %s: %s" % (a.portfolio, e))


def cmd_status(a):
    cfg = _cfg(a)
    now, session = _clock(a, cfg)
    state = load_state(a.state)
    cl = client_for(cfg, a.live)
    recs = [r for r in state.get("orders", []) if r.get("status") in ("placed", "protected") and
            (a.all or r.get("session") == session)]
    if not recs:
        print("Nothing placed in session %s." % session)
        return 1
    seen = set()
    for r in recs:
        key = (r.get("fingerprint"), r.get("status"))
        if key in seen:
            continue
        seen.add(key)
        try:
            if r["status"] == "placed" and r.get("order_id"):
                o = cl.get_order(r["symbol"], order_id=r["order_id"])
                print("%s %s order %s: %s, %s of %s filled." % (r["side"], r["symbol"], r["order_id"], o.get("status"),
                                                                o.get("executedQty"), o.get("origQty")))
            elif r["status"] == "protected" and r.get("oco_list_id") is not None:
                ol = cl.get_order_list(order_list_id=r["oco_list_id"])
                print("OCO %s on %s: %s / %s." % (r["oco_list_id"], r["symbol"], ol.get("listStatusType"),
                                                  ol.get("listOrderStatus")))
                if ol.get("listOrderStatus") == "ALL_DONE" and not latest(state, r["fingerprint"], r["session"],
                                                                         ("closed",)):
                    filled = None
                    for x in ol.get("orders") or []:
                        o = cl.get_order(r["symbol"], order_id=x.get("orderId"))
                        if o.get("status") == "FILLED":
                            filled = o
                    if filled:
                        px = avg_price(filled)
                        leg = "take-profit" if filled.get("type") in ("LIMIT_MAKER", "TAKE_PROFIT",
                                                                     "TAKE_PROFIT_LIMIT") else "stop"
                        append(a.state, {"at": tsess.iso(now), "session": r["session"], "fingerprint": r["fingerprint"],
                                         "symbol": r["symbol"], "side": "SELL", "kind": "exit", "status": "closed",
                                         "leg": leg, "executed_qty": filled.get("executedQty"),
                                         "avg_price": format(px, "f") if px else None})
                        print("  Closed by the %s: SELL %s at about %s. Recorded." % (leg, filled.get("executedQty"),
                                                                                     num(px) if px else "?"))
        except bc.BinanceError as e:
            print("Could not read %s: %s" % (r.get("symbol"), e))
    _refresh(cl, a, cfg, now)
    return 0


def cmd_flatten(a):
    cfg = _cfg(a)
    now, session = _clock(a, cfg)
    sym = a.symbol.strip().upper()
    state = load_state(a.state)
    prot = [r for r in state.get("orders", []) if r.get("symbol") == sym and r.get("status") == "protected"]
    closed = {r.get("fingerprint") for r in state.get("orders", []) if r.get("status") in ("closed", "oco_canceled")}
    open_oco = [r for r in prot if r.get("fingerprint") not in closed]
    if a.qty is not None:
        qty, source = D(a.qty), "--qty"
    elif open_oco:
        qty, source = D(open_oco[-1]["oco_quantity"]), "the open OCO"
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        try:
            rules = sf.rules_for(sym, getattr(a, "exchange_info", None), a.cache, None)
            base = rules["base"]
        except sf.FilterError:
            base = sym[:-len(cfg["quote_asset"])] if sym.endswith(cfg["quote_asset"]) else sym
        qty, source = D((pf.get("balances") or {}).get(base) or 0), a.portfolio
    if qty <= 0:
        raise TicketError("nothing to flatten: no %s found (open OCO, portfolio). Run ticket.py account, or pass --qty"
                          % sym)
    order = {"symbol": sym, "side": "SELL", "kind": "exit", "order": {"type": "MARKET", "quantity": format(qty, "f")},
             "why": "max hold reached or your call: closes the %s trade (quantity from %s)" % (sym, source)}
    if open_oco:
        order["cancel_order_list"] = {"orderListId": open_oco[-1]["oco_list_id"]}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    write_json(a.orders, {"created_by": "trader-broker", "created_at": tsess.iso(now), "status": "proposed",
                          "testnet": bool(cfg.get("testnet", True)), "source": "ticket.py flatten %s" % sym,
                          "note": "Closing ticket. Nothing is placed until trader-risk passes it and you say yes.",
                          "orders": [order]})
    print("Flatten %s: %s." % (sym, plain(order)))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)" % (replaced, "" if replaced == 1
                                                                                       else "s")) if replaced else ""))
    print("Next: trader-risk (check.py; a sell of coins you hold is never blocked by the entry limits), then "
          "ticket.py show, the user's yes for this one order, and place.")
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, session, now = _load(a)
    order = _pick(orders, a.index)
    append(a.state, {"at": tsess.iso(now), "session": session, "index": a.index, "fingerprint": om.fingerprint(order),
                     "symbol": order.get("symbol"), "side": order.get("side"), "kind": order.get("kind"),
                     "plain": plain(order), "status": "declined", "reply": a.reply or "no"})
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order)))
    return 0


def cmd_log(a):
    cfg = _cfg(a)
    now, session = _clock(a, cfg)
    state = load_state(a.state)
    rows = [r for r in state.get("orders", []) if a.all or r.get("session") == session]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " in session %s" % session))
        return 1
    for r in rows:
        extra = (" order %s" % r["order_id"]) if r.get("order_id") else ""
        extra += (" OCO %s" % r["oco_list_id"]) if r.get("oco_list_id") is not None else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        print("%s  #%s %-12s %s%s%s" % (r.get("at"), r.get("index", " "), r.get("status"), r.get("plain") or r.get(
            "symbol"), reply, extra))
    print("%d placed%s." % (sum(1 for r in rows if r.get("status") == "placed"),
                            "" if a.all else " in session %s" % session))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets, your yes and the Binance spot calls (trader-broker).")
    sub = ap.add_subparsers(dest="cmd")

    def base(p):
        p.add_argument("--config", default=trader_config.DEFAULT_PATH)
        p.add_argument("--state", default="trader/state/broker.json")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--live", action="store_true", help="the live API (needs \"testnet\": false as well)")
        p.add_argument("--now", help="pretend it is this UTC time")

    def common(p):
        base(p)
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--exchange-info", metavar="FILE", help="a saved exchangeInfo answer")
        p.add_argument("--cache", default="trader/cache")

    p = sub.add_parser("account", help="balances -> trader/portfolio.json (read only)")
    base(p)
    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("validate", help="Binance's test endpoint for the entry order (sends nothing)")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("place", help="send one approved order (and its OCO) to Binance")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--confirm", required=True, help="the code approve printed")
    p = sub.add_parser("protect", help="set the approved OCO of a placed entry")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p = sub.add_parser("status", help="the placed orders and OCOs at Binance; records fills")
    base(p)
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("flatten", help="a closing ticket: cancel the OCO, sell at market")
    base(p)
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="how much to sell (default: the open OCO's quantity, else the portfolio)")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--exchange-info", metavar="FILE")
    p.add_argument("--cache", default="trader/cache")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("log", help="this session's decisions")
    base(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"account": cmd_account, "show": cmd_show, "validate": cmd_validate, "approve": cmd_approve,
                "place": cmd_place, "protect": cmd_protect, "status": cmd_status, "flatten": cmd_flatten,
                "decline": cmd_decline, "log": cmd_log}[a.cmd](a)
    except (TicketError, tsess.SessionError) as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1
    except bc.BinanceError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
