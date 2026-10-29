# shellcheck shell=bash
# lib-env (sourced, not run)
# lib-env -- read KEY from a .env file the way docker compose does (2026-10-24, reusable-stack r5).
#
# WHY. By 2026-10-24 ~30 scripts across edge-host / trade-infra / home-host read a .env with a
# hand pipeline (`grep '^K=' | cut -d= -f2- | tr -d quotes`, `sed -n 's/^K=//p' | sed -n 1p`), and
# they disagreed in RESULTS, not style: edge-active.sh took the LAST match while env_value took the
# first and said it "mirrors compose"; lan_var cut at `[[:space:]]*#` so `a#b` came back `a`;
# knowledge-sync.sh kept the quotes; `tr -d '"'` also ate a quote INSIDE a value; and not one of
# them undid compose's `$$`, so the box's own CADDY_BASIC_AUTH_HASH (three `$$` in it) read back
# as a different string from the one Caddy was given. Two scripts comparing one key could compare
# two strings. This file and lib_env.py (its Python twin) are the one reader each.
#
# HOME: mjdevaccount/edge-platform edge_platform/env/ (since v0.4.0, 2026-10-29; edge-host scripts/
# before that). Edit it THERE, release, re-vendor: every repo that sources it keeps a byte-identical
# copy beside its scripts and proves it in CI with `edge-env check-copy` from its pinned wheel.
#
# THE REFERENCE is compose-go v2.16.2 dotenv/parser.go + template/template.go -- what docker compose
# applies to the project .env and every env_file:. tests/env/lib-env.test.sh runs BOTH twins over one
# fixture (tests/env/fixtures/fixture.dotenv) against answers docker compose itself produced, and
# re-asks compose where it is installed (CI), so the claim is re-measured on every PR. The rules
# (lib_env.py's docstring has them in full):
#   last duplicate wins - `export ` dropped - `KEY: v` accepted - unquoted ends at " #" (space
#   first: `a#b` keeps its `#`) and loses trailing whitespace (CRLF's \r) - 'single' literal -
#   "double" takes \n \t \\ \" \$ and may span lines - $$ $NAME ${NAME} ${NAME:-x} -x :+x +x :?x ?x
#   expand from the PROCESS environment first, then earlier keys in the file - bare `KEY` inherits.
# REFUSED (exit 2), never guessed: a template inside a modifier's argument, a non-ASCII key
# character, a non-ASCII whitespace character -- and whatever compose refuses (unterminated quote,
# key with a space, invalid ${...}, a failed ${NAME:?}). A twin may refuse where compose answers;
# it must never answer differently. Errors name a line, never a value: these files hold secrets.
#
#   . "$(dirname "$0")/lib-env.sh"
#   env_get KEY [FILE]  -> the value on stdout. rc 0 = assigned (maybe empty), 1 = not assigned,
#                          2 = BLIND (unreadable / compose would refuse it / not ported). Under
#                          `set -e`, `v=$(env_get K F)` EXITS on 1 -- write `v=$(env_get K F) || true`
#                          where absent is normal, and decide your own side of 2.
#   env_has KEY [FILE]  -> rc only, same codes
#   FILE defaults to $EDGE_INFRA_ENV, else ~/edge/trade-infra/.env (the stack's one .env).
#
# This file does NOT honour an exported KEY over the file: that is the caller's policy (lib-lan's
# lan_gateway does it on purpose). An exported variable only feeds ${...} inside the file, as in
# compose. Needs only bash + POSIX awk (mawk on the box).
ENV_DEFAULT_FILE="${ENV_DEFAULT_FILE:-${EDGE_INFRA_ENV:-${HOME:-/home/matt}/edge/trade-infra/.env}}"

env_get() {  # env_get KEY [FILE]
  local f="${2:-$ENV_DEFAULT_FILE}"
  if [ ! -r "$f" ] || [ -d "$f" ]; then
    printf 'lib-env: BLIND %s: cannot read\n' "$f" >&2
    return 2
  fi
  # BINMODE=3: gawk on Windows (Git Bash) otherwise drops the \r of CRLF before the parser sees it,
  # which loses it from a quoted multi-line value; mawk (the box) has no text mode and ignores it
  # nonl: awk hands every record back as a full line, so a file whose last line has NO newline would
  # read as one that has it -- and compose parses those differently (a bare trailing `KEY`).
  local nonl=0
  if [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ]; then nonl=1; fi
  __LIBENV_KEY="$1" LC_ALL=C awk -v BINMODE=3 -v fname="$f" -v nonl="$nonl" "$__LIBENV_AWK" "$f"
}

