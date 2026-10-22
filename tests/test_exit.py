import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import BFIX, FakeBinance, bfix, load

ex = load("trader-exit", "exit")
entry = load("trader-entry", "entry")

INFO = os.path.join(BFIX, "exchangeInfo.json")
KL = os.path.join(BFIX, "klines-BTCUSDT-5m-2026-10-19.json")


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class Math(unittest.TestCase):
    def test_or_mid_stop_and_r_target_on_the_tick(self):
        c = ex.compute(52730.0, "or_mid", None, "r", Decimal("1.5"), "0.01", 52680.36, 52410.22)
        self.assertEqual(c["stop"], Decimal("52545.29"))
        self.assertEqual(c["risk"], Decimal("184.71"))
        self.assertEqual(c["target"], Decimal("53007.07"))      # 52730 + 1.5 x 184.71 = 53007.065, half up
        self.assertEqual(c["reward_risk"], Decimal("1.50"))

    def test_fees_lower_the_reward_risk(self):
        c = ex.compute(52730.0, "or_mid", None, "r", Decimal("1.5"), "0.01", 52680.36, 52410.22, fee_rate=0.001)
        # (276.07 - 0.001 x (52730 + 53007.07)) / (184.71 + 0.001 x (52730 + 52545.29))
        self.assertEqual(c["reward_risk_net"], Decimal("0.59"))
        c0 = ex.compute(52730.0, "or_mid", None, "r", Decimal("1.5"), "0.01", 52680.36, 52410.22, fee_rate=0)
        self.assertEqual(c0["reward_risk_net"], Decimal("1.50"))

    def test_a_tiny_tick(self):
        c = ex.compute("0.00000878", "pct", Decimal("1"), "r", Decimal("2"), "0.00000001")
        self.assertEqual(c["stop"], Decimal("0.00000869"))      # 0.0000086922 rounded DOWN
        self.assertEqual(c["target"], Decimal("0.00000896"))

    def test_stop_rounds_down_to_the_tick(self):
        c = ex.compute(100.0, "price", Decimal("98.555"), "r", Decimal("2"), "0.01")
        self.assertEqual(c["stop"], Decimal("98.55"))
        self.assertIn("rounded down to the tick", c["stop_detail"])

    def test_refusals(self):
        with self.assertRaises(ex.PlanError):
            ex.compute(100.0, "price", Decimal("100"), "r", Decimal("2"), "0.01")           # stop at the entry
        with self.assertRaises(ex.PlanError):
            ex.compute(100.0, "price", Decimal("99.99"), "r", Decimal("2"), "0.01")         # 0.01 % risk
        with self.assertRaises(ex.PlanError):
            ex.compute(100.0, "price", Decimal("99"), "price", Decimal("99.5"), "0.01")     # target under entry
        with self.assertRaises(ex.PlanError):
            ex.compute(100.0, "or_mid", None, "r", Decimal("2"), "0.01")                     # no range

    def test_atr_on_the_session_candles(self):
        import datetime
        bars = ex.load_bars("BTCUSDT", "5m", datetime.datetime(2026, 10, 19), datetime.datetime(2026, 10, 19, 2, 0),
                            KL)
        self.assertEqual(len(bars), 24)
        atr, n = ex.atr_of(bars)
        self.assertEqual(n, 14)
        self.assertAlmostEqual(atr, 136.5442857, places=5)

    def test_parsers(self):
        self.assertEqual(ex.parse_stop("or-mid"), ("or_mid", None))
        self.assertEqual(ex.parse_stop("pct:1.5"), ("pct", Decimal("1.5")))
        self.assertEqual(ex.parse_target("1.5R"), ("r", Decimal("1.5")))
        self.assertEqual(ex.parse_target("price:53,000"), ("price", Decimal("53000")))
        for bad in ("pct:0", "pct:120", "foo"):
            with self.assertRaises(ex.PlanError):
                ex.parse_stop(bad)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = lambda n: os.path.join(self.d, n)
        self.base = ["--exchange-info", INFO, "--config", self.p("none.json")]

    def tearDown(self):
        shutil.rmtree(self.d)

    def entry_json(self):
        code, _, err = run(entry, ["check", "BTCUSDT", "--offline", KL, "--now", "2026-10-19 02:00",
                                   "--write", self.p("entry.json"), "--config", self.p("none.json")])
        self.assertEqual(code, 0, err)
        return self.p("entry.json")

    def test_entry_to_plan_end_to_end(self):
        e = self.entry_json()
        code, text, err = run(ex, ["plan", "--from", e, "--target", "1.5R", "--now", "2026-10-19 02:01",
                                   "--write", self.p("plan.json")] + self.base)
        self.assertEqual(code, 0, err)
        with open(self.p("plan.json")) as fh:
            plan = json.load(fh)
        self.assertEqual((plan["entry"], plan["stop"], plan["target"]), (52730.0, 52545.29, 53007.07))
        self.assertEqual(plan["entry_time"], "2026-10-19T02:00:00Z")
        self.assertEqual(plan["exit_by"], "2026-10-19T06:00:00Z")
        self.assertEqual(plan["max_hold_minutes"], 240)
        self.assertEqual(plan["tick_size"], "0.01")
        self.assertEqual(plan["opening_range"], {"high": 52680.36, "low": 52410.22, "minutes": 15})
        self.assertEqual(plan["reward_risk_net"], 0.59)
        self.assertIn("trader-risk will block it", text)
        self.assertIn("Exit by        2026-10-19 06:00:00 UTC", text)

    def test_max_hold_over_midnight(self):
        code, text, err = run(ex, ["plan", "--symbol", "BTCUSDT", "--entry", "52730", "--or-high", "52680.36",
                                   "--or-low", "52410.22", "--time", "2026-10-19 22:30", "--max-hold", "180",
                                   "--now", "2026-10-19 22:31", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        plan = json.loads(text)
        self.assertEqual(plan["exit_by"], "2026-10-20T01:30:00Z")

    def test_atr_stop_reads_candles(self):
        e = self.entry_json()
        code, text, err = run(ex, ["plan", "--from", e, "--stop", "atr:1.5", "--offline", KL,
                                   "--now", "2026-10-19 02:01", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        plan = json.loads(text)
        self.assertEqual(plan["stop"], 52525.18)
        self.assertEqual(plan["atr_bars"], 14)

    def test_not_triggered_is_refused(self):
        with open(self.p("entry.json"), "w") as fh:
            json.dump({"symbol": "BTCUSDT", "status": "waiting", "side": "buy"}, fh)
        code, _, err = run(ex, ["plan", "--from", self.p("entry.json"), "--now", "2026-10-19 02:01"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("not triggered", err)

    def test_a_symbol_that_does_not_trade_is_refused(self):
        code, _, err = run(ex, ["plan", "--symbol", "LUNAUSDT", "--entry", "1", "--stop", "pct:2",
                                "--setup", "level", "--now", "2026-10-19 02:01"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("not trading", err)

    def test_tick_size_comes_from_binance_and_is_cached(self):
        info = bfix("exchangeInfo.json")
        one = dict(info, symbols=[s for s in info["symbols"] if s["symbol"] == "ETHUSDT"])
        fake = FakeBinance({("GET", "/api/v3/exchangeInfo"): one})
        saved = ex.bc.client
        ex.bc.client = lambda cfg, live=False, **k: ex.bc.BinanceClient(ex.bc.TESTNET_URL, opener=fake)
        try:
            argv = ["plan", "--symbol", "ETHUSDT", "--entry", "2484.49", "--setup", "level", "--stop", "pct:1",
                    "--now", "2026-10-19 02:55", "--cache", self.p("cache"), "--config", self.p("none.json"),
                    "--json"]
            for _ in range(2):
                code, text, err = run(ex, argv)
                self.assertEqual(code, 0, err)
        finally:
            ex.bc.client = saved
        self.assertEqual(len(fake.calls), 1)                   # the second plan read the cache
        self.assertEqual(fake.calls[0]["params"]["symbol"], "ETHUSDT")
        self.assertEqual(json.loads(text)["stop"], 2459.64)

    def test_short_is_refused(self):
        code, _, err = run(ex, ["plan", "--symbol", "BTCUSDT", "--entry", "1", "--side", "sell"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("long only", err)


if __name__ == "__main__":
    unittest.main()
