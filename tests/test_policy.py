"""The packaged policy through THE reader: shared cases, the pin, must-fire, the name guards."""

from __future__ import annotations

import copy
import datetime as dt
import json

import pytest

from edge_platform.notify import policy as pol

RAW = json.loads(pol.POLICY_PATH.read_text(encoding="utf-8"))
CASES = json.loads(pol.CASES_PATH.read_text(encoding="utf-8"))
POLICY = pol.load()
ALL = [(fn, c) for fn in pol.CASE_KEYS for c in CASES.get(fn, [])]

# The bytes every consumer already carries (edge-host, trade-data, mattpc-llm-stack, 2026-10-24).
# v0.1.0 ships them UNCHANGED so no existing copy has to move; changing them is a new release.
RELEASED_SHA = "186af62618e5511d52f12c8402da1e28f954eb18843a3b938bdbe142876254f9"


@pytest.mark.parametrize(("fn", "case"), ALL, ids=[f"{fn}-{n}" for n, (fn, _c) in enumerate(ALL)])
def test_the_shared_cases_hold(fn, case):
    assert pol.run_case(POLICY, fn, case) == case["out"], case["why"]


def test_every_required_section_has_cases():
    for fn in CASES["required"]:
        assert CASES.get(fn), f"cases file has no {fn} cases"


def test_the_policy_is_pinned_by_its_cases_and_is_the_bytes_consumers_carry():
    assert pol.check_pin() is None
    assert pol.sha256_lf(pol.POLICY_PATH) == RELEASED_SHA


def test_the_pin_fires_on_an_edit_and_ignores_crlf(tmp_path):
    p, c = tmp_path / "p.json", tmp_path / "c.json"
    c.write_bytes(pol.CASES_PATH.read_bytes())
    p.write_bytes(pol.POLICY_PATH.read_bytes().replace(b'"reserve": 75', b'"reserve": 80'))
    assert pol.check_pin(p, c) is not None
    p.write_bytes(pol.POLICY_PATH.read_bytes().replace(b"\n", b"\r\n"))
    assert pol.check_pin(p, c) is None


@pytest.mark.parametrize(
    ("where", "key", "value"),
    [
        (("quota",), "reserve", 80),
        (("publisher_cap",), "per_utc_day", 41),
        (("layout",), "ledger_dir", "x"),
        (("night",), "start_hour", 23),
        (("night",), "end_hour", 6),
        (("quota", "reserve_exempt"), "am-heartbeat-watch", None),
        (("publisher_cap", "exempt"), "trade-data-market", None),
    ],
)
def test_must_fire_one_moved_number_fails_the_shared_cases(where, key, value):
    edited = copy.deepcopy(RAW)
    node = edited
    for part in where:
        node = node[part]
    if value is None:
        del node[key]
    else:
        node[key] = value
    assert pol.case_failures(pol.parse(edited), CASES), f"moving {'.'.join(where)}.{key} broke no case"


def test_the_committed_policy_breaks_no_case():
    assert pol.case_failures(POLICY, CASES) == []


@pytest.mark.parametrize(
    ("where", "key", "value"),
    [
        ("layout", "ledger_dir", "../../../home/matt"),
        ("layout", "ledger_dir", "a/b"),
        ("layout", "deferred_dir", ".."),
        ("layout", "undelivered_dir", "../x"),
        ("layout", "ledger_dir", ""),
        ("layout", "digest_file", "lines"),
        ("layout", "digest_file", "a/../b"),
        ("night", "tz", "../etc"),
        ("quota", "reserve", -1),
        ("quota", "daily_messages", "250"),
        ("night", "start_hour", 25),
        ("night", "start_hour", 5),  # 05..07 does not wrap midnight: every reader would misread it
    ],
)
def test_an_unsafe_name_or_bad_number_is_refused(where, key, value):
    edited = copy.deepcopy(RAW)
    edited[where][key] = value
    with pytest.raises(ValueError):
        pol.parse(edited)


def test_an_unsafe_exempt_publisher_is_refused():
    edited = copy.deepcopy(RAW)
    edited["quota"]["reserve_exempt"]["../x"] = "r"
    with pytest.raises(ValueError, match="unsafe"):
        pol.parse(edited)


def test_night_hours_are_the_policy_window():
    assert POLICY.night_hours() == frozenset([22, 23, 0, 1, 2, 3, 4, 5, 6])