env_has() { env_get "$@" >/dev/null; }

# The parser. A literal port of compose-go's (functions named after theirs); reads the whole file
# in END. Exit 0/1/2 as env_get documents.
__LIBENV_AWK='
function blind(why) { printf "lib-env: BLIND %s: line %d: %s\n", fname, line, why > "/dev/stderr"; failed = 1; exit 2 }
function isspace(c) { return c == " " || c == "\t" || c == "\r" || c == "\v" || c == "\f" }
function isgospace(c) { return isspace(c) || c == "\n" }
function rtrim(s) { while (s != "" && isgospace(substr(s, length(s), 1))) s = substr(s, 1, length(s) - 1); return s }
function ltrim(s) { while (s != "" && isspace(substr(s, 1, 1))) s = substr(s, 2); return s }
function lookup(name) {                        # GetEnvFromFile: process env first, then the file so far
  found = 0
  if (name != "__LIBENV_KEY" && (name in ENVIRON)) { found = 1; return ENVIRON[name] }
  if (name in out) { found = 1; return out[name] }
  return ""
}
function statement_start(src,   c, nl) {
  while (1) {
    while (src != "") { c = substr(src, 1, 1); if (!isgospace(c)) break; if (c == "\n") line++; src = substr(src, 2) }
    if (src == "" || substr(src, 1, 1) != "#") return src
    nl = index(src, "\n"); if (nl == 0) return ""
    src = substr(src, nl)
  }
}
function key_name(src,   i, c, n, off) {            # sets KEY REST INHERITED
  if (match(src, /^export[\t\n\f\r ]+/)) src = ltrim(substr(src, 7))
  KEY = ""; INHERITED = 0; off = 0; n = length(src)
  for (i = 1; i <= n; i++) {
    c = substr(src, i, 1)
    if (isspace(c)) continue
    if (c == "=" || c == ":" || c == "\n") { KEY = substr(src, 1, i - 1); off = i; INHERITED = (c == "\n"); break }
    if (c ~ /[A-Za-z0-9]/ || index("_.-[]", c)) continue
    blind("unexpected character in variable name" (c > "~" ? " (non-ASCII: not ported)" : ""))
  }
  if (INHERITED && index(KEY, " ") == 0) line++
  KEY = rtrim(KEY); REST = ltrim(substr(src, off + 1))
}
function unescape(s,   o, c, d, i, n) {              # expandEscapes: \a\b\f\n\r\t\v\\ \$->$$ \0ooo
  o = ""
  while ((i = index(s, "\\")) > 0) {
    o = o substr(s, 1, i - 1); c = substr(s, i + 1, 1)
    if (c == "$") { o = o "$$"; s = substr(s, i + 2); continue }
    if (c == "0") {
      d = substr(s, i + 2, 3); match(d, /^[0-9]*/); d = substr(d, 1, RLENGTH)
      n = (length(d) == 3 && d ~ /^[0-7]+$/) ? oct(d) : 256
      if (n == 0) blind("an octal escape for NUL (not ported)")
      if (n < 128) o = o sprintf("%c", n)
      else if (n <= 255) o = o sprintf("%c%c", 192 + int(n / 64), 128 + n % 64)   # Go string(rune): UTF-8
      else o = o "\\0" d
      s = substr(s, i + 2 + length(d)); continue
    }
    if (c in ESC) { o = o ESC[c]; s = substr(s, i + 2); continue }
    o = o "\\"; s = substr(s, i + 1)               # \c is no escape: the backslash stays, c is rescanned
  }
  return o s
}
function unicode_space(s,   i) {               # U+0085 U+00A0 U+1680 U+2000-200A U+2028 U+2029 U+202F U+205F U+3000
  if (index(s, "\302\205") || index(s, "\302\240") || index(s, "\341\232\200") || index(s, "\342\201\237") || index(s, "\343\200\200")) return 1
  for (i = 128; i <= 138; i++) if (index(s, "\342\200" sprintf("%c", i))) return 1
  return index(s, "\342\200\250") || index(s, "\342\200\251") || index(s, "\342\200\257")
}
function oct(d) { return (substr(d,1,1)+0)*64 + (substr(d,2,1)+0)*8 + (substr(d,3,1)+0) }
function interpolate(s,   o, j, nx, name, k, sep, cl, nl, arg, v) {
  o = ""
  while ((j = index(s, "$")) > 0) {
    o = o substr(s, 1, j - 1); nx = substr(s, j + 1, 1)
    if (nx == "$") { o = o "$"; s = substr(s, j + 2); continue }
    if (nx == "{") {
      s = substr(s, j + 2)
      if (!match(s, /^[_A-Za-z][_A-Za-z0-9]*/)) blind("invalid template")
      name = substr(s, 1, RLENGTH); s = substr(s, RLENGTH + 1)
      if (substr(s, 1, 1) == "}") { o = o lookup(name); s = substr(s, 2); continue }
      sep = ""
      if (substr(s, 1, 2) == ":?" || substr(s, 1, 2) == ":-" || substr(s, 1, 2) == ":+") sep = substr(s, 1, 2)
      else if (substr(s, 1, 1) == "?" || substr(s, 1, 1) == "-" || substr(s, 1, 1) == "+") sep = substr(s, 1, 1)
      cl = index(s, "}"); nl = index(s, "\n")
      if (sep == "" || cl == 0 || (nl > 0 && nl < cl)) blind("invalid template")
      arg = substr(s, length(sep) + 1, cl - length(sep) - 1); s = substr(s, cl + 1)
      if (index(arg, "$") || index(arg, "{")) blind("a template inside a ${...} modifier (not ported)")
      v = lookup(name)
      if (sep == ":-" || sep == "-") o = o ((!found || (sep == ":-" && v == "")) ? arg : v)
      else if (sep == ":+" || sep == "+") o = o ((found && (sep == "+" || v != "")) ? arg : v)
      else { if (!found || (sep == ":?" && v == "")) blind("required variable " name " is missing a value"); o = o v }
      continue
    }
    if (match(substr(s, j + 1), /^[_A-Za-z][_A-Za-z0-9]*/)) {
      o = o lookup(substr(s, j + 1, RLENGTH)); s = substr(s, j + 1 + RLENGTH); continue
    }
    o = o "$"; s = substr(s, j + 1)
  }
  return o s
}
function value(src,   q, i, n, c, esc, chars, v, nl) {   # sets VAL REST
  q = substr(src, 1, 1)
  if (q != "\"" && q != "\047") {
    nl = index(src, "\n")
    if (nl) { v = substr(src, 1, nl - 1); REST = substr(src, nl + 1) } else { v = src; REST = "" }
    line++
    if ((i = index(v, " #")) > 0) v = substr(v, 1, i - 1)
    VAL = interpolate(rtrim(v)); return
  }
  chars = ""; esc = 0; n = length(src)
  for (i = 2; i <= n; i++) {
    c = substr(src, i, 1)
    if (c == "\n") line++
    if (c != q) {
      if (!esc && c == "\\") { esc = 1; continue }
      if (esc) { esc = 0; chars = chars "\\" }
      chars = chars c; continue
    }
    if (esc) { esc = 0; chars = chars c; continue }
    VAL = (q == "\"") ? interpolate(unescape(chars)) : chars
    REST = substr(src, i + 1); return
  }
  blind("unterminated quoted value")
}
BEGIN { ESC["a"]="\007"; ESC["b"]="\b"; ESC["f"]="\f"; ESC["n"]="\n"; ESC["r"]="\r"; ESC["t"]="\t"; ESC["v"]="\v"; ESC["\\"]="\\"; ESC["\""]="\""; line = 1 }
{ src = src $0 "\n" }
END {
  if (failed) exit 2
  if (nonl + 0 && src != "") src = substr(src, 1, length(src) - 1)
  if (substr(src, 1, 3) == "\357\273\277") src = substr(src, 4)
  if (unicode_space(src)) blind("a non-ASCII whitespace character (not ported)")
  while ((src = statement_start(src)) != "") {
    key_name(src)
    if (index(KEY, " ")) blind("key cannot contain a space")
    if (INHERITED) { v = lookup(KEY); if (found) out[KEY] = v; src = REST; continue }
    k = KEY; value(REST); out[k] = VAL; src = REST
  }
  want = ENVIRON["__LIBENV_KEY"]
  if (!(want in out)) exit 1
  printf "%s", out[want]
  exit 0
}'
