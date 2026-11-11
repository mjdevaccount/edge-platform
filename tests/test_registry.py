"""The one duplicate guard behind every declare-and-check registry (ported from trade-data tests/platform/test_registry.py)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from edge_platform.registry import Registry


@dataclass(frozen=True)
class _Spec:
    id: str
    note: str = ""


def test_strict_refuses_any_second_declaration_of_an_id() -> None:
    reg: Registry[_Spec] = Registry("thing")
    a = _Spec("a")
    assert reg.add(a) is a
    with pytest.raises(ValueError, match="thing 'a' already registered"):
        reg.add(_Spec("a"))  # equal, still refused: strict means strict
    assert reg["a"] is a


def test_identical_ok_returns_the_held_spec_and_refuses_a_different_one() -> None:
    reg: Registry[_Spec] = Registry("thing", identical_ok=True)
    a = _Spec("a")
    assert reg.add(a) is a
    assert reg.add(_Spec("a")) is a  # re-import: the FIRST object is what is held
    with pytest.raises(ValueError, match="thing 'a' registered twice with a different spec"):
        reg.add(_Spec("a", note="other"))
    assert reg["a"] is a


def test_verb_and_key_are_parameters_of_the_message() -> None:
    reg: Registry[_Spec] = Registry("kind", key=lambda s: s.note, verb="declared")
    reg.add(_Spec("x", note="k"))
    with pytest.raises(ValueError, match="kind 'k' already declared"):
        reg.add(_Spec("y", note="k"))


def test_iteration_is_insertion_order_and_the_mapping_surface_works() -> None:
    reg: Registry[_Spec] = Registry("thing")
    for i in ("c", "a", "b"):
        reg.add(_Spec(i))
    assert list(reg) == ["c", "a", "b"]
    assert set(reg) == {"a", "b", "c"} and len(reg) == 3
    assert reg.get("zz") is None
    with pytest.raises(KeyError):
        reg["zz"]
    assert reg.pop("a", None) == _Spec("a") and "a" not in reg
    fresh = reg.empty()
    assert len(fresh) == 0 and len(reg) == 2
    fresh.add(_Spec("c"))  # own table, own guard
    with pytest.raises(ValueError, match="already registered"):
        fresh.add(_Spec("c"))


def test_a_raw_item_write_is_guarded_too() -> None:
    """`reg[k] = spec`, `update` and `setdefault` all pass through the guard: a production writer
    cannot silently replace a spec by skipping add() (#2454 review note 1). `|=` is not on the
    ABC at all (TypeError, #2456 review note 1). Teardown's two shapes -- plant a NEW id, refill an
    emptied table -- still pass; pop-then-set is the documented raw hole."""
    reg: Registry[_Spec] = Registry("thing")
    reg["a"] = _Spec("a")  # new id: fine
    with pytest.raises(ValueError, match="thing 'a' already registered"):
        reg["a"] = _Spec("a", note="replacement")
    with pytest.raises(ValueError, match="thing 'a' already registered"):
        reg.update({"a": _Spec("a")})
    assert reg.setdefault("a", _Spec("a", note="x")) == _Spec("a")  # held: no write, no raise
    assert reg.setdefault("b", _Spec("b")) == _Spec("b")  # new id: guarded write
    with pytest.raises(TypeError):
        reg |= {"c": _Spec("c")}  # not a MutableMapping operator
    with pytest.raises(ValueError, match="not the spec's own id"):
        reg["zz"] = _Spec("a")  # a key that lies about the spec
    saved = dict(reg)
    reg.clear()
    reg.update(saved)  # the teardown refill shape
    assert dict(reg) == saved
