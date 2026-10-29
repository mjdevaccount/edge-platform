#!/usr/bin/env python3
"""lib_env -- read KEY from a .env file the way docker compose does. Python twin of lib-env.sh.

HOME: mjdevaccount/edge-platform `edge_platform/env/` (since v0.4.0, 2026-10-29; edge-host
`scripts/` before that). Edit it THERE, release, re-vendor. A consumer that cannot pip install --
the box's system python, the observer VM's Debian python3, bash -- VENDORS this file and its
twin byte-identical and proves it in CI with `edge-env check-copy FILE` from the pinned wheel;
everything else imports `from edge_platform.env import lib_env`.

Same rules, same answers: edge-platform tests/env/lib-env.test.sh drives BOTH readers over one
fixture (tests/env/fixtures/fixture.dotenv) and, where docker compose is installed (CI), asks
compose itself for the same keys -- so "matches compose" is a measurement on every PR, not a
claim. Read lib-env.sh's header for the WHY; in one line: ~30 hand-rolled `grep|cut|tr` copies
disagreed with compose and with each other (first match vs last, `#` truncation, `$$` left
doubled, quotes kept), so two scripts reading the same key could compare different strings.

    from edge_platform.env import lib_env      # a vendored copy: `import lib_env`
    lib_env.get("WINDMILL_TOKEN", path)   # -> str, or None when the key is absent
    lib_env.load(path)                    # -> {key: value}, the whole file
    lib_env.parse(text)                   # -> the same, from text

THE REFERENCE is compose-go v2.16.2 `dotenv/parser.go` + `template/template.go` (what
`docker compose` uses for the project .env and every `env_file:`). Ported rules, each in the
fixture:
  * blank lines and `#` lines are skipped; a leading `export ` is dropped; `KEY: value` is
    accepted (yaml form); whitespace around the separator is dropped
  * DUPLICATE KEYS: the LAST assignment wins (`out[key] = value` in a loop)
  * unquoted: the value ends at the newline, then at the first " #" (a SPACE then `#`: `a#b`
    keeps its `#`, and so does `a<TAB>#b`), then trailing whitespace (so CRLF's \\r) is trimmed
  * 'single': literal; only \\' is an escape. "double": \\n \\t \\r \\\\ \\" \\$ and friends, and
    either quote may span lines. Whatever follows the closing quote on its line is a comment
  * interpolation (unquoted and double-quoted only): $$ -> $, $NAME, ${NAME}, and
    ${NAME:-x} ${NAME-x} ${NAME:+x} ${NAME+x} ${NAME:?x} ${NAME?x}. A name resolves from the
    PROCESS ENVIRONMENT first, then from keys earlier in the file (compose-go GetEnvFromFile);
    an unset one is the empty string
  * a bare `KEY` line takes its value from that same lookup (compose's "inherited" key)

REFUSED, never guessed -- EnvBlind (the CLI exits 2), where compose would answer and this does
not port the rule: a template inside a modifier's argument (`${A:-${B}}`, `${A:-{x}}`), a non-ASCII
key character, a non-ASCII whitespace character anywhere in the file. And EnvBlind wherever compose
itself refuses the file: an unterminated quote, a key with a space, a bad key character, an
invalid `${...}`, a failed `${NAME:?...}`. A reader may refuse where compose answers; it must never
answer differently.

Error messages carry a line number and never a value -- these files hold secrets.

CLI (the box's system python has no pip, so stdlib only):
  lib_env.py get KEY [FILE]          print the value; exit 0 found, 1 absent, 2 blind
  lib_env.py digest FILE KEY...      `KEY sha256[:12]` per key (`absent` when missing) -- for
                                     comparing two readers on a real file without printing it
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Mapping

DEFAULT_FILE = os.environ.get("EDGE_INFRA_ENV") or os.path.join(
    os.environ.get("HOME", "/home/matt"), "edge/trade-infra/.env"
)


class EnvBlind(RuntimeError):
    """The file cannot be read the way compose reads it. Exit 2 territory, never a guess."""


_SPACE = "\t\v\f\r "  # parser.go isSpace, minus 0x85/0xA0 (refused below)
_GO_SPACE = _SPACE + "\n"  # unicode.IsSpace over ASCII
#: every non-ASCII Unicode White_Space code point: compose treats them as space, we refuse the file
_UNICODE_SPACE = re.compile("[\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]")
_EXPORT = re.compile(r"^export[\t\n\f\r ]+")  # RE2 \s is ASCII [\t\n\f\r ]
_ESCAPE = re.compile(r'(\\(?:[abcfnrtv$"\\]|0[0-9]{0,3}))')
_SIMPLE = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", '"': '"'}
_KEY_PUNCT = "_.-[]"
_NAME = re.compile(r"[_a-zA-Z][_a-zA-Z0-9]*")
#: the modifiers, longest first so `:-` is not read as `:` + `-`
_SEPS = (":?", "?", ":-", "-", ":+", "+")


def _unescape(m: re.Match[str]) -> str:
    s = m.group(0)
    if s == "\\$":
        return "$$"  # a literal $, kept away from interpolation
    if s.startswith("\\0"):  # Go octal: \0123 -> \123 -> "S"; needs exactly 3 digits
        digits = s[2:]
        if len(digits) == 3 and all(c in "01234567" for c in digits) and int(digits, 8) <= 255:
            if int(digits, 8) == 0:
                raise EnvBlind("an octal escape for NUL (not ported)")
            return chr(int(digits, 8))
        return s
    return _SIMPLE.get(s[1], s)  # \c is no Go escape: kept as written


class _Parser:
    def __init__(self, text: str, environ: Mapping[str, str]) -> None:
        self.src = text
        self.env = environ
        self.out: dict[str, str] = {}
        self.line = 1

    # -- the lookup compose-go GetEnvFromFile builds: process env first, then the file so far --
    def lookup(self, name: str) -> tuple[str, bool]:
        if name in self.env:
            return self.env[name], True
        if name in self.out:
            return self.out[name], True
        return "", False

    def blind(self, why: str) -> EnvBlind:
        return EnvBlind(f"line {self.line}: {why}")

    def run(self) -> dict[str, str]:
        src = self.src
        while True:
            src = self.statement_start(src)
            if not src:
                return self.out
            key, rest, inherited = self.key_name(src)
            if " " in key:
                raise self.blind("key cannot contain a space")
            if inherited:
                v, ok = self.lookup(key)
                if ok:
                    self.out[key] = v
                src = rest
                continue
            value, src = self.value(rest)
            self.out[key] = value

    def statement_start(self, src: str) -> str:
        while True:
            i = 0
            while i < len(src) and src[i] in _GO_SPACE:
                if src[i] == "\n":
                    self.line += 1
                i += 1
            if i == len(src):
                return ""
            src = src[i:]
            if src[0] != "#":
                return src
            nl = src.find("\n")
            if nl == -1:
                return ""
            src = src[nl:]

    def key_name(self, src: str) -> tuple[str, str, bool]:
        if _EXPORT.match(src):
            src = src[len("export") :].lstrip(_SPACE)
        key, offset, inherited = "", 0, False
        for i, c in enumerate(src):
            if c in _SPACE:
                continue
            if c in "=:\n":
                key, offset, inherited = src[:i], i + 1, c == "\n"
                break
            if c in _KEY_PUNCT or (c.isascii() and c.isalnum()):
                continue
            raise self.blind(
                "unexpected character in variable name" + ("" if c.isascii() else " (non-ASCII: not ported)")
            )
        if src == "":
            raise self.blind("zero length string")
        if inherited and " " not in key:
            self.line += 1
        return key.rstrip(_GO_SPACE), src[offset:].lstrip(_SPACE), inherited

    def value(self, src: str) -> tuple[str, str]:
        quote = src[:1] if src[:1] in ("'", '"') else ""
        if not quote:
            value, _, rest = src.partition("\n")
            self.line += 1
            value = value.split(" #", 1)[0].rstrip(_GO_SPACE)
            return self.interpolate(value), rest
        chars: list[str] = []
        escaped = False
        for i in range(1, len(src)):
            c = src[i]
            if c == "\n":
                self.line += 1
            if c != quote:
                if not escaped and c == "\\":
                    escaped = True
                    continue
                if escaped:
                    escaped = False
                    chars.append("\\")
                chars.append(c)
                continue
            if escaped:  # \" or \' -- the quote itself, kept
                escaped = False
                chars.append(c)
                continue
            value = "".join(chars)
            if quote == '"':
                value = self.interpolate(_ESCAPE.sub(_unescape, value))
            return value, src[i + 1 :]
        raise self.blind("unterminated quoted value")

    # -- template.Substitute, the ported subset ------------------------------------------------
    def interpolate(self, s: str) -> str:
        out: list[str] = []
        i = 0
        while True:
            j = s.find("$", i)
            if j == -1:
                out.append(s[i:])
                return "".join(out)
            out.append(s[i:j])
            nxt = s[j + 1 : j + 2]
            if nxt == "$":
                out.append("$")
                i = j + 2
            elif nxt == "{":
                text, i = self.braced(s, j)
                out.append(text)
            elif (m := _NAME.match(s, j + 1)) is not None:
                out.append(self.lookup(m.group(0))[0])
                i = m.end()
            else:  # `$` before anything else is literal
                out.append("$")
                i = j + 1

    def braced(self, s: str, j: int) -> tuple[str, int]:
        m = _NAME.match(s, j + 2)
        if m is None:
            raise self.blind("invalid template")
        name, k = m.group(0), m.end()
        if s[k : k + 1] == "}":
            return self.lookup(name)[0], k + 1
        sep = next((x for x in _SEPS if s.startswith(x, k)), None)
        close = s.find("}", k)
        nl = s.find("\n", k)
        if sep is None or close == -1 or (nl != -1 and nl < close):
            raise self.blind("invalid template")
        arg = s[k + len(sep) : close]
        if "$" in arg or "{" in arg:
            raise self.blind("a template inside a ${...} modifier (not ported)")
        value, ok = self.lookup(name)
        if sep in (":-", "-"):
            text = arg if (not ok or (sep == ":-" and value == "")) else value
        elif sep in (":+", "+"):
            text = arg if (ok and (sep == "+" or value != "")) else value
        else:  # :? and ?
            if not ok or (sep == ":?" and value == ""):
                raise self.blind(f"required variable {name} is missing a value")
            text = value
        return text, close + 1


def parse(text: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """{key: value} for an env file's TEXT, as compose reads it. Raises EnvBlind."""
    if text.startswith("\ufeff"):
        text = text[1:]
    if _UNICODE_SPACE.search(text):
        raise EnvBlind("a non-ASCII whitespace character (not ported)")
    return _Parser(text, os.environ if environ is None else environ).run()


def load(path: str | os.PathLike[str] = DEFAULT_FILE, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The whole file. Raises EnvBlind when it cannot be read or parsed."""
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        raise EnvBlind(f"{path}: cannot read ({type(e).__name__})") from e
    try:
        return parse(text, environ)
    except EnvBlind as e:
        raise EnvBlind(f"{path}: {e}") from e


def get(key: str, path: str | os.PathLike[str] = DEFAULT_FILE, environ: Mapping[str, str] | None = None) -> str | None:
    """KEY's value, or None when the file does not assign it. Raises EnvBlind."""
    return load(path, environ).get(key)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def main(argv: list[str]) -> int:
    try:
        if len(argv) in (2, 3) and argv[0] == "get":
            v = get(argv[1], argv[2] if len(argv) == 3 else DEFAULT_FILE)
            if v is None:
                return 1
            sys.stdout.write(v)
            return 0
        if len(argv) >= 3 and argv[0] == "digest":
            env = load(argv[1])
            for k in argv[2:]:
                print(k, digest(env[k]) if k in env else "absent")
            return 0
    except EnvBlind as e:
        print(f"lib_env: BLIND {e}", file=sys.stderr)
        return 2
    print("usage: lib_env.py get KEY [FILE] | digest FILE KEY...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
