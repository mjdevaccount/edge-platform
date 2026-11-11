"""The response envelope: ONE freshness block and ONE error body (extra `[service]`).

Lifted from trade-data `service/schemas/envelope.py` (2026-10-23; M6 PL2). Before it, freshness was
spelled six ways across 431 routes -- `stale`, `age_s`, `as_of`, `asof`, `provenance`, `freshness`
-- and the same name meant different things (`as_of` the REQUEST time on some routes, the DATA time
on others). A consumer (a UI, an LLM agent, a generated client) could not ask any route "how old is
this?" without reading that route's source.

THE CONTRACT. An adopted route carries a `fresh` block, always under that one key:

    as_of          when the underlying DATA was observed (never the request time), tz-aware
    age_s          seconds between as_of and now, >= 0; null only when as_of is null
    stale          FAIL-CLOSED: true when age_s > max_age_s OR the age is unknown OR data is absent
    max_age_s      the producer cadence `stale` was judged against (so the verdict is auditable)
    provenance     what produced the data (schema.table or lane name) -- machine-facing, not prose
    absent_reason  non-null iff there is no observation; a null as_of with no reason is REFUSED

Why a NAMESPACED block and not five top-level keys: legacy top-level names often already exist with
conflicting meanings, and "do not break existing response keys" means they stay. A key nobody uses
(`fresh`) cannot collide, so the canonical `fresh.as_of` is ADDED beside a legacy `as_of`.

Two shapes carry the block:
  Fresh[T]      new routes: {"data": T | null, "fresh": {...}}; null data REQUIRES absent_reason
                (unknown is never empty).
  FreshStamped  existing routes, ADDITIVE: the legacy dict passes through as model extras (typed
                Any) and gains `fresh`. Byte parity holds because FastAPI >= 0.13x already INFERS a
                response model from a `-> dict[str, Any]` annotation and serialises it through
                pydantic's JSON mode (Decimal -> string, UTC -> `Z`); Any-typed extras take that
                exact serializer (tests/service/test_envelope.py compares the two on the wire). A
                route WITHOUT that annotation is encoded by jsonable_encoder instead (Decimal ->
                number) and must not adopt FreshStamped without re-checking its bytes.

`ApiError` / `api_error` is the error half; `edge_platform.service.http_errors` installs it.
Zero-I/O: pure pydantic models and functions.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")


def snapshot_age_s(snap_ts: dt.datetime | None, asof: dt.datetime | None) -> float | None:
    """Seconds between the `asof` anchor and the observation it resolved (>= 0).

    None = EMPTY (no observation) -- kept distinct from any numeric age. `asof=None` means live:
    the age is measured against wall-clock now (UTC)."""
    if snap_ts is None:
        return None
    ref = asof if asof is not None else dt.datetime.now(dt.UTC)
    return max(0.0, (ref - snap_ts).total_seconds())


def is_stale_or_unknown(age_s: float | None, cap_s: float) -> bool:
    """FAIL-CLOSED: an age nobody can compute is not a clean bill of health.

    `cap_s` is deliberately REQUIRED: each caller's cap encodes its own producer's cadence, and a
    default here would let a caller inherit a cadence that is not its own."""
    return age_s is None or age_s > cap_s


class Freshness(BaseModel):
    """How old a payload's data is, judged against its producer's cadence. Build it with
    `observe` / `absent`; the validator refuses the two dishonest shapes by construction."""

    # Defaulted fields are always serialised: the OpenAPI output schema says so (see ApiError).
    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_serialization_defaults_required=True)

    as_of: dt.datetime | None = Field(
        description="When the underlying data was observed (UTC, tz-aware); never request time."
    )
    age_s: float | None = Field(ge=0, description="Seconds from as_of to now; null only when as_of is null.")
    stale: bool = Field(description="Fail-closed: age_s > max_age_s, or age unknown, or data absent.")
    max_age_s: float | None = Field(default=None, gt=0, description="The producer cadence `stale` was judged against.")
    provenance: str = Field(min_length=1, description="What produced the data (schema.table or lane).")
    absent_reason: str | None = Field(
        default=None, description="Why there is no observation; non-null iff as_of is null."
    )

    @model_validator(mode="after")
    def _honest(self) -> Freshness:
        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError("as_of must be tz-aware: a naive timestamp's age depends on the server's TZ")
        if self.as_of is None and not self.absent_reason:
            raise ValueError("as_of is null with no absent_reason: an unexplained null reads as fresh-but-empty")
        if self.as_of is not None and self.absent_reason:
            raise ValueError("absent_reason set but as_of is present: absent means NO observation")
        if self.age_s is None and self.as_of is not None:
            raise ValueError("as_of present but age_s null: the age is computable, so compute it")
        if not self.stale and (self.age_s is None or self.max_age_s is None):
            raise ValueError("stale=false needs a known age AND a cadence to judge it by (unknown is never fresh)")
        return self

    @classmethod
    def observe(
        cls,
        as_of: dt.datetime | None,
        *,
        max_age_s: float,
        provenance: str,
        now: dt.datetime | None = None,
        absent_reason: str = "no observation",
    ) -> Freshness:
        """The block for data last observed at `as_of`. `now=None` is live wall clock; pass the
        read's own `now` when the payload already measured one, so both ages agree. A None
        `as_of` is ABSENT with `absent_reason` -- never a zero age."""
        if as_of is None:
            return cls.absent(absent_reason, provenance=provenance, max_age_s=max_age_s)
        age = snapshot_age_s(as_of, now)
        return cls(
            as_of=as_of,
            age_s=None if age is None else round(age, 1),
            stale=is_stale_or_unknown(age, max_age_s),
            max_age_s=max_age_s,
            provenance=provenance,
        )

    @classmethod
    def absent(cls, reason: str, *, provenance: str, max_age_s: float | None = None) -> Freshness:
        return cls(as_of=None, age_s=None, stale=True, max_age_s=max_age_s, provenance=provenance, absent_reason=reason)

    def block(self) -> dict[str, Any]:
        """The JSON-ready dict a usecase puts under the `fresh` key."""
        return self.model_dump(mode="json")


def weakest(*parts: Freshness) -> Freshness:
    """A payload built from several inputs is as fresh as its WORST one: absent beats stale beats
    the oldest age. The returned part's provenance names the input that set the verdict."""
    if not parts:
        raise ValueError("weakest() of nothing: a payload with no inputs has no freshness to report")
    return max(parts, key=lambda f: (f.absent_reason is not None, f.stale, math.inf if f.age_s is None else f.age_s))


