---
name: trader-setup
description: >-
  Setup for the Binance day-trading skills. Creates trader/config.json from the
  template (testnet on), makes sure git ignores trader/, and checks everything a
  testnet trade needs: Python 3.8+, the shared common/ folder, a valid config
  without keys in it, whether BINANCE_API_KEY and BINANCE_API_SECRET are set
  (never their values), that the Binance spot testnet answers, the clock drift,
  and that the key works (a read-only account call). Says in plain words what
  to fix. Use when the user says "set up the trading skills", "set up Binance",
  "connect Binance", "is everything ready", "check my setup", "why does the
  broker not work", or before the first /trader-scanner.
argument-hint: "[--init]"
---

# trader-setup

The first thing to run. It changes nothing unless asked (`--init`), and it never
reads a key's value: it only says whether the keys are there and whether Binance
accepts them.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Run it from the folder the user trades in: the `trader/` folder is created there.

```bash
python3 "${CLAUDE_SKILL_DIR}/setup.py" --init        # create trader/config.json, ignore trader/ in git, check all
python3 "${CLAUDE_SKILL_DIR}/setup.py"               # check only
python3 "${CLAUDE_SKILL_DIR}/setup.py" --offline     # skip the network
```

Exit code 0 = ready for a testnet trade.

## What it checks

| # | check | when it fails |
| --- | --- | --- |
| 1 | Python 3.8+ and `common/` | install a newer Python; copy `common/` next to the skill folders |
| 2 | `trader/config.json` valid, no keys in it | `--init` creates it from `config.example.json`; a file holding keys is refused |
| 3 | git ignores `trader/` | `--init` adds it to `.gitignore` |
| 4 | `BINANCE_API_KEY`, `BINANCE_API_SECRET` set | the user sets them in their own terminal (below) |
| 5 | the testnet answers, the clock is close | firewall, proxy, or Binance blocking the region (HTTP 451) |
| 6 | the key works (`GET /api/v3/account`, read only) | a testnet key only works on the testnet, a real key only on binance.com |

## The keys: what to tell the user

1. Open https://testnet.binance.vision, log in with GitHub, "Generate HMAC_SHA256 Key".
2. In their OWN terminal, before starting Claude Code (or in their shell profile):

   ```bash
   export BINANCE_API_KEY="..."
   export BINANCE_API_SECRET="..."
   ```

3. Restart Claude Code in that terminal, then run this check again.

Never ask for the keys, never set or print them yourself. If the user pastes a key
into the chat: tell them to delete it there and make a new one.

## Steps

1. `setup.py --init`. Read every line marked FIX to the user with what to do.
2. When it says "Ready for the testnet": suggest `/trader-scanner` after the
   opening range of their session (00:15 UTC with the defaults).
3. The plugin asks before every order on its own (a hook); outside the plugin, add
   `trader-broker/claude_settings.example.json` to `.claude/settings.json`.

## What it never does

It never places, changes or cancels an order, never prints or stores a key, never
switches the config to live.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Testnet by default (`"testnet": true`). Live trading needs `"testnet": false` in the config AND `--live`,
  and is the user's own decision.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order in this session, the user typed an explicit yes for that one order, and
  `ticket.py place` is called with the one-time code within 5 minutes. Keep
  `Bash(python3 *ticket.py place*)` on "ask" in Claude Code; never "allow".
- Spot only: no margin, futures, leverage, short selling or withdrawals. The client cannot call those endpoints.
- API keys live in `BINANCE_API_KEY` / `BINANCE_API_SECRET` in the environment, never in a file, the chat or a log.
