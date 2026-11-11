"""Typed configuration: every knob has a class, a home, a source and a fingerprint (configuration.md,
M6 CF1). Extra `[service]` (pydantic-settings).

    class RiskConfig(Domain):
        domain = "risk"
        daily_loss_limit_r: float = knob(3.0, cls=KnobClass.TUNABLE, env="RISK_DAILY_LOSS_LIMIT_R",
            unit="R", doc="Daily realised loss at which the governor halts new entries.", ge=0)

    domains = load_all([RiskConfig, ...], overrides=store, on_boot=publish)   # composition root, once

THE CLASSES: SECRET (masked everywhere, typed SecretStr), TOPOLOGY (hosts, ports, paths), TUNABLE
(changes behaviour or results; its home is the CODE DEFAULT), POLICY (structured declarations; may
come from the domain's committed TOML `file`), OPERATIONAL (what the operator moves while the system
runs -- the ONLY class a runtime override may set), SEALED (pinned docs and their shas).

PRECEDENCE, one order everywhere: code default < committed file (POLICY knobs only) < env < runtime
override (OPERATIONAL knobs only) < constructor kwargs. `effective(d)` reports each value's `Source`.
`fingerprint(d)` hashes the TUNABLE/POLICY/OPERATIONAL/SEALED values only, so moving a host or
rotating a secret never forks a grade.

LIBRARY CHECKED: pydantic-settings 2.16 is the loader (env + TOML sources, validation, JSON Schema);
this module only adds the class taxonomy, provenance (a wrapper that records which source supplied
each field), the runtime layer and the renderers. Hydra/OmegaConf (a second type system and an
interpolation language), dynaconf (untyped at the edge) and OpenFeature (per-user targeting; its
`reason` idea is kept as `Source`) were not chosen.

TELEMETRY: a bad value FAILS BOOT listing every error (domain, knob, env, source) -- loud by
construction. What fails silently is a runtime override that does not apply: one for a non-OPERATIONAL
knob, an invalid value, or an unreadable store. Each is IGNORED and reported via `on_degraded`
(`config_override_refused|invalid|unreadable:...`) plus a WARNING, and an unreadable store never
claims RUNTIME (its knobs report DEFAULT or FAILSAFE). `load_all` logs `config_loaded` per domain
(fingerprint + every value with its source, secrets masked); the consumer exposes `effective()`
(e.g. `/ops/config`) and its watchdog flags any TUNABLE whose source is ENV.

CLI (`python -m edge_platform.config lint|schema|catalog module:ATTR [--check PATH | --write PATH]`):
exit 0 clean, 1 findings or drift, 2 could not load the domains or run.
"""

from .knobs import FINGERPRINTED, MASK, ConfigError, Domain, KnobClass, Source, knob
from .render import (
    SECRET_LOOKING,
    Effective,
    Overrides,
    catalog,
    effective,
    exemptions,
    fingerprint,
    lint,
    load_all,
    redact_userinfo,
    schema,
)

__all__ = [
    "FINGERPRINTED",
    "MASK",
    "SECRET_LOOKING",
    "ConfigError",
    "Domain",
    "Effective",
    "KnobClass",
    "Overrides",
    "Source",
    "catalog",
    "effective",
    "exemptions",
    "fingerprint",
    "knob",
    "lint",
    "load_all",
    "redact_userinfo",
    "schema",
]
