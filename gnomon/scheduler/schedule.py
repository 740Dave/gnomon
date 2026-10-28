"""Trigger parsing and next-fire arithmetic (recurring forms +).

Forms (a job carries EXACTLY ONE):
  {"times": ["06:10", ...], "weekdays": ["mon", ...]?}   local time of day, optional weekdays
  {"every_minutes": N}                                   fixed interval from the confirm
  {"cron": "m h dom mon dow"}                            five fields, local time
  {"once_at": "2026-10-10T14:00:00Z"}                    one-shot, a UTC instant (Z or an explicit offset)
Pure: no clock, no I/O. Local-time DST rule: a nonexistent time runs at the next valid minute; a repeated time runs
once (the first occurrence).
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .limits import LIMITS

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
RECURRING_KEYS = ("times", "every_minutes", "cron")
ONE_SHOT_KEY = "once_at"
STAGE_KEYS = ("boot", "login", "on_login", "on_gnomon_start", "on_start", "on-gnomon-start", "at_boot")
_UTC = _dt.timezone.utc


class TriggerError(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field, self.reason = field, reason


def local_tz() -> _dt.tzinfo:
    """The Mac's local zone as a DST-aware zoneinfo (from /etc/localtime), UTC if it cannot be read."""
    try:
        import zoneinfo

        tzenv = os.environ.get("TZ")
        if tzenv:
            return zoneinfo.ZoneInfo(tzenv.lstrip(":"))
        real = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in real:
            return zoneinfo.ZoneInfo(real.split("zoneinfo/", 1)[1])
    except Exception:  # noqa: BLE001
        pass
    return _UTC


def parse_once_at(value: Any) -> float:
    """An ISO 8601 instant with Z or an explicit offset -> epoch seconds. A naive time is refused."""
    if not isinstance(value, str) or not value.strip():
        raise TriggerError("trigger.once_at", "must be an ISO 8601 UTC instant such as 2026-10-10T14:00:00Z")
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        raise TriggerError("trigger.once_at", f"{value!r} is not an ISO 8601 instant") from None
    if d.tzinfo is None or d.utcoffset() is None:
        raise TriggerError("trigger.once_at", "needs a Z or an explicit UTC offset (a one-shot is always UTC)")
    return d.astimezone(_UTC).timestamp()


def _parse_hhmm(v: Any) -> Tuple[int, int]:
    if not isinstance(v, str) or len(v) != 5 or v[2] != ":" or not (v[:2] + v[3:]).isdigit():
        raise TriggerError("trigger.times", f"{v!r} is not HH:MM")
    h, m = int(v[:2]), int(v[3:])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise TriggerError("trigger.times", f"{v!r} is out of range")
    return h, m


