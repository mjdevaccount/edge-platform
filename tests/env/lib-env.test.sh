#!/usr/bin/env bash
# lib-env.sh + lib_env.py against ONE fixture, and both against docker compose.
#
# The answers in tests/fixtures/env/expected.json are not written to agree with the code: they are
# what `docker compose config` resolved for every fixture key (`--regen` rewrites them, and needs
# docker compose). Where compose is installed -- CI's ubuntu-latest has it -- section 1 re-asks it
# on every run, so the file cannot drift from compose without this test going red; a run that
# could not ask is SAID, and in CI it is exit 2, never a quiet pass. Section 2 then holds both
# readers to those answers, on the fixture as committed (LF) and on a CRLF copy of it.
# Section 3 proves the refusals both ways: what compose refuses, and what we do not port, is
# exit 2 from both twins WITHOUT the value in the message; a clean file is not refused.
#
#   bash tests/env/lib-env.test.sh           needs jq + python3; docker compose for section 1
#   bash tests/env/lib-env.test.sh --regen   rewrite expected.json from docker compose
#
# HOME since v0.4.0 (2026-10-29): this repo owns the twins and this test. Consumers vendor the
# twins byte-identical and prove THAT with `edge-env check-copy`, so this oracle runs once, here.
# edge-host still runs its own copy of this test for section 4 (lan_var in its lib-lan.sh).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE/../.."
# LIBENV_DIR names where the twins live, relative to the repo root. Section 4 runs only where
# lib-lan.sh sits beside them (edge-host); LIBENV_REQUIRE_LAN=1 makes its absence a failure there.
LIB="$ROOT/${LIBENV_DIR:-edge_platform/env}"
FXD="$HERE/fixtures"
EXPECTED="$FXD/expected.json"
fails=0; n=0
ok(){ n=$((n+1)); printf '  ok   %s\n' "$1"; }
bad(){ n=$((n+1)); printf '  FAIL %s\n' "$1"; fails=$((fails+1)); }
command -v jq >/dev/null && command -v python3 >/dev/null || {
  echo "lib-env.test: SKIPPED -- needs jq and python3 (exit 2 = did not look)"; exit 2; }

TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT
LF="$TMPD/lf.env"; CRLF="$TMPD/crlf.env"
# editors and the repo's whitespace hygiene strip a committed trailing space, so it is put back here
sed 's/^FX_TRAILING_WS=val$/&   /' "$FXD/fixture.dotenv" > "$LF"
sed 's/$/\r/' "$LF" > "$CRLF"
grep -q '^FX_TRAILING_WS=val   $' "$LF" || { echo "lib-env.test: fixture lost FX_TRAILING_WS (exit 2)"; exit 2; }
# the process environment the fixture's header promises; compose and both readers all see it
export FX_OS_SET=from-os FX_INHERIT=inherited
ABSENT='<<absent>>'
KEYS=$(grep -oE '^[[:space:]]*(export[[:space:]]+)?FX_[A-Z_]+' "$FXD/fixture.dotenv" \
       | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//' | sort -u)
KEYS="$KEYS FX_COMMENTED FX_MISSING FX_NOT_SET"

# shellcheck source=../scripts/lib-env.sh
. "$LIB/lib-env.sh"

# compose_answers FILE -> {key: value} for every key, as docker compose resolves it. The env map
# comes back with every literal `$` doubled (config output re-escapes for reloading), so it is
# un-doubled here; ${K-<<absent>>} tells "not assigned" from "assigned empty".
compose_answers() {
  local yml="$TMPD/compose.yml" k
  { echo 'services:'; echo '  probe:'; echo '    image: scratch'; echo '    environment:'
    for k in $KEYS; do printf '      %s: "${%s-%s}"\n' "$k" "$k" "$ABSENT"; done; } > "$yml"
  docker compose --project-directory "$TMPD" --env-file "$1" -f "$yml" config --format json 2>/dev/null \
    | jq '.services.probe.environment | with_entries(.value |= gsub("\\$\\$"; "$"))
          | with_entries(select(.value != "<<absent>>"))'
}
have_compose(){ docker compose version >/dev/null 2>&1; }

if [ "${1:-}" = "--regen" ]; then
  have_compose || { echo "lib-env.test --regen: needs docker compose"; exit 2; }
  jq -n --argjson lf "$(compose_answers "$LF")" --argjson crlf "$(compose_answers "$CRLF")" \
     '{"_doc": "written by `bash tests/lib-env.test.sh --regen` FROM docker compose; never by hand",
       lf: $lf, crlf: $crlf}' > "$EXPECTED"
  echo "lib-env.test: wrote $EXPECTED from $(docker compose version --short 2>/dev/null)"; exit 0
