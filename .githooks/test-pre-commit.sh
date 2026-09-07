#!/bin/bash
# test-pre-commit.sh — proves .githooks/pre-commit actually blocks, rather than
# assuming it does.
#
# WHY THIS EXISTS: on the sibling ha-trappers repo, where this gate originated, the hook's
# first version used a grep pipeline that errored out under ugrep (this machine's `grep`)
# and, with `|| true` swallowing the error, exited 0 on a staged diff containing a live
# credential. It looked installed and correct and guarded nothing. A leak gate nobody
# tests is worse than none, because it is trusted.
#
# That is not a historical worry here. Porting the gate to this repo introduced a fresh
# bug of exactly the same class: the new Scaleway secret-key pattern was written with a
# `\2` backreference where the group is `\1`, so it raised re.error and — because
# load_private()/scan() compile patterns at scan time — would have aborted every commit
# rather than checking one. Caught by running the patterns, not by reading them.
#
# Every string below is synthetic. The identity-specific patterns the real hook also
# loads are deliberately NOT in this repo (see the hook's header), so this script tests
# the *loading mechanism* with a throwaway pattern file and a canary string instead —
# which proves the private half works without publishing any of it.
#
# Run from the repo root:  .githooks/test-pre-commit.sh
# Exits 0 only if every case behaves as expected.
set -uo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel) || exit 1
HOOK="$REPO_ROOT/.githooks/pre-commit"
[ -x "$HOOK" ] || { echo "FAIL: $HOOK is not executable"; exit 1; }

WORK=$(mktemp -d) || { echo "FAIL: could not create a scratch directory"; exit 1; }
[ -d "$WORK" ] || { echo "FAIL: scratch directory missing"; exit 1; }
trap 'rm -rf "$WORK"' EXIT

pass=0
fail=0
case_n=0

# Each case gets a FRESH scratch repo. An earlier draft reused one, and state left over
# from prior cases (a deleted HEAD plus a still-populated index) made a later `git commit`
# find nothing to commit — which the assertion then read as "the hook blocked it". A test
# harness that reports the wrong reason is the same fail-open trap the hook itself fell
# into; isolation is cheaper than interpreting the residue.
fresh_repo() {
  case_n=$((case_n + 1))
  SCRATCH="$WORK/case$case_n"
  git init -q -b main "$SCRATCH" || {
    echo "FAIL: could not create scratch repo (harness broken, not a hook verdict)"
    exit 1
  }
  git -C "$SCRATCH" config user.name "test"
  git -C "$SCRATCH" config user.email "test@example.com"
  git -C "$SCRATCH" config core.hooksPath "$REPO_ROOT/.githooks"
}

# Default: no identity pattern file, so only the generic half is exercised. Individual
# cases override SCALEWAY_LEAK_PATTERNS where they need the private half.
export SCALEWAY_LEAK_PATTERNS="$WORK/no-such-pattern-file.txt"

# _commit <content> → 0 if the commit went through, 1 if the hook blocked it.
# Output of the attempt is left in $LAST_OUT for assertions that inspect it.
_commit() {
  fresh_repo
  local path="${PROBE_PATH:-probe.txt}"
  mkdir -p "$SCRATCH/$(dirname "$path")"
  printf '%s\n' "$1" > "$SCRATCH/$path"
  git -C "$SCRATCH" add "$path"
  local rc=0
  LAST_OUT=$(git -C "$SCRATCH" commit -m "probe" 2>&1) || rc=1
  # A commit that produced no new commit is a harness bug, not a hook verdict.
  if [ "$rc" -eq 0 ] && ! git -C "$SCRATCH" rev-parse --verify -q HEAD >/dev/null; then
    echo "HARNESS BUG: commit reported success but created no commit:"
    printf '%s\n' "$LAST_OUT" | sed 's/^/    /'
    return 2
  fi
  return "$rc"
}

# _commit_msg <message> → 0 if the commit went through, 1 if a hook blocked it.
# Content is benign; only the message varies, so this isolates the commit-msg hook.
_commit_msg() {
  fresh_repo
  printf 'nothing to see here\n' > "$SCRATCH/probe.txt"
  git -C "$SCRATCH" add probe.txt
  local rc=0
  LAST_OUT=$(git -C "$SCRATCH" commit -m "$1" 2>&1) || rc=1
  if [ "$rc" -eq 0 ] && ! git -C "$SCRATCH" rev-parse --verify -q HEAD >/dev/null; then
    echo "HARNESS BUG: commit reported success but created no commit:"
    printf '%s\n' "$LAST_OUT" | sed 's/^/    /'
    return 2
  fi
  return "$rc"
}

