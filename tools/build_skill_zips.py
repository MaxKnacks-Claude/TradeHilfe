#!/usr/bin/env python3
"""Builds one upload-ready ZIP per skill for the Claude app (claude.ai: Settings > Capabilities > Skills).

    python3 tools/build_skill_zips.py               # -> dist/trader-*.zip
    python3 tools/build_skill_zips.py --out /tmp/x

A skill uploaded on its own cannot see the repository's common/ folder, so each ZIP carries
its own copy inside the skill folder (the scripts look there first). trader-setup also gets
config.example.json. The unused stock skills (news, analyst, pelosi) are left out.

Note: in the Claude app the scripts run in Anthropic's sandbox, whose network may not reach
Binance. The full experience (the order guard hook, your keys in your own environment) is
Claude Code with the plugin; see the README.
"""

import argparse
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = ["trader-setup", "trader-scanner", "trader-entry", "trader-exit", "trader-timemachine",
          "trader-budget", "trader-risk", "trader-broker", "trader-alerts"]
EPOCH = (2026, 1, 1, 0, 0, 0)       # fixed timestamps: the same sources give the same bytes


def add(z, src, arc):
    info = zipfile.ZipInfo(arc, EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    with open(src, "rb") as fh:
        z.writestr(info, fh.read())


def files_of(folder):
    out = []
    for base, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for f in sorted(files):
            if not f.endswith((".pyc", ".tmp")):
                out.append(os.path.join(base, f))
    return out


def build(skill, out_dir):
    path = os.path.join(out_dir, skill + ".zip")
    with zipfile.ZipFile(path, "w") as z:
        for f in files_of(os.path.join(ROOT, skill)):
            add(z, f, os.path.join(skill, os.path.relpath(f, os.path.join(ROOT, skill))))
        for f in files_of(os.path.join(ROOT, "common")):
            add(z, f, os.path.join(skill, "common", os.path.relpath(f, os.path.join(ROOT, "common"))))
        if skill == "trader-setup":
            add(z, os.path.join(ROOT, "config.example.json"), os.path.join(skill, "config.example.json"))
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    for s in SKILLS:
        p = build(s, a.out)
        print("%-36s %6.1f KB" % (os.path.relpath(p), os.path.getsize(p) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
