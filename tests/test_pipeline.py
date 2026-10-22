"""The whole pipeline, offline: scanner -> entry -> exit -> timemachine -> budget -> risk -> broker -> alerts.

Every step runs its real command line on the Binance fixtures; the broker talks to the
fake exchange from test_broker. This is the offline twin of the testnet run in the README."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import BFIX, load
from test_broker import FakeExchange

scan = load("trader-scanner", "scan")
entry = load("trader-entry", "entry")
ex = load("trader-exit", "exit")
tm = load("trader-timemachine", "timemachine")
size = load("trader-budget", "size")
check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")
alerts = load("trader-alerts", "alerts")


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = lambda n: os.path.join(self.d, n)
        with open(self.p("config.json"), "w") as fh:
            # a wider target than the reel's 1.5R, so the trade clears min_reward_risk 1.5 after fees
            json.dump({"testnet": True, "min_reward_risk": 1.0, "max_loss_per_trade": 10}, fh)
        self.ex = FakeExchange()
        ticket.CLIENT = self.ex.client

    def tearDown(self):
        ticket.CLIENT = None
        shutil.rmtree(self.d)

    def run_cmd(self, mod, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mod.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_scanner_to_broker(self):
        cfg = ["--config", self.p("config.json")]
        info = ["--exchange-info", os.path.join(BFIX, "exchangeInfo.json")]
        code, out = self.run_cmd(scan, ["orb", "--offline", os.path.join(BFIX, "scan-5m-2026-10-19.json.gz"),
                                        "--now", "2026-10-19 02:00", "--out", self.p("watchlist.json")] + cfg)
        self.assertEqual(code, 0, out)
        with open(self.p("watchlist.json")) as fh:
            self.assertIn("BTCUSDT", [h["symbol"] for h in json.load(fh)["hits"]])

        code, out = self.run_cmd(entry, ["check", "BTCUSDT", "--offline",
                                         os.path.join(BFIX, "klines-BTCUSDT-5m-2026-10-19.json"),
                                         "--now", "2026-10-19 02:00", "--write", self.p("entry.json")] + cfg)
        self.assertEqual(code, 0, out)

        code, out = self.run_cmd(ex, ["plan", "--from", self.p("entry.json"), "--stop", "or-mid", "--target", "4R",
                                      "--now", "2026-10-19 02:01", "--write", self.p("plan.json")] + cfg + info)
        self.assertEqual(code, 0, out)

        code, out = self.run_cmd(tm, ["run", "--plan", self.p("plan.json"), "--offline",
                                      os.path.join(BFIX, "klines-BTCUSDT-5m-60d.json.gz"), "--now", "2026-10-19 02:01",
                                      "--replays", "2000", "--write", self.p("timemachine.json")] + cfg)
        self.assertEqual(code, 0, out)
        self.assertIn("target first", out)

        with open(self.p("none.json"), "w") as fh:
            fh.write("{}")
        code, out = self.run_cmd(size, ["--plan", self.p("plan.json"), "--portfolio", self.p("none.json"),
                                        "--prices", self.p("none.json"), "--now", "2026-10-19 02:01",
                                        "--write", self.p("orders.json")] + cfg + info)
        self.assertEqual(code, 0, out)

        code, out = self.run_cmd(check, ["--orders", self.p("orders.json"), "--portfolio", self.p("portfolio.json"),
                                         "--prices", self.p("none.json"), "--state", self.p("broker.json"),
                                         "--out", self.p("risk.json"), "--now", "2026-10-19 02:01"] + cfg + info)
        self.assertEqual(code, 0, out)

        bfiles = ["--orders", self.p("orders.json"), "--risk", self.p("risk.json"), "--state", self.p("broker.json"),
                  "--portfolio", self.p("portfolio.json"), "--now", "2026-10-19 02:02"] + cfg + info
        code, out = self.run_cmd(ticket, ["show"] + bfiles)
        self.assertEqual(code, 0, out)
        self.assertIn("READY", out)
        self.assertEqual(self.ex.net.calls, [])                  # nothing sent until here
        code, out = self.run_cmd(ticket, ["approve", "--index", "1", "--reply", "yes"] + bfiles)
        self.assertEqual(code, 0, out)
        conf = [l.split(": ")[1].split(" ")[0] for l in out.splitlines() if l.startswith("Confirmation code")][0]
        self.assertEqual(self.ex.net.calls, [])
        code, out = self.run_cmd(ticket, ["place", "--index", "1", "--confirm", conf] + bfiles)
        self.assertEqual(code, 0, out)
        self.assertEqual([c["path"] for c in self.ex.orders_sent()], ["/api/v3/order", "/api/v3/orderList/oco"])
        self.assertIn("OCO set", out)

        code, out = self.run_cmd(alerts, ["add", "--from-plan", "--plan", self.p("plan.json"), "--rules",
                                          self.p("alerts.json"), "--state", self.p("alerts-state.json")])
        self.assertEqual(code, 0, out)
        code, out = self.run_cmd(alerts, ["check", "--offline", BFIX, "--rules", self.p("alerts.json"), "--state",
                                          self.p("alerts-state.json"), "--prices-out", self.p("prices.json"),
                                          "--now", "2026-10-19 06:00"] + cfg)
        self.assertEqual(code, 0, out)
        self.assertIn("Max hold reached for BTCUSDT", out)


if __name__ == "__main__":
    unittest.main()
