"""edge_platform.config (configuration.md CF1, acceptance 1-5 and 8; 6, 7, 9 are trade-data CF2/CF3
and trade-infra CF5)."""

from __future__ import annotations

import json
import logging

import pytest

from edge_platform.config import (
    ConfigError,
    Domain,
    KnobClass,
    Source,
    catalog,
    effective,
    exemptions,
    fingerprint,
    knob,
    lint,
    load_all,
    schema,
)
from edge_platform.config.__main__ import main as cli

from .domains import BAD, CANARY, DOMAINS, EXEMPT, Risk

ENVS = [
    "T_RISK_LOSS_LIMIT_R",
    "T_RISK_HALTED",
    "T_RISK_MAX_CARDS",
    "T_RISK_HOST",
    "T_RISK_API_SECRET",
    "T_RISK_BUDGET_CAPACITY",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for e in ENVS:
        monkeypatch.delenv(e, raising=False)


class _Store:
    def __init__(self, rows=None, boom: bool = False) -> None:
        self.rows, self.boom = rows or {}, boom

    def current(self, domain: str):
        if self.boom:
            raise ConnectionError("override store down")
        return self.rows.get(domain, {})


def _src(d: Domain) -> dict[str, Source]:
    return {e.name: e.source for e in effective(d)}


def _with_file(tmp_path, body: str) -> type[Domain]:
    p = tmp_path / "risk.toml"
    p.write_text(body, encoding="utf-8")

    class Filed(Risk):
        file = str(p)

    return Filed


# --- acceptance 1: precedence, and each value says where it came from -----------------------------


def test_precedence_default_file_env_runtime(tmp_path, monkeypatch) -> None:
    d = load_all(DOMAINS)["t_risk"]
    assert d.max_cards == 5 and d.budget_capacity == 100 and set(_src(d).values()) == {Source.DEFAULT}

    filed = _with_file(tmp_path, "budget_capacity = 80\n")
    assert (filed().budget_capacity, filed().source_of("budget_capacity")) == (80, Source.FILE)
    monkeypatch.setenv("T_RISK_BUDGET_CAPACITY", "90")
    assert (filed().budget_capacity, filed().source_of("budget_capacity")) == (90, Source.ENV)  # env beats file

    monkeypatch.setenv("T_RISK_MAX_CARDS", "7")
    assert (Risk().max_cards, Risk().source_of("max_cards")) == (7, Source.ENV)
    d = load_all(DOMAINS, overrides=_Store({"t_risk": {"max_cards": 2}}))["t_risk"]
    assert (d.max_cards, d.source_of("max_cards")) == (2, Source.RUNTIME)  # runtime beats env
    assert (Risk(max_cards=9).max_cards, Risk(max_cards=9).source_of("max_cards")) == (9, Source.INIT)


def test_the_committed_file_holds_policy_knobs_only(tmp_path) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        _with_file(tmp_path, "nope = 1\n")()
    with pytest.raises(ConfigError, match="a tunable knob, not POLICY"):
        _with_file(tmp_path, "loss_limit_r = 9.0\n")()

    class Missing(Risk):
        file = str(tmp_path / "never_committed.toml")

    with pytest.raises(ConfigError, match="not found"):  # never "all defaults" in silence
        Missing()


def test_every_invalid_value_is_listed_with_its_source(monkeypatch) -> None:
    monkeypatch.setenv("T_RISK_LOSS_LIMIT_R", "-1")
    monkeypatch.setenv("T_RISK_MAX_CARDS", "lots")
    with pytest.raises(ConfigError) as ei:
        load_all(DOMAINS)
    text = "\n".join(ei.value.errors)
    assert len(ei.value.errors) == 2
    assert (
        "t_risk.loss_limit_r (T_RISK_LOSS_LIMIT_R, from env)" in text and "t_risk.max_cards (T_RISK_MAX_CARDS" in text
    )


def test_a_domain_is_frozen() -> None:
    with pytest.raises(Exception, match="frozen"):
        Risk().max_cards = 3  # type: ignore[misc]


# --- acceptance 2: runtime scope must fire both ways -----------------------------------------------


def test_a_runtime_override_applies_to_operational_knobs_only(caplog) -> None:
    seen: list[str] = []
    rows = {"t_risk": {"loss_limit_r": 0.5, "halted": True}}
    with caplog.at_level(logging.WARNING, logger="edge_platform.config"):
        d = load_all(DOMAINS, overrides=_Store(rows), on_degraded=seen.append)["t_risk"]
    assert d.loss_limit_r == 3.0 and d.source_of("loss_limit_r") is Source.DEFAULT  # TUNABLE: ignored
    assert d.halted is True and d.source_of("halted") is Source.RUNTIME  # OPERATIONAL: applied
    assert seen == ["config_override_refused:t_risk.loss_limit_r (tunable)"]
    assert "config_override_refused" in caplog.text


def test_an_invalid_override_is_ignored_and_reported_not_a_boot_failure() -> None:
    seen: list[str] = []
    d = load_all(DOMAINS, overrides=_Store({"t_risk": {"max_cards": 99, "ghost": 1}}), on_degraded=seen.append)
    assert d["t_risk"].max_cards == 5 and d["t_risk"].source_of("max_cards") is Source.DEFAULT
    assert seen == [
        "config_override_refused:t_risk.ghost (unknown knob)",
        "config_override_invalid:t_risk.max_cards ValidationError",
    ]


def test_an_unreadable_store_never_claims_runtime_and_fails_closed_where_declared() -> None:
    seen: list[str] = []
    d = load_all(DOMAINS, overrides=_Store(boom=True), on_degraded=seen.append)["t_risk"]
    assert seen == ["config_overrides_unreadable:t_risk ConnectionError"]
    assert d.halted is True and d.source_of("halted") is Source.FAILSAFE  # on_unreadable=True: halt
    assert d.source_of("max_cards") is Source.DEFAULT and Source.RUNTIME not in _src(d).values()


def test_on_unreadable_is_for_operational_knobs_only() -> None:
    with pytest.raises(ValueError, match="OPERATIONAL knobs only"):
        knob(1, cls=KnobClass.TUNABLE, env="T_X", doc="x", on_unreadable=0)
    with pytest.raises(ValueError, match="doc is required"):
        knob(1, cls=KnobClass.TUNABLE, env="T_X", doc=" ")
    with pytest.raises(ValueError, match="UPPER_SNAKE"):
        knob(1, cls=KnobClass.TUNABLE, env="t_x", doc="x")


# --- acceptance 3: fingerprint moves with tunables, never with topology or secrets ----------------


def test_fingerprint_follows_what_a_grade_means(monkeypatch) -> None:
    base = fingerprint(Risk())
    monkeypatch.setenv("T_RISK_HOST", "db.elsewhere")
    monkeypatch.setenv("T_RISK_API_SECRET", "rotated")
    assert fingerprint(Risk()) == base
    monkeypatch.setenv("T_RISK_LOSS_LIMIT_R", "2.5")
    assert fingerprint(Risk()) != base
    monkeypatch.delenv("T_RISK_LOSS_LIMIT_R")
    monkeypatch.setenv("T_RISK_MAX_CARDS", "6")  # OPERATIONAL is in the fingerprint too
    assert fingerprint(Risk()) != base


# --- acceptance 4: a secret never renders ---------------------------------------------------------


def test_a_secret_canary_never_appears_in_any_output(caplog) -> None:
    boot: list = []
    with caplog.at_level(logging.INFO, logger="edge_platform.config"):
        d = load_all(DOMAINS, on_boot=boot.append)["t_risk"]
    assert d.api_secret.get_secret_value() == CANARY  # the value is there for the code that needs it
    rendered = [
        repr(effective(d)), repr(boot), caplog.text, json.dumps(schema(DOMAINS), sort_keys=True), catalog(DOMAINS),
    ]  # fmt: skip
    for out in rendered:
        assert CANARY not in out
    assert "config_loaded domain=t_risk" in caplog.text and "***" in caplog.text


# --- acceptance 5: the declaration lint -----------------------------------------------------------


def test_lint_must_fire_both_ways() -> None:
    assert lint(DOMAINS) == []
    found = lint(BAD)
    assert any("t_undeclared.loose: not declared with knob()" in f for f in found)
    assert any("env T_RISK_LOSS_LIMIT_R declared twice" in f for f in found)
    assert any("T_VENDOR_TOKEN looks like a credential but is classed tunable" in f for f in found)
    with pytest.raises(ConfigError):
        load_all(BAD)  # the composition root refuses an undeclared tree


def test_cli_exit_codes(tmp_path, capsys) -> None:
    assert cli(["lint", "tests.config.domains:DOMAINS"]) == 0
    assert cli(["lint", "tests.config.domains:BAD"]) == 1
    assert cli(["lint", "tests.config.no_such_module:DOMAINS"]) == 2  # did not finish looking
    assert cli(["lint", "missing-colon"]) == 2
    capsys.readouterr()


# --- acceptance 8: schema / catalog drift ---------------------------------------------------------


@pytest.mark.parametrize("what", ["schema", "catalog"])
def test_a_rendered_file_that_drifted_fails_the_check(tmp_path, what: str) -> None:
    out = tmp_path / f"{what}.out"
    assert cli([what, "tests.config.domains:DOMAINS", "--write", str(out)]) == 0
    assert cli([what, "tests.config.domains:DOMAINS", "--check", str(out)]) == 0
    out.write_text(out.read_text(encoding="utf-8").replace("t_risk", "t_rizk", 1), encoding="utf-8")
    assert cli([what, "tests.config.domains:DOMAINS", "--check", str(out)]) == 1
    assert cli([what, "tests.config.domains:DOMAINS", "--check", str(tmp_path / "absent")]) == 2


def test_the_catalog_lists_every_knob_with_its_class() -> None:
    text = catalog(DOMAINS)
    for env in ENVS:
        assert f"`{env}`" in text
    assert (
        "| t_risk | `T_RISK_LOSS_LIMIT_R` | tunable | `3.0` | R | Daily loss at which new entries halt. | t.decisions |"
        in text
    )


# --- review of PR #18: the names the first pattern missed, and credentials INSIDE a value ----------

MISSED = [
    "LITELLM_MASTER_KEY",
    "LANGFUSE_ENCRYPTION_KEY",
    "CADDY_BASIC_AUTH_PLAINTEXT",
    "LANGFUSE_REDIS_AUTH",
    "BACKUP_S3_ACCESS_KEY_ID",
    "DATABASE_URL",
]


def _one(env: str, cls: KnobClass, default: str = "x", **kw) -> type[Domain]:
    class _D(Domain):
        domain = "t_one"
        value: str = knob(default, cls=cls, env=env, doc="One knob.", **kw)

    return _D


@pytest.mark.parametrize("env", MISSED)
def test_every_credential_name_the_estate_uses_must_be_classed_secret(env: str) -> None:
    found = lint([_one(env, KnobClass.TOPOLOGY)])
    assert any(f"{env} looks like a credential" in f for f in found), found


@pytest.mark.parametrize("env", ["T_RISK_HOST", "T_MONKEY_COUNT", "T_KEYBOARD_LAYOUT", "T_UPSTREAM_URL"])
def test_an_ordinary_name_is_not_called_a_credential(env: str) -> None:
    assert lint([_one(env, KnobClass.TOPOLOGY)]) == []


def test_url_credentials_never_render_whatever_the_knob_is_called(caplog, monkeypatch) -> None:
    canary = "pw-0b7e-never-render-me"
    monkeypatch.setenv("T_UPSTREAM_URL", f"postgresql://trade:{canary}@db:5432/trade")
    dom = _one("T_UPSTREAM_URL", KnobClass.TOPOLOGY)
    boot: list = []
    with caplog.at_level(logging.INFO, logger="edge_platform.config"):
        d = load_all([dom], on_boot=boot.append)["t_one"]
    assert canary in d.value  # the code that connects still gets the real value
    for out in (caplog.text, repr(boot), repr(effective(d))):
        assert canary not in out
    assert "postgresql://***@db:5432/trade" in caplog.text  # host and db stay readable


def test_a_url_credential_in_a_default_is_a_lint_finding_both_ways() -> None:
    leaky = _one("T_UPSTREAM_URL", KnobClass.TOPOLOGY, default="https://user:pw@vendor.example/api")
    assert any("carries URL credentials" in f for f in lint([leaky]))
    assert lint([_one("T_UPSTREAM_URL", KnobClass.TOPOLOGY, default="https://vendor.example/api")]) == []


# --- review round 2: real trade-data names must not false-fire; the declared exemption -------------

REAL_NOT_SECRET = ["TRADE_DATA_AUTH_MODE", "TRADE_DATA_AUTH_TARGET_ANON_PERMISSIONS", "LANGFUSE_PUBLIC_KEY"]


@pytest.mark.parametrize("env", REAL_NOT_SECRET)
def test_a_real_non_credential_name_lints_clean_and_boots(env: str) -> None:
    dom = _one(env, KnobClass.TOPOLOGY)
    assert lint([dom]) == []
    assert load_all([dom])["t_one"].value == "x"  # round 1's pattern refused this boot


def test_not_secret_silences_the_name_finding_and_is_audited(capsys) -> None:
    assert any("looks like a credential" in f for f in lint([_one("T_VENDOR_TOKEN_KIND", KnobClass.TOPOLOGY)]))
    assert lint(EXEMPT) == []
    assert exemptions(EXEMPT) == [
        "t_exempt.vendor_token_kind: T_VENDOR_TOKEN_KIND -- names the scheme (bearer/basic), never carries the token"
    ]
    assert "(not secret: names the scheme" in catalog(EXEMPT)
    assert cli(["lint", "tests.config.domains:EXEMPT"]) == 0
    assert "exempt (not_secret): t_exempt.vendor_token_kind" in capsys.readouterr().out


def test_an_exemption_needs_a_reason_a_non_secret_class_and_a_name_that_needs_it() -> None:
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="not_secret needs a reason"):
            _one("T_VENDOR_TOKEN_KIND", KnobClass.TOPOLOGY, not_secret=bad)
    with pytest.raises(ValueError, match="pick one"):
        knob("x", cls=KnobClass.SECRET, env="T_VENDOR_TOKEN", doc="d.", not_secret="why")
    stale = _one("T_RISK_HOST", KnobClass.TOPOLOGY, not_secret="never looked like one")
    assert any("does not look like a credential -- drop it" in f for f in lint([stale]))


def test_url_userinfo_is_redacted_even_on_an_exempt_knob(caplog, monkeypatch) -> None:
    monkeypatch.setenv("T_VENDOR_TOKEN_KIND", "https://u:pw-canary-77@vendor.example/x")
    with caplog.at_level(logging.INFO, logger="edge_platform.config"):
        load_all(EXEMPT)
    assert "pw-canary-77" not in caplog.text and "https://***@vendor.example/x" in caplog.text
