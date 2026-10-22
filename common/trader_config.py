#!/usr/bin/env python3
"""trader/config.json for the Binance toolkit: the defaults, the loader and the checks.

The template is config.example.json at the repository root. API keys are NEVER in this
file: they come from BINANCE_API_KEY and BINANCE_API_SECRET in the environment.
Money amounts are in the quote asset (USDT by default).
"""

import json
import os

DEFAULT_PATH = "trader/config.json"
DEFAULTS = {
    "testnet": True,
    "market_data": "trading",
    "quote_asset": "USDT",
    "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"],
    "budget": 1000,
    "cash_buffer_pct": 10,
    "max_position_pct": 50,
    "max_order": 500,
    "min_order": 10,
    "max_loss_per_trade": 10,
    "max_daily_loss": 30,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.5,
    "require_stop": True,
    "fee_rate": 0.001,
    "stop_limit_offset_pct": 0.5,
    "session_start_utc": "00:00",
    "opening_range_minutes": 15,
    "entry_window_minutes": 360,
    "max_hold_minutes": 240,
    "history_days": 60,
    "min_quote_volume_24h": 5000000,
    "exclude_leveraged_tokens": True,
    "exclude_stable_pairs": True,
    "blocked": [],
}
STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDE", "PYUSD", "EUR", "EURI", "AEUR",
           "USD1", "XUSD", "RLUSD", "BFUSD", "U"}
SECRET_KEYS = {"api_key", "api_secret", "apikey", "secret", "secret_key", "binance_api_key", "binance_api_secret"}


class ConfigFileError(Exception):
    pass


def load(path=DEFAULT_PATH, required=False):
    """DEFAULTS overlaid with the file (if any). Refuses a file that holds keys."""
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise ConfigFileError("%s is not valid JSON: %s" % (path, e))
        if not isinstance(doc, dict):
            raise ConfigFileError("%s must hold a JSON object" % path)
        bad = sorted(k for k in doc if str(k).lower() in SECRET_KEYS)
        if bad:
            raise ConfigFileError("%s holds %s: API keys never go in a file. Delete them there, revoke them in "
                                  "Binance, and set BINANCE_API_KEY / BINANCE_API_SECRET in the environment."
                                  % (path, ", ".join(bad)))
        cfg.update(doc)
    elif required:
        raise ConfigFileError("no config at %s: copy config.example.json there first" % path)
    if cfg.get("testnet") not in (True, False):
        raise ConfigFileError("\"testnet\" must be true or false, got %r" % cfg.get("testnet"))
    try:
        fee = float(cfg.get("fee_rate"))
    except (TypeError, ValueError):
        raise ConfigFileError("\"fee_rate\" must be a number like 0.001 (0.1 %%), got %r" % cfg.get("fee_rate"))
    if not 0 <= fee < 0.05:
        raise ConfigFileError("\"fee_rate\" %r looks wrong: 0.001 means 0.1 %% a trade" % cfg.get("fee_rate"))
    cfg["quote_asset"] = str(cfg.get("quote_asset") or "USDT").upper()
    return cfg


def is_leveraged_token(base):
    """Binance's old leveraged tokens: BTCUP, BTCDOWN, ETHBULL, ETHBEAR ..."""
    b = str(base or "").upper()
    return len(b) > 2 and (b.endswith("UP") or b.endswith("DOWN") or b.endswith("BULL") or b.endswith("BEAR")) \
        and b not in ("JUP", "SUP", "PUP", "UP")


def is_stable_pair(base, quote):
    return str(base or "").upper() in STABLES and str(quote or "").upper() in STABLES
