#!/bin/bash

# Cases are invoked through the test-case dispatch loop.
# shellcheck disable=SC2329

case "$(uname)" in
    MINGW*|MSYS*|CYGWIN*) printf 'SKIP duplicate-session-guard: stubs POSIX de ps y osascript.\n'; exit 0 ;;
esac

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
HOOK=$REPO/configs/claude/hooks/duplicate-session-guard
TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/duplicate-session-guard-test.XXXXXX") || exit 1
trap 'rm -rf "$TEST_TMP"' EXIT
mkdir -p "$TEST_TMP/stubs"
cat > "$TEST_TMP/stubs/ps" <<'STUB'
#!/bin/bash
# Fuera del locale C, lstart sale con otro número de campos, como en ja_JP.
if [ "${LC_ALL:-}" = C ]; then cat "$FAKE_PS"; else sed 's/Sun Oct 18 /日 10\/18 /; s/Sat Oct 17 /土 10\/17 /' "$FAKE_PS"; fi
STUB
cat > "$TEST_TMP/stubs/osascript" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_NOTIFY"
STUB
chmod +x "$TEST_TMP/stubs/ps" "$TEST_TMP/stubs/osascript"
cp "$TEST_TMP/stubs/ps" "$TEST_TMP/ps-stub"
# Un enlace con nombre de claude que apunta al cli.js del paquete npm.
mkdir -p "$TEST_TMP/bin" "$TEST_TMP/lib/node_modules/@anthropic-ai/claude-code"
touch "$TEST_TMP/lib/node_modules/@anthropic-ai/claude-code/cli.js"
ln -s "$TEST_TMP/lib/node_modules/@anthropic-ai/claude-code/cli.js" "$TEST_TMP/bin/claude"
export FAKE_PS=$TEST_TMP/ps.txt FAKE_NOTIFY=$TEST_TMP/notify.log
export DUPLICATE_SESSION_GUARD_SELF_PID=500

SESSION=c0c6adf7-8097-47d5-9801-9581f1b9aa86
STARTED='Sun Oct 18 22:32:50 2026'
PAYLOAD='{"session_id":"'$SESSION'","transcript_path":"/tmp/t.jsonl","source":"resume"}'

assert() { "$@" || { printf 'Falló la comprobación: %s\n' "$*" >&2; return 1; }; }

hook() { printf '%s' "$1" | PATH="$TEST_TMP/stubs:$PATH" "$HOOK"; }

# Sin duplicado, el hook sale 0 sin escribir nada en stdout ni en stderr.
quiet() {
    local status
    hook "$1" > "$TEST_TMP/out" 2> "$TEST_TMP/err"
    status=$?
    assert test "$status" = 0
    assert test ! -s "$TEST_TMP/out"
    assert test ! -s "$TEST_TMP/err"
}

table() {
    cat > "$FAKE_PS" <<EOF
    1     0 $STARTED /sbin/launchd
  400     1 $STARTED /Applications/T3 Code.app/Contents/MacOS/T3 Code
  450   400 $STARTED /Users/x/.local/bin/claude --output-format stream-json --input-format stream-json --resume=$SESSION --add-dir /repo
  500   450 $STARTED /bin/sh -c bash "\$HOME/.claude/hooks/duplicate-session-guard"
  600     1 Sat Oct 17 16:33:19 2026 /Users/x/.local/bin/claude --output-format stream-json --input-format stream-json --session-id=$SESSION --add-dir /repo
  601   600 $STARTED /bin/zsh -c source /snap.sh 2>/dev/null || true\\012export A=1\\012: && eval 'remote-run wait -q 20261018-224625-266b36' < /dev/null && pwd -P >| /tmp/cwd
  602   601 $STARTED bash /Users/x/.local/bin/remote-run wait -q 20261018-224625-266b36
  700     1 $STARTED /Users/x/.local/bin/claude --input-format stream-json --resume=93da02d0-3826-41c8-9320-d28f5ac538c4
  800   400 $STARTED /Users/x/.local/bin/claude --input-format=stream-json --resume $SESSION
  900     1 $STARTED /usr/local/bin/claude-backup --input-format stream-json --resume=$SESSION
  901     1 $STARTED /Users/claude/bin/worker --input-format stream-json --resume=$SESSION
  902     1 $STARTED node $TEST_TMP/bin/claude --input-format stream-json -r $SESSION
  903     1 $STARTED /Users/x/.local/bin/claude --input-format stream-json --session-id=otra -p Explica --resume=$SESSION por favor
  904     1 $STARTED /Users/x/.local/bin/claude --input-format stream-json --resume=$SESSION --fork-session
  905     1 $STARTED /Users/x/.local/bin/claude --input-format stream-json -p -- --resume=$SESSION
  906     1 $STARTED /Users/x/.local/bin/claude --input-format stream-json --resume=$SESSION -p -- --fork-session
  907     1 $STARTED /Users/x/.local/bin/claude --resume=$SESSION
  908     1 $STARTED node /x/other/cli.js --input-format stream-json --resume=$SESSION
EOF
}

