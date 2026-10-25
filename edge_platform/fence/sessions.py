"""The XNYS session table: one reader for the four places it can come from.

Ported from edge-host `scripts/lib_clock.py` (the Python twin of `lib-clock.sh`): same schema-1
document, same rules, same answers -- `tests/fence/test_sessions.py` drives lib-clock's own
fixture and cases through it. What is new is WHERE the table comes from (`load_sources`):

    db       quant.market_sessions (trade-data mig371), read with a select-only role
    cache    the schema-1 file the box already writes daily (edge-clock-export.timer), or the
             copy this module writes after every good db read
    bundled  the snapshot shipped inside this release (scripts/gen_bundled_sessions.py)
    superset no table at all: Mon-Fri 09:15-16:35 ET wall clock, holiday-unaware (fence.py)

Every question about an instant outside [first, horizon] raises ClockBlind. Blind never guesses;
the caller picks its safe side.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

CACHE_FILE = "/var/lib/edge-clock/sessions.json"
MAX_AGE_H = 336.0  # lib-clock.sh CLOCK_MAX_AGE_H: a cache older than 14 days is not trusted
BUNDLED = "sessions-bundled.json"


class ClockBlind(RuntimeError):
    """The session table cannot answer. Exit 2 territory, never a guess."""


def _utc(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.UTC)


def _z(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_zone() -> ZoneInfo | None:
    # NOT at import as a bare ZoneInfo(...): without tzdata that RAISES (ZoneInfoNotFoundError), and
    # an import-time traceback is rc 1 with no record -- the exit-2 path below could never fire.
    try:
        return ZoneInfo("America/New_York")
    except Exception:
        return None


_ZONE = _load_zone()


def et() -> ZoneInfo:
    """America/New_York, or ClockBlind (exit 2) when this host has no usable tzdata."""
    if _ZONE is None or dt.datetime(1970, 1, 1, tzinfo=dt.UTC).astimezone(_ZONE).tzname() != "EST":
        raise ClockBlind("this host cannot resolve America/New_York (no tzdata)")
    return _ZONE


def tz_ok() -> bool:
    """America/New_York resolves AND is right (1970-01-01 was EST)."""
    try:
        et()
        return True
    except Exception:
        return False


class Sessions:
    """A validated session table. `max_age_h=None` skips the age check (db and bundled sources:
    their truth does not age, only their horizon runs out)."""

    def __init__(self, doc: object, *, now: float | None = None, max_age_h: float | None = MAX_AGE_H) -> None:
        if not isinstance(doc, dict) or doc.get("schema") != 1:
            raise ClockBlind("unrecognised document (want schema 1)")
        try:
            self.first = dt.date.fromisoformat(doc["first"])
            self.horizon = dt.date.fromisoformat(doc["horizon"])
            self.generated_at = _utc(doc["generated_at"])
            rows = [
                (dt.date.fromisoformat(r["date"]), _utc(r["open_utc"]), _utc(r["close_utc"])) for r in doc["sessions"]
            ]
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise ClockBlind(f"malformed document ({type(e).__name__}: {e})") from e
        self.source_label = str(doc.get("source", ""))
        age_h = ((time.time() if now is None else now) - self.generated_at.timestamp()) / 3600
        if max_age_h is not None and age_h > max_age_h:
            raise ClockBlind(f"stale: generated {doc['generated_at']}, older than {max_age_h:g}h")
        if not tz_ok():
            raise ClockBlind("this host cannot resolve America/New_York (no tzdata)")
        self.rows = sorted(rows)
        self.by_day = {d: (o, c) for d, o, c in self.rows}
        self.doc = doc

    def _day(self, t: dt.datetime) -> dt.date:
        if t.tzinfo is None:
            raise ValueError("the fence needs a tz-aware instant")
        d = t.astimezone(et()).date()
        if not self.first <= d <= self.horizon:
            raise ClockBlind(f"{d} is outside the table {self.first}..{self.horizon}")
        return d

    def is_trading_day(self, t: dt.datetime) -> bool:
        return self._day(t) in self.by_day

    def is_rth(self, t: dt.datetime) -> bool:
        b = self.by_day.get(self._day(t))
        return bool(b and b[0] <= t <= b[1])

    def close_utc(self, t: dt.datetime) -> dt.datetime | None:
        b = self.by_day.get(self._day(t))
        return b[1] if b else None

    def next_open(self, t: dt.datetime) -> dt.datetime:
        self._day(t)
        for _, o, _c in self.rows:
            if o > t:
                return o
        raise ClockBlind(f"no open after {t.isoformat()} before the horizon {self.horizon}")

    def window_end(self, t: dt.datetime, before: dt.timedelta, after: dt.timedelta) -> dt.datetime | None:
        """When the [open-before, close+after) window containing t lifts; None outside one."""
        self._day(t)
        for _, o, c in self.rows:
            if o - before <= t < c + after:
                return c + after
        return None

    def next_window_start(self, t: dt.datetime, before: dt.timedelta) -> dt.datetime:
        """The first window start strictly after t (callers check window_end first)."""
        self._day(t)
        for _, o, _c in self.rows:
            if o - before > t:
                return o - before
        raise ClockBlind(f"no session starts after {t.isoformat()} before the horizon {self.horizon}")


def doc_from_rows(
    rows: list[tuple[dt.date, dt.datetime, dt.datetime, bool]], *, source: str, now: dt.datetime
) -> dict[str, Any]:
    """A schema-1 document from quant.market_sessions rows. first/horizon = the table's own
    [min, max]: mig371's comment says a date inside with no row is not a session."""
    if not rows:
        raise ClockBlind("quant.market_sessions returned no rows")
    rows = sorted(rows)
    return {
        "schema": 1,
        "calendar": "XNYS",
        "generated_at": _z(now),
        "source": source,
        "first": rows[0][0].isoformat(),
        "horizon": rows[-1][0].isoformat(),
        "sessions": [
            {"date": d.isoformat(), "open_utc": _z(o), "close_utc": _z(c), "early_close": bool(e)}
            for d, o, c, e in rows
        ],
    }


