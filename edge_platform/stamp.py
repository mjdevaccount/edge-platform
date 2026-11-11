"""Fingerprinting, defined once. Stdlib.

Lifted from trade-data `service/platform/stamp.py` (M6 PL7). Fourteen sites there had hand-rolled
canonical-json -> sha256 -> truncate: three truncation lengths, one site secretly on SHA-1, and the
code version resolved four ways. A fingerprint's entire job is comparability -- same content, same
stamp, across planes and across time -- and every local copy is a chance for two planes to disagree
about what "the same config" means.

THE CONTINUITY LAW: a fingerprint that is PERSISTED may never change its algorithm. A changed stamp
for unchanged content reads as a config change that never happened and silently splits every
historical cohort. Every function here is pinned bit-for-bit by tests/test_stamp.py on fixed inputs;
a failing pin is fixed by finding what changed the serialization, NEVER by updating the literal.

  fingerprint(obj, n=16)  sha256 over canonical JSON (sort_keys, compact separators, default=str).
  sha(text, n=16)         sha256 over an already-canonical string, for callers that own their
                          canonicalization. Byte-identical to llm-ports' `prompts.sha` (the pack
                          content sha); not imported from it, because edge-platform sits BELOW
                          llm-ports and depends on nothing of the estate's.
  canon(v)                the hash-input canonicalizer for one VALUE: floats round to 10 decimals
                          (a float64 round-trip may not flip a seal), date/datetime -> isoformat,
                          anything else unchanged.
  doc_sha(path, n=16)     sha256 over file BYTES (no newline normalization).
  git_sha(env, short=)    the code version from an environment variable THE CALLER NAMES (baked at
                          image build). None when unset: honest, never a fake.

Not here: a consumer's frozen legacy variants (trade-data keeps its SHA-1 `config_fingerprint` as a
private wrapper -- it is stamped on years of rows and is nobody else's continuity to carry).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
from typing import Any

__all__ = ["canon", "doc_sha", "fingerprint", "git_sha", "sha"]


def fingerprint(obj: Any, *, n: int = 16) -> str:
    """sha256 hex over canonical JSON of `obj`, truncated to n chars."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:n]


def sha(text: str, *, n: int = 16) -> str:
    """sha256 hex over an already-canonical string, truncated to n chars."""
    return hashlib.sha256(text.encode()).hexdigest()[:n]


def canon(v: Any) -> Any:
    """Canonicalize one value before hashing: float -> round(v, 10), date/datetime -> isoformat(),
    else unchanged. Feeds SEALED hashes -- never change its output."""
    if isinstance(v, float):
        return round(v, 10)
    if isinstance(v, dt.datetime | dt.date):
        return v.isoformat()
    return v


def doc_sha(path: pathlib.Path, *, n: int = 16) -> str:
    """sha256 hex over a file's BYTES, truncated to n chars. No newline normalization: a document is
    hashed exactly as shipped."""
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()[:n]


def git_sha(env: str, *, short: bool = False) -> str | None:
    """The code version this process runs, read from `$<env>` (the NAME is the caller's). None when
    unset or blank. short=True trims to 12 chars."""
    if not env or not env.strip():
        raise ValueError("git_sha needs the name of the environment variable that carries the version")
    raw = (os.environ.get(env) or "").strip()
    if not raw:
        return None
    return raw[:12] if short else raw
