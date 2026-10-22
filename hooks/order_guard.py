#!/usr/bin/env python3
"""PreToolUse hook of the binance-day-trader plugin. Standard library only.

Claude Code calls it before every Bash command. It reads the tool call as JSON on stdin and:

  ask   for every `ticket.py place` and `ticket.py protect`: the only commands that send an
        order to Binance. Claude Code then shows its own permission prompt, on top of the
        user's yes and the one-time code, even if Bash is otherwise allowed.
  deny  for commands that would print the API keys: anything naming BINANCE_API_KEY or
        BINANCE_API_SECRET, and a bare env / printenv / set that lists every variable.

Everything else passes untouched (no output, exit 0).
"""

import json
import re
import sys

ORDER = re.compile(r"ticket\.py[\"']?\s+(place|protect)\b")
KEYS = re.compile(r"BINANCE_API_(KEY|SECRET)")
DUMP = re.compile(r"(^|[;&|]\s*)(env|printenv|set|export\s+-p)\s*($|[;&|>])")


def decide(command):
    """(decision, reason) or (None, None) to let the command pass."""
    c = command or ""
    if KEYS.search(c):
        return "deny", ("binance-day-trader: this command names BINANCE_API_KEY / BINANCE_API_SECRET. The keys stay "
                        "in the user's environment and are never read, printed or set by Claude. The tools read them "
                        "themselves.")
    if DUMP.search(c.strip()):
        return "deny", ("binance-day-trader: listing every environment variable would print the API keys. Ask for the "
                        "one variable you need instead.")
    m = ORDER.search(c)
    if m:
        return "ask", ("binance-day-trader: `ticket.py %s` sends an order to Binance (the testnet unless --live). "
                       "Allow only if the user just said yes to exactly this ticket." % m.group(1))
    return None, None


def main():
    try:
        call = json.load(sys.stdin)
    except ValueError:
        return 0
    if call.get("tool_name") != "Bash":
        return 0
    decision, reason = decide((call.get("tool_input") or {}).get("command"))
    if decision:
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                          "permissionDecisionReason": reason}}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
