"""`edge-notify`: exit 0 clean / 1 findings / 2 BLIND, for every subcommand a consumer runs."""

from __future__ import annotations

import pytest

from edge_platform.notify import cli
from edge_platform.notify import policy as pol

from .test_alertmanager import GOOD


def _run(capsys, *argv: str) -> tuple[int, str]:
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out + out.err


def test_shell_and_rows_and_path(capsys):
    rc, out = _run(capsys, "shell")
    assert rc == 0 and "_ENP_RESERVE='75'" in out
    rc, out = _run(capsys, "rows")
    assert rc == 0 and out.startswith("decide\t")
    rc, out = _run(capsys, "path", "publishers")
    assert rc == 0 and out.strip().endswith("ntfy-publishers.json")


def test_check_pin_and_check_copy_both_ways(capsys, tmp_path):
    assert _run(capsys, "check-pin")[0] == 0
    copy = tmp_path / "ntfy-policy.json"
    copy.write_bytes(pol.POLICY_PATH.read_bytes().replace(b"\n", b"\r\n"))
    assert _run(capsys, "check-copy", str(copy))[0] == 0  # a CRLF checkout is the same policy
    copy.write_bytes(pol.POLICY_PATH.read_bytes().replace(b'"per_utc_day": 40', b'"per_utc_day": 41'))
    rc, out = _run(capsys, "check-copy", str(copy))
    assert rc == 1 and "!= released policy" in out


def test_am_night_both_ways(capsys, tmp_path):
    am = tmp_path / "alertmanager.yml"
    am.write_text(GOOD, encoding="utf-8")
    assert _run(capsys, "am-night", str(am))[0] == 0
    am.write_text(GOOD.replace('end_time: "07:00"', 'end_time: "08:00"'), encoding="utf-8")
    rc, out = _run(capsys, "am-night", str(am))
    assert rc == 1 and "07:00-08:00" in out


@pytest.mark.parametrize(
    "argv",
    [("am-night", "{missing}"), ("check-copy", "{missing}"), ("am-night", "{garbage}"), ("shell", "{garbage}")],
)
def test_unreadable_or_invalid_input_is_blind_exit_2(capsys, tmp_path, argv):
    garbage = tmp_path / "g"
    garbage.write_text("route: [unclosed\n{", encoding="utf-8")
    args = [a.format(missing=tmp_path / "nope", garbage=garbage) for a in argv]
    rc, out = _run(capsys, *args)
    assert rc == 2 and "BLIND" in out


def test_senders_lists_the_registry_and_names_the_uncounted(capsys):
    rc, out = _run(capsys, "senders")
    assert rc == 0 and "2 uncounted: grafana-contact-points, trade-infra-deploy-bluegreen" in out


def test_a_usage_error_is_blind_and_help_is_clean(capsys):
    assert _run(capsys, "no-such-command")[0] == 2  # argparse's own exit, never read as 1 = findings
    assert _run(capsys, "am-night")[0] == 2  # missing required argument
    assert _run(capsys, "--help")[0] == 0
