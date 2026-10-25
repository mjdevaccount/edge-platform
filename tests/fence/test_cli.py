"""`edge-fence` exit codes, the JSON record, the textfile counters, and the bundled table."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from edge_platform.fence import cli
from edge_platform.fence.sessions import Sessions, bundled_doc

from .test_sessions import FX


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    d = json.loads(FX.read_text(encoding="utf-8"))
    d["generated_at"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = tmp_path / "sessions.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    return p


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    rc = cli.main(list(argv))
    return rc, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_exit_codes_and_record(capsys: pytest.CaptureFixture[str], cache: Path, tmp_path: Path) -> None:
    tf = tmp_path / "tf" / "edge_fence.prom"
    common = ["--cache", str(cache), "--textfile", str(tf)]
    rc, rec = _run(capsys, *common, "check", "--tier", "service", "--at", "2025-11-26T10:00")
    assert rc == 1 and rec["allowed"] is False and rec["source"] == "cache" and len(rec["decision_id"]) == 32
    rc, rec = _run(capsys, *common, "check", "--tier", "service", "--at", "2025-11-29T12:00", "--need", "2h")
    assert rc == 0 and rec["until"] == "2025-12-01T14:15:00Z" and rec["need_min"] == 120
    rc, rec = _run(capsys, *common, "check", "--tier", "nope", "--at", "2025-11-29T12:00")
    assert rc == 2 and rec["allowed"] is False and rec["source"] == "none"
    rc, _ = _run(capsys, *common, "check", "--tier", "service", "--at", "not a time")
    assert rc == 2
    prom = tf.read_text(encoding="utf-8")
    assert 'edge_fence_decisions_total{tier="service",result="refuse",source="cache"} 1' in prom
    assert 'edge_fence_decisions_total{tier="service",result="allow",source="cache"} 1' in prom
    assert 'edge_fence_decisions_total{tier="service",result="blind",source="none"} 1' in prom
    assert 'edge_fence_last_decision_timestamp_seconds{tier="service"}' in prom


def test_epoch_and_offset_instants(capsys: pytest.CaptureFixture[str], cache: Path) -> None:
    sat = int(dt.datetime(2025, 11, 29, 17, tzinfo=dt.UTC).timestamp())
    assert _run(capsys, "--cache", str(cache), "check", "--tier", "infra", "--at", f"@{sat}")[0] == 0
    assert _run(capsys, "--cache", str(cache), "check", "--tier", "infra", "--at", "2025-11-26T15:00:00Z")[0] == 1


def test_status(capsys: pytest.CaptureFixture[str], cache: Path, tmp_path: Path) -> None:
    rc, rec = _run(capsys, "--cache", str(cache), "status")
    assert rc == 0 and rec["source"] == "cache" and rec["horizon"] == "2027-03-31"
    rc, rec = _run(capsys, "--cache", str(tmp_path / "none.json"), "status")
    assert rc == 0 and rec["source"] == "bundled"


def test_bundled_horizon_has_room() -> None:
    """The release's own table: when this fails, run scripts/gen_bundled_sessions.py and release."""
    s = Sessions(bundled_doc(), max_age_h=None)
    left = (s.horizon - dt.datetime.now(dt.UTC).date()).days
    assert left >= 180, f"bundled sessions end {s.horizon}: {left} days left, regenerate"


def test_bundled_agrees_with_lib_clock_fixture() -> None:
    """Two exchange_calendars runs, months apart, must agree wherever both have a date."""
    a = Sessions(bundled_doc(), max_age_h=None)
    b = Sessions(json.loads(FX.read_text(encoding="utf-8")), max_age_h=None)
    lo, hi = max(a.first, b.first), min(a.horizon, b.horizon)
    pick = lambda s: [r for r in s.rows if lo <= r[0] <= hi]  # noqa: E731
    assert pick(a) == pick(b) and len(pick(a)) > 100


def test_unwritable_textfile_never_changes_the_verdict(
    capsys: pytest.CaptureFixture[str], cache: Path, tmp_path: Path
) -> None:
    """--textfile under a FILE (not a dir): same JSON + rc as without it, a WARNING on stderr (reviewer 409 item 1)."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    argv = ["check", "--tier", "service", "--at", "2025-11-26T10:00"]
    rc0 = cli.main(["--cache", str(cache), *argv])
    ref = json.loads(capsys.readouterr().out.strip())
    rc = cli.main(["--cache", str(cache), "--textfile", str(blocker / "edge_fence.prom"), *argv])
    cap = capsys.readouterr()
    rec = json.loads(cap.out.strip())
    assert rc == rc0 == 1
    assert {k: v for k, v in rec.items() if k != "decision_id"} == {k: v for k, v in ref.items() if k != "decision_id"}
    assert "WARNING -- counter not written" in cap.err and "the verdict stands" in cap.err


def test_no_tzdata_is_exit_2_with_a_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], cache: Path
) -> None:
    """sessions._ZONE None (no tzdata on the host) -> rc 2 and one JSON record, never a traceback (reviewer 409 item 2)."""
    from edge_platform.fence import sessions as sess

    monkeypatch.setattr(sess, "_ZONE", None)
    rc, rec = _run(capsys, "--cache", str(cache), "check", "--tier", "service", "--at", "2025-11-29T17:00:00Z")
    assert rc == 2 and rec["allowed"] is False and rec["source"] == "none" and "tzdata" in rec["reason"]
    rc, rec = _run(capsys, "--cache", str(cache), "status")
    assert rc == 2 and rec["tz_ok"] is False


def test_textfile_samples_are_exact(tmp_path: Path) -> None:
    from edge_platform.fence.cli import bump_textfile

    tf = tmp_path / "edge_fence.prom"

    bump_textfile(tf, "service", "allow", "cache", 1792895200.25)
    prom = tf.read_text(encoding="utf-8")
    assert 'edge_fence_last_decision_timestamp_seconds{tier="service"} 1792895200.25' in prom
    assert 'edge_fence_decisions_total{tier="service",result="allow",source="cache"} 1' in prom
    tf.write_text('edge_fence_decisions_total{tier="service",result="allow",source="cache"} 1234567' + chr(10))
    bump_textfile(tf, "service", "allow", "cache", 1792895201.0)
    prom = tf.read_text(encoding="utf-8")
    assert 'result="allow",source="cache"} 1234568' in prom
    assert 'edge_fence_last_decision_timestamp_seconds{tier="service"} 1792895201' in prom