msg_should_block() {
  local rc=0
  _commit_msg "$2" || rc=$?
  case "$rc" in
    0) echo "FAIL: commit-msg did NOT block: $1"; fail=$((fail + 1)) ;;
    1) if printf '%s' "$LAST_OUT" | grep -qF "$BLOCK_MARKER"; then
         echo "ok:   blocked $1"; pass=$((pass + 1))
       else
         echo "FAIL: commit failed but not via the leak check: $1"
         printf '%s\n' "$LAST_OUT" | sed 's/^/    /'; fail=$((fail + 1))
       fi ;;
    *) echo "FAIL: harness error on: $1"; fail=$((fail + 1)) ;;
  esac
}

msg_should_pass() {
  local rc=0
  _commit_msg "$2" || rc=$?
  case "$rc" in
    0) echo "ok:   allowed $1"; pass=$((pass + 1)) ;;
    1) echo "FAIL: commit-msg blocked a clean message: $1"
       printf '%s\n' "$LAST_OUT" | sed 's/^/    /'; fail=$((fail + 1)) ;;
    *) echo "FAIL: harness error on: $1"; fail=$((fail + 1)) ;;
  esac
}

# A commit can fail for reasons that have nothing to do with the hook — a broken
# scratch repo, a missing mktemp, a git that would not run. Counting any failure
# as "blocked" is the same fail-open the hook itself once had, one level up. So a
# block only counts if the scanner said so in its own words.
BLOCK_MARKER="leak check:"

should_block() {
  local rc=0
  _commit "$2" || rc=$?
  case "$rc" in
    0) echo "FAIL: hook did NOT block: $1"; fail=$((fail + 1)) ;;
    1) if printf '%s' "$LAST_OUT" | grep -qF "$BLOCK_MARKER"; then
         echo "ok:   blocked $1"; pass=$((pass + 1))
       else
         echo "FAIL: commit failed but not via the leak check: $1"
         printf '%s\n' "$LAST_OUT" | sed 's/^/    /'; fail=$((fail + 1))
       fi ;;
    *) echo "FAIL: harness error on: $1"; fail=$((fail + 1)) ;;
  esac
}

should_pass() {
  local rc=0
  _commit "$2" || rc=$?
  case "$rc" in
    0) echo "ok:   allowed $1"; pass=$((pass + 1)) ;;
    1) echo "FAIL: hook blocked a clean commit: $1"
       printf '%s\n' "$LAST_OUT" | sed 's/^/    /'; fail=$((fail + 1)) ;;
    *) echo "FAIL: harness error on: $1"; fail=$((fail + 1)) ;;
  esac
}

echo "── generic patterns (shipped in the hook) ──"
should_block "RFC1918 192.168 address"  'the box lives at 192.168.8.60'
should_block "RFC1918 10.x address"     'coordinator at 10.4.2.9'
should_block "RFC1918 172.16 address"   'gateway 172.20.0.1'
should_block "loopback address"         'bind to 127.0.0.1:8123'
should_block "pct exec"                 'run pct exec 100 -- docker restart ha'
should_block "hypervisor name"          'copy it onto the Proxmox host first'
should_block "homelab ssh alias"        'ssh pve then restart'
should_block "homelab storage path"     'config at /tank/docker/homeassistant'
should_block "a real JWT" \
  'token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpYXQiOjE3ODAwMDAwMDB9.AbCdEfGhIjK'
should_block "IBAN"                     'ibanAccountNumber: NL91ABNA0417164300'

echo "── benign content must still commit ──"
should_pass  "placeholder email"        'contact: you@example.com'
should_pass  "sanitized API sample"     '{"units": 9, "nanos": 450000000}'
should_pass  "ordinary python"          'async def async_get_cost(self, organization_id: str):'
should_pass  "public API host"          'BASE_URL = "https://api.trappers.net/api/"'
should_pass  "a version-looking number" 'MIN_HA_VERSION = "2026.3.0"'

# ── Scaleway credentials ─────────────────────────────────────────────────────
# These are the reason this repo needed more than the ported generic half: an API
# key here is spend-authorising, not merely identifying. They match on shape and
# ship in the hook, because an outside contributor is developing against their own
# live account and needs the same protection.
# This file must not itself contain a non-placeholder UUID on any single line —
# the credential patterns are deliberately NOT exempt inside .githooks/ (see
# leakcheck.py lesson 2), and the gate would rightly block its own test file. So
# the synthetic secret is assembled from halves that are each harmless alone.
SYNTH_A='3f8a91c2-77de'
SYNTH_B='4b05-9c14-2ea6d0713b8f'
SYNTH_SECRET="$SYNTH_A-$SYNTH_B"
# Same for the access keys: the pattern anchors on a literal "SCW" prefix, so
# holding the 17-character tail separately keeps this file clean, while the
# assembled value is a realistic key by the time it is staged.
SYNTH_KEY="SCW"'Q7J4M2XKD9PL5N3RT'
SYNTH_KEY_LETTERS="SCW"'ABCDEFGHIJKLMNOPQ'

