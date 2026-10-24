"""Alertmanager's night mute vs policy.night -- both directions, minute-exact."""

from __future__ import annotations

import pytest
import yaml

from edge_platform.notify import policy as pol
from edge_platform.notify.alertmanager import PROBES, am_night_findings, landing_routes

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
        ("mute_time_intervals: [quiet-night]", "mute_time_intervals: []", "want exactly ['quiet-night']"),
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


# The reviewer-401 shapes: each one PASSED the old "some route lists quiet-night" check.
TI = GOOD[GOOD.index("time_intervals:") :]
WEEKEND = """  - name: weekend
    time_intervals:
      - weekdays: ['saturday', 'sunday']
"""
P2_NEG = """['tier!~"p1|p3|none"']"""

MIS_MUTED = {
    "the muted route matches nothing, so p2 falls to the unmuted root": (
        """
  routes:
    - matchers: ['tier="never"']
      receiver: p2
      mute_time_intervals: [quiet-night]
""",
        "a p2 alert lands on receiver 'p2', which mutes with []",
    ),
    "the mute is on the p1 route only": (
        """
  routes:
    - matchers: ['tier="p1"']
      receiver: p1
      mute_time_intervals: [quiet-night]
""",
        "p1 must always wake",
    ),
    "an extra interval in the same list mutes p2 all weekend": (
        f"""
  routes:
    - matchers: {P2_NEG}
      receiver: p2
      mute_time_intervals: [quiet-night, weekend]
""",
        "want exactly ['quiet-night']",
    ),
    "an unmuted sibling catches p2 first": (
        f"""
  routes:
    - matchers: ['tier=~"p2|"']
      receiver: p2
    - matchers: {P2_NEG}
      receiver: p2
      mute_time_intervals: [quiet-night]
""",
        "which mutes with []",
    ),
    "an unmuted sibling keyed on ANOTHER label catches a p2 that carries it": (
        f"""
  routes:
    - matchers: ['severity="warning"']
      receiver: p2
    - matchers: {P2_NEG}
      receiver: p2
      mute_time_intervals: [quiet-night]
""",
        "with severity='warning' lands on receiver 'p2', which mutes with []",
    ),
    "a bare string, not a list": (
        f"""
  routes:
    - matchers: {P2_NEG}
      receiver: p2
      mute_time_intervals: quiet-night-and-more
""",
        "not a list",
    ),
    "the p2 route is only active inside another window": (
        f"""
  routes:
    - matchers: {P2_NEG}
      receiver: p2
      mute_time_intervals: [quiet-night]
      active_time_intervals: [weekend]
""",
        "only active in",
    ),
    "the mute is on the root": (
        """
  mute_time_intervals: [quiet-night]
""",
        "root route",
    ),
}


@pytest.mark.parametrize("why", list(MIS_MUTED))
def test_a_mis_muted_route_tree_fires(why):
    routes, says = MIS_MUTED[why]
    found = am_night_findings("route:\n  receiver: p2" + routes + TI + WEEKEND, POLICY)
    assert any(says in f for f in found), found


def test_an_unlabelled_alert_is_judged_as_p2():
    # tier="p2" (not the negation) leaves an UNLABELLED alert on the unmuted root: a 03:00 page.
    found = am_night_findings(GOOD.replace(P2_NEG, """['tier="p2"']"""), POLICY)
    assert found == ["a unlabelled alert lands on receiver 'p2', which mutes with [], want exactly ['quiet-night']"]


def test_continue_routes_are_all_judged_and_legacy_match_is_read():
    audit = """    - match_re: {tier: '.*'}
      receiver: audit
      continue: true
"""
    yml = GOOD.replace("  routes:\n", "  routes:\n" + audit, 1)
    found = am_night_findings(yml, POLICY)
    assert any("receiver 'audit', which mutes with []" in f for f in found), found
    root = yaml.safe_load(yml)["route"]
    assert [r["receiver"] for r in landing_routes(root, PROBES["p1"])] == ["audit", "p1"]
    assert [r["receiver"] for r in landing_routes(root, PROBES["unlabelled"])] == ["audit", "p2"]


def test_an_unparseable_matcher_or_bare_time_is_blind():
    with pytest.raises(ValueError, match="matcher"):
        am_night_findings(GOOD.replace("""['tier="p1"']""", "['tier ~ p1']"), POLICY)
    with pytest.raises(ValueError, match="quote it"):
        am_night_findings(GOOD.replace('"22:00"', "22:00"), POLICY)


def test_not_an_alertmanager_file_is_blind_not_clean():
    with pytest.raises(ValueError):
        am_night_findings("just: a map\n", POLICY)
    with pytest.raises(ValueError):
        am_night_findings(GOOD.replace('"07:00"', '"7 am"', 1), POLICY)
