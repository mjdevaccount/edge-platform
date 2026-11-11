"""edge_platform.service.flags: the old reads' semantics on every legal value, a boot failure on every
value the old read could only misread, exact-case names, and a read at construction time."""

from __future__ import annotations

import sys

import pytest
from pydantic import Field, ValidationError

from edge_platform.service.flags import Family, Flag01, Truthy


class _Demo(Family):
    apply: Flag01 = Field(False, alias="DEMO_APPLY")
    loud: Truthy = Field(False, alias="DEMO_LOUD")
    edge: float = Field(0.02, alias="DEMO_EDGE")
    syms: str = Field("SPY", alias="DEMO_SYMS")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("DEMO_APPLY", "DEMO_LOUD", "DEMO_EDGE", "DEMO_SYMS", "demo_apply"):
        monkeypatch.delenv(k, raising=False)


def test_defaults_when_unset() -> None:
    d = _Demo()
    assert (d.apply, d.loud, d.edge, d.syms) == (False, False, 0.02, "SPY")


@pytest.mark.parametrize(("raw", "want"), [("1", True), ("0", False), ("", False)])
def test_flag01_is_the_old_eq_one(monkeypatch: pytest.MonkeyPatch, raw: str, want: bool) -> None:
    monkeypatch.setenv("DEMO_APPLY", raw)
    assert _Demo().apply is want


@pytest.mark.parametrize("raw", ["true", "yes", "on", "2", "TRUE"])
def test_flag01_refuses_what_eq_one_silently_read_as_off(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("DEMO_APPLY", raw)
    with pytest.raises(ValidationError, match="silently meant OFF"):
        _Demo()


@pytest.mark.parametrize(
    ("raw", "want"),
    [("1", True), ("true", True), ("YES", True), ("", False), ("0", False), ("False", False), ("no", False)],
)
def test_truthy_keeps_its_words(monkeypatch: pytest.MonkeyPatch, raw: str, want: bool) -> None:
    monkeypatch.setenv("DEMO_LOUD", raw)
    assert _Demo().loud is want


def test_truthy_refuses_an_unknown_word(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_LOUD", "enabled")
    with pytest.raises(ValidationError):
        _Demo()


def test_numbers_parse_strictly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_EDGE", "0.05")
    assert _Demo().edge == 0.05
    monkeypatch.setenv("DEMO_EDGE", "five")
    with pytest.raises(ValidationError):
        _Demo()


def test_names_are_exact_case_and_reads_are_per_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "win32":  # Windows' environment is case-insensitive; the box's is not
        monkeypatch.setenv("demo_apply", "1")
        assert _Demo().apply is False
    monkeypatch.setenv("DEMO_APPLY", "1")
    assert _Demo().apply is True  # no cache: the next construction sees the change
    assert _Demo(apply=False).apply is False  # populate_by_name: a test can pin a value