echo "── Scaleway credential patterns (shape-matched, shipped in the hook) ──"
should_block "an access-key-shaped string" \
  "SCALEWAY_ACCESS_KEY = \"$SYNTH_KEY\""
should_block "an access key in prose"     "the key $SYNTH_KEY stopped working"
should_block "a secret key after secret_key" \
  "secret_key: \"$SYNTH_SECRET\""
should_block "a secret key after SCW_SECRET_KEY=" \
  "export SCW_SECRET_KEY=$SYNTH_SECRET"
should_block "a secret key in an auth header" \
  "\"X-Auth-Token\": \"$SYNTH_SECRET\""

# The four cases below are the P1 misses an external review found in the first
# version of these patterns, which required a credential label within a few
# characters of the UUID. Each of these escaped it. The patterns now match the
# VALUE rather than its context, so a nearby label is no longer load-bearing.
should_block "an unusually wide gap after the label" \
  "secret_key =        \"$SYNTH_SECRET\""
should_block "a YAML block value with the label on another line" \
  "  value: $SYNTH_SECRET"
should_block "a bare UUID whose label is on an unchanged line" \
  "    $SYNTH_SECRET"
should_block "a UUID with no credential context at all" "$SYNTH_SECRET"

# The access-key exemption used to be "contains no digits", inferred from a
# sample of one real key. It is now "17 of the same character", the only form
# no real key can take.
should_block "a letters-only access key that is not a placeholder" \
  "key = \"$SYNTH_KEY_LETTERS\""

# Lesson 5: the test suite has to contain credential-SHAPED strings to test the
# code that consumes them. A gate that blocked those would be routed around with
# SKIP_LEAK_CHECK on every commit, which is worse than no gate. These four are the
# documented placeholder forms and must stay allowed.
echo "── ...but the documented placeholder forms must still commit ──"
should_pass  "digit-free access key placeholder" \
  'ACCESS_KEY = "SCWXXXXXXXXXXXXXXXXX"'
should_pass  "repeated-block secret placeholder" \
  'CONF_SECRET_KEY: "00000000-0000-4000-8000-000000000000"'
should_pass  "second repeated-block secret placeholder" \
  'CONF_SECRET_KEY: "99999999-9999-4999-8999-999999999999"'
# A bare UUID is not a credential: entry ids, unique ids and half the test suite
# are UUIDs. Blocking every UUID would make the gate useless here.
should_pass  "a placeholder UUID as a unique_id" \
  'unique_id = "11111111-1111-4111-8111-111111111111"'
# A ULID is not a UUID and must not be caught — entry ids are ULIDs.
should_pass  "an entry id in a test fixture" \
  'entry_id="01KY2Q7SV1FY74NVS14SR6HG62"'
should_pass  "a long hex string that is not UUID-shaped" \
  'sha256 = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"'

# The .githooks/ exemption covers the generic half ONLY. A credential-shaped
# string has no business in a hook fixture either — the placeholder forms above
# are what fixtures use, so the exemption would buy nothing and would reopen
# exactly the lesson-2 hole.
echo "── credential patterns are NOT exempt inside .githooks/ ──"
PROBE_PATH=".githooks/fixture.sh" should_block \
  "an access key inside .githooks/ is still caught" \
  "should_block \"key\" \"$SYNTH_KEY\""
PROBE_PATH=".githooks/fixture.sh" should_block \
  "a non-placeholder UUID inside .githooks/ is still caught" "$SYNTH_SECRET"
unset PROBE_PATH

echo "── credentials in commit messages ──"
msg_should_block "an access key in the message" \
  "rotated $SYNTH_KEY after the expiry"
msg_should_block "a secret key in the message" \
  "old secret_key $SYNTH_SECRET is revoked"
msg_should_pass  "talking about keys without pasting one" \
  'Rotate the Scaleway API key and document the reauth flow'

echo "── identity pattern file (the private half) ──"
CANARY_FILE="$WORK/patterns.txt"
printf '# synthetic\nZZQQ-CANARY-[0-9]{4}\tsynthetic canary\n' > "$CANARY_FILE"

SCALEWAY_LEAK_PATTERNS="$CANARY_FILE" should_block "canary from pattern file" \
  'secret marker ZZQQ-CANARY-4711 here'
SCALEWAY_LEAK_PATTERNS="$CANARY_FILE" should_pass "non-matching line with file loaded" \
  'secret marker ZZQQ-CANARY-not-a-number here'

