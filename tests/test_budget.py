import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import BFIX, bfix, load, common

size = load("trader-budget", "size")
sf = common("symbol_filters")
om = common("order_math")

INFO = bfix("exchangeInfo.json")
BTC = sf.rules_of(sf.find_symbol(INFO, "BTCUSDT"))
DOGE = sf.rules_of(sf.find_symbol(INFO, "DOGEUSDT"))
PEPE = sf.rules_of(sf.find_symbol(INFO, "PEPEUSDT"))
NOW = datetime.datetime(2026, 10, 19, 2, 1)
PLAN = {"symbol": "BTCUSDT", "side": "buy", "setup": "orb", "date": "2026-10-19", "entry": 52730.0,
        "stop": 52545.29, "target": 53007.07, "entry_time": "2026-10-19T02:00:00Z", "exit_by": "2026-10-19T06:00:00Z"}
CFG = {"min_reward_risk": 0.5}


def sz(plan=None, cfg=None, rules=BTC, **kw):
    c = dict(CFG)
    c.update(cfg or {})
    return size.size_trade(dict(PLAN, **(plan or {})), c, rules, now_utc=kw.pop("now", NOW), **kw)


class Sizing(unittest.TestCase):
    def test_position_cap_binds_and_quantity_is_on_the_step(self):
        s = sz()
        self.assertIsNone(s["refusal"])
        self.assertEqual(s["qty"], Decimal("0.00948"))          # 500 / 52730 = 0.009482.. down to 0.00001
        self.assertEqual(s["bound_by"], "position cap")
        self.assertTrue(sf.is_multiple(s["qty"], BTC["step"]))

    def test_the_buy_fee_shrinks_what_the_oco_can_sell(self):
        m = sz()["money"]
        self.assertEqual(m["qty_sell"], Decimal("0.00947"))     # 0.00948 x 0.999 = 0.00947052 down to the step

    def test_fees_are_in_the_max_loss(self):
        m = sz()["money"]
        q, qs = Decimal("0.00948"), Decimal("0.00947")
        expect = q * Decimal("52730.0") - qs * Decimal("52545.29") * Decimal("0.999")
        self.assertEqual(m["max_loss"], expect)
        no_fee = sz(cfg={"fee_rate": 0})["money"]
        self.assertLess(no_fee["max_loss"], m["max_loss"])
        self.assertEqual(no_fee["max_loss"], q * (Decimal("52730.0") - Decimal("52545.29")))
        self.assertAlmostEqual(float(m["max_loss"]), 2.7741076, places=6)

    def test_max_loss_binds(self):
        s = sz(cfg={"max_loss_per_trade": 1, "max_position_pct": 100, "max_order": 100000})
        self.assertEqual(s["bound_by"], "max loss")
        self.assertLessEqual(s["money"]["max_loss"], Decimal("1"))
        one_more = om.money(BTC, s["qty"] + Decimal(BTC["step"]), s["entry"], s["stop"], s["target"],
                            Decimal("0.001"), Decimal("0.5"))
        self.assertGreater(one_more["max_loss"], Decimal("1"))

    def test_a_tiny_position_loses_a_whole_step_to_the_fee(self):
        # 0.00013 BTC: the fee leaves 0.00012987, the OCO can sell 0.00012: the dust step makes it a loser
        s = sz(cfg={"budget": 7, "cash_buffer_pct": 0, "max_position_pct": 100})
        self.assertEqual((s["qty"], s["money"]["qty_sell"]), (Decimal("0.00013"), Decimal("0.00012")))
        self.assertLess(s["money"]["gain_at_target"], 0)
        self.assertIn("reward:risk after fees", s["refusal"])

    def test_reward_risk_after_fees_is_enforced(self):
        s = sz(cfg={"min_reward_risk": 1.5})
        self.assertIn("reward:risk after fees 0.57", s["refusal"])
        self.assertIsNone(size.proposed_order(s, 0.5))

    def test_minimum_notional(self):
        s = sz(cfg={"budget": 5.5, "cash_buffer_pct": 0, "max_position_pct": 100})
        self.assertIn("stop leg: order value 4.73 is under the minimum notional 5", s["refusal"])

    def test_stop_limit_when_the_symbol_has_no_stop_loss(self):
        plan = {"symbol": "DOGEUSDT", "entry": 0.11873, "stop": 0.1176, "target": 0.1240}
        s = sz(plan, rules=DOGE)
        self.assertIsNone(s["refusal"], s["refusal"])
        o = size.proposed_order(s, 0.5)
        self.assertEqual(o["oco"]["belowType"], "STOP_LOSS_LIMIT")
        self.assertEqual(o["oco"]["belowStopPrice"], "0.11760")
        self.assertEqual(o["oco"]["belowPrice"], "0.11701")      # 0.5 % under, down to the tick
        self.assertEqual(o["oco"]["belowTimeInForce"], "GTC")
        self.assertEqual(s["money"]["worst_exit"], Decimal("0.11701"))

    def test_whole_coin_steps(self):
        plan = {"symbol": "PEPEUSDT", "entry": 0.00000878, "stop": 0.00000869, "target": 0.00000896}
        s = sz(plan, rules=PEPE, cfg={"min_reward_risk": 0.5})
        self.assertEqual(s["qty"], s["qty"].to_integral_value())
        o = size.proposed_order(s, 0.5)
        self.assertNotIn(".", o["order"]["quantity"])
        self.assertEqual(o["oco"]["abovePrice"], "0.00000896")

    def test_a_fresh_higher_price_is_used(self):
        s = sz(price=52800.0)
        self.assertEqual((s["entry"], s["entry_from"]), (Decimal("52800.0"), "price"))
        s = sz(price=52700.0)
        self.assertEqual(s["entry_from"], "plan")

    def test_limit_order(self):
        s = sz(limit=52700.004)
        self.assertEqual(s["entry"], Decimal("52700.00"))
        o = size.proposed_order(s, 0.5)
        self.assertEqual(o["order"], {"type": "LIMIT", "timeInForce": "GTC", "quantity": "0.00948",
                                      "price": "52700.00"})

    def test_cash_from_the_portfolio(self):
        s = sz(cash=300.0)
        self.assertEqual(s["bound_by"], "cash")
        self.assertEqual(s["qty"], Decimal("0.00379"))           # (300 - 100 buffer) / 52730

    def test_refusals(self):
        with self.assertRaises(size.SizeError):
            sz(now=datetime.datetime(2026, 10, 19, 6, 0))       # the plan's exit time has passed
        with self.assertRaises(size.SizeError):
            sz(cfg={"blocked": ["BTCUSDT"]})
        with self.assertRaises(size.SizeError):
            sz({"side": "sell"})
        with self.assertRaises(size.SizeError):
            sz({"stop": 52800.0})
        luna = sf.rules_of(sf.find_symbol(INFO, "LUNAUSDT"))
        with self.assertRaises(size.SizeError):
            sz({"symbol": "LUNAUSDT"}, rules=luna)

    def test_the_proposed_order_shape(self):
        o = size.proposed_order(sz(), 0.5)
        self.assertEqual((o["symbol"], o["side"], o["kind"]), ("BTCUSDT", "BUY", "entry"))
        self.assertEqual(o["order"], {"type": "MARKET", "quantity": "0.00948"})
        self.assertEqual(o["oco"], {"side": "SELL", "quantity": "0.00947", "aboveType": "LIMIT_MAKER",
                                    "abovePrice": "53007.07", "belowType": "STOP_LOSS", "belowStopPrice": "52545.29"})
        self.assertEqual(o["plan"]["max_loss"], 2.77)
        self.assertEqual(o["plan"]["exit_by"], "2026-10-19T06:00:00Z")


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = lambda n: os.path.join(self.d, n)
        with open(self.p("plan.json"), "w") as fh:
            json.dump(PLAN, fh)
        with open(self.p("cfg.json"), "w") as fh:
            json.dump(CFG, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_size(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--plan", self.p("plan.json"), "--config", self.p("cfg.json"), "--exchange-info",
                os.path.join(BFIX, "exchangeInfo.json"), "--portfolio", self.p("none.json"), "--prices",
                self.p("none.json"), "--now", "2026-10-19 02:01"] + list(extra)
        with open(self.p("none.json"), "w") as fh:
            fh.write("{}")
        with redirect_stdout(out), redirect_stderr(err):
            code = size.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_write_orders(self):
        code, text, err = self.run_size("--write", self.p("orders.json"))
        self.assertEqual(code, 0, err)
        self.assertIn("Max loss at the stop 52545.29, fees in: 2.77 USDT", text)
        self.assertIn("2. as soon as it fills: OCO SELL 0.00947: take-profit 53007.07 / stop 52545.29", text)
        with open(self.p("orders.json")) as fh:
            doc = json.load(fh)
        self.assertIs(doc["testnet"], True)
        self.assertEqual(len(doc["orders"]), 1)

    def test_refusal_exits_1_and_writes_nothing(self):
        with open(self.p("cfg.json"), "w") as fh:
            json.dump({"min_reward_risk": 2}, fh)
        code, text, _ = self.run_size("--write", self.p("orders.json"))
        self.assertEqual(code, 1)
        self.assertIn("No order", text)
        self.assertFalse(os.path.exists(self.p("orders.json")))


if __name__ == "__main__":
    unittest.main()
