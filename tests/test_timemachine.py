import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import BFIX, FakeBinance, bfix, load

tm = load("trader-timemachine", "timemachine")

HIST = os.path.join(BFIX, "klines-BTCUSDT-5m-60d.json.gz")
ROWS = bfix("klines-BTCUSDT-5m-60d.json.gz")
NOW = datetime.datetime(2026, 10, 19, 2, 1)
PLAN = {"symbol": "BTCUSDT", "side": "buy", "setup": "orb", "entry": 52730.0, "entry_time": "2026-10-19T02:00:00Z",
        "entry_bar": 23, "interval": "5m", "opening_range": {"high": 52680.36, "low": 52410.22, "minutes": 15},
        "stop": 52545.29, "stop_rule": "or_mid", "target": 53007.07, "max_hold_minutes": 240, "tick_size": "0.01"}


def plan(**over):
    return tm.normalize_plan(dict(PLAN, **over), {})


def run_tm(p=None, cfg=None, **kw):
    return tm.time_machine(ROWS, p or plan(), NOW, cfg or {}, **kw)


class Plan(unittest.TestCase):
    def test_normalize(self):
        p = plan()
        self.assertEqual((p["date"], p["session_start"], p["entry_bar"]), ("2026-10-19", "2026-10-19T00:00:00Z", 23))
        self.assertEqual(p["target_r"], 1.5)
        self.assertEqual(p["max_hold_minutes"], 240)

    def test_session_over_midnight_moves_the_entry_candle(self):
        p = tm.normalize_plan(PLAN, {"session_start_utc": "22:00"})
        self.assertEqual(p["date"], "2026-10-18")
        self.assertEqual(p["entry_bar"], 47)            # 01:55 is 47 candles after 22:00

    def test_refusals(self):
        for bad in ({"stop": 53000.0}, {"entry_time": "2026-10-19T02:03:00Z"}, {"side": "sell"},
                    {"interval": "1m"}, {"symbol": "btc usdt"}):
            with self.assertRaises(tm.TMError):
                plan(**bad)


class Run(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = run_tm()

    def test_pool_and_short_sessions(self):
        m = self.doc["mc"]
        self.assertEqual(m["pool_sessions"], 59)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-20", "2026-10-18"))
        self.assertEqual(self.doc["data"]["short_sessions_left_out"], [{"date": "2026-09-02", "bars": 276}])

    def test_the_numbers_are_reproducible(self):
        m = self.doc["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"]), (0.3847, 0.6148, 0.0005))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["bars_left"], 48)
        self.assertEqual(m["exit_at"], "2026-10-19T06:00:00Z")
        again = run_tm()
        self.assertEqual(again["mc"]["p_target"], m["p_target"])
        other = run_tm(seed=7)
        self.assertNotEqual(other["mc"]["p_target"], m["p_target"])

    def test_fees_are_shown(self):
        m = self.doc["mc"]
        self.assertLess(m["avg_R_after_fees"], m["avg_R"])
        self.assertEqual(m["fee_rate"], 0.001)
        self.assertIn("after fees", self.doc["verdict"])

    def test_history_replays_the_rule(self):
        h = self.doc["history"]
        self.assertEqual(h["rule"]["stop_rule"], "or_mid")
        self.assertEqual((h["sessions"], h["triggered"], h["target"], h["stop"], h["close"]), (59, 48, 14, 34, 0))
        t = h["trades"][0]
        self.assertTrue(t["time"].endswith("UTC"))
        self.assertLessEqual(t["stop"], t["entry"])

    def test_auto_stop_rule_finds_the_midpoint(self):
        doc = run_tm(plan(stop_rule=None))
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_mid")

    def test_no_future_data_is_used(self):
        # cutting the data at the entry gives the same answer: nothing after 02:00 is read
        cut = [r for r in ROWS if r[0] < 1792375200000]
        doc = tm.time_machine(cut, plan(), NOW, {})
        self.assertEqual(doc["mc"]["p_target"], self.doc["mc"]["p_target"])

    def test_the_verdict_break_even_without_fees_is_one_over_one_plus_r(self):
        m = dict(self.doc["mc"], fee_rate=0.0)
        d, share, even = tm.lean(m, plan())
        self.assertAlmostEqual(even, 1 / 2.5, places=3)
        self.assertTrue(self.doc["verdict"].startswith("Strongly toward the stop"))

    def test_report_and_chart(self):
        text = "\n".join(tm.report(self.doc))
        self.assertIn("10,000 replays of the next 240 minutes, to 06:00 UTC", text)
        self.assertIn("target first   38.5 %", text)
        self.assertIn("History: the same rule", text)
        chart = tm.render_chart(self.doc)
        self.assertTrue(any(l.rstrip().endswith("target") for l in chart))
        self.assertIn("02:00", chart[-2])


class Edges(unittest.TestCase):
    def test_a_hold_past_the_session_end_wraps_the_clock(self):
        e = float(next(r for r in ROWS if r[0] == 1792452300000)[4])     # the 23:25 candle's close
        p = plan(entry=e, stop=round(e * 0.99, 2), target=round(e * 1.02, 2), entry_time="2026-10-19T23:30:00Z",
                 stop_rule="pct")
        doc = tm.time_machine(ROWS, p, datetime.datetime(2026, 10, 19, 23, 31), {}, replays=500, hold=120)
        self.assertEqual(doc["mc"]["exit_at"], "2026-10-20T01:30:00Z")
        self.assertEqual(doc["mc"]["bars_left"], 24)

    def test_entry_missing(self):
        with self.assertRaises(tm.TMError) as cm:
            tm.time_machine(ROWS, plan(entry_time="2026-10-21T02:00:00Z"), datetime.datetime(2026, 10, 21, 3, 0), {})
        self.assertIn("not in the data", str(cm.exception))

    def test_too_few_sessions(self):
        few = [r for r in ROWS if r[0] >= 1792368000000 - 10 * 86400000]
        with self.assertRaises(tm.TMError) as cm:
            tm.time_machine(few, plan(), NOW, {})
        self.assertIn("needs at least 20", str(cm.exception))

    def test_network_path_pages_from_history_days_back(self):
        def answer(p):
            return [r for r in ROWS if int(p["startTime"]) <= r[0] <= int(p["endTime"])][:int(p["limit"])]
        fake = FakeBinance({("GET", "/api/v3/klines"): answer})
        saved = tm.bc.client
        tm.bc.client = lambda cfg, live=False, **k: tm.bc.BinanceClient(tm.bc.TESTNET_URL, opener=fake)
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "plan.json"), "w") as fh:
                json.dump(PLAN, fh)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = tm.main(["run", "--plan", os.path.join(d, "plan.json"), "--now", "2026-10-19 02:01",
                                "--config", os.path.join(d, "none.json"), "--replays", "200"])
        finally:
            tm.bc.client = saved
            shutil.rmtree(d)
        self.assertEqual(code, 0)
        first = fake.calls[0]["params"]
        self.assertEqual(first["startTime"], str(1792368000000 - 61 * 86400000))
        self.assertEqual(fake.calls[-1]["params"]["endTime"], "1792375200000")
        self.assertLessEqual(len(fake.calls), 19)


if __name__ == "__main__":
    unittest.main()