# A corrupt pattern file must abort, not silently check with fewer patterns — the same
# fail-open shape that made the first version of this hook worthless.
BAD_FILE="$WORK/bad-patterns.txt"
printf 'ZZQQ-[unclosed\tbroken regex\n' > "$BAD_FILE"
SCALEWAY_LEAK_PATTERNS="$BAD_FILE" should_block "corrupt pattern file aborts the commit" \
  'entirely harmless line'

# Missing pattern file: the generic half still runs (a contributor has no secrets of the
# maintainer's to leak), and the hook says so rather than implying full coverage.
if SCALEWAY_LEAK_PATTERNS="$WORK/definitely-absent.txt" _commit 'harmless' &&
   printf '%s\n' "$LAST_OUT" | grep -q "generic patterns only"; then
  echo "ok:   missing pattern file warns instead of implying full coverage"
  pass=$((pass + 1))
else
  echo "FAIL: missing pattern file did not warn; output was:"
  printf '%s\n' "${LAST_OUT:-<none>}" | sed 's/^/    /'
  fail=$((fail + 1))
fi

echo "── .githooks/ exemption is asymmetric ──"
# The hook and this file must be able to contain generic patterns — they define and
# exercise them. Without this exemption the gate blocks its own test fixtures, which is
# exactly what happened on the third commit attempt.
PROBE_PATH=".githooks/fixture.sh" should_pass "generic pattern inside .githooks/" \
  'should_block "LAN" "the box lives at 192.168.8.60"'
# But the exemption must NOT cover identity patterns: putting a real secret in the hook
# is the v2 mistake, and .githooks/ is precisely where it would land.
PROBE_PATH=".githooks/fixture.sh" SCALEWAY_LEAK_PATTERNS="$CANARY_FILE" \
  should_block "identity pattern inside .githooks/ is still caught" \
  'ZZQQ-CANARY-4711'
# And outside .githooks/, generic patterns still apply.
PROBE_PATH="custom_components/scaleway/api.py" should_block \
  "generic pattern outside .githooks/ still blocked" 'HOST = "192.168.8.60"'

echo
echo "── a '++' content line is content, not a diff header ──"
# A staged line reading "++ x" renders as "+++ x" in the diff. Treating any "+++ "
# line as the file header let an identity canary on such a line through unchecked.
CANARY_FILE2="$WORK/patterns2.txt"
printf '# synthetic\nZZQQ-CANARY-[0-9]{4}\tsynthetic canary\n' > "$CANARY_FILE2"
SCALEWAY_LEAK_PATTERNS="$CANARY_FILE2" \
  should_block "canary on a line beginning with ++" '++ ZZQQ-CANARY-1234'
SCALEWAY_LEAK_PATTERNS="$CANARY_FILE2" \
  should_block "canary on a line beginning with +++" '+++ ZZQQ-CANARY-1234'
should_pass  "an ordinary ++ line with nothing secret" '++ just a diff-looking line'

echo "── an existing but empty pattern file must not pass silently ──"
EMPTY_FILE="$WORK/empty-patterns.txt"
printf '# only comments, no patterns\n\n' > "$EMPTY_FILE"
SCALEWAY_LEAK_PATTERNS="$EMPTY_FILE" \
  should_block "empty pattern file aborts rather than running with no identity cover" \
  'perfectly ordinary content'

echo "── commit messages are scanned too ──"
msg_should_pass  "an ordinary commit message" 'Fix the paging offset'
msg_should_block "a LAN address in the message" 'Deploy tested against 192.168.8.60'
msg_should_block "a homelab path in the message" 'copied into /tank/docker/homeassistant'
msg_should_block "an IBAN in the message" 'removed NL91ABNA0417164300 from the fixture'
SCALEWAY_LEAK_PATTERNS="$CANARY_FILE2" \
  msg_should_block "an identity canary in the message" 'redacted ZZQQ-CANARY-1234 from README'

echo "── a '#' line in a commit message is still published ──"
# git commit -m and -F use cleanup=whitespace, which does NOT strip comments.
msg_should_block "a commented-out LAN address in the message" '# staged on 192.168.8.60'
SCALEWAY_LEAK_PATTERNS="$CANARY_FILE2" \
  msg_should_block "a commented-out identity canary" '# ZZQQ-CANARY-1234'

echo "── a non-ASCII filename must still be scanned ──"
# With core.quotePath on, --name-only renders "café.txt" C-escaped; feeding that
# back as a pathspec matches nothing and yields an empty, unscanned diff.
PROBE_PATH='café.txt' should_block "LAN address in a non-ASCII filename" \
  'the box lives at 192.168.8.60'
unset PROBE_PATH

echo "pre-commit leak-guard: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
