"""Knob declaration and the layered `Domain` (configuration.md CF1). See the package docstring."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextvars import ContextVar
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field, PrivateAttr, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, TomlConfigSettingsSource

_UNSET: Any = object()
MASK = "***"
_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]*$")


class KnobClass(StrEnum):
    SECRET = "secret"
    TOPOLOGY = "topology"
    TUNABLE = "tunable"
    POLICY = "policy"
    OPERATIONAL = "operational"
    SEALED = "sealed"


#: the classes whose value enters `fingerprint` (what a grade MEANS); secret + topology never do
FINGERPRINTED = frozenset({KnobClass.TUNABLE, KnobClass.POLICY, KnobClass.OPERATIONAL, KnobClass.SEALED})


class Source(StrEnum):
    DEFAULT = "default"
    FILE = "file"
    ENV = "env"
    RUNTIME = "runtime"
    INIT = "init"  # constructor kwargs (tests, scripts) -- outranks every other layer
    FAILSAFE = "failsafe"  # override store unreadable: the knob's declared `on_unreadable` value


class ConfigError(ValueError):
    """Boot-time configuration failure; `errors` lists every problem (domain, knob, env, source)."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("configuration invalid:\n  " + "\n  ".join(errors))
        self.errors = errors


def knob(
    default: Any = ...,
    *,
    cls: KnobClass,
    doc: str,
    env: str,
    unit: str | None = None,
    affects: tuple[str, ...] = (),
    ge: float | None = None,
    le: float | None = None,
    on_unreadable: Any = _UNSET,
) -> Any:
    """A pydantic Field whose env name is `env` and whose metadata carries the knob's class. `doc` is
    one sentence a stranger understands; `affects` names the graded tables or lanes it shapes.
    `on_unreadable` (OPERATIONAL only): the value used when the override store cannot be read -- how a
    knob that gates cost fails CLOSED."""
    if not isinstance(cls, KnobClass):
        raise TypeError(f"knob {env!r}: cls must be a KnobClass")
    if not isinstance(env, str) or not _ENV.match(env):
        raise ValueError(f"knob env {env!r} must be UPPER_SNAKE")
    if not isinstance(doc, str) or not doc.strip():
        raise ValueError(f"knob {env}: doc is required (one sentence)")
    if on_unreadable is not _UNSET and cls is not KnobClass.OPERATIONAL:
        raise ValueError(f"knob {env}: on_unreadable is for OPERATIONAL knobs only")
    meta: dict[str, Any] = {"cls": cls.value, "doc": doc.strip(), "env": env, "unit": unit, "affects": list(affects)}
    if on_unreadable is not _UNSET:
        meta["on_unreadable"] = on_unreadable
    return Field(default, validation_alias=env, ge=ge, le=le, description=doc.strip(), json_schema_extra={"knob": meta})


def meta(info: FieldInfo) -> dict[str, Any] | None:
    """The knob metadata of a field, or None when it was not declared with `knob()`."""
    extra = info.json_schema_extra
    k = extra.get("knob") if isinstance(extra, dict) else None
    return k if isinstance(k, dict) else None


# -- provenance: which layer supplied each field, recorded while pydantic-settings builds the model --
_PROV: ContextVar[dict[str, Source] | None] = ContextVar("edge_platform_config_prov", default=None)
_RUNTIME: ContextVar[Mapping[str, Any] | None] = ContextVar("edge_platform_config_runtime", default=None)


class _Tracked(PydanticBaseSettingsSource):
    """Wraps a pydantic-settings source and records the fields it supplied (first = highest wins)."""

    def __init__(self, settings_cls: type[BaseSettings], source: Source, inner: Any) -> None:
        super().__init__(settings_cls)
        self.source, self.inner = source, inner

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:  # pragma: no cover
        return None, field_name, False  # unused: __call__ is the whole contract

    def __call__(self) -> dict[str, Any]:
        got = dict(self.inner()) if self.inner is not None else {}
        prov = _PROV.get()
        if prov is not None:
            by_key = _keys(self.settings_cls)
            for k in got:
                name = by_key.get(k, k)
                prov.setdefault(name, self.source)
        return got


