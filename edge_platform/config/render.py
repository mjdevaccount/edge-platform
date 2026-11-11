"""What a loaded config IS: effective values with their source, the fingerprint, schema, catalog,
the declaration lint, and `load_all` (configuration.md CF1)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from pydantic import SecretBytes, SecretStr, TypeAdapter

from edge_platform.stamp import fingerprint as _fp

from .knobs import FINGERPRINTED, MASK, ConfigError, Domain, KnobClass, Source, is_snake, meta, runtime_values

log = logging.getLogger("edge_platform.config")

#: names that look like credentials; such a knob must be classed SECRET (invariant 10), or declare
#: `knob(not_secret="<why>")`. `KEY` as the final word (LITELLM_MASTER_KEY, LANGFUSE_ENCRYPTION_KEY)
#: except PUBLIC_KEY, `KEY_ID` (BACKUP_S3_ACCESS_KEY_ID), `AUTH` as the final word or a basic-auth /
#: auth-token name (LANGFUSE_REDIS_AUTH, CADDY_BASIC_AUTH_PLAINTEXT) but NOT `AUTH_` as a prefix
#: (TRADE_DATA_AUTH_MODE is a mode switch), and a connection string by name (DATABASE_URL, *_DSN).
SECRET_LOOKING = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|PLAINTEXT|PRIVATE_KEY|API_KEY|KEY_ID"
    r"|(?:^|_)AUTH$|BASIC_AUTH|AUTH_TOKEN|(?<!PUBLIC)_KEY$|^KEY$|DSN|DB_URL|DATABASE_URL)"
)
#: `scheme://<userinfo>@` -- a credential carried INSIDE a value, whatever the knob is called
_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+@")


def redact_userinfo(v: Any) -> Any:
    """Mask the userinfo of every `scheme://user:pass@host` inside a rendered value (str, list, dict),
    regardless of the knob's name or class: `postgresql://u:pw@h/db` -> `postgresql://***@h/db`."""
    if isinstance(v, str):
        return _USERINFO.sub(rf"\1{MASK}@", v)
    if isinstance(v, list):
        return [redact_userinfo(x) for x in v]
    if isinstance(v, dict):
        return {k: redact_userinfo(x) for k, x in v.items()}
    return v


@dataclass(frozen=True, slots=True)
class Effective:
    domain: str
    name: str
    env: str
    cls: KnobClass
    value: Any  # MASK for SECRET, always
    source: Source
    default: Any  # MASK for SECRET, always
    doc: str
    unit: str | None
    affects: tuple[str, ...]


class Overrides(Protocol):
    """The runtime layer's reader: the newest non-expired value per knob NAME for a domain."""

    def current(self, domain: str) -> Mapping[str, Any]: ...


def _jsonable(v: Any) -> Any:
    if isinstance(v, (SecretStr, SecretBytes)):
        return MASK
    return redact_userinfo(TypeAdapter(Any).dump_python(v, mode="json"))


def _default(info: Any) -> Any:
    from pydantic_core import PydanticUndefined

    return None if info.default is PydanticUndefined else info.default


def effective(d: Domain) -> tuple[Effective, ...]:
    out = []
    for name, info in type(d).model_fields.items():
        m = meta(info) or {"cls": KnobClass.TUNABLE, "doc": "", "env": name, "unit": None, "affects": []}
        cls = KnobClass(m["cls"])
        secret = cls is KnobClass.SECRET
        out.append(Effective(
            domain=type(d).domain, name=name, env=m["env"], cls=cls,
            value=MASK if secret else _jsonable(getattr(d, name)), source=d.source_of(name),
            default=MASK if secret else _jsonable(_default(info)), doc=m["doc"], unit=m["unit"],
            affects=tuple(m["affects"]),
        ))  # fmt: skip
    return tuple(out)


def fingerprint(d: Domain) -> str:
    """stamp.fingerprint over {name: value} of the TUNABLE/POLICY/OPERATIONAL/SEALED knobs: moving a
    host or rotating a secret never forks a grade."""
    vals = {e.name: e.value for e in effective(d) if e.cls in FINGERPRINTED}
    return _fp({"domain": type(d).domain, "values": vals})


def schema(domains: Sequence[type[Domain]]) -> dict[str, Any]:
    """JSON Schema per domain (SECRET defaults masked). Render with `json.dumps(..., sort_keys=True)`."""
    out: dict[str, Any] = {}
    for cls in sorted(domains, key=lambda c: c.domain):
        sch = cls.model_json_schema()
        for name, info in cls.model_fields.items():
            m = meta(info)
            prop = sch.get("properties", {}).get(info.validation_alias or name)
            if m and m["cls"] == KnobClass.SECRET and isinstance(prop, dict) and "default" in prop:
                prop["default"] = MASK
        out[cls.domain] = sch
    return out


def catalog(domains: Sequence[type[Domain]]) -> str:
    """The generated knob catalog (markdown). Never a live value -- defaults only, secrets masked."""
    rows = ["| domain | env | class | default | unit | doc | affects |", "|---|---|---|---|---|---|---|"]
    for cls in sorted(domains, key=lambda c: c.domain):
        for name, info in cls.model_fields.items():
            m = meta(info) or {}
            k = m.get("cls", "UNDECLARED")
            dflt = MASK if k == KnobClass.SECRET else json.dumps(_jsonable(_default(info)), sort_keys=True)
            doc = (m.get("doc") or "") + (f" (not secret: {m['not_secret']})" if m.get("not_secret") else "")
            cells = [cls.domain, f"`{m.get('env', name)}`", str(k), f"`{dflt}`", m.get("unit") or "",
                     doc.replace("|", "\\|"), ", ".join(m.get("affects") or [])]  # fmt: skip
            rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"


