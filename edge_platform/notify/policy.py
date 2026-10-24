"""THE reader of ntfy-policy.json: validation, the one decision, the ledger path, the sha256 pin.

This consolidates the two readers that existed before the package (2026-10-24): trade-data
service/platform/ntfy_policy.py (parse / NtfyPolicy / decide) and edge-host scripts/ntfy_policy.py
(validation, --shell, --rows, --check-pin). Their validation was not the same -- edge-host checked
`undelivered_dir` and `quota.daily_messages`, trade-data did not -- so this is the UNION.

STDLIB ONLY. edge-host's bash evaluates `shell_lines()` at every lib-notify source on the box's
python3, so this module may import nothing that is not in the standard library.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

TAB = chr(9)

# A name that becomes a PATH SEGMENT is one segment, so a policy edit cannot aim the ledger
# outside the state dir (`"ledger_dir": "../../../home/matt"`, edge-host reviewer ledger 388).
_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_PUBLISHER = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
_DIGEST = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+")
_CHARCLASS = re.compile(r"[A-Za-z0-9._/ _-]+")
_TZ = re.compile(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*")
TIERS = ("p1", "p2", "p3")
_FLAGS = ("pushes", "capped", "reserve", "night_gate")

# decide() answers: the same words every library uses.
PUSH, DIGEST, HOLD_CAP, DEFER_NIGHT, HOLD_RESERVE = "push", "digest", "hold_cap", "defer_night", "hold_reserve"


def packaged(name: str) -> Path:
    """Path of a data file shipped in this package (ntfy-policy.json, its cases, the registry)."""
    return Path(str(resources.files(__package__).joinpath(name)))


POLICY_PATH = packaged("ntfy-policy.json")
CASES_PATH = packaged("ntfy-policy.cases.json")


@dataclass(frozen=True)
class TierRule:
    pushes: bool
    capped: bool
    reserve: bool
    night_gate: bool
    cooldown_s: int


@dataclass(frozen=True)
class NtfyPolicy:
    daily_messages: int
    reserve: int
    cap: int
    cap_exempt: frozenset[str]
    reserve_exempt: frozenset[str]
    night_tz: str
    night_start: int
    night_end: int
    tiers: dict[str, TierRule]
    ledger_dir: str
    deferred_dir: str
    undelivered_dir: str
    digest_file: str
    safe_chars: str
    max_len: int

    def in_night(self, local_hour: int) -> bool:
        """[start, end) in the policy's zone, wrapping midnight -- the SAME expression lib-notify.sh
        and lib-notify.ps1 use. They all assume the window wraps, so parse() refuses one that does
        not rather than this reader answering differently from the others."""
        return local_hour >= self.night_start or local_hour < self.night_end

    def night_hours(self) -> frozenset[int]:
        return frozenset(h for h in range(24) if self.in_night(h))

    def decide(
        self,
        tier: str,
        publisher: str,
        sent_today: int,
        remaining: int | None,
        et_hour: int,
        can_defer: bool,
        has_sink: bool,
    ) -> str:
        """The one decision: cap -> night defer -> reserve -> push.

        remaining None = the quota probe was BLIND, and blind fails open. A hold needs somewhere to
        land (has_sink); with nowhere it pushes -- a lost page is worse than a noisy one.
        """
        rule = self.tiers.get(tier, self.tiers["p2"])
        if not rule.pushes:
            return DIGEST if has_sink else PUSH
        if rule.capped and publisher not in self.cap_exempt and sent_today >= self.cap and has_sink:
            return HOLD_CAP
        if rule.night_gate and can_defer and self.in_night(et_hour):
            return DEFER_NIGHT
        if (
            rule.reserve
            and publisher not in self.reserve_exempt
            and remaining is not None
            and remaining <= self.reserve
            and has_sink
        ):
            return HOLD_RESERVE
        return PUSH

    def wants_probe(self, tier: str, publisher: str, has_sink: bool) -> bool:
        """Whether the quota probe could change decide()'s answer for this push."""
        rule = self.tiers.get(tier, self.tiers["p2"])
        return rule.reserve and has_sink and publisher not in self.reserve_exempt

    def replay_budget(self, remaining: int | None, cap: int) -> int:
        """What a spool replay may spend: what lies above the reserve, at most `cap`."""
        if remaining is None:
            return cap
        return max(0, min(cap, remaining - self.reserve))

    def safe_publisher(self, publisher: str) -> str:
        return re.sub(f"[^{self.safe_chars}]", "_", publisher)[: self.max_len]

    def ledger_rel(self, publisher: str, utc: dt.datetime) -> str:
        """<ledger_dir>/<UTC YYYYMMDD>/<safe publisher>: the quota's own day is UTC."""
        day = utc.astimezone(dt.UTC).strftime("%Y%m%d")
        return f"{self.ledger_dir}/{day}/{self.safe_publisher(publisher)}"


