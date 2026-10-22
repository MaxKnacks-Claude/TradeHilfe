#!/usr/bin/env python3
"""Shared Binance Spot client for the trader-* skills. Standard library only.

Public market data (no key):  ping, time, exchangeInfo, klines, ticker/24hr,
                              ticker/price, avgPrice
Signed (HMAC-SHA256, key):    account, commission, orders, order lists (OCO),
                              open orders, my trades

Safety, by construction:
  - Only the paths in PUBLIC_PATHS and SIGNED_ROUTES can be called. Anything
    else (withdrawals, /sapi, margin, futures) raises before a byte is sent.
  - The testnet (https://testnet.binance.vision) is the default. The live API
    (https://api.binance.com) is used only when the config says
    "testnet": false AND the caller passes live=True (the --live flag).
  - Keys come from the environment only (BINANCE_API_KEY, BINANCE_API_SECRET).
    They are never printed, logged, put in an error message or in repr().

Rate limits (Binance spot docs, "LIMITS"):
  - Every answer carries X-MBX-USED-WEIGHT-1M. At THROTTLE_AT of the limit the
    client sleeps until the next minute starts before it sends again.
  - HTTP 429: Binance asks us to back off for Retry-After seconds. The client
    remembers that, waits it out before the next request, and retries a public
    GET once. A signed request or an order is never retried.
  - HTTP 418: the IP is banned. The client refuses every request locally until
    the ban is over; it never hammers a ban.

Candles: Binance answers klines as arrays of strings. klines_to_chart() turns
them into the Yahoo v8 chart shape the skills already parse, so their bar
logic stays as it was. Times are UTC; a kline's timestamp is its OPEN time.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

TESTNET_URL = "https://testnet.binance.vision"
LIVE_URL = "https://api.binance.com"
UA = "binance-day-trader/1.0 (+day-trader-skill fork; python urllib)"
RECV_WINDOW = 5000
THROTTLE_AT = 0.7          # share of the weight limit at which we pause until the next minute
WEIGHT_LIMIT = 6000        # REQUEST_WEIGHT per minute (exchangeInfo rateLimits); updated from the answer
MAX_WAIT = 120             # never sleep longer than this on our own; longer bans raise instead
INTERVAL_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
                    "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400}

PUBLIC_PATHS = {
    "/api/v3/ping": 1,
    "/api/v3/time": 1,
    "/api/v3/exchangeInfo": 20,
    "/api/v3/klines": 2,
    "/api/v3/ticker/24hr": 2,      # 80 for all symbols; see ticker_24hr()
    "/api/v3/ticker/price": 2,     # 4 for all symbols
    "/api/v3/avgPrice": 2,
}
SIGNED_ROUTES = {
    ("GET", "/api/v3/account"): 20,
    ("GET", "/api/v3/account/commission"): 20,
    ("POST", "/api/v3/order"): 1,
    ("POST", "/api/v3/order/test"): 1,
    ("GET", "/api/v3/order"): 4,
    ("DELETE", "/api/v3/order"): 1,
    ("GET", "/api/v3/openOrders"): 6,
    ("POST", "/api/v3/orderList/oco"): 1,
    ("DELETE", "/api/v3/orderList"): 1,
    ("GET", "/api/v3/orderList"): 4,
    ("GET", "/api/v3/openOrderList"): 6,
    ("GET", "/api/v3/myTrades"): 20,
}
ORDER_ROUTES = {("POST", "/api/v3/order"), ("POST", "/api/v3/orderList/oco")}


class BinanceError(Exception):
    """An answer from Binance that is not a success, or a refusal by this client."""

    def __init__(self, message, status=None, code=None, retry_after=None):
        Exception.__init__(self, message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


class RateLimitError(BinanceError):
    pass


class BannedError(BinanceError):
    pass


class ConfigError(BinanceError):
    pass


# ---------------------------------------------------------------- signing

def sign(secret, payload):
    """HMAC-SHA256 hex digest of the payload (the query string), as Binance wants it."""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def encode_params(params):
    """Ordered key=value&... with percent-encoding; None values are left out."""
    items = [(k, _str(v)) for k, v in params.items() if v is not None]
    return urllib.parse.urlencode(items, quote_via=urllib.parse.quote, safe="")


def _str(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return json.dumps(list(v), separators=(",", ":"))
    return str(v)


# ---------------------------------------------------------------- keys

class _Keys(object):
    """Holds the key pair without ever showing it."""
    __slots__ = ("_key", "_secret")

    def __init__(self, key, secret):
        self._key = key or ""
        self._secret = secret or ""

    def present(self):
        return bool(self._key and self._secret)

    def __repr__(self):
        return "<keys %s>" % ("set" if self.present() else "missing")

    __str__ = __repr__


def keys_from_env(env=None):
    env = os.environ if env is None else env
    return _Keys(env.get("BINANCE_API_KEY"), env.get("BINANCE_API_SECRET"))


# ---------------------------------------------------------------- the client

class BinanceClient(object):
    def __init__(self, base_url=TESTNET_URL, keys=None, opener=None, sleep=time.sleep, clock=time.time,
                 timeout=20):
        if base_url not in (TESTNET_URL, LIVE_URL):
            raise ConfigError("unknown Binance base URL %r: only the spot testnet or the live spot API" % base_url)
        self.base_url = base_url
        self.testnet = base_url == TESTNET_URL
        self._keys = keys or _Keys(None, None)
        self._open = opener or _urlopen
        self._sleep = sleep
        self._clock = clock
        self.timeout = timeout
        self.used_weight = 0
        self.weight_limit = WEIGHT_LIMIT
        self.order_count = {}
        self.backoff_until = 0.0
        self.banned_until = 0.0
        self.time_offset_ms = None
        self.requests = 0

    def __repr__(self):
        return "<BinanceClient %s, %r>" % ("testnet" if self.testnet else "LIVE", self._keys)

    __str__ = __repr__

    @property
    def venue(self):
        return "testnet" if self.testnet else "LIVE"

    # ------------------------------------------------------------ transport

    def _wait_if_needed(self):
        now = self._clock()
        if self.banned_until > now:
            raise BannedError("Binance banned this IP (HTTP 418) for %d more seconds; nothing was sent"
                              % int(self.banned_until - now + 0.999), status=418,
                              retry_after=int(self.banned_until - now + 0.999))
        if self.backoff_until > now:
            wait = self.backoff_until - now
            if wait > MAX_WAIT:
                raise RateLimitError("Binance asked for a %d-second pause (HTTP 429); try again later" % int(wait),
                                     status=429, retry_after=int(wait))
            self._sleep(wait)
        if self.used_weight >= THROTTLE_AT * self.weight_limit:
            wait = 61 - (self._clock() % 60)
            self._sleep(wait)
            self.used_weight = 0

    def _read_headers(self, headers):
        if not headers:
            return
        for k, v in headers.items():
            kl = k.lower()
            if kl == "x-mbx-used-weight-1m":
                try:
                    self.used_weight = int(v)
                except (TypeError, ValueError):
                    pass
            elif kl.startswith("x-mbx-order-count-"):
                self.order_count[kl[len("x-mbx-order-count-"):]] = v

    def request(self, method, path, params=None, signed=False):
        method = method.upper()
        params = dict(params or {})
        if signed:
            if (method, path) not in SIGNED_ROUTES:
                raise BinanceError("refused: %s %s is not an endpoint this toolkit may call" % (method, path))
            if not self._keys.present():
                raise ConfigError("no API key: set BINANCE_API_KEY and BINANCE_API_SECRET in the environment "
                                  "(never in a file or the chat)")
            if self.time_offset_ms is None:
                self.sync_time()
            params["recvWindow"] = params.get("recvWindow", RECV_WINDOW)
            params["timestamp"] = int(self._clock() * 1000) + (self.time_offset_ms or 0)
        else:
            if method != "GET" or path not in PUBLIC_PATHS:
                raise BinanceError("refused: %s %s is not an endpoint this toolkit may call" % (method, path))
        query = encode_params(params)
        if signed:
            query += "&signature=" + sign(self._keys._secret, query)
        retry_ok = not signed and method == "GET"
        for attempt in (0, 1):
            self._wait_if_needed()
            url = self.base_url + path + ("?" + query if query else "")
            headers = {"User-Agent": UA, "Accept": "application/json"}
            if signed:
                headers["X-MBX-APIKEY"] = self._keys._key
            self.requests += 1
            try:
                status, hdrs, body = self._open(method, url, headers, self.timeout)
            except _HTTPFailure as e:
                status, hdrs, body = e.status, e.headers, e.body
            except (urllib.error.URLError, OSError) as e:
                raise BinanceError("could not reach Binance (%s): %s" % (self.venue, getattr(e, "reason", e)))
            self._read_headers(hdrs)
            if 200 <= status < 300:
                try:
                    return json.loads(body) if body else {}
                except ValueError:
                    raise BinanceError("Binance answered something that is not JSON for %s" % path, status=status)
            retry_after = _retry_after(hdrs)
            if status == 418:
                self.banned_until = self._clock() + (retry_after or 120)
                raise BannedError("Binance banned this IP (HTTP 418) for %s seconds after too many requests. "
                                  "Wait it out; do not retry." % (retry_after or "some"), status=418,
                                  retry_after=retry_after)
            if status == 429:
                self.backoff_until = self._clock() + (retry_after or 60)
                if retry_ok and attempt == 0 and (retry_after or 60) <= MAX_WAIT:
                    continue          # _wait_if_needed() sleeps it out first
                raise RateLimitError("Binance rate limit (HTTP 429): wait %s seconds before the next request"
                                     % (retry_after or 60), status=429, retry_after=retry_after)
            code, msg = _error_of(body)
            if code == -1021 and signed and attempt == 0 and (method, path) not in ORDER_ROUTES:
                self.sync_time()      # our clock drifted; a read can be resent, an order never is
                params["timestamp"] = int(self._clock() * 1000) + (self.time_offset_ms or 0)
                query = encode_params(params)
                query += "&signature=" + sign(self._keys._secret, query)
                continue
            raise BinanceError("Binance %s answered HTTP %d%s for %s %s" % (
                self.venue, status, (" (%s %s)" % (code, msg)) if code is not None else "", method, path),
                status=status, code=code)
        raise BinanceError("no answer from Binance for %s" % path)

    # ------------------------------------------------------------ public endpoints

    def ping(self):
        return self.request("GET", "/api/v3/ping")

    def server_time(self):
        return int(self.request("GET", "/api/v3/time")["serverTime"])

    def sync_time(self):
        local = int(self._clock() * 1000)
        self.time_offset_ms = 0
        server = self.server_time()
        self.time_offset_ms = server - local
        return self.time_offset_ms

    def exchange_info(self, symbol=None, symbols=None, permissions=None, status=None):
        p = {}
        if symbol:
            p["symbol"] = symbol.upper()
        elif symbols:
            p["symbols"] = [s.upper() for s in symbols]
        else:
            if permissions:
                p["permissions"] = permissions
            if status:
                p["symbolStatus"] = status
        doc = self.request("GET", "/api/v3/exchangeInfo", p)
        self._limits_from(doc)
        return doc

    def _limits_from(self, info):
        for rl in (info or {}).get("rateLimits") or []:
            if rl.get("rateLimitType") == "REQUEST_WEIGHT" and rl.get("interval") == "MINUTE" \
                    and int(rl.get("intervalNum") or 0) == 1:
                self.weight_limit = int(rl.get("limit") or WEIGHT_LIMIT)

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=1000):
        if interval not in INTERVAL_SECONDS:
            raise BinanceError("unknown kline interval %r" % interval)
        return self.request("GET", "/api/v3/klines", {"symbol": symbol.upper(), "interval": interval,
                                                      "startTime": start_ms, "endTime": end_ms,
                                                      "limit": max(1, min(int(limit), 1000))})

    def klines_range(self, symbol, interval, start_ms, end_ms):
        """Every kline from start_ms to end_ms (open times), 1,000 a request, oldest first."""
        step = INTERVAL_SECONDS[interval] * 1000
        out, cur = [], int(start_ms)
        while cur <= end_ms:
            rows = self.klines(symbol, interval, cur, end_ms, 1000)
            if not rows:
                break
            out.extend(rows)
            nxt = int(rows[-1][0]) + step
            if nxt <= cur or len(rows) < 1000:
                break
            cur = nxt
        seen, uniq = set(), []
        for r in out:
            if int(r[0]) not in seen:
                seen.add(int(r[0]))
                uniq.append(r)
        return uniq

    def ticker_24hr(self, symbols=None, mini=True):
        p = {"type": "MINI" if mini else None}
        if symbols:
            if len(symbols) == 1:
                p["symbol"] = symbols[0].upper()
            else:
                p["symbols"] = [s.upper() for s in symbols]
        doc = self.request("GET", "/api/v3/ticker/24hr", p)
        return doc if isinstance(doc, list) else [doc]

    def ticker_price(self, symbols=None):
        p = {}
        if symbols:
            if len(symbols) == 1:
                p["symbol"] = symbols[0].upper()
            else:
                p["symbols"] = [s.upper() for s in symbols]
        doc = self.request("GET", "/api/v3/ticker/price", p)
        return doc if isinstance(doc, list) else [doc]

    # ------------------------------------------------------------ signed endpoints

    def account(self):
        return self.request("GET", "/api/v3/account", {"omitZeroBalances": True}, signed=True)

    def commission(self, symbol):
        return self.request("GET", "/api/v3/account/commission", {"symbol": symbol.upper()}, signed=True)

    def new_order(self, params, test=False):
        path = "/api/v3/order/test" if test else "/api/v3/order"
        return self.request("POST", path, params, signed=True)

    def get_order(self, symbol, order_id=None, client_order_id=None):
        return self.request("GET", "/api/v3/order", {"symbol": symbol.upper(), "orderId": order_id,
                                                    "origClientOrderId": client_order_id}, signed=True)

    def cancel_order(self, symbol, order_id=None, client_order_id=None):
        return self.request("DELETE", "/api/v3/order", {"symbol": symbol.upper(), "orderId": order_id,
                                                       "origClientOrderId": client_order_id}, signed=True)

    def open_orders(self, symbol=None):
        return self.request("GET", "/api/v3/openOrders", {"symbol": symbol.upper() if symbol else None},
                            signed=True)

    def new_oco(self, params):
        return self.request("POST", "/api/v3/orderList/oco", params, signed=True)

    def cancel_order_list(self, symbol, order_list_id=None, list_client_order_id=None):
        return self.request("DELETE", "/api/v3/orderList", {"symbol": symbol.upper(), "orderListId": order_list_id,
                                                           "listClientOrderId": list_client_order_id}, signed=True)

    def get_order_list(self, order_list_id=None, list_client_order_id=None):
        return self.request("GET", "/api/v3/orderList", {"orderListId": order_list_id,
                                                        "origClientOrderId": list_client_order_id}, signed=True)

    def open_order_lists(self):
        return self.request("GET", "/api/v3/openOrderList", {}, signed=True)

    def my_trades(self, symbol, order_id=None, limit=50):
        return self.request("GET", "/api/v3/myTrades", {"symbol": symbol.upper(), "orderId": order_id,
                                                       "limit": limit}, signed=True)


# ---------------------------------------------------------------- transport helpers

class _HTTPFailure(Exception):
    def __init__(self, status, headers, body):
        Exception.__init__(self, "HTTP %d" % status)
        self.status, self.headers, self.body = status, headers, body


def _urlopen(method, url, headers, timeout):
    """The real network call -> (status, headers dict, body text)."""
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        hdrs = dict(e.headers.items()) if e.headers else {}
        e.close()
        raise _HTTPFailure(e.code, hdrs, body)


def _retry_after(headers):
    for k, v in (headers or {}).items():
        if k.lower() == "retry-after":
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None
    return None


def _error_of(body):
    try:
        doc = json.loads(body)
        return doc.get("code"), doc.get("msg")
    except (ValueError, AttributeError, TypeError):
        return None, None


# ---------------------------------------------------------------- picking the venue

def base_url_for(cfg, live=False):
    """The one place that decides testnet or live. Live needs BOTH "testnet": false and --live."""
    testnet = (cfg or {}).get("testnet", True)
    if testnet is not False and live:
        raise ConfigError("--live was given but the config says \"testnet\": true. Live trading needs both "
                          "\"testnet\": false in trader/config.json and --live.")
    if testnet is False and not live:
        raise ConfigError("the config says \"testnet\": false (live), but --live was not given. Pass --live to "
                          "use real money, or set \"testnet\": true.")
    return LIVE_URL if (testnet is False and live) else TESTNET_URL


def market_base_url(cfg, live=False):
    """Where chart data comes from. "market_data": "trading" (default) reads the same venue the
    orders go to, so the plan's prices match the book the OCO is checked against. "live" reads
    the live public market (no key) even while trading on the testnet."""
    if str((cfg or {}).get("market_data", "trading")).lower() == "live":
        return LIVE_URL
    return base_url_for(cfg, live)


def client(cfg, live=False, signed=False, env=None, opener=None, sleep=time.sleep, clock=time.time):
    """A client for orders and the account (signed=True) or for market data."""
    url = base_url_for(cfg, live) if signed else market_base_url(cfg, live)
    return BinanceClient(url, keys_from_env(env) if signed else None, opener=opener, sleep=sleep, clock=clock)


# ---------------------------------------------------------------- candles

def kline_rows(rows):
    """Binance kline arrays -> plain dicts with numbers (open time in ms)."""
    out = []
    for r in rows or []:
        out.append({"open_time": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                    "c": float(r[4]), "v": float(r[5]), "close_time": int(r[6]), "quote_volume": float(r[7]),
                    "trades": int(r[8])})
    return out


def klines_to_chart(rows, symbol, interval, now_ms=None):
    """Binance klines -> the Yahoo v8 chart document the skills parse:
    {"chart": {"result": [{"meta", "timestamp", "indicators": {"quote": [{open, high, low, close, volume}]}}]}}.
    timestamp = the kline's open time in seconds. meta.regularMarketTime = the later of the last
    trade we know of: the last kline's close time + 1 ms, capped at now."""
    ks = kline_rows(rows)
    ks.sort(key=lambda k: k["open_time"])
    last = ks[-1] if ks else None
    data_ms = None
    if last:
        data_ms = last["close_time"] + 1
        if now_ms is not None and now_ms < data_ms:
            data_ms = now_ms
    meta = {"symbol": symbol.upper(), "exchangeName": "BINANCE", "dataGranularity": interval,
            "regularMarketPrice": last["c"] if last else None,
            "regularMarketTime": int(data_ms // 1000) if data_ms is not None else None,
            "regularMarketVolume": sum(k["v"] for k in ks), "source": "binance"}
    return {"chart": {"result": [{
        "meta": meta,
        "timestamp": [k["open_time"] // 1000 for k in ks],
        "indicators": {"quote": [{
            "open": [k["o"] for k in ks], "high": [k["h"] for k in ks], "low": [k["l"] for k in ks],
            "close": [k["c"] for k in ks], "volume": [k["v"] for k in ks]}]},
    }], "error": None}}


def chart_text(rows, symbol, interval, now_ms=None):
    return json.dumps(klines_to_chart(rows, symbol, interval, now_ms))


def load_json(path):
    """A fixture or cache file (.json or .json.gz)."""
    import gzip
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
