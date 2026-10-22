import copy
import datetime
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import BFIX, FakeBinance, FakeClock, bfix, load, common

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")
size = load("trader-budget", "size")
bc = common("binance_client")
sf = common("symbol_filters")
om = common("order_math")

INFO = os.path.join(BFIX, "exchangeInfo.json")
BTC = sf.rules_of(sf.find_symbol(bfix("exchangeInfo.json"), "BTCUSDT"))
KEY = "testkey-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SECRET = "testsecret-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
CFG = {"min_reward_risk": 0.5}
PLAN = {"symbol": "BTCUSDT", "side": "buy", "setup": "orb", "date": "2026-10-19", "entry": 52730.0,
        "stop": 52545.29, "target": 53007.07, "entry_time": "2026-10-19T02:00:00Z", "exit_by": "2026-10-19T06:00:00Z"}
NOW = "2026-10-19 02:01"


class FakeExchange(object):
    """Binance spot as far as these tools use it, answering in the documented shapes
    (rest-api.md: FULL order response, order list response, account information)."""

    def __init__(self, fill=True, oco_error=None, order_error=None):
        self.fill, self.oco_error, self.order_error = fill, oco_error, order_error
        self.orders, self.lists, self.next_id = {}, {}, 1000
        self.btc, self.usdt = Decimal("0"), Decimal("1000")
        routes = {("GET", "/api/v3/time"): {"serverTime": 1792375260000},
                  ("POST", "/api/v3/order"): self.new_order, ("POST", "/api/v3/order/test"): {},
                  ("POST", "/api/v3/orderList/oco"): self.new_oco, ("GET", "/api/v3/account"): self.account,
                  ("GET", "/api/v3/order"): self.get_order, ("GET", "/api/v3/orderList"): self.get_list,
                  ("DELETE", "/api/v3/orderList"): self.cancel_list}
        self.net = FakeBinance(routes)
        self.clock = FakeClock(1792375260.0)
        self.client = bc.BinanceClient(bc.TESTNET_URL, bc.keys_from_env({"BINANCE_API_KEY": KEY,
                                                                         "BINANCE_API_SECRET": SECRET}),
                                       opener=self.net, sleep=self.clock.sleep, clock=self.clock)

    def sent(self, method=None, path=None):
        return [c for c in self.net.calls if (method is None or c["method"] == method) and
                (path is None or c["path"] == path) and c["path"] != "/api/v3/time"]

    def orders_sent(self):
        return [c for c in self.net.calls if c["method"] in ("POST", "DELETE") and not c["path"].endswith("/test")]

    def new_order(self, p):
        if self.order_error:
            return (400, {}, {"code": -2010, "msg": self.order_error})
        self.next_id += 1
        q = Decimal(p["quantity"])
        filled = self.fill or p["type"] == "MARKET"
        price = Decimal("52730.00") if p["side"] == "BUY" else Decimal("52800.00")
        o = {"symbol": p["symbol"], "orderId": self.next_id, "orderListId": -1, "clientOrderId": p.get("newClientOrderId"),
             "transactTime": 1792375260000, "price": p.get("price", "0.00000000"), "origQty": p["quantity"],
             "executedQty": p["quantity"] if filled else "0.00000000",
             "origQuoteOrderQty": "0.00000000", "cummulativeQuoteQty": str(q * price) if filled else "0.00000000",
             "status": "FILLED" if filled else "NEW", "timeInForce": p.get("timeInForce", "GTC"), "type": p["type"],
             "side": p["side"], "workingTime": 1792375260000, "selfTradePreventionMode": "EXPIRE_MAKER", "fills": []}
        if filled:
            fee = (q * Decimal("0.001")) if p["side"] == "BUY" else (q * price * Decimal("0.001"))
            o["fills"] = [{"price": str(price), "qty": p["quantity"], "commission": str(fee),
                           "commissionAsset": "BTC" if p["side"] == "BUY" else "USDT", "tradeId": 1}]
            if p["side"] == "BUY":
                self.btc += q - fee
                self.usdt -= q * price
            else:
                self.btc -= q
                self.usdt += q * price - fee
        self.orders[self.next_id] = o
        return o

    def new_oco(self, p):
        if self.oco_error:
            return (400, {}, {"code": -2010, "msg": self.oco_error})
        lid = len(self.lists) + 1
        legs = []
        for t in (p["belowType"], p["aboveType"]):
            self.next_id += 1
            self.orders[self.next_id] = {"symbol": p["symbol"], "orderId": self.next_id, "status": "NEW", "type": t,
                                         "executedQty": "0.00000000", "cummulativeQuoteQty": "0.00000000",
                                         "origQty": p["quantity"]}
            legs.append({"symbol": p["symbol"], "orderId": self.next_id, "clientOrderId": "leg%d" % self.next_id})
        self.lists[lid] = {"orderListId": lid, "contingencyType": "OCO", "listStatusType": "EXEC_STARTED",
                           "listOrderStatus": "EXECUTING", "listClientOrderId": p.get("listClientOrderId"),
                           "transactionTime": 1792375260000, "symbol": p["symbol"], "orders": legs}
        return dict(self.lists[lid], orderReports=[])

    def account(self, p):
        return {"makerCommission": 10, "takerCommission": 10, "canTrade": True, "canWithdraw": True,
                "canDeposit": True, "accountType": "SPOT", "permissions": ["SPOT"],
                "balances": [{"asset": "BTC", "free": str(self.btc), "locked": "0.00000000"},
                             {"asset": "USDT", "free": str(self.usdt), "locked": "0.00000000"}]}

    def get_order(self, p):
        return self.orders[int(p["orderId"])]

    def get_list(self, p):
        return self.lists[int(p["orderListId"])]

    def cancel_list(self, p):
        lst = self.lists[int(p["orderListId"])]
        lst.update(listStatusType="ALL_DONE", listOrderStatus="ALL_DONE")
        return lst

    def take_profit_fills(self, lid=1):
        lst = self.lists[lid]
        lst.update(listStatusType="ALL_DONE", listOrderStatus="ALL_DONE")
        tp = self.orders[lst["orders"][1]["orderId"]]
        tp.update(status="FILLED", executedQty=tp["origQty"],
                  cummulativeQuoteQty=str(Decimal(tp["origQty"]) * Decimal("53007.07")))
        self.orders[lst["orders"][0]["orderId"]]["status"] = "EXPIRED"


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = lambda n: os.path.join(self.d, n)
        with open(self.p("cfg.json"), "w") as fh:
            json.dump(CFG, fh)
        s = size.size_trade(dict(PLAN), CFG, BTC, now_utc=datetime.datetime(2026, 10, 19, 2, 1))
        self.order = size.proposed_order(s, 0.5)
        size.write_orders(self.p("orders.json"), [self.order], CFG, "test")
        self.ex = FakeExchange()
        ticket.CLIENT = self.ex.client

    def tearDown(self):
        ticket.CLIENT = None
        shutil.rmtree(self.d)

    def files(self):
        return ["--orders", self.p("orders.json"), "--config", self.p("cfg.json"), "--risk", self.p("risk.json"),
                "--state", self.p("broker.json"), "--portfolio", self.p("portfolio.json"), "--exchange-info", INFO]

    def t(self, *argv, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        args = list(argv)
        sub = args[0]
        base = ["--config", self.p("cfg.json"), "--state", self.p("broker.json"), "--portfolio",
                self.p("portfolio.json")]
        extra = self.files() if sub in ("show", "validate", "approve", "place", "protect", "decline") else base
        if sub == "flatten":
            extra = base + ["--orders", self.p("orders.json"), "--exchange-info", INFO]
        with redirect_stdout(out), redirect_stderr(err):
            code = ticket.main(args + extra + (["--now", now] if now else []))
        return code, out.getvalue(), err.getvalue()

    def risk(self, now=NOW):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = check.main(["--orders", self.p("orders.json"), "--config", self.p("cfg.json"), "--exchange-info",
                               INFO, "--portfolio", self.p("portfolio.json"), "--prices", self.p("none.json"),
                               "--state", self.p("broker.json"), "--out", self.p("risk.json"), "--now", now])
        return code, out.getvalue()

    def approve(self, reply="yes", now=NOW):
        code, text, err = self.t("approve", "--index", "1", "--reply", reply, now=now)
        codes = [l.split(": ")[1].split(" ")[0] for l in text.splitlines() if l.startswith("Confirmation code")]
        return code, codes[0] if codes else None, text + err

    # ------------------------------------------------------------ no order without the three gates

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = self.t("show")
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: trader-risk has not checked", text)
        code, _, err = self.t("approve", "--index", "1", "--reply", "yes")
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)
        self.assertEqual(self.ex.net.calls, [])

    def test_place_without_a_yes_sends_nothing(self):
        self.risk()
        code, _, err = self.t("place", "--index", "1", "--confirm", "abc123")
        self.assertEqual(code, 1)
        self.assertIn("no open yes", err)
        self.assertEqual(self.ex.net.calls, [])

    def test_a_vague_reply_is_not_a_yes(self):
        self.risk()
        for reply in ("ok", "sure", "go", "yes to all", "yes but later", "ja, aber nur halb", "yes ETHUSDT",
                      "yes 0.5", "yes?", "do it"):
            code, conf, _ = self.approve(reply)
            self.assertEqual(code, 1, reply)
            self.assertIsNone(conf, reply)
        self.assertEqual(self.ex.net.calls, [])

    def test_the_wrong_code_sends_nothing(self):
        self.risk()
        code, conf, _ = self.approve()
        self.assertEqual(code, 0)
        code, _, err = self.t("place", "--index", "1", "--confirm", "000000" if conf != "000000" else "111111")
        self.assertEqual(code, 1)
        self.assertIn("confirmation code does not match", err)
        self.assertEqual(self.ex.orders_sent(), [])

    def test_an_old_yes_expires(self):
        self.risk()
        _, conf, _ = self.approve()
        code, _, err = self.t("place", "--index", "1", "--confirm", conf, now="2026-10-19 02:07")
        self.assertEqual(code, 1)
        self.assertIn("minutes old", err)
        self.assertEqual(self.ex.orders_sent(), [])

    def test_a_changed_order_needs_a_new_check(self):
        self.risk()
        _, conf, _ = self.approve()
        changed = copy.deepcopy(self.order)
        changed["order"]["quantity"] = "0.00900"
        size.write_orders(self.p("orders.json"), [changed], CFG, "test")
        code, _, err = self.t("place", "--index", "1", "--confirm", conf)
        self.assertEqual(code, 1)
        self.assertIn("not in the last risk check", err)
        self.assertEqual(self.ex.orders_sent(), [])

    def test_a_blocked_order_cannot_be_approved_or_placed(self):
        with open(self.p("cfg.json"), "w") as fh:
            json.dump({"min_reward_risk": 2}, fh)
        self.risk()
        code, text, _ = self.t("show")
        self.assertIn("BLOCKED by trader-risk", text)
        code, conf, _ = self.approve()
        self.assertEqual(code, 1)
        self.assertEqual(self.ex.net.calls, [])

    def test_the_code_is_stored_only_as_a_hash(self):
        self.risk()
        _, conf, _ = self.approve()
        with open(self.p("broker.json")) as fh:
            text = fh.read()
        self.assertNotIn(conf, text)
        self.assertIn(hashlib.sha256(conf.encode()).hexdigest(), text)

    # ------------------------------------------------------------ the full flow on the (fake) testnet

    def test_the_full_flow_entry_then_oco(self):
        code, text = self.risk()
        self.assertEqual(code, 0, text)
        code, text, _ = self.t("show")
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, Binance spot, TESTNET, no real money", text)
        self.assertIn("BUY 0.00948 BTCUSDT at market", text)
        self.assertIn("then at once an OCO: SELL 0.00947, take-profit 53007.07 / stop 52545.29", text)
        self.assertIn('POST /api/v3/order {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.00948"}',
                      text)
        code, conf, out = self.approve("yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ex.net.calls, [])                  # nothing went out before place
        code, text, err = self.t("place", "--index", "1", "--confirm", conf)
        self.assertEqual(code, 0, err)
        posts = self.ex.orders_sent()
        self.assertEqual([c["path"] for c in posts], ["/api/v3/order", "/api/v3/orderList/oco"])
        buy, oco = posts[0]["params"], posts[1]["params"]
        self.assertEqual((buy["symbol"], buy["side"], buy["type"], buy["quantity"], buy["newOrderRespType"]),
                         ("BTCUSDT", "BUY", "MARKET", "0.00948", "FULL"))
        self.assertIn("signature", buy)
        self.assertEqual(posts[0]["base"], bc.TESTNET_URL)
        # 0.00948 bought, 0.00000948 BTC fee: 0.00947052 arrived, the OCO sells 0.00947
        self.assertEqual((oco["side"], oco["quantity"], oco["aboveType"], oco["abovePrice"], oco["belowType"],
                          oco["belowStopPrice"]), ("SELL", "0.00947", "LIMIT_MAKER", "53007.07", "STOP_LOSS",
                                                    "52545.29"))
        self.assertIn("OCO set: SELL 0.00947 BTCUSDT", text)
        with open(self.p("broker.json")) as fh:
            st = [r["status"] for r in json.load(fh)["orders"]]
        self.assertEqual(st, ["approved", "placed", "protected"])
        with open(self.p("portfolio.json")) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["balances"]["BTC"], "0.00947052")
        self.assertEqual(pf["venue"], "testnet")
        # the same code cannot place twice
        code, _, err = self.t("place", "--index", "1", "--confirm", conf)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.ex.orders_sent()), 2)
        # and trader-risk now counts the trade
        code, text = self.risk()
        self.assertEqual(code, 1)
        self.assertIn("duplicate", text)

    def test_keys_never_show_in_the_output(self):
        self.risk()
        _, conf, out1 = self.approve()
        code, text, err = self.t("place", "--index", "1", "--confirm", conf)
        for blob in (out1, text, err, open(self.p("broker.json")).read(), open(self.p("portfolio.json")).read()):
            self.assertNotIn(KEY, blob)
            self.assertNotIn(SECRET, blob)
        self.assertEqual(self.ex.net.calls[1]["headers"]["X-MBX-APIKEY"], KEY)   # it is sent, as a header only

    def test_a_refused_oco_says_unprotected_and_does_not_retry(self):
        self.ex.oco_error = "Order would trigger immediately."
        self.risk()
        _, conf, _ = self.approve()
        code, text, err = self.t("place", "--index", "1", "--confirm", conf)
        self.assertIn("THE POSITION IS UNPROTECTED", text)
        self.assertEqual([c["path"] for c in self.ex.orders_sent()], ["/api/v3/order", "/api/v3/orderList/oco"])
        with open(self.p("broker.json")) as fh:
            self.assertEqual(json.load(fh)["orders"][-1]["status"], "unprotected")
        self.ex.oco_error = None
        code, text, _ = self.t("protect", "--index", "1")
        self.assertEqual(code, 0, text)
        self.assertIn("OCO set", text)
        code, _, err = self.t("protect", "--index", "1")
        self.assertEqual(code, 1)
        self.assertIn("already has its OCO", err)

    def test_a_refused_entry_places_no_oco(self):
        self.ex.order_error = "Filter failure: NOTIONAL"
        self.risk()
        _, conf, _ = self.approve()
        code, _, err = self.t("place", "--index", "1", "--confirm", conf)
        self.assertEqual(code, 1)
        self.assertIn("Nothing was bought or sold", err)
        self.assertEqual([c["path"] for c in self.ex.orders_sent()], ["/api/v3/order"])

    def test_a_limit_buy_waits_for_its_fill(self):
        s = size.size_trade(dict(PLAN), CFG, BTC, limit=52700.0, now_utc=datetime.datetime(2026, 10, 19, 2, 1))
        size.write_orders(self.p("orders.json"), [size.proposed_order(s, 0.5)], CFG, "test")
        self.ex.fill = False
        self.risk()
        _, conf, _ = self.approve()
        code, text, err = self.t("place", "--index", "1", "--confirm", conf)
        self.assertEqual(code, 0, err)
        self.assertIn("not filled yet: no OCO", text)
        self.assertEqual([c["path"] for c in self.ex.orders_sent()], ["/api/v3/order"])
        code, text, _ = self.t("protect", "--index", "1")
        self.assertEqual(code, 1)
        self.assertIn("nothing to protect yet", text)
        o = self.ex.orders[max(self.ex.orders)]
        o.update(status="FILLED", executedQty=o["origQty"])
        code, text, _ = self.t("protect", "--index", "1")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.ex.orders_sent()[-1]["params"]["quantity"], "0.00947")

    def test_status_records_the_take_profit(self):
        self.risk()
        _, conf, _ = self.approve()
        self.t("place", "--index", "1", "--confirm", conf)
        self.ex.take_profit_fills()
        code, text, _ = self.t("status")
        self.assertEqual(code, 0)
        self.assertIn("Closed by the take-profit: SELL 0.00947", text)
        with open(self.p("broker.json")) as fh:
            last = json.load(fh)["orders"][-1]
        self.assertEqual((last["status"], last["leg"], last["kind"]), ("closed", "take-profit", "exit"))

    def test_flatten_cancels_the_oco_and_sells_after_its_own_yes(self):
        self.risk()
        _, conf, _ = self.approve()
        self.t("place", "--index", "1", "--confirm", conf)
        code, text, _ = self.t("flatten", "--symbol", "BTCUSDT", now="2026-10-19 06:00")
        self.assertEqual(code, 0, text)
        with open(self.p("orders.json")) as fh:
            ex_order = json.load(fh)["orders"][0]
        self.assertEqual(ex_order["cancel_order_list"], {"orderListId": 1})
        self.assertEqual(ex_order["order"], {"type": "MARKET", "quantity": "0.00947"})
        code, text = self.risk(now="2026-10-19 06:00")
        self.assertEqual(code, 0, text)                         # an exit is never blocked by the entry window
        sent_before = len(self.ex.orders_sent())
        code, conf2, _ = self.approve("yes", now="2026-10-19 06:00")
        self.assertEqual(len(self.ex.orders_sent()), sent_before)
        code, text, err = self.t("place", "--index", "1", "--confirm", conf2, now="2026-10-19 06:01")
        self.assertEqual(code, 0, err)
        last = self.ex.orders_sent()[-2:]
        self.assertEqual([(c["method"], c["path"]) for c in last],
                         [("DELETE", "/api/v3/orderList"), ("POST", "/api/v3/order")])
        self.assertEqual(last[1]["params"]["side"], "SELL")

    # ------------------------------------------------------------ live needs both switches

    def test_live_needs_the_config_and_the_flag(self):
        ticket.CLIENT = None
        self.risk()
        _, conf, _ = self.approve()
        # --live alone: the config still says testnet
        code, _, err = self.t("place", "--index", "1", "--confirm", conf, "--live")
        self.assertEqual(code, 1)
        self.assertIn("--live was given but the config says \"testnet\": true", err)
        with open(self.p("cfg.json"), "w") as fh:
            json.dump(dict(CFG, testnet=False), fh)
        # the config alone: --live missing
        code, _, err = self.t("account")
        self.assertEqual(code, 1)
        self.assertIn("--live was not given", err)
        # both switches, but the yes was given for the testnet ticket
        code, _, err = self.t("place", "--index", "1", "--confirm", conf, "--live")
        self.assertEqual(code, 1)
        self.assertIn("the yes was for the TESTNET", err)
        self.assertEqual(self.ex.net.calls, [])

    def test_validate_uses_the_test_endpoint_only(self):
        self.risk()
        code, text, _ = self.t("validate", "--index", "1")
        self.assertEqual(code, 0)
        self.assertEqual([c["path"] for c in self.ex.net.calls if c["path"] != "/api/v3/time"],
                         ["/api/v3/order/test"])
        self.assertEqual(self.ex.orders_sent(), [])


class Yes(unittest.TestCase):
    ORDER = {"symbol": "BTCUSDT", "side": "BUY", "kind": "entry", "order": {"type": "MARKET", "quantity": "0.00948"},
             "oco": {"quantity": "0.00947", "abovePrice": "53007.07", "belowStopPrice": "52545.29"},
             "plan": {"max_loss": 2.77}}

    def yes(self, reply):
        return ticket.parse_yes(reply, self.ORDER, ["BTCUSDT", "ETHUSDT"])[0]

    def test_yes(self):
        for r in ("yes", "Yes.", "YES", "y", "ja", "yes, place it", "yes BTCUSDT", "yes 0.00948", "yes 52545.29"):
            self.assertTrue(self.yes(r), r)

    def test_not_yes(self):
        for r in ("", "ok", "okay", "sure", "go", "yep", "yes to all", "yes both", "yes ETHUSDT", "yes 0.01",
                  "yes?", "no", "yes, wait", "ja aber später", "yes if it dips", "yes SOLUSDT", "yes ethbtc"):
            self.assertFalse(self.yes(r), r)


if __name__ == "__main__":
    unittest.main()
