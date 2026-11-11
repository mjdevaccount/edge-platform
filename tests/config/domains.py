"""Test domains (the CLI imports these by `module:ATTR`). Env names are T_-prefixed test names."""

from __future__ import annotations

from pydantic import SecretStr

from edge_platform.config import Domain, KnobClass, knob

CANARY = "canary-6f1c2e-never-render-me"


class Risk(Domain):
    domain = "t_risk"
    loss_limit_r: float = knob(3.0, cls=KnobClass.TUNABLE, env="T_RISK_LOSS_LIMIT_R", unit="R",
                               doc="Daily loss at which new entries halt.", affects=("t.decisions",), ge=0)  # fmt: skip
    halted: bool = knob(False, cls=KnobClass.OPERATIONAL, env="T_RISK_HALTED", doc="Operator pause switch.",
                        on_unreadable=True)  # fmt: skip
    max_cards: int = knob(5, cls=KnobClass.OPERATIONAL, env="T_RISK_MAX_CARDS", doc="Cards per day.", ge=0, le=20)
    db_host: str = knob("localhost", cls=KnobClass.TOPOLOGY, env="T_RISK_HOST", doc="Database host.")
    api_secret: SecretStr = knob(SecretStr(CANARY), cls=KnobClass.SECRET, env="T_RISK_API_SECRET",
                                 doc="Vendor credential.")  # fmt: skip
    budget_capacity: int = knob(100, cls=KnobClass.POLICY, env="T_RISK_BUDGET_CAPACITY",
                                doc="Requests per window the consumer allows itself.")  # fmt: skip


DOMAINS = [Risk]


class _Undeclared(Domain):
    domain = "t_undeclared"
    loose: int = 3  # no knob(): no class, no doc, no env name


class _Twin(Domain):
    domain = "t_twin"
    again: float = knob(1.0, cls=KnobClass.TUNABLE, env="T_RISK_LOSS_LIMIT_R", doc="Declared twice.")


class _Leaky(Domain):
    domain = "t_leaky"
    token: str = knob("x", cls=KnobClass.TUNABLE, env="T_VENDOR_TOKEN", doc="Looks like a credential.")


BAD = [Risk, _Undeclared, _Twin, _Leaky]


class _Exempt(Domain):
    """Matches SECRET_LOOKING by name, is not a credential, and says why (the declared exemption)."""

    domain = "t_exempt"
    sort_key: str = knob("ts", cls=KnobClass.TOPOLOGY, env="T_SORT_KEY", doc="Sort column.",
                                  not_secret="names the column results sort by, never a credential")  # fmt: skip


EXEMPT = [_Exempt]
