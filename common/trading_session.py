#!/usr/bin/env python3
"""The configurable trading session for a 24/7 market. All times are UTC, naive datetimes.

Crypto never closes, so "the session" is a convention you set in trader/config.json:

  session_start_utc      "00:00"  the session starts every day at this UTC time and lasts 24 hours
  opening_range_minutes  15       the opening range: the first N minutes after the start
  entry_window_minutes   360      new entries only until N minutes after the start (then: expired)
  max_hold_minutes       240      a trade is closed at the latest N minutes after its entry

A session is named by the UTC date it STARTS on. With "session_start_utc": "22:00" the
session of 2026-10-19 runs from 2026-10-19 22:00 to 2026-10-20 22:00 UTC, so 01:30 on the
20th still belongs to the session of the 19th.
"""

import datetime
import re

DEFAULTS = {"session_start_utc": "00:00", "opening_range_minutes": 15, "entry_window_minutes": 360,
            "max_hold_minutes": 240}
EPOCH = datetime.datetime(1970, 1, 1)
DAY = datetime.timedelta(days=1)


class SessionError(Exception):
    pass


def parse_hhmm(text, what="a time"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text if text is not None else ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise SessionError("%s wants a UTC time like 00:00 or 13:30, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def ms_of(utc):
    return ts_of(utc) * 1000


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def label(utc):
    return utc.strftime("%Y-%m-%d %H:%M UTC")


def parse_now(text):
    """--now -> naive UTC. "2026-10-19 10:15" (no offset) is UTC; "...Z" or "+02:00" are honoured;
    a bare "10:15" means today at 10:15 UTC."""
    if not text:
        return utc_now()
    t = str(text).strip()
    if t.upper().endswith(" UTC"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (utc_now().date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise SessionError('--now wants a UTC time like "2026-10-19 10:15" (or with an offset, ...+02:00)')
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def settings(cfg=None):
    """The session settings from a config dict, checked."""
    c = dict(DEFAULTS)
    for k in DEFAULTS:
        if (cfg or {}).get(k) is not None:
            c[k] = cfg[k]
    start = parse_hhmm(c["session_start_utc"], "session_start_utc")
    out = {"start": start, "session_start_utc": "%02d:%02d" % (start.hour, start.minute)}
    for k in ("opening_range_minutes", "entry_window_minutes", "max_hold_minutes"):
        try:
            v = int(c[k])
        except (TypeError, ValueError):
            raise SessionError("%s must be a whole number of minutes, got %r" % (k, c[k]))
        if not 1 <= v <= 24 * 60:
            raise SessionError("%s must be between 1 and 1440 minutes, got %d" % (k, v))
        out[k] = v
    if out["opening_range_minutes"] >= out["entry_window_minutes"]:
        raise SessionError("entry_window_minutes (%d) must be longer than opening_range_minutes (%d)"
                           % (out["entry_window_minutes"], out["opening_range_minutes"]))
    return out


def session_start(now_utc, start):
    """The start of the session that holds now_utc (the latest start at or before it)."""
    cand = datetime.datetime.combine(now_utc.date(), start)
    if cand > now_utc:
        cand -= DAY
    return cand


def start_of_date(day, start):
    """The start of the session named `day` (a date or YYYY-MM-DD)."""
    if isinstance(day, str):
        day = datetime.date.fromisoformat(day)
    return datetime.datetime.combine(day, start)


def window(start_dt, s):
    """All the edges of one session, from its start."""
    return {"date": start_dt.date().isoformat(), "start": start_dt, "end": start_dt + DAY,
            "range_end": start_dt + datetime.timedelta(minutes=s["opening_range_minutes"]),
            "entry_end": start_dt + datetime.timedelta(minutes=s["entry_window_minutes"])}


def current(now_utc, cfg=None):
    """The session around now_utc, plus where in it we are ("range", "entries", "late")."""
    s = settings(cfg)
    w = window(session_start(now_utc, s["start"]), s)
    if now_utc < w["range_end"]:
        w["phase"] = "range"
    elif now_utc <= w["entry_end"]:
        w["phase"] = "entries"
    else:
        w["phase"] = "late"
    w["settings"] = s
    return w


def of_date(day, cfg=None):
    s = settings(cfg)
    w = window(start_of_date(day, s["start"]), s)
    w["settings"] = s
    return w


def session_date_of(utc, cfg=None):
    """The session name (YYYY-MM-DD) a UTC instant belongs to."""
    return session_start(utc, settings(cfg)["start"]).date().isoformat()


def exit_by(entry_utc, cfg=None, minutes=None):
    """When a trade entered at entry_utc must be closed at the latest."""
    m = minutes if minutes is not None else settings(cfg)["max_hold_minutes"]
    return entry_utc + datetime.timedelta(minutes=int(m))
