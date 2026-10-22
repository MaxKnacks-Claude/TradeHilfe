import copy
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import BFIX, bfix, load, common

check = load("trader-risk", "check")
size = load("trader-budget", "size")
sf = common("symbol_filters")
om = common("order_math")

INFO = bfix("exchangeInfo.json")
NOW = datetime.datetime(2026, 10, 19, 2, 1)
CFG = {"min_reward_risk": 0.5}
PLAN = {"symbol": "BTCUSDT", "side": "buy", "setup": "orb", "date": "2026-10-19", "entry": 52730.0,
        "stop": 52545.29, "target": 53007.07, "entry_time": "2026-10-19T02:00:00Z", "exit_by": "2026-10-19T06:00:00Z"}


def rules_of(sym):
    try:
        return sf.rules_of(sf.find_symbol(INFO, sym))
    except sf.FilterError:
        return None


BTC = rules_of("BTCUSDT")


def btc_order():
    s = size.size_trade(dict(PLAN), CFG, BTC, now_utc=NOW)
    return size.proposed_order(s, 0.5)


ORDER = btc_order()


def ck(orders=None, cfg=None, now=NOW, **kw):
    c = dict(CFG)
    c.update(cfg or {})
    return check.check(orders if orders is not None else [copy.deepcopy(ORDER)], c, rules_of, now=now, **kw)


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def modified(**over):
    o = copy.deepcopy(ORDER)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(o.get(k), dict):
            o[k].update(v)
        elif v is None:
            o.pop(k, None)
        else:
            o[k] = v
    return o


def placed(n, max_loss="5", symbol="ETHUSDT", kind="entry", session="2026-10-19"):
    return {"orders": [{"status": "placed", "session": session, "symbol": symbol, "kind": kind, "max_loss": max_loss}
                       for _ in range(n)]}


class Pass(unittest.TestCase):
    def test_the_budget_order_passes(self):
        res = ck()
        self.assertEqual(res["pass"], 1, reasons(res))
        r = res["results"][0]
        self.assertEqual(r["kind"], "entry")
        self.assertAlmostEqual(r["max_loss"], 2.7741, places=4)
        self.assertEqual(r["reward_risk_net"], 0.57)
        self.assertEqual(r["fingerprint"], om.fingerprint(ORDER))

    def test_the_fingerprint_follows_every_sent_field_and_ignores_ours(self):
        fp = om.fingerprint(ORDER)
        self.assertNotEqual(om.fingerprint(modified(order={"quantity": "0.00947"})), fp)
        self.assertNotEqual(om.fingerprint(modified(oco={"belowStopPrice": "52500.00"})), fp)
        self.assertEqual(om.fingerprint(modified(why="changed words", plan={"note": "x"})), fp)