def lint(domains: Sequence[type[Domain]]) -> list[str]:
    """Declaration findings (empty = clean): undeclared field, missing doc, duplicate env or domain,
    secret-looking name classed otherwise, SECRET not typed SecretStr, bad domain name."""
    findings: list[str] = []
    envs: dict[str, str] = {}
    names: dict[str, str] = {}
    for cls in domains:
        dname = getattr(cls, "domain", None)
        if not isinstance(dname, str) or not is_snake(dname):
            findings.append(f"{cls.__name__}: domain must be a snake_case ClassVar, got {dname!r}")
            dname = cls.__name__
        if dname in names:
            findings.append(f"domain {dname!r} declared by both {names[dname]} and {cls.__name__}")
        names[dname] = cls.__name__
        for name, info in cls.model_fields.items():
            where = f"{dname}.{name}"
            m = meta(info)
            if m is None:
                findings.append(f"{where}: not declared with knob() (cls/doc/env)")
                continue
            env = m["env"]
            if env in envs:
                findings.append(f"env {env} declared twice: {envs[env]} and {where}")
            envs[env] = where
            secret = m["cls"] == KnobClass.SECRET
            looks = bool(SECRET_LOOKING.search(env))
            if looks and not secret and not m.get("not_secret"):
                findings.append(f"{where}: {env} looks like a credential but is classed {m['cls']} "
                                "(class it SECRET, or declare knob(not_secret='<why>'))")  # fmt: skip
            if m.get("not_secret") and not looks:
                findings.append(f"{where}: declares not_secret but {env} does not look like a credential -- drop it")
            dflt = _default(info)
            if not secret and isinstance(dflt, str) and _USERINFO.search(dflt):
                findings.append(f"{where}: its default carries URL credentials (scheme://user:pass@) but is "
                                f"classed {m['cls']}")  # fmt: skip
            typed_secret = "Secret" in repr(info.annotation)
            if secret and not typed_secret:
                findings.append(f"{where}: a SECRET knob must be typed SecretStr")
            if typed_secret and not secret:
                findings.append(f"{where}: typed as a secret but classed {m['cls']}")
    return findings


def exemptions(domains: Sequence[type[Domain]]) -> list[str]:
    """Every declared `not_secret` exemption, `domain.name: ENV -- <reason>` (the audit list)."""
    out = []
    for cls in sorted(domains, key=lambda c: getattr(c, "domain", c.__name__)):
        for name, info in cls.model_fields.items():
            m = meta(info)
            if m and m.get("not_secret"):
                out.append(f"{cls.domain}.{name}: {m['env']} -- {m['not_secret']}")
    return out


def load_all(
    domains: Sequence[type[Domain]],
    *,
    overrides: Overrides | None = None,
    on_boot: Callable[[tuple[Effective, ...]], None] | None = None,
    on_degraded: Callable[[str], None] | None = None,
) -> Mapping[str, Domain]:
    """Build every domain once (the composition root). Every invalid value in every domain is listed
    in ONE `ConfigError`. Runtime overrides apply to OPERATIONAL knobs only; any other override is
    IGNORED and reported (`on_degraded("config_override_refused:<domain>.<knob>")` + a WARNING). An
    unreadable override store reports `config_overrides_unreadable:<domain>` and applies each
    OPERATIONAL knob's `on_unreadable` (source FAILSAFE) -- never claims RUNTIME."""
    bad = lint(domains)
    if bad:
        raise ConfigError(bad)
    built: dict[str, Domain] = {}
    errors: list[str] = []
    for cls in domains:
        rt, failsafe = _runtime_for(cls, overrides, on_degraded)
        try:
            with runtime_values(rt):
                d = cls()
        except ConfigError as exc:
            errors.extend(exc.errors)
            continue
        for name in failsafe:
            if d.source_of(name) is Source.RUNTIME:
                d._sources[name] = Source.FAILSAFE
        built[cls.domain] = d
    if errors:
        raise ConfigError(errors)
    every = tuple(e for d in built.values() for e in effective(d))
    for dname, d in built.items():
        log.info("config_loaded domain=%s fingerprint=%s %s", dname, fingerprint(d),
                 " ".join(f"{e.env}={json.dumps(e.value)}[{e.source}]" for e in effective(d)))  # fmt: skip
    if on_boot is not None:
        on_boot(every)
    return built


def _runtime_for(
    cls: type[Domain], overrides: Overrides | None, on_degraded: Callable[[str], None] | None
) -> tuple[dict[str, Any], set[str]]:
    if overrides is None:
        return {}, set()

    def degraded(reason: str) -> None:
        log.warning(reason)
        if on_degraded is not None:
            try:
                on_degraded(reason)
            except Exception:
                log.warning("config_on_degraded_hook_failed", exc_info=True)

    try:
        raw = dict(overrides.current(cls.domain))
    except Exception as exc:
        degraded(f"config_overrides_unreadable:{cls.domain} {type(exc).__name__}")
        safe = {n: m["on_unreadable"] for n, i in cls.model_fields.items() if (m := meta(i)) and "on_unreadable" in m}
        return safe, set(safe)
    rt: dict[str, Any] = {}
    for name, value in sorted(raw.items()):
        info = cls.model_fields.get(name)
        m = meta(info) if info is not None else None
        if m is None or m["cls"] != KnobClass.OPERATIONAL:
            degraded(f"config_override_refused:{cls.domain}.{name} ({m['cls'] if m else 'unknown knob'})")
            continue
        try:
            ann = Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation  # type: ignore[union-attr]
            TypeAdapter(ann).validate_python(value)
        except Exception as exc:
            degraded(f"config_override_invalid:{cls.domain}.{name} {type(exc).__name__}")
            continue
        rt[name] = value
    return rt, set()
