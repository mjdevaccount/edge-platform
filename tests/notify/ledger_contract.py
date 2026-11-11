"""The notify ledger's laws, run against EVERY implementation (ported from trade-data
tests/notify_ledger_contract.py).

Unit (test_pipeline.py): the in-memory double. Postgres (test_ledger_pg.py): SqlLedger. One suite,
so a call-site test pinned on the double pins production's semantics.

Each case takes `ledger`, `age(scope, key, seconds)` (make the newest claim of (scope, key) that much
older) and `first_sight(channel, keys)` (a Notifier's, over the same ledger)."""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Awaitable, Callable
from typing import Any

from edge_platform.notify.types import Budget, Channel, Dedup, Notice, Outcome

Age = Callable[[str, str, float], Awaitable[None]]
NOW = dt.datetime(2026, 10, 23, 15, 0, tzinfo=dt.UTC)
_n = itertools.count()


def _scope(tag: str) -> str:
    return f"contract-{tag}-{next(_n)}"  # fresh per case: the SQL ledger persists across cases


def _notice(ch: Channel, key: str) -> Notice:
    return Notice(ch, key=key, title=f"t {key}", body="b")


async def once_dedups_forever(ledger: Any, age: Age) -> None:
    ch = Channel(_scope("once"), dedup=Dedup.once())
    assert await ledger.admit(_notice(ch, "a"), NOW) == (None, None)
    assert await ledger.admit(_notice(ch, "a"), NOW) == (Outcome.DEDUPED, None)
    await age(ch.scope, "a", 10 * 365 * 86400.0)
    assert await ledger.admit(_notice(ch, "a"), NOW) == (Outcome.DEDUPED, None)
    assert await ledger.admit(_notice(ch, "b"), NOW) == (None, None)


async def window_dedups_inside_and_passes_after(ledger: Any, age: Age) -> None:
    ch = Channel(_scope("win"), dedup=Dedup.window(3600))
    assert await ledger.admit(_notice(ch, "k"), NOW) == (None, None)
    assert await ledger.admit(_notice(ch, "k"), NOW) == (Outcome.DEDUPED, None)
    await age(ch.scope, "k", 3601.0)
    assert await ledger.admit(_notice(ch, "k"), NOW) == (None, None)
    assert await ledger.admit(_notice(ch, "k"), NOW) == (Outcome.DEDUPED, None)


async def budget_caps_the_window_and_turns(ledger: Any, age: Age) -> None:
    ch = Channel(_scope("budget"), budget=Budget(2, "utc_day"))
    assert await ledger.admit(_notice(ch, "1"), NOW) == (None, None)
    assert await ledger.admit(_notice(ch, "2"), NOW) == (None, None)
    assert await ledger.admit(_notice(ch, "3"), NOW) == (Outcome.BUDGET_REFUSED, "utc-day:2026-10-23")
    assert await ledger.admit(_notice(ch, "4"), NOW + dt.timedelta(days=1)) == (None, None)


async def a_refused_notice_leaves_no_claim(ledger: Any, age: Age) -> None:
    ch = Channel(_scope("rollback"), dedup=Dedup.once(), budget=Budget(1, "utc_day"))
    assert await ledger.admit(_notice(ch, "x"), NOW) == (None, None)
    assert (await ledger.admit(_notice(ch, "y"), NOW))[0] is Outcome.BUDGET_REFUSED
    # y never pushed, so it is NOT a duplicate once the window turns
    assert await ledger.admit(_notice(ch, "y"), NOW + dt.timedelta(days=1)) == (None, None)
    assert await ledger.admit(_notice(ch, "x"), NOW + dt.timedelta(days=2)) == (Outcome.DEDUPED, None)


async def a_zero_cap_refuses_everything(ledger: Any, age: Age) -> None:
    ch = Channel(_scope("zero"), budget=Budget(0))
    assert (await ledger.admit(_notice(ch, "a"), NOW))[0] is Outcome.BUDGET_REFUSED


async def caller_dedup_admits_every_notice(ledger: Any, age: Age) -> None:
    ch = Channel(_scope("caller"))
    for _i in range(3):
        assert await ledger.admit(_notice(ch, "same"), NOW) == (None, None)


async def et_week_budget_counts_the_et_week(ledger: Any, age: Age) -> None:
    ch = Channel(_scope("week"), budget=Budget(1, "et_iso_week"))
    sunday_night_et = dt.datetime(2026, 10, 26, 3, 0, tzinfo=dt.UTC)  # Sun 23:00 ET, W43
    assert await ledger.admit(_notice(ch, "a"), sunday_night_et) == (None, None)
    assert await ledger.admit(_notice(ch, "b"), sunday_night_et) == (Outcome.BUDGET_REFUSED, "et-week:2026-W43")
    assert await ledger.admit(_notice(ch, "c"), sunday_night_et + dt.timedelta(hours=2)) == (None, None)  # W44


async def first_sight_names_only_never_claimed_keys(ledger: Any, age: Age) -> None:
    """A key is new exactly once, however long ago it was claimed -- a restart is just a later call."""
    from edge_platform.notify.pipeline import Notifier

    from .fakes import RecordingTransport

    n = Notifier(transport=RecordingTransport(), ledger=ledger)
    ch = Channel(_scope("sight"), dedup=Dedup.once())
    assert await n.first_sight(ch, ["a", "b"], now=NOW) == {"a", "b"}
    await age(ch.scope, "a", 30 * 86400.0)
    assert await n.first_sight(ch, ["a", "b", "c"], now=NOW) == {"c"}
    assert await n.first_sight(ch, ["a", "b", "c"], now=NOW) == set()


CASES = (
    once_dedups_forever, window_dedups_inside_and_passes_after, budget_caps_the_window_and_turns,
    a_refused_notice_leaves_no_claim, a_zero_cap_refuses_everything, caller_dedup_admits_every_notice,
    et_week_budget_counts_the_et_week, first_sight_names_only_never_claimed_keys,
)  # fmt: skip