reports_orphan() {
    table
    local output
    output=$(hook "$PAYLOAD")
    printf '%s' "$output" | python3 -c '
import json, sys
value = json.load(sys.stdin)
context = value["hookSpecificOutput"]["additionalContext"]
assert value["hookSpecificOutput"]["hookEventName"] == "SessionStart"
assert value["systemMessage"] == "duplicate-session-guard: otro proceso (PID 600 800 902 906) corre esta conversación"
assert "- PID 600 (huérfano, su padre es el PID 1), arrancó el Sat Oct 17 16:33:19 2026." in context
assert "- PID 800 (padre 400)" in context
assert "   - PID 601: remote-run wait -q 20261018-224625-266b36\n" in context
assert "   - PID 602: bash /Users/x/.local/bin/remote-run wait -q 20261018-224625-266b36" in context
assert "- PID 902 (huérfano" in context
for absent in ("450", "700", "900", "901", "903", "904", "905", "907", "908"):
    assert f"PID {absent}" not in context, absent
assert "/tmp/t.jsonl" in context and "`kill 600 800 902 906`" in context
'
    assert grep -q 'PID 600 800 902 906 sigue corriendo' "$FAKE_NOTIFY"
}

silent_without_duplicate() {
    cat > "$FAKE_PS" <<EOF
  450     1 $STARTED /Users/x/.local/bin/claude --input-format stream-json --resume=$SESSION
  500   450 $STARTED /bin/sh -c hook
  700     1 $STARTED /Users/x/.local/bin/claude --input-format stream-json --resume=93da02d0-3826-41c8-9320-d28f5ac538c4
  710     1 $STARTED /usr/bin/grep --resume=$SESSION
EOF
    quiet "$PAYLOAD"
    assert test ! -e "$FAKE_NOTIFY"
}

silent_on_bad_input() {
    table
    quiet 'invalid json'
    quiet '{}'
    quiet '{"session_id":"a b|c"}'
    printf '#!/bin/bash\nexit 1\n' > "$TEST_TMP/stubs/ps"
    quiet "$PAYLOAD"
    cp "$TEST_TMP/ps-stub" "$TEST_TMP/stubs/ps"
    assert test ! -e "$FAKE_NOTIFY"
}

FAILURES=0 CASE=0
for test_case in reports_orphan silent_without_duplicate silent_on_bad_input; do
    CASE=$((CASE + 1))
    rm -f "$FAKE_NOTIFY"
    (set -e; "$test_case") > "$TEST_TMP/$test_case.log" 2>&1
    if [ "$?" = 0 ]; then printf 'PASS %s\n' "$test_case";
    else printf 'FAIL %s\n' "$test_case"; cat "$TEST_TMP/$test_case.log"; FAILURES=$((FAILURES + 1)); fi
done
printf '%s casos, %s fallos\n' "$CASE" "$FAILURES"
exit "$FAILURES"
