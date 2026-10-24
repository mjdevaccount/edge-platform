"""Alertmanager's night mute vs policy.night -- both directions, minute-exact."""

from __future__ import annotations

import pytest

from edge_platform.notify import policy as pol
from edge_platform.notify.alertmanager import am_night_findings

POLICY = pol.load()

# The shape trade-infra infra/alertmanager/alertmanager.yml has on origin/main (2026-10-24).
GOOD = """
route:
  receiver: p2
  routes:
    - matchers: ['tier="p1"']
      receiver: p1
    - matchers: ['tier!~"p1|p3|none"']
      receiver: p2
      mute_time_intervals: [quiet-night]
time_intervals:
  - name: quiet-night
    time_intervals:
      - times:
          - start_time: "22:00"
            end_time: "24:00"
          - start_time: "00:00"
            end_time: "07:00"
        location: America/New_York
  - name: digest-window
    time_intervals:
      - times:
          - start_time: "08:00"
            end_time: "08:10"
        location: America/New_York
"""


def test_the_trade_infra_shape_agrees():
    assert am_night_findings(GOOD, POLICY) == []


@pytest.mark.parametrize(
    ("old", "new", "says"),
    [
        ('end_time: "07:00"', 'end_time: "08:00"', "mutes 07:00-08:00"),  # an hour too late
        ('start_time: "22:00"', 'start_time: "23:00"', "leaves 22:00-23:00 loud"),  # an hour too late to start
        ('end_time: "07:00"', 'end_time: "06:59"', "leaves 06:59-07:00 loud"),  # one minute
        ("location: America/New_York\n  - name: digest", "location: UTC\n  - name: digest", "location 'UTC'"),
        ("mute_time_intervals: [quiet-night]", "mute_time_intervals: []", "mutes nothing"),
        (
            '      - times:\n          - start_time: "22:00"',
            "      - weekdays: ['monday:friday']\n        times:\n          - start_time: \"22:00\"",
            "weekdays",
        ),
    ],
)
def test_a_drifted_window_fires(old, new, says):
    assert old in GOOD, f"fixture edit {old!r} no longer applies"
    found = am_night_findings(GOOD.replace(old, new, 1), POLICY)
    assert any(says in f for f in found), found


def test_a_moved_policy_hour_fires_against_the_unchanged_file():
    raw = pol.json.loads(pol.POLICY_PATH.read_text(encoding="utf-8"))
    raw["night"]["end_hour"] = 6
    assert any("mutes 06:00-07:00" in f for f in am_night_findings(GOOD, pol.parse(raw)))


def test_a_missing_or_doubled_interval_fires():
    assert am_night_findings(GOOD.replace("name: quiet-night", "name: other"), POLICY)
    doubled = GOOD + "  - name: quiet-night\n    time_intervals: []\n"
    assert "defined 2 times" in am_night_findings(doubled, POLICY)[0]


def test_not_an_alertmanager_file_is_blind_not_clean():
    with pytest.raises(ValueError):
        am_night_findings("just: a map\n", POLICY)
    with pytest.raises(ValueError):
        am_night_findings(GOOD.replace('"07:00"', '"7 am"', 1), POLICY)