class Fresh(BaseModel, Generic[T]):
    """The envelope for NEW routes: the payload under `data`, its freshness under `fresh`."""

    data: T | None
    fresh: Freshness

    @model_validator(mode="after")
    def _absent_is_explained(self) -> Fresh[T]:
        if self.data is None and self.fresh.absent_reason is None:
            raise ValueError("data is null but fresh.absent_reason is not set: unknown is never empty")
        return self


class FreshStamped(BaseModel):
    """The ADDITIVE envelope for EXISTING routes: every legacy key passes through byte-for-byte
    (as extras) and `fresh` is added. Subclass it per route so each gets its own OpenAPI component
    -- the place its body is typed field by field later, without changing a byte on the wire."""

    model_config = ConfigDict(extra="allow")

    fresh: Freshness


class ApiError(BaseModel):
    """The ONE error body. `detail` keeps FastAPI's key and value exactly (a string, a dict, or
    the validation error list), so every consumer that read `detail` still does. `error_code` is a
    stable machine code (not `error`: the admin/run 503 body already spends that on a message);
    `retryable` says whether trying again can help (null = unknown)."""

    # api_error() always dumps every field, so the OUTPUT schema marks `retryable` required too:
    # a generated client then types it `boolean | null`, never `| undefined`.
    model_config = ConfigDict(extra="allow", json_schema_serialization_defaults_required=True)

    detail: Any
    error_code: str
    retryable: bool | None = None


_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal",
    502: "bad_gateway",
    503: "unavailable",
    504: "gateway_timeout",
}
# Transient by HTTP's own definition. Other 4xx are the caller's mistake (retrying repeats it);
# a 500 is UNKNOWN -- could be a race, could be a bug -- so it stays null rather than a guess.
_RETRYABLE = {408: True, 425: True, 429: True, 502: True, 503: True, 504: True}


def error_code(status: int) -> str:
    return _CODES.get(status, f"http_{status}")


def retryable(status: int) -> bool | None:
    if status in _RETRYABLE:
        return True
    return False if 400 <= status < 500 else None


def api_error(http_status: int, detail: Any, /, **extra: Any) -> dict[str, Any]:
    """The error body for `http_status`. `extra` carries a route's legacy keys (kept, never renamed)
    and may override error_code with a more specific one."""
    body = {"detail": detail, "error_code": error_code(http_status), "retryable": retryable(http_status), **extra}
    return ApiError(**body).model_dump()