def test_shell_lines_are_quoted_and_carry_every_tier_flag():
    lines = pol.shell_lines(POLICY)
    assert "_ENP_RESERVE='75'" in lines and "_ENP_CAP='40'" in lines
    assert "_ENP_UNDELIVERED_DIR='undelivered'" in lines
    assert sum(ln.startswith("_ENP_P") for ln in lines) == 3 * 5
    assert all(ln.count("'") == 2 for ln in lines)


def test_case_rows_flatten_null_and_bools():
    rows = pol.case_rows(CASES)
    assert len(rows) == len(ALL)
    blind = next(r for r in rows if "BLIND" in r)
    assert "\t\t" in blind  # remaining=null -> empty field


# --- the llm box reader's refusals, folded into parse() (2026-11-26) ---


@pytest.mark.parametrize(
    "value",
    ["v1/account", "/v1/../account", "//evil.example/v1", "/v1/account?x=1", "", 7, None],
)
def test_a_bad_account_path_is_refused(value):
    edited = copy.deepcopy(RAW)
    edited["quota"]["account_path"] = value
    with pytest.raises(ValueError, match="account_path"):
        pol.parse(edited)


def test_a_missing_account_path_is_refused():
    edited = copy.deepcopy(RAW)
    del edited["quota"]["account_path"]
    with pytest.raises(KeyError):
        pol.parse(edited)


def test_the_account_path_is_read():
    assert POLICY.account_path == "/v1/account"


@pytest.mark.parametrize(
    ("where", "key", "value"),
    [
        ("quota", "reserve", 251),
        ("publisher_cap", "per_utc_day", 251),
        ("publisher_cap", "per_utc_day", 0),
        ("layout", "publisher_max_len", 256),
    ],
)
def test_a_count_past_one_days_quota_is_refused(where, key, value):
    edited = copy.deepcopy(RAW)
    edited[where][key] = value
    with pytest.raises(ValueError):
        pol.parse(edited)


def test_an_exempt_publisher_with_dotdot_is_refused():
    edited = copy.deepcopy(RAW)
    edited["publisher_cap"]["exempt"]["a..b"] = "r"
    with pytest.raises(ValueError, match="unsafe"):
        pol.parse(edited)


def test_strict_night_refuses_24_and_the_default_keeps_it():
    edited = copy.deepcopy(RAW)
    edited["night"]["start_hour"], edited["night"]["end_hour"] = 24, 0
    assert pol.parse(edited).night_hours() == frozenset()
    with pytest.raises(ValueError, match="start_hour"):
        pol.parse(edited, strict_night=True)
    assert pol.parse(RAW, strict_night=True) == POLICY


# --- sent_today: blind is not zero ---

UTC_NOON = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)


def test_sent_today_counts_non_blank_lines_per_publisher(tmp_path):
    day = tmp_path / "sent" / "20261126"
    day.mkdir(parents=True)
    (day / "health-probe").write_text("12:00:00 4\n12:05:00 3\n\n", encoding="ascii")
    (day / "crashloop-watch").write_text("01:00:00 5\n", encoding="ascii")
    (tmp_path / "sent" / "20261125").mkdir()
    (tmp_path / "sent" / "20261125" / "health-probe").write_text("x\n", encoding="ascii")
    assert POLICY.sent_today(tmp_path, UTC_NOON) == {"health-probe": 2, "crashloop-watch": 1}


def test_sent_today_reads_the_utc_day_of_a_local_time(tmp_path):
    (tmp_path / "sent" / "20261126").mkdir(parents=True)
    (tmp_path / "sent" / "20261126" / "p").write_text("a\n", encoding="ascii")
    local_evening = dt.datetime(2026, 11, 25, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    assert POLICY.sent_today(tmp_path, local_evening) == {"p": 1}


def test_sent_today_missing_day_is_empty_not_none(tmp_path):
    assert POLICY.sent_today(tmp_path, UTC_NOON) == {}


def test_sent_today_unreadable_is_none_not_zero(tmp_path):
    # the day "dir" is a FILE: iterdir raises NotADirectoryError (an OSError) on every platform
    (tmp_path / "sent").mkdir()
    (tmp_path / "sent" / "20261126").write_text("not a dir", encoding="ascii")
    assert POLICY.sent_today(tmp_path, UTC_NOON) is None