class Blocks(unittest.TestCase):
    def assertBlocked(self, res, word):
        self.assertEqual(res["block"], 1, "expected a BLOCK")
        self.assertIn(word, reasons(res))

    def test_max_loss_fees_in(self):
        self.assertBlocked(ck(cfg={"max_loss_per_trade": 2.5}), "loss: 2.77 at the stop, fees in, over your 2.50")

    def test_reward_risk_after_fees(self):
        self.assertBlocked(ck(cfg={"min_reward_risk": 1.5}), "reward:risk: 0.57 after fees")

    def test_daily_loss(self):
        self.assertBlocked(ck(cfg={"max_daily_loss": 12}, state=placed(2)), "daily loss: 10.00 already at risk")

    def test_trades_per_day(self):
        self.assertBlocked(ck(state=placed(3)), "trades: 3 entries in this session already, your limit is 3")

    def test_trades_from_an_earlier_session_do_not_count(self):
        self.assertEqual(ck(state=placed(3, session="2026-10-18"))["pass"], 1)

    def test_orders_per_day(self):
        self.assertBlocked(ck(cfg={"max_orders_per_day": 2}, state=placed(2, kind="exit", max_loss=None)),
                           "orders: 2 placed in this session already")

    def test_duplicate(self):
        self.assertBlocked(ck(state=placed(1, symbol="BTCUSDT")), "duplicate")
        res = ck(orders=[copy.deepcopy(ORDER), copy.deepcopy(ORDER)])
        self.assertEqual((res["pass"], res["block"]), (1, 1))

    def test_window(self):
        self.assertBlocked(ck(now=datetime.datetime(2026, 10, 19, 0, 10)), "window: the opening range")
        self.assertBlocked(ck(now=datetime.datetime(2026, 10, 19, 6, 30)), "window: after 06:00 UTC")

    def test_plan_expired(self):
        o = modified(plan=dict(ORDER["plan"], exit_by="2026-10-19T02:00:00Z"))
        self.assertBlocked(ck([o]), "plan expired")

    def test_cash(self):
        self.assertBlocked(ck(portfolio={"cash": "450", "balances": {}}), "cash: needs 499.88 USDT")

    def test_position_and_size(self):
        self.assertBlocked(ck(cfg={"max_position_pct": 10}), "position: BTCUSDT is 499.88")
        self.assertBlocked(ck(cfg={"max_order": 100}), "over your 100.00 max order")
        self.assertBlocked(ck(cfg={"min_order": 600}), "under your 600.00 minimum order")

    def test_blocked_list(self):
        self.assertBlocked(ck(cfg={"blocked": ["btcusdt"]}), "blocked: BTCUSDT")

    def test_a_stop_is_required(self):
        self.assertBlocked(ck([modified(oco=None)]), "stop: no protective OCO")

    def test_filters(self):
        self.assertBlocked(ck([modified(order={"quantity": "0.000105"})]), "not a multiple of the LOT_SIZE step")
        self.assertBlocked(ck([modified(oco={"belowStopPrice": "52545.295"})]), "not a multiple of the tick size")
        tiny = modified(order={"quantity": "0.00010"}, oco={"quantity": "0.00009"})
        self.assertBlocked(ck([tiny]), "OCO stop leg: order value 4.73 is under the minimum notional 5")

    def test_the_oco_cannot_sell_more_than_the_buy_delivers(self):
        self.assertBlocked(ck([modified(oco={"quantity": "0.00948"})]), "the buy delivers only 0.00947")

    def test_no_margin_futures_or_leverage_fields(self):
        for extra in ({"isIsolated": "TRUE"}, {"sideEffectType": "MARGIN_BUY"}, {"leverage": 3}):
            o = copy.deepcopy(ORDER)
            o.update(extra)
            self.assertBlocked(ck([o]), "unknown field")
        self.assertBlocked(ck([modified(order={"quoteOrderQty": "100"})]), "quoteOrderQty")

    def test_format(self):
        self.assertBlocked(ck([modified(oco={"side": "BUY"})]), "the protective OCO must SELL")
        self.assertBlocked(ck([modified(order={"type": "STOP_LOSS"})]), "MARKET or LIMIT")
        self.assertBlocked(ck([modified(order={"type": "LIMIT"})]), "a LIMIT order needs a price")
        self.assertBlocked(ck([modified(side="SELL")]), "an entry is a BUY")

    def test_assets(self):
        self.assertBlocked(ck([modified(symbol="NOPEUSDT")]), "not a Binance spot symbol")
        self.assertBlocked(ck([modified(symbol="LUNAUSDT")]), "not trading")
        self.assertBlocked(ck([modified(symbol="ETHBTC")]), "quoted in BTC")
        doge = modified(symbol="DOGEUSDT", order={"quantity": "100"},
                        oco={"quantity": "99", "abovePrice": "0.12400", "belowStopPrice": "0.11760"},
                        plan=dict(ORDER["plan"], entry=0.11873))
        self.assertBlocked(ck([doge]), "does not take STOP_LOSS orders")

    def test_stop_above_the_entry(self):
        self.assertBlocked(ck([modified(oco={"belowStopPrice": "52800.00"})]), "must be under the entry")


class Exits(unittest.TestCase):
    def exit_order(self, qty="0.00947"):
        return {"symbol": "BTCUSDT", "side": "SELL", "kind": "exit", "order": {"type": "MARKET", "quantity": qty},
                "cancel_order_list": {"orderListId": 7}}

    def test_an_exit_passes_outside_the_window(self):
        res = ck([self.exit_order()], now=datetime.datetime(2026, 10, 19, 9, 0),
                 portfolio={"cash": "500", "balances": {"BTC": "0.00947"}}, state=placed(3, symbol="BTCUSDT"))
        self.assertEqual(res["pass"], 1, reasons(res))

    def test_spot_cannot_go_short(self):
        res = ck([self.exit_order("0.01")], portfolio={"cash": "500", "balances": {"BTC": "0.00947"}})
        self.assertEqual(res["block"], 1)
        self.assertIn("short: selling 0.01 BTC but the account holds 0.00947", reasons(res))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = lambda n: os.path.join(self.d, n)
        size.write_orders(self.p("orders.json"), [ORDER], {}, "test", NOW)
        with open(self.p("cfg.json"), "w") as fh:
            json.dump(CFG, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_check(self, *extra):
        argv = ["--orders", self.p("orders.json"), "--config", self.p("cfg.json"), "--exchange-info",
                os.path.join(BFIX, "exchangeInfo.json"), "--portfolio", self.p("no.json"), "--prices",
                self.p("no.json"), "--state", self.p("no.json"), "--out", self.p("risk.json")] + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = check.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_pass_records_the_fingerprint(self):
        code, text, err = self.run_check("--now", "2026-10-19 02:01")
        self.assertEqual(code, 0, err + text)
        self.assertIn("1 passed, 0 blocked", text)
        with open(self.p("risk.json")) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["session"], "2026-10-19")
        self.assertEqual(rec["results"][0]["fingerprint"], om.fingerprint(ORDER))
        self.assertEqual(rec["results"][0]["verdict"], "PASS")

    def test_block_exits_1(self):
        code, text, _ = self.run_check("--now", "2026-10-19 07:00")
        self.assertEqual(code, 1)
        self.assertIn("BLOCK #1", text)

    def test_live_is_flagged(self):
        with open(self.p("cfg.json"), "w") as fh:
            json.dump(dict(CFG, testnet=False), fh)
        code, text, _ = self.run_check("--now", "2026-10-19 02:01")
        self.assertIn("WARNING: LIVE trading", text)


if __name__ == "__main__":
    unittest.main()