fi

echo "── 1. expected.json IS docker compose's answer"
if have_compose; then
  for v in lf crlf; do
    f=$LF; [ $v = crlf ] && f=$CRLF
    if [ "$(compose_answers "$f" | jq -S .)" = "$(jq -S ".$v" "$EXPECTED")" ]; then
      ok "$v: compose $(docker compose version --short 2>/dev/null) agrees with expected.json"
    else
      bad "$v: compose disagrees with expected.json -- run --regen and read the diff"
    fi
  done
elif [ -n "${CI:-}" ]; then
  echo "lib-env.test: docker compose missing in CI -- the oracle did not run (exit 2)"; exit 2
else
  echo "  --   docker compose not installed here: the oracle runs in CI"
fi

echo "── 2. both readers give compose's answer for every key, LF and CRLF"
# table VARIANT FILE -> "key want_b64 python_b64" per key: expected.json and lib_env.load in ONE
# python call, base64 so a newline or \r inside a value survives the shell (and Windows jq.exe,
# which rewrites LF as CRLF in raw output)
table(){ python3 -c '
import base64, json, sys
sys.stdout.reconfigure(newline="\n")
sys.path.insert(0, sys.argv[1]); import lib_env
want = json.load(open(sys.argv[2], encoding="utf-8"))[sys.argv[3]]
try: got = lib_env.load(sys.argv[4])
except lib_env.EnvBlind: got = {}
b = lambda d, k: base64.b64encode(d.get(k, sys.argv[5]).encode()).decode() or "-"
for k in sys.argv[6:]: print(k, b(want, k), b(got, k))' "$LIB" "$EXPECTED" "$1" "$2" "$ABSENT" $KEYS; }
b64(){ local s; s=$(base64 | tr -d '\n'); printf '%s' "${s:--}"; }
for v in lf crlf; do
  f=$LF; [ $v = crlf ] && f=$CRLF
  bfail=0; pfail=0
  while read -r k want py; do
    got=$({ env_get "$k" "$f"; rc=$?; [ $rc = 1 ] && printf '%s' "$ABSENT"; [ $rc = 2 ] && printf BLIND; } | b64)
    [ "$got" = "$want" ] || { bfail=1; bad "$v bash  $k: want $want got $got (base64)"; }
    [ "$py" = "$want" ]  || { pfail=1; bad "$v python $k: want $want got $py (base64)"; }
  done < <(table "$v" "$f")
  [ $bfail = 0 ] && ok "$v: lib-env.sh matches compose on all $(wc -w <<<"$KEYS") keys"
  [ $pfail = 0 ] && ok "$v: lib_env.py matches compose on all $(wc -w <<<"$KEYS") keys"
done
# the cases this exists for, named, so a regression reads as what it is
is(){ [ "$(env_get "$1" "$LF")" = "$2" ] && ok "$3" || bad "$3"; }
is FX_HASH_NOSPACE 'abc#def'          "a # with no space before it is part of the value (lan_var cut it)"
is FX_DUP          second             "a duplicate key: the LAST one wins, as compose (env_value took the first)"
is FX_DOLLAR       '$2a$10$abcdef'    "\$\$ is one \$ (every hand pipeline returned it doubled)"
is FX_QUOTE_MID    'ab"cd'            "a quote inside an unquoted value survives (tr -d ate it)"
is FX_EXPORT       exported           "export prefix dropped"
is FX_DQ           'double quoted # not a comment' "# inside quotes is the value"
env_get FX_MISSING "$LF" >/dev/null; [ $? = 1 ] && ok "a missing key is rc 1" || bad "a missing key is rc 1"
env_has FX_EMPTY "$LF" && ok "an assigned-empty key is present (rc 0)" || bad "assigned-empty is present"
crlf_v=$(env_get FX_PLAIN "$CRLF" | od -An -c | tr -d ' \n')
[ "$crlf_v" = plain ] && ok "CRLF: no \\r reaches the value" || bad "CRLF leaked: $crlf_v"
# no newline after the last line: awk hands it back as a full line, so the bash twin must be told.
# Compose leaves a bare trailing KEY at EOF unassigned (reviewer, compose 5.3.1); lib_env.py agrees.
nonl(){ # <label> <key> <file body, no final newline>
  local f="$TMPD/nonl.env" gb gp rb rp
  printf '%s' "$3" > "$f"
  gb=$(FX_NONL=from-os env_get "$2" "$f" 2>/dev/null); rb=$?
  gp=$(FX_NONL=from-os python3 "$LIB/lib_env.py" get "$2" "$f" 2>/dev/null); rp=$?
  [ "$rb:$gb" = "$rp:$gp" ] && ok "no final newline, both twins agree: $1 (rc $rb)" \
    || bad "no final newline, $1: bash rc $rb [$gb], python rc $rp [$gp]"
}
nonl "KEY=val at EOF still reads"  FX_A     'FX_A=val'
nonl "a bare KEY at EOF"           FX_NONL  "$(printf 'FX_A=1\nFX_NONL')"
nonl "a CRLF file ending in \\r"   FX_A     "$(printf 'FX_B=1\r\nFX_A=val\r')"
[ "$(printf 'FX_A=val' > "$TMPD/n1.env"; env_get FX_A "$TMPD/n1.env")" = val ] \
  && ok "KEY=val with no final newline is val" || bad "KEY=val with no final newline lost its value"

echo "── 3. refusals: exit 2 from both, never the value in the message; a clean file is not refused"
SECRET=fake-SEKRIT-9f8e
refuse(){ # <label> <file body>
  local f="$TMPD/r.env" erb erp rb rp
  printf '%s\n' "$2" > "$f"
  env_get FX_A "$f" >/dev/null 2>"$TMPD/eb"; rb=$?
  python3 "$LIB/lib_env.py" get FX_A "$f" >/dev/null 2>"$TMPD/ep"; rp=$?
  erb=$(cat "$TMPD/eb"); erp=$(cat "$TMPD/ep")
  if [ $rb = 2 ] && [ $rp = 2 ]; then ok "refused (both): $1"; else bad "$1: bash rc $rb, python rc $rp (want 2, 2)"; fi
  case "$erb$erp" in *"$SECRET"*) bad "$1: the message carried the value" ;; esac
}
refuse "unterminated quote (compose refuses)"   "FX_A=\"$SECRET"
refuse "key with a space (compose refuses)"     "FX A=$SECRET"
refuse "invalid \${...} (compose refuses)"      "FX_A=$SECRET\${1}"
refuse "failed \${X:?} (compose refuses)"       "FX_A=$SECRET\${FX_NOT_SET:?boom}"
refuse "nested template (not ported)"           "FX_A=\${FX_NOT_SET:-\${FX_PLAIN}}$SECRET"
refuse "non-ASCII whitespace (not ported)"      "$(printf 'FX_A=%s\302\240x' "$SECRET")"
refuse "octal NUL (not ported)"                 "FX_A=\"$SECRET\\0000\""
env_get FX_A "$TMPD/does-not-exist.env" >/dev/null 2>&1; [ $? = 2 ] && ok "an unreadable file is rc 2" || bad "unreadable file rc"
printf 'FX_A=%s\n' "$SECRET" > "$TMPD/clean.env"
[ "$(env_get FX_A "$TMPD/clean.env")" = "$SECRET" ] && ok "the same key in a clean file is answered (not refused)" \
  || bad "a clean file was refused"

if [ -r "$LIB/lib-lan.sh" ]; then
  echo "── 4. lan_var keeps its contract (value or empty, always rc 0)"
  # shellcheck source=../scripts/lib-lan.sh
  LAN_ENV_FILE="$LF" . "$LIB/lib-lan.sh"
  LAN_ENV_FILE="$LF"
  [ "$(lan_var FX_HASH_NOSPACE)" = 'abc#def' ] && ok "lan_var reads through lib-env" || bad "lan_var reads through lib-env"
  LAN_ENV_FILE="$TMPD/absent.env"
  v=$(lan_var FX_PLAIN); rc=$?
  [ $rc = 0 ] && [ -z "$v" ] && ok "lan_var on a missing file: empty, rc 0" || bad "lan_var missing file: rc $rc [$v]"
elif [ "${LIBENV_REQUIRE_LAN:-0}" = 1 ]; then   # edge-host CI sets it
  bad "edge-host lost scripts/lib-lan.sh -- section 4 has nothing to test"
fi

echo
echo "lib-env.test: $((n - fails))/$n passed"
[ "$fails" -eq 0 ]
