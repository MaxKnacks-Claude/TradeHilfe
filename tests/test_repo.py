import json
import os
import re
import unittest

from _load import ROOT

# v2, the day-trading pack: the eleven folders the reel's repo card shows
SKILLS = ["trader-scanner", "trader-news", "trader-entry", "trader-exit", "trader-timemachine",
          "trader-risk", "trader-budget", "trader-broker", "trader-alerts", "trader-analyst",
          "trader-pelosi", "trader-setup"]
ABOUT = ("Claude skills for crypto day trading on Binance spot: scan USDT pairs, time the entry, plan stop, target "
         "and max hold, replay 10,000 outcomes, size with fees, check your limits, and place on the testnet by "
         "default, never without your yes.")
NOT_USED = ["trader-news", "trader-analyst", "trader-pelosi"]
# skills on the way to an order carry the safety block
ORDER_PATH = ["trader-scanner", "trader-entry", "trader-exit", "trader-timemachine", "trader-budget",
              "trader-risk", "trader-broker", "trader-alerts", "trader-setup"]
V1_ONLY = ["trader-copycat", "trader-screener", "trader-tax", "trader-rebalance"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_eleven_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_matches_the_reel(self):
        root = sorted(d for d in os.listdir(ROOT) if not d.startswith(".git"))
        root = [r for r in root if r != "dist"]           # build output, ignored by git
        self.assertEqual(root, sorted([".claude-plugin", "LICENSE", "README.md", "common", "config.example.json",
                                      "hooks", "tests", "tools"] + SKILLS))

    def test_gitignore_keeps_the_trader_folder_and_local_configs_out(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh]
        for pat in ("trader/", "*.local.json", "config.json", "!config.example.json", ".env", "dist/"):
            self.assertIn(pat, lines)

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_plugin_manifest_points_at_the_root_skills(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "binance-day-trader")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        self.assertEqual(plugin["license"], "MIT")
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "binance-day-trader")
        self.assertEqual(market["plugins"][0]["name"], "binance-day-trader")

    def test_readme_explains_setup_and_risks(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# Binance Day Trader"))
        for line in ("github.com/Jakeschincariol/day-trader-skill", "## Einrichtung (Testnet)", "## Risiken",
                     "BINANCE_API_KEY", "BINANCE_API_SECRET", "Enable Withdrawals", "IP-Beschränkung",
                     "## Keine Order ohne Ihre Bestätigung", "## Testnet-Durchlauf", "\"testnet\": false",
                     "--live", "Nicht genutzt", "flowchart LR"):
            self.assertIn(line, readme)
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)

    def test_license_is_the_original(self):
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("MIT License\n\nCopyright (c) 2026 Jake Schincariol\n"))

    def test_unused_skills_say_so(self):
        for s in NOT_USED:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("NOT", fm.split("description:")[1][:40].upper(), s)
            self.assertIn("Not used in the Binance pipeline", text, s)

    def test_pipeline_skills_speak_binance(self):
        for s in sorted(set(SKILLS) - set(NOT_USED)):
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("Binance", text, s)
            self.assertIn("testnet", text.lower(), s)
            for old in ("Alpaca", "Yahoo", "NYSE", "place_stock_order", "\"paper\""):
                self.assertNotIn(old, text, "%s still mentions %s" % (s, old))

    def test_no_em_dashes_and_no_personal_email(self):
        for path in every_text_file():
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            if not rel.startswith(os.path.join("tests", "fixtures")):  # fixtures are real data, kept as published
                self.assertNotIn("\u2014", text, rel)
            self.assertNotIn("jakeschincariol" + "@" + "gmail.com", text, rel)  # built, so this file passes too

    def test_no_v1_only_skill_is_left_behind(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith("tests" + os.sep) or rel == "README.md":
                continue  # the README points v1 users to the v1 commit
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for old in V1_ONLY:
                self.assertNotIn(old, text, rel)

    def test_every_tool_runs_on_python_3_8(self):
        # no 3.9+ only modules or syntax in the tools (the README promises 3.8)
        for s in SKILLS + ["common"]:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix(",
                            "match ", "list[", "dict[", "tuple["):
                    if bad == "match ":
                        self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", "%s/%s" % (s, f))
                    elif bad.endswith("["):
                        self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), "%s/%s" % (s, f))
                    else:
                        self.assertNotIn(bad, src, "%s/%s" % (s, f))

    def test_day_trading_limits_ship_in_the_example_config(self):
        with open(os.path.join(ROOT, "config.example.json")) as fh:
            cfg = json.load(fh)
        for k in ("max_loss_per_trade", "max_daily_loss", "max_trades_per_day", "min_reward_risk", "require_stop",
                  "fee_rate", "session_start_utc", "opening_range_minutes", "entry_window_minutes",
                  "max_hold_minutes", "quote_asset", "symbols"):
            self.assertIn(k, cfg, k)
        self.assertIs(cfg["require_stop"], True)
        self.assertEqual(cfg["fee_rate"], 0.001)
        for k in cfg:   # no switch that turns on margin, futures, shorts or leverage, and no keys in the file
            self.assertNotRegex(k.lower(), r"^allow_|margin|futures|^leverage|short|api_?key|secret", k)

    def test_testnet_is_the_default(self):
        with open(os.path.join(ROOT, "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["testnet"], True)


if __name__ == "__main__":
    unittest.main()
