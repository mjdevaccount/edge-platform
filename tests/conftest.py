"""The estate config the package no longer ships (v0.6.0): every test runs against the fixtures.

A consumer supplies its own fence policy (`EDGE_FENCE_POLICY` / `--policy`) and sender registry
(`EDGE_NOTIFY_PUBLISHERS` / `--registry`); the tests that prove the ABSENT case unset them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

HERE = Path(__file__).parent
FENCE_POLICY = HERE / "fence" / "fixtures" / "fence-policy.json"
PUBLISHERS = HERE / "fixtures" / "ntfy-publishers.json"


@pytest.fixture(autouse=True)
def _estate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGE_FENCE_POLICY", str(FENCE_POLICY))
    monkeypatch.setenv("EDGE_NOTIFY_PUBLISHERS", str(PUBLISHERS))
