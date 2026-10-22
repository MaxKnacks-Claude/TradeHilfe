#!/usr/bin/env python3
"""Setup check for the Binance day-trading skills (trader-setup). Standard library only.

    python3 setup.py                 # check everything, change nothing
    python3 setup.py --init          # also create trader/config.json (testnet) and ignore trader/ in git
    python3 setup.py --offline       # skip the network checks

It checks, in order, and says in plain words what to do about each problem:
  1. Python 3.8 or newer, and the shared folder common/
  2. trader/config.json: present, valid, no keys in it, testnet on
  3. trader/ is ignored by git (when this folder is a git repository)
  4. BINANCE_API_KEY and BINANCE_API_SECRET are set (only whether, never what)
  5. the Binance venue answers (GET /api/v3/ping and /api/v3/time, public, no key) and your clock is close
  6. the key works there: GET /api/v3/account, read only (it places nothing)

Exit code 0 when everything needed for a testnet trade is in place, 1 otherwise.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMMON = next((p for p in (os.path.join(_HERE, "common"), os.path.join(_HERE, os.pardir, "common"))
                if os.path.isfile(os.path.join(p, "binance_client.py"))), os.path.join(_HERE, os.pardir, "common"))
_COMMON = os.path.abspath(_COMMON)
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)

OK, WARN, FAIL = "ok  ", "note", "FIX "


class Report(object):
    def __init__(self):
        self.lines, self.failed = [], False

    def add(self, level, text):
        self.lines.append("%s  %s" % (level, text))
        if level == FAIL:
            self.failed = True


def template_path():
    for p in (os.path.join(_HERE, "config.example.json"), os.path.join(_HERE, os.pardir, "config.example.json")):
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None


def check_python(r):
    v = sys.version_info
    if v < (3, 8):
        r.add(FAIL, "Python %d.%d: the tools need 3.8 or newer" % (v[0], v[1]))
    else:
        r.add(OK, "Python %d.%d.%d" % (v[0], v[1], v[2]))
    try:
        import binance_client  # noqa: F401
        r.add(OK, "shared folder common/ found (%s)" % _COMMON)
        return True
    except ImportError:
        r.add(FAIL, "the shared folder common/ is missing next to the skill folders: copy it too")
        return False


def check_config(r, path, init):
    import trader_config
    if not os.path.exists(path):
        if init:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            tpl = template_path()
            if tpl:
                shutil.copyfile(tpl, path)
            else:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(trader_config.DEFAULTS, fh, indent=2)
                    fh.write("\n")
            r.add(OK, "created %s from the template (testnet on); set your budget and limits there" % path)
        else:
            r.add(FAIL, "no %s yet: run setup.py --init (or copy config.example.json there)" % path)
            return None
    try:
        cfg = trader_config.load(path)
    except trader_config.ConfigFileError as e:
        r.add(FAIL, str(e))
        return None
    if cfg.get("testnet") is False:
        r.add(WARN, "%s says \"testnet\": false: LIVE trading with real money (every order call also needs --live)"
              % path)
    else:
        r.add(OK, "%s is valid, testnet on, budget %s %s, max loss %s a trade, fee %.2f %%"
              % (path, cfg.get("budget"), cfg["quote_asset"], cfg.get("max_loss_per_trade"),
                 100 * float(cfg.get("fee_rate") or 0)))
    return cfg


def check_git(r, init):
    try:
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=10).stdout.decode().strip() == "true"
    except (OSError, subprocess.SubprocessError):
        r.add(WARN, "git not found: make sure trader/ never ends up in a repository")
        return
    if not inside:
        r.add(OK, "this folder is not a git repository: trader/ cannot be committed by accident")
        return
    ignored = subprocess.run(["git", "check-ignore", "-q", "trader/config.json"], timeout=10).returncode == 0
    if ignored:
        r.add(OK, "git ignores trader/")
    elif init:
        with open(".gitignore", "a", encoding="utf-8") as fh:
            fh.write("\n# binance-day-trader: orders, logs, config\ntrader/\n")
        r.add(OK, "added trader/ to .gitignore")
    else:
        r.add(FAIL, "git would commit trader/ (orders, logs, your config): add \"trader/\" to .gitignore "
                    "(setup.py --init does it)")


def check_keys(r, env):
    key, secret = env.get("BINANCE_API_KEY") or "", env.get("BINANCE_API_SECRET") or ""
    if key and secret:
        r.add(OK, "BINANCE_API_KEY and BINANCE_API_SECRET are set (not shown)")
        if len(key) < 20 or len(secret) < 20 or " " in key + secret:
            r.add(WARN, "the key or secret looks too short or has a space in it: check the copy")
        return True
    missing = [n for n, v in (("BINANCE_API_KEY", key), ("BINANCE_API_SECRET", secret)) if not v]
    r.add(FAIL, "%s not set. Make a key at https://testnet.binance.vision (log in with GitHub), then in YOUR "
                "terminal, before starting Claude: export BINANCE_API_KEY=... and export BINANCE_API_SECRET=... "
                "Never paste keys into the chat." % " and ".join(missing))
    return False


def check_network(r, cfg, live, have_keys, env):
    import binance_client as bc
    try:
        cl = bc.client(cfg, live=live)
    except bc.BinanceError as e:
        r.add(FAIL, str(e))
        return
    venue = "testnet" if cl.testnet else "LIVE"
    try:
        t0 = time.time()
        cl.ping()
        server = cl.server_time()
        ms = int((time.time() - t0) * 1000)
        drift = abs(server / 1000.0 - time.time())
        r.add(OK, "Binance %s answers (%s, %d ms)" % (venue, cl.base_url, ms))
        if drift > 1.0:
            r.add(WARN, "your clock is %.1f s off Binance's: the tools correct for it, but sync your clock (NTP)"
                  % drift)
    except bc.BinanceError as e:
        r.add(FAIL, "Binance %s does not answer (%s): %s. A firewall, a proxy, or Binance blocking your region "
                    "(HTTP 451) can cause this." % (venue, cl.base_url, e))
        return
    if not have_keys:
        return
    try:
        signed = bc.client(cfg, live=live, signed=True, env=env)
        acct = signed.account()
    except bc.BinanceError as e:
        hint = ""
        if e.code in (-2014, -2015):
            hint = (" The key is not valid on the %s: testnet keys only work on testnet.binance.vision, real keys "
                    "only on binance.com." % venue)
        r.add(FAIL, "the key does not work: %s.%s" % (e, hint))
        return
    quote = cfg["quote_asset"]
    free = "0"
    for b in acct.get("balances") or []:
        if str(b.get("asset", "")).upper() == quote:
            free = b.get("free")
    if acct.get("canTrade") is False:
        r.add(FAIL, "the key works but trading is not enabled for it")
    else:
        r.add(OK, "the key works on the %s: %s %s free (read only, nothing placed)" % (venue, free, quote))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Setup check for the Binance day-trading skills (trader-setup).")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--init", action="store_true", help="create trader/config.json and ignore trader/ in git")
    ap.add_argument("--offline", action="store_true", help="skip the network and key checks")
    ap.add_argument("--live", action="store_true", help="check the live API (needs \"testnet\": false)")
    a = ap.parse_args(argv)
    r = Report()
    if check_python(r):
        cfg = check_config(r, a.config, a.init)
        check_git(r, a.init)
        have = check_keys(r, os.environ)
        if cfg is not None and not a.offline:
            check_network(r, cfg, a.live, have, os.environ)
    print("\n".join(r.lines))
    print("")
    if r.failed:
        print("Not ready yet: fix the lines marked FIX, then run setup.py again.")
    else:
        print("Ready for the testnet. Start with /trader-scanner (scan.py orb) after your session's opening range.")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
