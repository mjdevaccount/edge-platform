"""The ntfy.sh push budget and the one push path.

policy, publishers   HOW MUCH (the policy file) and WHO (the sender registry) -- stdlib
alertmanager         the Alertmanager night-window check -- needs PyYAML
cli                  `edge-notify` -- stdlib, runs from a bare checkout under `python -S`

Moved to platform-kit in v0.13.0 and resolved from there on first use, the same objects
(`edge_platform._moved`), so importing this package -- and the CLI -- never imports platform_kit:
types       Notice / Channel / Dedup / Budget / Outcome / Delivery
pipeline    Notifier: dedup, budget, publish, record, never raise
local       LocalLedger: the ledger's laws in process memory for an outage
ledger      SqlLedger over the consumer's three tables -- extra `[sql]` (not imported here)
"""

from __future__ import annotations

import importlib
from typing import Any

from .policy import CASES_PATH, POLICY_PATH, NtfyPolicy, check_pin, load, parse, sha256_lf
from .publishers import REGISTRY_ENV, Sender

#: name -> the platform_kit.notify submodule it is read from on first use.
_MOVED_NAMES: dict[str, str] = {
    "LocalLedger": "local",
    "Ledger": "pipeline",
    "Notifier": "pipeline",
    "Transport": "pipeline",
    "default_tier_for": "pipeline",
    "RECORDED": "types",
    "Budget": "types",
    "Channel": "types",
    "Dedup": "types",
    "Delivery": "types",
    "Notice": "types",
    "Outcome": "types",
    "delivered_for": "types",
    "window_key": "types",
}
_MOVED_MODULES = frozenset({"ledger", "local", "pipeline", "types"})


def __getattr__(name: str) -> Any:
    if name in _MOVED_NAMES:
        value = getattr(importlib.import_module(f"{__name__}.{_MOVED_NAMES[name]}"), name)
        globals()[name] = value  # resolve once
        return value
    if name in _MOVED_MODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    # the moved NAMES (they are in __all__); not the moved submodules, which a bare checkout or a
    # missing extra cannot import -- `inspect.getmembers` walks dir()
    return sorted({*globals(), *_MOVED_NAMES})


__all__ = [
    "CASES_PATH",
    "POLICY_PATH",
    "RECORDED",
    "REGISTRY_ENV",
    "Budget",
    "Channel",
    "Dedup",
    "Delivery",
    "Ledger",
    "LocalLedger",
    "Notice",
    "Notifier",
    "NtfyPolicy",
    "Outcome",
    "Sender",
    "Transport",
    "check_pin",
    "default_tier_for",
    "delivered_for",
    "load",
    "parse",
    "sha256_lf",
    "window_key",
]