class _Runtime(PydanticBaseSettingsSource):
    """The runtime-override layer: the values `load_all` already filtered to OPERATIONAL knobs."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:  # pragma: no cover
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        rt = _RUNTIME.get() or {}
        return {self.settings_cls.model_fields[n].validation_alias or n: v for n, v in rt.items()}


def _keys(cls: type[BaseSettings]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, info in cls.model_fields.items():
        out[name] = name
        if isinstance(info.validation_alias, str):
            out[info.validation_alias] = name
    return out


class Domain(BaseSettings):
    """One config domain (risk, sizing, marketdata, ...). Frozen; unknown keys in a committed file
    fail; env names are case-sensitive and exactly the knob's `env` (no prefix, no nested delimiter).
    Precedence: init kwargs > runtime override (OPERATIONAL only) > env > committed file (POLICY only)
    > code default. Construct through `load_all` at the composition root; `Domain()` alone reads
    env + file + defaults (what a script or a test does)."""

    model_config = SettingsConfigDict(
        case_sensitive=True, frozen=True, extra="forbid", populate_by_name=True, env_prefix="", env_nested_delimiter=None,
    )  # fmt: skip

    domain: ClassVar[str]
    file: ClassVar[str | None] = None

    _sources: dict[str, Source] = PrivateAttr(default_factory=dict)

    def __init__(self, **kw: Any) -> None:
        prov: dict[str, Source] = {}
        token = _PROV.set(prov)
        try:
            super().__init__(**kw)
        except ValidationError as exc:
            raise ConfigError(_explain(type(self), exc, prov)) from exc
        finally:
            _PROV.reset(token)
        for name in type(self).model_fields:
            self._sources[name] = prov.get(name, Source.DEFAULT)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        layers: list[PydanticBaseSettingsSource] = [
            _Tracked(settings_cls, Source.INIT, init_settings),
            _Tracked(settings_cls, Source.RUNTIME, _Runtime(settings_cls)),
            _Tracked(settings_cls, Source.ENV, env_settings),
        ]
        file = getattr(settings_cls, "file", None)
        if file:
            layers.append(_Tracked(settings_cls, Source.FILE, _PolicyFile(settings_cls, file)))
        return tuple(layers)

    def source_of(self, name: str) -> Source:
        return self._sources[name]


class _PolicyFile(PydanticBaseSettingsSource):
    """The committed TOML: keys are FIELD names, and only POLICY knobs may be set from it."""

    def __init__(self, settings_cls: type[BaseSettings], path: str) -> None:
        super().__init__(settings_cls)
        self.path, self.inner = path, TomlConfigSettingsSource(settings_cls, toml_file=path)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:  # pragma: no cover
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        if not Path(self.path).is_file():  # a declared file that is absent must not read as "all defaults"
            raise ConfigError([f"{getattr(self.settings_cls, 'domain', '?')}: committed file {self.path} not found"])
        raw = self.inner()  # pydantic-settings already keys these by the field's alias
        fields, by_key = self.settings_cls.model_fields, _keys(self.settings_cls)
        out: dict[str, Any] = {}
        for k, v in raw.items():
            info = fields.get(by_key.get(k, k))
            m = meta(info) if info is not None else None
            if m is None or m["cls"] != KnobClass.POLICY:
                why = "unknown key" if info is None else f"a {m['cls'] if m else 'undeclared'} knob, not POLICY"
                raise ConfigError([f"{self.path}: {k!r} -- {why} (only POLICY knobs live in the committed file)"])
            out[info.validation_alias or k] = v  # type: ignore[union-attr]
        return out


def _explain(cls: type[Domain], exc: ValidationError, prov: dict[str, Source]) -> list[str]:
    by_key = _keys(cls)
    lines = []
    for e in exc.errors():
        loc = str(e["loc"][0]) if e["loc"] else "?"
        name = by_key.get(loc, loc)
        info = cls.model_fields.get(name)
        env = (meta(info) or {}).get("env", loc) if info is not None else loc
        src = prov.get(name, Source.DEFAULT)
        lines.append(f"{getattr(cls, 'domain', cls.__name__)}.{name} ({env}, from {src}): {e['msg']}")
    return lines


def runtime_values(rt: Mapping[str, Any] | None) -> Any:
    """Context manager: the runtime layer seen by a Domain built inside it (used by `load_all`)."""
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        token = _RUNTIME.set(rt)
        try:
            yield
        finally:
            _RUNTIME.reset(token)

    return _cm()


def is_snake(name: str) -> bool:
    return bool(_SNAKE.match(name))
