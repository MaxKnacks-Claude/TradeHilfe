import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import BFIX, ROOT, FakeBinance, bfix, load

alerts = load("trader-alerts", "alerts")
bc = alerts.bc

PLAN = {"symbol": "BTCUSDT", "date": "2026-10-19", "stop": 52545.29, "target": 53007.07,
        "stop_label": "opening-range midpoint", "target_label": "1.5R", "exit_by": "2026-10-19T06:00:00Z",
        "max_hold_minutes": 240}


def at(hhmm, day=19):
    h, m = map(int, hhmm.split(":"))
    return datetime.datetime(2026, 10, day, h, m)


def q(price, open24=None):
    return {"BTCUSDT": {"symbol": "BTCUSDT", "price": price, "open_24h": open24, "time": "", "source": "binance"}}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


class Rules(unittest.TestCase):
    def test_from_plan(self):
        rules = alerts.rules_from_plan(PLAN)
        self.assertEqual([r["type"] for r in rules], ["stop", "target", "exit_by"])
        self.assertEqual(rules[2]["at_utc"], "2026-10-19T06:00:00Z")
        self.assertTrue(all(r["expires_utc"] == "2026-10-19T07:00:00Z" for r in rules))

    def test_plan_without_exit_by_is_refused(self):
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan(dict(PLAN, exit_by=None))

    def test_stop_rings_once(self):
        rules = alerts.rules_from_plan(PLAN)
        rings, st = alerts.evaluate(rules, q(52800.0), at("02:10"), {})
        self.assertEqual(rings, [])
        rings, st = alerts.evaluate(rules, q(52540.0), at("02:20"), st)
        self.assertEqual([r["id"] for r in rings], ["btcusdt-plan-stop"])
        self.assertIn("at or below your stop 52,545.29", rings[0]["message"])
        rings, st = alerts.evaluate(rules, q(52500.0), at("02:25"), st)
        self.assertEqual(rings, [])

    def test_exit_by_rings_when_max_hold_is_reached(self):
        rules = alerts.rules_from_plan(PLAN)
        rings, st = alerts.evaluate(rules, q(52800.0), at("05:59"), {})
        self.assertEqual(rings, [])
        rings, st = alerts.evaluate(rules, q(52800.0), at("06:00"), st)
        self.assertEqual([r["id"] for r in rings], ["btcusdt-exit-by"])
        self.assertIn("Max hold reached", rings[0]["message"])
        self.assertIn("ticket.py flatten --symbol BTCUSDT", rings[0]["message"])

    def test_plan_rules_expire(self):
        rules = alerts.rules_from_plan(PLAN)
        rings, _ = alerts.evaluate(rules, q(1.0), at("07:01"), {})
        self.assertEqual(rings, [])

    def test_exit_by_over_midnight(self):
        plan = dict(PLAN, exit_by="2026-10-20T01:30:00Z")
        rules = alerts.rules_from_plan(plan)
        rings, st = alerts.evaluate(rules, q(52800.0), at("23:59"), {})
        self.assertEqual(rings, [])
        rings, st = alerts.evaluate(rules, q(52800.0), at("01:30", day=20), st)
        self.assertEqual([r["id"] for r in rings], ["btcusdt-exit-by"])

    def test_session_start_and_time_ring_once_per_session(self):
        rules = [{"id": "s", "type": "session_start"}, {"id": "t", "type": "time", "at": "05:30", "label": "look"}]
        rings, st = alerts.evaluate(rules, {}, at("00:01"), {})
        self.assertEqual([r["id"] for r in rings], ["s"])
        rings, st = alerts.evaluate(rules, {}, at("05:31"), st)
        self.assertEqual([r["id"] for r in rings], ["t"])
        rings, st = alerts.evaluate(rules, {}, at("06:00"), st)
        self.assertEqual(rings, [])
        rings, st = alerts.evaluate(rules, {}, at("00:02", day=20), st)
        self.assertEqual([r["id"] for r in rings], ["s"])

    def test_time_rule_in_a_session_over_midnight(self):
        cfg = {"session_start_utc": "22:00"}
        rules = [{"id": "t", "type": "time", "at": "01:00"}]
        rings, st = alerts.evaluate(rules, {}, at("23:00"), {}, cfg)
        self.assertEqual(rings, [])
        rings, st = alerts.evaluate(rules, {}, at("01:00", day=20), st, cfg)
        self.assertEqual([r["id"] for r in rings], ["t"])

    def test_pct_move_against_24_hours_ago(self):
        rules = [{"id": "m", "symbol": "BTCUSDT", "type": "pct_move", "pct": 3, "direction": "up"}]
        rings, _ = alerts.evaluate(rules, q(103.5, 100.0), at("03:00"), {})
        self.assertEqual(len(rings), 1)
        self.assertIn("+3.50%", rings[0]["message"])
        rings, _ = alerts.evaluate(rules, q(96.0, 100.0), at("03:00"), {})
        self.assertEqual(rings, [])

    def test_bad_rules(self):
        for r in ({"id": "x", "type": "nope"}, {"id": "x", "type": "stop", "symbol": "BTCUSDT"},
                  {"id": "x", "type": "time", "at": "25:00"}, {"id": "x", "type": "exit_by", "symbol": "BTCUSDT"}):
            with self.assertRaises(alerts.AlertError):
                alerts.validate(r)

    def test_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json")) as fh:
            for r in json.load(fh)["rules"]:
                alerts.validate(r)


