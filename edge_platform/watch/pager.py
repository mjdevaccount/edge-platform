"""notify_pager -- page a watchdog through the notify pipeline (stdlib).

Lifted from trade-data `watchdog_runner._deliver` (M6 PL5/PL6). The watchdog's dedup is its OWN
state machine (`should_page` + the state table's `last_page_at`), so the channel this pages on must
be caller-dedup with no budget: a ledger-side dedup would swallow the 6-hourly re-alert and silently
halve its cadence. The bool returned is `Delivery.delivered` -- a failed push leaves `last_page_at`
NULL and the next sweep retries. A notice that cannot even be built raises into the runner, which
reports `watchdog_page_failed`; the pipeline's own failures arrive as `notify_failed:<scope>`.
"""

from __future__ import annotations

from edge_platform.notify.pipeline import Notifier
from edge_platform.notify.types import Channel, Notice

from .runner import Pager
from .spec import Probe, WatchdogSpec


def notify_pager(notifier: Notifier, channel: Channel) -> Pager:
    """The `page` a WatchRunner takes: one Notice per page, keyed by the watchdog id, carrying the
    spec's title, tier, push severity and tags and the probe's detail."""
    if channel.dedup.claims or channel.budget is not None:
        raise ValueError(
            f"watchdog channel {channel.scope!r} must be Dedup.caller() with no budget -- the runner's "
            "state machine owns dedup, and a ledger dedup would swallow the re-alert"
        )

    async def page(spec: WatchdogSpec, probe: Probe) -> bool:
        notice = Notice(
            channel, key=spec.id, title=spec.title, body=probe.detail,
            severity=spec.ntfy_severity or "default", tier=spec.severity, tags=tuple(spec.tags),
        )  # fmt: skip
        return (await notifier.notify(notice)).delivered

    return page