def _cron_field(text: str, lo: int, hi: int, name: str) -> frozenset:
    out = set()
    for part in text.split(","):
        if not part:
            raise TriggerError("trigger.cron", f"empty item in the {name} field")
        step = 1
        if "/" in part:
            part, st = part.split("/", 1)
            if not st.isdigit() or int(st) < 1:
                raise TriggerError("trigger.cron", f"bad step in the {name} field")
            step = int(st)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            if not (x.isdigit() and y.isdigit()):
                raise TriggerError("trigger.cron", f"bad range in the {name} field")
            a, b = int(x), int(y)
        elif part.isdigit():
            a = int(part)
            b = hi if step != 1 else a
        else:
            raise TriggerError("trigger.cron", f"bad item {part!r} in the {name} field")
        if a < lo or b > hi or a > b:
            raise TriggerError("trigger.cron", f"{name} value out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return frozenset(out)


def parse_cron(expr: Any) -> Dict[str, Any]:
    if not isinstance(expr, str):
        raise TriggerError("trigger.cron", "must be a string of five fields")
    f = expr.split()
    if len(f) != 5:
        raise TriggerError("trigger.cron", "needs exactly five fields: minute hour day-of-month month day-of-week")
    dow = _cron_field(f[4], 0, 7, "day-of-week")
    return {"minute": _cron_field(f[0], 0, 59, "minute"), "hour": _cron_field(f[1], 0, 23, "hour"),
            "dom": _cron_field(f[2], 1, 31, "day-of-month"), "month": _cron_field(f[3], 1, 12, "month"),
            "dow": frozenset(0 if d == 7 else d for d in dow),
            "dom_star": f[2].startswith("*"), "dow_star": f[4].startswith("*")}


def normalize_trigger(trig: Any) -> Dict[str, Any]:
    """Validate and normalise a proposed trigger. Raises :class:`TriggerError` naming the field."""
    if not isinstance(trig, dict) or not trig:
        raise TriggerError("trigger", "must be an object with exactly one trigger form")
    for k in trig:
        if k in STAGE_KEYS:
            from .vocab import STAGE_2_TRIGGER

            raise TriggerError("trigger." + str(k), STAGE_2_TRIGGER)
    forms = [k for k in trig if k in RECURRING_KEYS or k == ONE_SHOT_KEY]
    extra = sorted(k for k in trig if k not in RECURRING_KEYS and k not in (ONE_SHOT_KEY, "weekdays"))
    if extra:
        raise TriggerError("trigger." + extra[0], "unknown trigger field")
    if len(forms) != 1:
        raise TriggerError("trigger", "must carry exactly one trigger form: times, every_minutes, cron or once_at")
    if "weekdays" in trig and forms[0] != "times":
        raise TriggerError("trigger.weekdays", "weekdays go with the times form only")
    form = forms[0]
    if form == ONE_SHOT_KEY:
        epoch = parse_once_at(trig[ONE_SHOT_KEY])
        return {"once_at": _dt.datetime.fromtimestamp(epoch, tz=_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if form == "every_minutes":
        n = trig["every_minutes"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise TriggerError("trigger.every_minutes", "must be a whole number of minutes, at least 1")
        return {"every_minutes": n}
    if form == "cron":
        parse_cron(trig["cron"])
        return {"cron": " ".join(trig["cron"].split())}
    times = trig["times"]
    if not isinstance(times, list) or not times or len(times) > 48:
        raise TriggerError("trigger.times", "must be a non-empty list of HH:MM (at most 48)")
    norm = sorted({"%02d:%02d" % _parse_hhmm(t) for t in times})
    out: Dict[str, Any] = {"times": norm}
    if "weekdays" in trig:
        wd = trig["weekdays"]
        if (not isinstance(wd, list) or not wd or any(not isinstance(w, str) or w.lower() not in WEEKDAYS
                                                      for w in wd)):
            raise TriggerError("trigger.weekdays", "must be a non-empty list drawn from " + ", ".join(WEEKDAYS))
        out["weekdays"] = [w for w in WEEKDAYS if w in {x.lower() for x in wd}]
    return out


def is_one_shot(trig: Dict[str, Any]) -> bool:
    return ONE_SHOT_KEY in trig


def one_shot_epoch(trig: Dict[str, Any]) -> float:
    return parse_once_at(trig[ONE_SHOT_KEY])


def local_to_epoch(naive: _dt.datetime, tz: _dt.tzinfo) -> float:
    """A wall-clock time -> epoch. A nonexistent time (spring forward) moves to the next valid minute; a repeated time
    (fall back) takes the first occurrence."""
    d = naive.replace(tzinfo=tz, fold=0)
    for _ in range(0, 181):
        rt = d.astimezone(_UTC).astimezone(tz).replace(tzinfo=None)
        if rt == d.replace(tzinfo=None):
            return d.timestamp()
        naive = naive + _dt.timedelta(minutes=1)
        d = naive.replace(tzinfo=tz, fold=0)
    return d.timestamp()


def _day_slots(trig: Dict[str, Any], day: _dt.date, cron: Optional[Dict[str, Any]]) -> List[Tuple[int, int]]:
    if "cron" in trig:
        assert cron is not None
        dow = (day.weekday() + 1) % 7
        dom_ok, dow_ok = day.day in cron["dom"], dow in cron["dow"]
        if cron["dom_star"] or cron["dow_star"]:
            day_ok = dom_ok and dow_ok
        else:
            day_ok = dom_ok or dow_ok
        if day.month not in cron["month"] or not day_ok:
            return []
        return [(h, m) for h in sorted(cron["hour"]) for m in sorted(cron["minute"])]
    wd = trig.get("weekdays")
    if wd and WEEKDAYS[day.weekday()] not in wd:
        return []
    return [_parse_hhmm(t) for t in trig["times"]]


def next_fires(trig: Dict[str, Any], after: float, n: int, *, tz: Optional[_dt.tzinfo] = None,
               anchor: Optional[float] = None) -> List[float]:
    """The next ``n`` fire instants strictly after ``after`` (epoch). A one-shot yields its instant if after it, else
    nothing. ``every_minutes`` counts from ``anchor`` (the confirm time)."""
    if ONE_SHOT_KEY in trig:
        t = one_shot_epoch(trig)
        return [t] if t > after else []
    if "every_minutes" in trig:
        iv = trig["every_minutes"] * 60.0
        a = float(anchor if anchor is not None else after)
        k = int((after - a) // iv) + 1 if after >= a else 1
        return [a + (k + i) * iv for i in range(n)]
    tz = tz or local_tz()
    cron = parse_cron(trig["cron"]) if "cron" in trig else None
    out: List[float] = []
    day = _dt.datetime.fromtimestamp(after, tz=tz).date() - _dt.timedelta(days=1)
    for _ in range(0, 366 * 5):
        for h, m in _day_slots(trig, day, cron):
            e = local_to_epoch(_dt.datetime(day.year, day.month, day.day, h, m), tz)
            if e > after and (not out or e > out[-1]):
                out.append(e)
        if len(out) >= n:
            return out[:n]
        day += _dt.timedelta(days=1)
    return out


def min_gap_s(trig: Dict[str, Any], now: float, *, tz: Optional[_dt.tzinfo] = None) -> Optional[float]:
    """The smallest gap between consecutive fires, from a sample of upcoming fires (None for a one-shot or a trigger
    with fewer than two fires in the sample). ``every_minutes`` is exact."""
    if ONE_SHOT_KEY in trig:
        return None
    if "every_minutes" in trig:
        return trig["every_minutes"] * 60.0
    fires = next_fires(trig, now, 400, tz=tz)
    if len(fires) < 2:
        return None
    return min(b - a for a, b in zip(fires, fires[1:]))


def describe(trig: Dict[str, Any]) -> str:
    """Plain language for the tab's schedule line."""
    if ONE_SHOT_KEY in trig:
        return "Once at " + trig[ONE_SHOT_KEY] + " (UTC)"
    if "every_minutes" in trig:
        n = trig["every_minutes"]
        return "Every minute" if n == 1 else f"Every {n} minutes"
    if "cron" in trig:
        return "On the schedule " + trig["cron"] + " (local time)"
    days = ""
    if trig.get("weekdays"):
        days = " on " + ", ".join(trig["weekdays"])
    return "Every day" + days + " at " + ", ".join(trig["times"]) + " (local time)" if not trig.get("weekdays") \
        else "At " + ", ".join(trig["times"]) + days + " (local time)"