def _name(value: object, pat: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pat.fullmatch(value):
        raise ValueError(f"{what}: unsafe or empty name {value!r}")
    return value


def _int(value: object, what: str, lo: int = 0, hi: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < lo or (hi is not None and value > hi):
        raise ValueError(f"{what} is not an int in range")
    return value


def parse(raw: dict) -> NtfyPolicy:
    """The policy, validated. Raises ValueError (or KeyError) naming what is wrong."""
    if raw.get("schema") != 1:
        raise ValueError(f"schema {raw.get('schema')!r}, this reader knows 1")
    q, cap, night, lay = raw["quota"], raw["publisher_cap"], raw["night"], raw["layout"]
    tiers = {}
    for t in TIERS:
        r = raw["tiers"][t]
        if not all(isinstance(r[f], bool) for f in _FLAGS):
            raise ValueError(f"tiers.{t}: a flag is not a bool")
        tiers[t] = TierRule(*(r[f] for f in _FLAGS), _int(r["cooldown_s"], f"tiers.{t}.cooldown_s"))
    start, end = _int(night["start_hour"], "night.start_hour", 0, 24), _int(night["end_hour"], "night.end_hour", 0, 24)
    if start <= end:
        raise ValueError(f"night window {start}..{end} does not wrap midnight; every reader assumes it does")
    return NtfyPolicy(
        daily_messages=_int(q["daily_messages"], "quota.daily_messages"),
        reserve=_int(q["reserve"], "quota.reserve"),
        cap=_int(cap["per_utc_day"], "publisher_cap.per_utc_day"),
        cap_exempt=frozenset(_name(p, _PUBLISHER, "publisher") for p in cap["exempt"]),
        reserve_exempt=frozenset(_name(p, _PUBLISHER, "publisher") for p in q["reserve_exempt"]),
        night_tz=_name(night["tz"], _TZ, "night.tz"),
        night_start=start,
        night_end=end,
        tiers=tiers,
        ledger_dir=_name(lay["ledger_dir"], _SEGMENT, "layout.ledger_dir"),
        deferred_dir=_name(lay["deferred_dir"], _SEGMENT, "layout.deferred_dir"),
        undelivered_dir=_name(lay["undelivered_dir"], _SEGMENT, "layout.undelivered_dir"),
        digest_file=_name(lay["digest_file"], _DIGEST, "layout.digest_file (<dir>/<file>)"),
        safe_chars=_name(lay["publisher_safe_chars"], _CHARCLASS, "layout.publisher_safe_chars"),
        max_len=_int(lay["publisher_max_len"], "layout.publisher_max_len", 1),
    )


def load(path: Path = POLICY_PATH) -> NtfyPolicy:
    return parse(json.loads(path.read_text(encoding="utf-8")))


def sha256_lf(path: Path) -> str:
    """sha256 of the file with CRLF folded to LF: a Windows checkout must hash like Linux."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def check_pin(policy: Path = POLICY_PATH, cases: Path = CASES_PATH) -> str | None:
    """None when the cases file pins this policy's bytes; else the finding."""
    want = json.loads(cases.read_text(encoding="utf-8")).get("policy_sha256")
    got = sha256_lf(policy)
    if want != got:
        return (
            f"{policy.name} sha256 {got} but {cases.name} pins {want}: the policy was edited "
            "without its cases (or this copy drifted from the released one)"
        )
    return None


def shell_lines(p: NtfyPolicy) -> list[str]:
    """`_ENP_<NAME>='<value>'` lines for lib-notify.sh to eval. Every value passed parse()."""
    out: dict[str, object] = {
        "RESERVE": p.reserve,
        "CAP": p.cap,
        "CAP_EXEMPT": " " + " ".join(sorted(p.cap_exempt)) + " ",
        "RESERVE_EXEMPT": " " + " ".join(sorted(p.reserve_exempt)) + " ",
        "NIGHT_START": p.night_start,
        "NIGHT_END": p.night_end,
        "NIGHT_TZ": p.night_tz,
        "LEDGER_DIR": p.ledger_dir,
        "SAFE_CHARS": p.safe_chars,
        "MAX_LEN": p.max_len,
        "DEFERRED_DIR": p.deferred_dir,
        "UNDELIVERED_DIR": p.undelivered_dir,
        "DIGEST_FILE": p.digest_file,
    }
    for t, rule in p.tiers.items():
        for flag in _FLAGS:
            out[f"{t.upper()}_{flag.upper()}"] = 1 if getattr(rule, flag) else 0
        out[f"{t.upper()}_COOLDOWN"] = rule.cooldown_s
    return [f"_ENP_{k}='{v}'" for k, v in out.items()]


CASE_KEYS = {
    "decide": ("tier", "publisher", "sent_today", "remaining", "et_hour", "can_defer", "has_sink"),
    "ledger_path": ("publisher", "utc"),
    "replay_budget": ("remaining", "cap"),
}


def case_rows(cases: dict) -> list[str]:
    """The shared cases flattened for bash: `<fn>\\t<why>\\t<in...>\\t<out>`, null -> empty."""

    def cell(v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, bool):
            return "1" if v else "0"
        return str(v)

    out = []
    for fn, keys in CASE_KEYS.items():
        for c in cases.get(fn, []):
            out.append(TAB.join([fn, c["why"], *(cell(c["in"][k]) for k in keys), cell(c["out"])]))
    return out


def run_case(p: NtfyPolicy, fn: str, case: dict) -> object:
    """One shared case through THIS reader -- what every consumer's CI does in its own language."""
    i = case["in"]
    if fn == "decide":
        return p.decide(*(i[k] for k in CASE_KEYS["decide"]))
    if fn == "ledger_path":
        return p.ledger_rel(i["publisher"], dt.datetime.fromisoformat(i["utc"]))
    if fn == "replay_budget":
        return p.replay_budget(i["remaining"], i["cap"])
    raise ValueError(f"unknown case section {fn!r}")


def case_failures(p: NtfyPolicy, cases: dict) -> list[str]:
    return [f"{fn}: {c['why']}" for fn in CASE_KEYS for c in cases.get(fn, []) if run_case(p, fn, c) != c["out"]]
