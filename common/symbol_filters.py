#!/usr/bin/env python3
"""Binance symbol rules: tickSize, stepSize, notional. Exact Decimal math, standard library only.

From exchangeInfo, per symbol (Binance spot docs, filters.md):
  PRICE_FILTER     minPrice, maxPrice, tickSize   (price and stopPrice; 0 disables a rule)
  LOT_SIZE         minQty, maxQty, stepSize       (quantity of any order)
  MARKET_LOT_SIZE  minQty, maxQty, stepSize       (quantity of a MARKET order, on top of LOT_SIZE)
  NOTIONAL         minNotional, applyMinToMarket, maxNotional, applyMaxToMarket, avgPriceMins
  MIN_NOTIONAL     minNotional, applyToMarket     (the older filter; some symbols still carry it)

Rounding used by the skills:
  quantities   always DOWN to the step (never buy or sell more than sized)
  a long stop  DOWN to the tick (away from the entry)
  a target     to the NEAREST tick, half up
  a limit buy  DOWN to the tick (never pay more than planned)
"""

from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, ROUND_CEILING

ZERO = Decimal("0")


class FilterError(Exception):
    pass


def D(x, what="a number"):
    """Anything -> Decimal, without float noise (0.1 stays 0.1)."""
    if isinstance(x, Decimal):
        return x
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        return Decimal(str(x).strip())
    except (InvalidOperation, ValueError):
        raise FilterError("%s is not a number: %r" % (what, x))


def _to_step(x, step, rounding):
    x, step = D(x), D(step)
    if step <= 0:
        return x
    return (x / step).to_integral_value(rounding=rounding) * step


def floor_step(qty, step):
    """A quantity, rounded down to the step size."""
    return _norm(_to_step(qty, step, ROUND_DOWN), step)


def floor_tick(price, tick):
    return _norm(_to_step(price, tick, ROUND_FLOOR), tick)


def ceil_tick(price, tick):
    return _norm(_to_step(price, tick, ROUND_CEILING), tick)


def round_tick(price, tick):
    return _norm(_to_step(price, tick, ROUND_HALF_UP), tick)


def _norm(x, step):
    """Keep exactly the step's decimals: 0.00001000 -> 5 places, 1.00000000 -> 0 places."""
    places = decimals(step)
    q = Decimal(1).scaleb(-places)
    return x.quantize(q, rounding=ROUND_DOWN)


def decimals(step):
    """Decimal places a step or tick allows: 0.01000000 -> 2, 1.00000000 -> 0, 0 -> 8."""
    s = D(step)
    if s <= 0:
        return 8
    s = s.normalize()
    exp = s.as_tuple().exponent
    return max(0, -exp)


def fmt(x, step=None):
    """A Decimal as Binance wants it in a request: plain digits, no exponent, the step's decimals."""
    x = D(x)
    if step is not None:
        x = _norm(x, step)
    s = format(x, "f")
    return s


def is_multiple(x, step):
    step = D(step)
    if step <= 0:
        return True
    return (D(x) / step) == (D(x) / step).to_integral_value()


# ---------------------------------------------------------------- rules from exchangeInfo

def find_symbol(info, symbol):
    symbol = symbol.upper()
    for s in (info or {}).get("symbols") or []:
        if str(s.get("symbol", "")).upper() == symbol:
            return s
    raise FilterError("%s is not in the exchange information" % symbol)


