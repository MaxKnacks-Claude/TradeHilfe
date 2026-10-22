#!/usr/bin/env python3
"""The order shape and the money math shared by trader-budget, trader-risk and trader-broker.

An order in trader/orders.json, Binance spot parameter names, numbers as strings:

    {"symbol": "BTCUSDT", "side": "BUY", "kind": "entry",
     "order": {"type": "MARKET", "quantity": "0.00054"},                  # POST /api/v3/order
     "oco":   {"side": "SELL", "quantity": "0.00053",                     # POST /api/v3/orderList/oco
               "aboveType": "LIMIT_MAKER", "abovePrice": "53007.07",
               "belowType": "STOP_LOSS", "belowStopPrice": "52545.29"},
     "plan": {...}, "why": "..."}                                          # ours, never sent

A closing order ("kind": "exit") has "side": "SELL", an "order" (a MARKET sell) and,
when an OCO still holds the coins, "cancel_order_list": {"orderListId": N} (DELETE
/api/v3/orderList first, then the sell).

Fees on Binance spot (fee_rate, 0.1 % by default, paid in the asset you receive unless
you pay with BNB): a buy of Q at E costs Q x E and delivers Q x (1 - f) coins; selling
Q' of them at X brings Q' x X x (1 - f). So the protective OCO can only sell
Q' = Q x (1 - f) rounded DOWN to the step size, and

    loss at the stop    = Q x E - Q' x W x (1 - f)      W = the worst stop fill we plan for
    gain at the target  = Q' x T x (1 - f) - Q x E

W is the stop price for a STOP_LOSS (market) leg, or the limit of a STOP_LOSS_LIMIT leg
(stop_limit_offset_pct under the stop). Both are estimates: a fast market can fill worse,
and a stop-limit can stay unfilled.
"""

import hashlib
import json

import symbol_filters as sf

ORDER_FIELDS = ("type", "quantity", "quoteOrderQty", "price", "timeInForce", "newClientOrderId")
OCO_FIELDS = ("side", "quantity", "aboveType", "abovePrice", "aboveStopPrice", "aboveTimeInForce",
              "belowType", "belowPrice", "belowStopPrice", "belowTimeInForce", "listClientOrderId")
ORDER_TYPES = {"MARKET", "LIMIT"}
ABOVE_TYPES = {"LIMIT_MAKER", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"}
BELOW_TYPES = {"STOP_LOSS", "STOP_LOSS_LIMIT"}
TIFS = {"GTC", "IOC", "FOK"}
TOP_FIELDS = ("symbol", "side", "kind", "order", "oco", "cancel_order_list")


class OrderError(Exception):
    pass


def fingerprint(order):
    """The exact order: everything that is sent, nothing that is ours. Same in risk and broker."""
    core = {}
    for k in TOP_FIELDS:
        v = order.get(k)
        if v is None:
            continue
        if isinstance(v, dict):
            v = {kk: str(vv) for kk, vv in v.items() if vv is not None}
        core[k] = v
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def order_params(order):
    """The parameters for POST /api/v3/order: Binance names, strings. Our extras never go out."""
    p = {"symbol": order["symbol"], "side": order["side"]}
    for k in ORDER_FIELDS:
        v = (order.get("order") or {}).get(k)
        if v is not None:
            p[k] = str(v)
    return p


def oco_params(order, quantity=None):
    """The parameters for POST /api/v3/orderList/oco. `quantity` overrides the planned one
    (the broker uses what actually arrived after the buy's fee)."""
    oco = order.get("oco") or {}
    p = {"symbol": order["symbol"]}
    for k in OCO_FIELDS:
        v = oco.get(k)
        if v is not None:
            p[k] = str(v)
    if quantity is not None:
        p["quantity"] = str(quantity)
    return p


def sell_quantity(rules, qty, fee):
    """What the OCO can sell after a buy of qty: qty x (1 - fee), down to the step."""
    return sf.floor_step(sf.D(qty) * (1 - sf.D(fee)), rules["step"])


def worst_stop_fill(rules, stop, offset_pct):
    """(belowType, the price we count the loss at, the stop-limit price or None)."""
    if "STOP_LOSS" in rules["order_types"]:
        return "STOP_LOSS", sf.D(stop), None
    lim = sf.floor_tick(sf.D(stop) * (1 - sf.D(offset_pct) / 100), rules["tick"])
    return "STOP_LOSS_LIMIT", lim, lim


def money(rules, qty, entry, stop, target, fee, offset_pct):
    """The exact outcome of buying qty at entry with the OCO at stop / target, fees in."""
    q = sf.D(qty)
    e, s, t, f = sf.D(entry), sf.D(stop), sf.D(target), sf.D(fee)
    below, worst, limit = worst_stop_fill(rules, s, offset_pct)
    q_sell = sell_quantity(rules, q, f)
    cost = q * e
    loss = cost - q_sell * worst * (1 - f)
    gain = q_sell * t * (1 - f) - cost
    fees = q * e * f + q_sell * worst * f
    return {"qty": q, "qty_sell": q_sell, "cost": cost, "worst_exit": worst, "below_type": below,
            "stop_limit": limit, "max_loss": loss, "gain_at_target": gain, "fees_at_stop": fees,
            "reward_risk_net": (gain / loss) if loss > 0 else sf.D(0)}


def build_oco(rules, qty_sell, stop, target, offset_pct):
    """The protective OCO: take-profit as a LIMIT_MAKER above, the stop below."""
    below, _, limit = worst_stop_fill(rules, stop, offset_pct)
    oco = {"side": "SELL", "quantity": sf.fmt(qty_sell, rules["step"]), "aboveType": "LIMIT_MAKER",
           "abovePrice": sf.fmt(sf.round_tick(target, rules["tick"]), rules["tick"]), "belowType": below,
           "belowStopPrice": sf.fmt(sf.floor_tick(stop, rules["tick"]), rules["tick"])}
    if below == "STOP_LOSS_LIMIT":
        oco["belowPrice"] = sf.fmt(limit, rules["tick"])
        oco["belowTimeInForce"] = "GTC"
    return oco


def describe(order):
    """One plain line: BUY 0.00054 BTCUSDT at market, then OCO SELL 0.00053: take-profit 53007.07 / stop 52545.29."""
    o = order.get("order") or {}
    head = "%s %s %s %s" % (order.get("side"), o.get("quantity"), order.get("symbol"),
                            "at market" if o.get("type") == "MARKET" else "limit %s %s" % (o.get("price"),
                                                                                            o.get("timeInForce", "")))
    if order.get("cancel_order_list"):
        head = "cancel OCO %s, then %s" % (order["cancel_order_list"].get("orderListId"), head)
    oco = order.get("oco")
    if oco:
        stop = "stop %s" % oco.get("belowStopPrice")
        if oco.get("belowType") == "STOP_LOSS_LIMIT":
            stop += " (limit %s)" % oco.get("belowPrice")
        head += ", then OCO SELL %s: take-profit %s / %s" % (oco.get("quantity"), oco.get("abovePrice"), stop)
    return head