_DB_SQL = "SELECT session_date, open_utc, close_utc, early_close FROM quant.market_sessions ORDER BY session_date"


def read_db(dsn: str, *, timeout_s: int = 5) -> list[tuple[dt.date, dt.datetime, dt.datetime, bool]]:
    """quant.market_sessions via psycopg (the `db` extra). Raises ClockBlind on any failure."""
    try:
        import psycopg
    except ImportError as e:
        raise ClockBlind("psycopg is not installed (pip install 'edge-platform[db]')") from e
    try:
        with psycopg.connect(dsn, connect_timeout=timeout_s) as conn, conn.cursor() as cur:
            cur.execute(_DB_SQL)
            return list(cur.fetchall())
    except Exception as e:
        raise ClockBlind(f"quant.market_sessions unreadable ({type(e).__name__}: {e})") from e


def write_cache(doc: dict[str, Any], path: Path) -> None:
    """Atomic: a torn cache would read blind, never wrong, but rename keeps it whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".sessions.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    os.chmod(tmp, 0o644)  # a public calendar; mkstemp's 0600 would hide it from other users' callers
    os.replace(tmp, path)


def bundled_doc() -> dict[str, Any]:
    return json.loads(resources.files(__package__).joinpath(BUNDLED).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Loaded:
    sessions: Sessions | None  # None = every table source was blind: use the superset
    source: str  # db | cache | bundled | superset
    blind: list[str]  # why each skipped source was skipped, in order


def load_sources(
    *,
    dsn: str | None,
    cache: Path | None,
    now: float | None = None,
    max_age_h: float = MAX_AGE_H,
    use_bundled: bool = True,
    write_to: Path | None = None,
) -> Loaded:
    """The first source that answers, in the spec's order. A good db read is written to `write_to`
    when given -- never to `cache` implicitly: on the box that is edge-clock-export.timer's file."""
    blind: list[str] = []
    stamp = dt.datetime.fromtimestamp(time.time() if now is None else now, dt.UTC)
    if dsn:
        try:
            doc = doc_from_rows(read_db(dsn), source="quant.market_sessions", now=stamp)
            s = Sessions(doc, now=now, max_age_h=None)
            if write_to is not None:
                try:
                    write_cache(doc, write_to)
                except OSError as e:
                    blind.append(f"cache: not refreshed ({e})")
            return Loaded(s, "db", blind)
        except ClockBlind as e:
            blind.append(f"db: {e}")
    if cache is not None:
        try:
            doc = json.loads(cache.read_text(encoding="utf-8"))
            return Loaded(Sessions(doc, now=now, max_age_h=max_age_h), "cache", blind)
        except (OSError, ValueError) as e:
            blind.append(f"cache: cannot read {cache} ({type(e).__name__})")
        except ClockBlind as e:
            blind.append(f"cache: {e}")
    if use_bundled:
        try:
            return Loaded(Sessions(bundled_doc(), now=now, max_age_h=None), "bundled", blind)
        except (OSError, ValueError, ClockBlind) as e:
            blind.append(f"bundled: {e}")
    return Loaded(None, "superset", blind)