def rules_of(symbol_info):
    """One exchangeInfo symbol -> a flat dict of the rules the skills need (Decimals as strings)."""
    s = symbol_info or {}
    f = {x.get("filterType"): x for x in s.get("filters") or []}
    pf, ls, ml = f.get("PRICE_FILTER") or {}, f.get("LOT_SIZE") or {}, f.get("MARKET_LOT_SIZE") or {}
    no, mn = f.get("NOTIONAL"), f.get("MIN_NOTIONAL")
    r = {
        "symbol": s.get("symbol"), "status": s.get("status"),
        "base": s.get("baseAsset"), "quote": s.get("quoteAsset"),
        "base_precision": s.get("baseAssetPrecision"), "quote_precision": s.get("quoteAssetPrecision"),
        "order_types": list(s.get("orderTypes") or []),
        "oco_allowed": bool(s.get("ocoAllowed", s.get("orderListAllowed", False))),
        "spot": bool(s.get("isSpotTradingAllowed", False)),
        "tick": pf.get("tickSize", "0"), "min_price": pf.get("minPrice", "0"), "max_price": pf.get("maxPrice", "0"),
        "step": ls.get("stepSize", "0"), "min_qty": ls.get("minQty", "0"), "max_qty": ls.get("maxQty", "0"),
        "market_step": ml.get("stepSize") or ls.get("stepSize", "0"),
        "market_min_qty": ml.get("minQty") or "0", "market_max_qty": ml.get("maxQty") or "0",
        "min_notional": "0", "max_notional": "0", "min_notional_market": True, "max_notional_market": False,
    }
    if no:
        r["min_notional"] = no.get("minNotional", "0")
        r["max_notional"] = no.get("maxNotional", "0")
        r["min_notional_market"] = bool(no.get("applyMinToMarket", True))
        r["max_notional_market"] = bool(no.get("applyMaxToMarket", False))
    elif mn:
        r["min_notional"] = mn.get("minNotional", "0")
        r["min_notional_market"] = bool(mn.get("applyToMarket", True))
    return r


def check_price(rules, price, what="price"):
    """Reasons a price breaks PRICE_FILTER (empty when it passes)."""
    p, out = D(price), []
    lo, hi, tick = D(rules["min_price"]), D(rules["max_price"]), D(rules["tick"])
    if lo > 0 and p < lo:
        out.append("%s %s is under the minimum price %s" % (what, fmt(p), fmt(lo.normalize())))
    if hi > 0 and p > hi:
        out.append("%s %s is over the maximum price %s" % (what, fmt(p), fmt(hi.normalize())))
    if tick > 0 and not is_multiple(p, tick):
        out.append("%s %s is not a multiple of the tick size %s" % (what, fmt(p), fmt(tick.normalize())))
    return out


def check_qty(rules, qty, market=False):
    """Reasons a quantity breaks LOT_SIZE (and MARKET_LOT_SIZE for a market order)."""
    q, out = D(qty), []
    checks = [("LOT_SIZE", rules["min_qty"], rules["max_qty"], rules["step"])]
    if market:
        checks.append(("MARKET_LOT_SIZE", rules["market_min_qty"], rules["market_max_qty"], rules["market_step"]))
    for name, lo, hi, step in checks:
        lo, hi, step = D(lo), D(hi), D(step)
        if lo > 0 and q < lo:
            out.append("quantity %s is under the %s minimum %s" % (fmt(q), name, fmt(lo.normalize())))
        if hi > 0 and q > hi:
            out.append("quantity %s is over the %s maximum %s" % (fmt(q), name, fmt(hi.normalize())))
        if step > 0 and not is_multiple(q, step):
            out.append("quantity %s is not a multiple of the %s step %s" % (fmt(q), name, fmt(step.normalize())))
    return out


def check_notional(rules, qty, price, market=False):
    """Reasons qty x price breaks NOTIONAL / MIN_NOTIONAL. For a market order `price` is the
    reference (the latest price); Binance itself uses an average price, so stay clear of the edge."""
    n, out = D(qty) * D(price), []
    lo, hi = D(rules["min_notional"]), D(rules["max_notional"])
    if lo > 0 and (not market or rules.get("min_notional_market", True)) and n < lo:
        out.append("order value %s is under the minimum notional %s" % (fmt(n.quantize(Decimal("0.01"))),
                                                                       fmt(lo.normalize())))
    if hi > 0 and (not market or rules.get("max_notional_market", False)) and n > hi:
        out.append("order value %s is over the maximum notional %s" % (fmt(n.quantize(Decimal("0.01"))),
                                                                      fmt(hi.normalize())))
    return out


def check_order(rules, qty, price, market=False, prices=()):
    """Every filter reason for one order: quantity, notional and each named price."""
    out = check_qty(rules, qty, market) + check_notional(rules, qty, price, market)
    for what, p in prices:
        out += check_price(rules, p, what)
    return out
