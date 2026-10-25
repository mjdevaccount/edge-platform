"""The sender registry: validation both ways, and its agreement with the policy's exemptions."""

from __future__ import annotations

import copy
import json

import pytest

from edge_platform.notify import policy as pol
from edge_platform.notify import publishers as pubs

RAW = json.loads(pubs.REGISTRY_PATH.read_text(encoding="utf-8"))


def _sender(raw: dict, name: str) -> dict:
    return next(s for s in raw["senders"] if s["name"] == name)


def test_the_committed_registry_parses_and_covers_every_policy_exemption():
    senders = pubs.load()
    assert pubs.policy_findings(senders, pol.load()) == []
    assert {s.name for s in senders} >= {"edge-host-lib-notify", "trade-data-ntfy-client", "alertmanager"}


def test_an_exemption_with_no_declaring_sender_fires():
    edited = copy.deepcopy(RAW)
    _sender(edited, "trade-data-ntfy-client")["publishers"] = ["trade-data"]
    found = pubs.policy_findings(pubs.parse(edited), pol.load())
    assert found == [
        "policy exempts publisher 'trade-data-market' from the cap, but no sender in the registry declares it"
    ]


def test_today_the_uncounted_senders_are_exactly_these():
    # Each is a known hole with its `owed`. When one is counted this test moves WITH the registry
    # edit -- on purpose: shrinking this list is the work, and growing it must be a reviewed diff.
    assert [s.name for s in pubs.uncounted(pubs.load())] == [
        "grafana-contact-points",
    ]


@pytest.mark.parametrize(
    ("name", "key", "value", "match"),
    [
        ("grafana-contact-points", "owed", None, "owed"),  # a known hole must say what closes it
        ("gatus", "counted_by", None, "counted_by"),  # the gap closed by counting must say where
        ("alertmanager", "counted_by", None, "counted_by"),  # counted must say by what
        ("edge-host-lib-notify", "owed", "something", "owed"),  # a closed gap carries no debt
        ("edge-host-lib-notify", "route", "carrier-pigeon", "route"),
        ("edge-host-lib-notify", "mode", "trusted", "mode"),
        ("edge-host-lib-notify", "why", "", "why"),
        ("edge-host-lib-notify", "where", [], "where"),
        ("edge-host-lib-notify", "publishers", ["../x"], "publishers"),
        ("edge-host-lib-notify", "name", "Edge Host", "name"),
    ],
)
def test_a_bad_entry_is_refused(name, key, value, match):
    edited = copy.deepcopy(RAW)
    entry = _sender(edited, name)
    if value is None:
        del entry[key]
    else:
        entry[key] = value
    with pytest.raises(ValueError, match=match):
        pubs.parse(edited)


def test_a_duplicate_sender_or_publisher_is_refused():
    edited = copy.deepcopy(RAW)
    edited["senders"].append(copy.deepcopy(_sender(edited, "gatus")))
    with pytest.raises(ValueError, match="twice"):
        pubs.parse(edited)
    edited = copy.deepcopy(RAW)
    _sender(edited, "alertmanager")["publishers"] = ["alertmanager", "trade-data"]
    with pytest.raises(ValueError, match="already declared"):
        pubs.parse(edited)