class Quotes(unittest.TestCase):
    def test_parse_tickers(self):
        quotes = alerts.parse_tickers(bfix("ticker24hr-mini.json"), ["BTCUSDT", "PEPEUSDT"])
        self.assertEqual(sorted(quotes), ["BTCUSDT", "PEPEUSDT"])
        self.assertEqual(quotes["BTCUSDT"]["time"], "2026-10-19T23:59:59Z")
        self.assertIsInstance(quotes["BTCUSDT"]["open_24h"], float)

    def test_one_request_for_all_symbols(self):
        fake = FakeBinance({("GET", "/api/v3/ticker/24hr"): bfix("ticker24hr-mini.json")[:2]})
        cl = bc.BinanceClient(bc.TESTNET_URL, opener=fake)
        quotes, errors = alerts.get_quotes(["BTCUSDT", "ETHUSDT", "NOPEUSDT"], {}, cl=cl)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["params"]["type"], "MINI")
        self.assertEqual(json.loads(fake.calls[0]["params"]["symbols"]), ["BTCUSDT", "ETHUSDT", "NOPEUSDT"])
        self.assertEqual(sorted(quotes), ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(len(errors), 1)


class Commands(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = lambda n: os.path.join(self.d, n)
        with open(self.p("plan.json"), "w") as fh:
            json.dump(PLAN, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_add_from_plan_then_check_offline(self):
        code, text, _ = run(["add", "--from-plan", "--plan", self.p("plan.json"), "--rules", self.p("alerts.json"),
                             "--state", self.p("state.json")])
        self.assertEqual(code, 0)
        base = ["check", "--offline", BFIX, "--rules", self.p("alerts.json"), "--state", self.p("state.json"),
                "--prices-out", self.p("prices.json"), "--config", self.p("none.json")]
        code, text, _ = run(base + ["--now", "2026-10-19 06:00"])
        self.assertEqual(code, 0)
        self.assertIn("ALERT Max hold reached for BTCUSDT", text)
        with open(self.p("prices.json")) as fh:
            prices = json.load(fh)
        self.assertIn("BTCUSDT", prices["prices"])
        self.assertEqual(prices["source"], "binance")
        code, text, _ = run(base + ["--now", "2026-10-19 06:01"])
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit", text)

    def test_clock(self):
        code, text, _ = run(["clock", "--now", "2026-10-19 02:00", "--config", self.p("none.json")])
        self.assertEqual(code, 0)
        self.assertIn("session 2026-10-19", text)
        self.assertIn("new entries allowed", text)
        code, text, _ = run(["clock", "--now", "2026-10-19 00:05", "--config", self.p("none.json")])
        self.assertEqual(code, 1)
        self.assertIn("opening range is forming", text)


if __name__ == "__main__":
    unittest.main()
