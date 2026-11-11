"""The shipped post_admin_run template, run as Windmill runs it (its own module, `wmill` stubbed,
`httpx.post` canned). Ported from trade-data tests/test_post_admin_run_fail_when.py, plus the PL3
contract: service_url and key_variable are REQUIRED, and a missing one sends NOTHING."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from importlib import resources

import pytest

TEMPLATE = resources.files("edge_platform.windmill").joinpath("templates", "post_admin_run.py")
URL, KEYVAR = "http://svc:8090", "u/admin/svc_api_key"


class _Resp:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self) -> dict:
        return self._body


@pytest.fixture
def step(monkeypatch: pytest.MonkeyPatch):
    variables: dict[str, object] = {KEYVAR: "k"}

    def get_variable(path: str) -> str:
        v = variables.get(path)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise LookupError(path)
        return v  # type: ignore[return-value]

    wmill = types.ModuleType("wmill")
    wmill.get_variable = get_variable  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wmill", wmill)
    spec = importlib.util.spec_from_file_location("_post_admin_run_under_test", str(TEMPLATE))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    sent: dict = {}

    def answer(status: int, body: dict) -> None:
        def post(url, **kw):
            sent.clear()
            sent.update(url=url, **kw)
            return _Resp(status, body)

        monkeypatch.setattr(m.httpx, "post", post)

    m.answer, m.sent, m.variables = answer, sent, variables
    m.answer(200, {"ok": True})
    return m


def _run(step, kind: str = "probe", **kw):
    return step.main(URL, KEYVAR, kind, **kw)


# --- the PL3 contract: no default service, no default key ---------------------------------------


@pytest.mark.parametrize("missing", ["service_url", "key_variable", "kind"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_a_missing_required_input_raises_and_sends_nothing(step, missing: str, value) -> None:
    args = {"service_url": URL, "key_variable": KEYVAR, "kind": "probe", missing: value}
    with pytest.raises(ValueError, match=f"{missing!r} is required"):
        step.main(**args)
    assert step.sent == {}


def test_the_named_service_and_key_are_the_ones_used(step) -> None:
    assert _run(step, "auto_tag", params={"symbol": "SPY", "blank": "", "none": None}) == {"ok": True}
    assert step.sent["url"] == "http://svc:8090/admin/run/auto_tag"
    assert step.sent["headers"] == {"X-API-Key": "k"}
    assert step.sent["params"] == {"symbol": "SPY"}
    assert step.sent["json"] is None and step.sent["timeout"] == 60.0


def test_nulls_for_unlisted_inputs_take_the_defaults(step) -> None:
    step.main(URL + "/", KEYVAR, "probe", None, None, None, None, None, None, None)
    assert step.sent["url"] == "http://svc:8090/admin/run/probe"  # never '/None/admin/run'
    assert step.sent["headers"] == {"X-API-Key": "k"} and step.sent["timeout"] == 60.0


def test_send_key_false_presents_no_key(step) -> None:
    _run(step, send_key=False)
    assert step.sent["headers"] is None


def test_an_empty_key_is_fail_open_by_default_and_closed_on_require_key(step, capsys) -> None:
    step.variables[KEYVAR] = RuntimeError("windmill api down")
    _run(step)
    assert step.sent["headers"] is None and "RuntimeError" in capsys.readouterr().out
    step.sent.clear()
    with pytest.raises(RuntimeError, match="raised RuntimeError"):
        _run(step, require_key=True)
    assert step.sent == {}
    step.variables[KEYVAR] = ""
    with pytest.raises(RuntimeError, match="empty and require_key"):
        _run(step, require_key=True)


# --- ported: fail_when and the body on failure ---------------------------------------------------


def test_a_jammed_200_fails_the_step_and_carries_the_body(step) -> None:
    step.answer(200, {"graded": 0, "worklist_jammed": True})
    with pytest.raises(RuntimeError, match=r"HTTP 200 body matched fail_when .*worklist_jammed"):
        _run(step, "grade_bounce_shadow", params={"symbol": "SPY", "limit": 10}, fail_when={"worklist_jammed": True})
    assert step.sent["url"].endswith("/admin/run/grade_bounce_shadow")
    assert step.sent["params"] == {"symbol": "SPY", "limit": 10}


@pytest.mark.parametrize(
    "body",
    [
        {"graded": 3, "worklist_jammed": False},  # healthy
        {"graded": 3},  # key absent: nothing claimed
        {"graded": 3, "worklist_jammed": 1},  # True == 1 in Python; not a jam
    ],
)
def test_a_body_that_does_not_say_it_failed_returns(step, body: dict) -> None:
    step.answer(200, body)
    assert _run(step, fail_when={"worklist_jammed": True}) == body


def test_ok_false_is_matched_on_type_so_a_zero_is_not_a_false(step) -> None:
    step.answer(200, {"ok": 0})
    assert _run(step, fail_when={"ok": False}) == {"ok": 0}
    step.answer(200, {"ok": False, "error": "grader raised"})
    with pytest.raises(RuntimeError, match="fail_when"):
        _run(step, fail_when={"ok": False})


@pytest.mark.parametrize("fail_when", [None, {}])
def test_no_fail_when_keeps_every_2xx_a_success(step, fail_when) -> None:
    step.answer(200, {"worklist_jammed": True})
    assert _run(step, fail_when=fail_when) == {"worklist_jammed": True}


def test_a_4xx_still_raises_before_any_body_check_and_keeps_the_body(step) -> None:
    step.answer(503, {"status": "degraded_planned"})
    with pytest.raises(RuntimeError, match=r"^HTTP 503: .*degraded_planned"):
        _run(step, fail_when={"status": "degraded_planned"})
