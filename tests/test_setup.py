"""Getting it into Claude: the plugin's order guard hook, the setup check, and the ZIPs for the Claude app."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout, redirect_stderr

from _load import BFIX, ROOT, load

guard = load("hooks", "order_guard")
setup = load("trader-setup", "setup")
builder = load("tools", "build_skill_zips")


def hook(command, tool="Bash"):
    """Run the hook as Claude Code does: JSON on stdin, decision JSON (or nothing) on stdout."""
    p = subprocess.run([sys.executable, os.path.join(ROOT, "hooks", "order_guard.py")],
                       input=json.dumps({"tool_name": tool, "tool_input": {"command": command}}).encode(),
                       stdout=subprocess.PIPE, timeout=20)
    out = p.stdout.decode().strip()
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else "pass"


class OrderGuard(unittest.TestCase):
    def test_every_order_command_asks(self):
        for c in ("python3 trader-broker/ticket.py place --index 1 --confirm ab12cd",
                  'python3 "${CLAUDE_SKILL_DIR}/ticket.py" place --index 1 --confirm ab12cd --live',
                  "cd x && python3 /p/trader-broker/ticket.py protect --index 1"):
            self.assertEqual(hook(c), "ask", c)

    def test_reading_commands_pass(self):
        for c in ("python3 trader-broker/ticket.py show", "python3 trader-broker/ticket.py approve --index 1 "
                  "--reply yes", "python3 trader-risk/check.py", "ls -la", "python3 trader-broker/ticket.py status"):
            self.assertEqual(hook(c), "pass", c)

    def test_keys_are_never_printed(self):
        for c in ("echo $BINANCE_API_SECRET", "printenv BINANCE_API_KEY", "env", "printenv", "env | grep -i bin",
                  "set", "export BINANCE_API_KEY=abc"):
            self.assertEqual(hook(c), "deny", c)

    def test_other_tools_are_left_alone(self):
        self.assertEqual(hook("ticket.py place", tool="Read"), "pass")

    def test_the_plugin_declares_the_hook(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json")) as fh:
            doc = json.load(fh)
        h = doc["hooks"]["PreToolUse"][0]
        self.assertEqual(h["matcher"], "Bash")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/order_guard.py", h["hooks"][0]["command"])


class SetupCheck(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("BINANCE_")}

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def run_setup(self, *argv):
        out = io.StringIO()
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        try:
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = setup.main(list(argv))
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return code, out.getvalue()

    def test_init_creates_a_testnet_config(self):
        code, text = self.run_setup("--init", "--offline")
        with open("trader/config.json") as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["testnet"], True)
        self.assertIn("created trader/config.json", text)
        self.assertIn("FIX   BINANCE_API_KEY and BINANCE_API_SECRET not set", text)
        self.assertEqual(code, 1)

    def test_keys_are_reported_but_never_shown(self):
        self.env.update(BINANCE_API_KEY="k" * 64, BINANCE_API_SECRET="s" * 64)
        code, text = self.run_setup("--init", "--offline")
        self.assertIn("are set (not shown)", text)
        self.assertNotIn("k" * 64, text)
        self.assertNotIn("s" * 64, text)
        self.assertEqual(code, 0)

    def test_without_init_nothing_is_created(self):
        code, text = self.run_setup("--offline")
        self.assertFalse(os.path.exists("trader"))
        self.assertIn("run setup.py --init", text)

    def test_a_config_with_keys_is_refused(self):
        os.makedirs("trader")
        with open("trader/config.json", "w") as fh:
            json.dump({"api_secret": "x"}, fh)
        code, text = self.run_setup("--offline")
        self.assertEqual(code, 1)
        self.assertIn("API keys never go in a file", text)


class Zips(unittest.TestCase):
    def test_each_zip_runs_on_its_own(self):
        out = tempfile.mkdtemp()
        try:
            with redirect_stdout(io.StringIO()):
                builder.main(["--out", out])
            names = sorted(os.listdir(out))
            self.assertEqual(names, sorted(s + ".zip" for s in builder.SKILLS))
            self.assertNotIn("trader-pelosi.zip", names)
            with zipfile.ZipFile(os.path.join(out, "trader-scanner.zip")) as z:
                members = z.namelist()
                self.assertIn("trader-scanner/SKILL.md", members)
                self.assertIn("trader-scanner/common/binance_client.py", members)
                z.extractall(os.path.join(out, "x"))
            with zipfile.ZipFile(os.path.join(out, "trader-setup.zip")) as z:
                self.assertIn("trader-setup/config.example.json", z.namelist())
            p = subprocess.run([sys.executable, os.path.join(out, "x", "trader-scanner", "scan.py"), "orb", "--offline",
                                os.path.join(BFIX, "scan-5m-2026-10-19.json.gz"), "--now", "2026-10-19 03:00",
                                "--no-write", "--config", os.path.join(out, "none.json")],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=out, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr.decode())
            self.assertIn("4 of 6 charts closed above", p.stdout.decode())
        finally:
            shutil.rmtree(out)

    def test_the_build_is_reproducible(self):
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        try:
            with redirect_stdout(io.StringIO()):
                builder.main(["--out", a])
                builder.main(["--out", b])
            for n in os.listdir(a):
                with open(os.path.join(a, n), "rb") as f1, open(os.path.join(b, n), "rb") as f2:
                    self.assertEqual(f1.read(), f2.read(), n)
        finally:
            shutil.rmtree(a)
            shutil.rmtree(b)


if __name__ == "__main__":
    unittest.main()
