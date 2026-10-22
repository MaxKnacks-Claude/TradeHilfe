"""Load a script from a skill folder (the folder names have hyphens), and keep the tests offline."""

import importlib.util
import json
import os
import socket
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
BFIX = os.path.join(FIX, "binance")
COMMON = os.path.join(ROOT, "common")
if COMMON not in sys.path:
    sys.path.insert(0, COMMON)


class NetworkBlocked(AssertionError):
    pass


def _no_network(*a, **k):
    raise NetworkBlocked("a test tried to reach the network; tests must run offline")


# Every test runs offline: a real urlopen or socket connection fails loudly.
urllib.request.urlopen = _no_network
socket.create_connection = _no_network


def load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def common(name):
    return importlib.import_module(name)


def bfix(name):
    """A Binance fixture: parsed JSON (.json or .json.gz)."""
    import binance_client
    return binance_client.load_json(os.path.join(BFIX, name))


class Seq(list):
    """Answers given in turn by FakeBinance (the last one repeats)."""


class FakeBinance(object):
    """A stand-in for the network behind BinanceClient: answers from a route table and
    records every request (method, path, params, headers). routes maps (METHOD, path) to a
    body, a callable(params) -> body, a (status, headers, body) tuple, or a Seq of those in turn."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        import urllib.parse
        import binance_client
        parts = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        self.calls.append({"method": method, "path": parts.path, "params": params, "headers": dict(headers),
                           "url": url, "base": "%s://%s" % (parts.scheme, parts.netloc)})
        key = (method, parts.path)
        if key not in self.routes:
            raise binance_client._HTTPFailure(404, {}, json.dumps({"code": -1, "msg": "no fake route %s %s"
                                                                   % key}))
        ans = self.routes[key]
        if isinstance(ans, Seq):
            ans = ans.pop(0) if len(ans) > 1 else ans[0]
        if callable(ans):
            ans = ans(params)
        if isinstance(ans, tuple):
            status, hdrs, body = ans
        else:
            status, hdrs, body = 200, {"X-MBX-USED-WEIGHT-1M": "10"}, ans
        text = body if isinstance(body, str) else json.dumps(body)
        if status >= 400:
            raise binance_client._HTTPFailure(status, hdrs, text)
        return status, hdrs, text

    def paths(self):
        return [c["path"] for c in self.calls]


class FakeClock(object):
    def __init__(self, t=1792368000.0):
        self.t = t
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s
