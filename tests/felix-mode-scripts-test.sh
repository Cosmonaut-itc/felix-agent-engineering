#!/bin/bash

# Los casos se invocan mediante el bucle de despacho al final.
# shellcheck disable=SC2329
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
SCRIPTS=$REPO/skills/felix-mode/scripts
TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/felix-mode-test.XXXXXX") || exit 1
TEST_TMP=$(cd "$TEST_TMP" && pwd -P) || exit 1
export HOME=$TEST_TMP/home XDG_STATE_HOME=$TEST_TMP/state
export CODEX_RUN_HOME=$TEST_TMP/codex-home
mkdir -p "$HOME" "$TEST_TMP/bin" "$TEST_TMP/cwd space" "$TEST_TMP/root space"
printf 'Brief de prueba.\nSegunda línea.\n' > "$TEST_TMP/brief.md"

cleanup() {
    local pid_file
    for pid_file in "$TEST_TMP"/run-*/pid "$TEST_TMP"/run-*/resume-*/pid; do
        [ -f "$pid_file" ] || continue
        "$SCRIPTS/codex-run" stop "$(dirname "$pid_file")" >/dev/null 2>&1 || true
    done
    rm -rf "$TEST_TMP"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cat > "$TEST_TMP/bin/codex" <<'FAKE'
#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
log = Path(os.environ['FAKE_LOG'])
log.mkdir(parents=True, exist_ok=True)
(log / 'stdin').write_text(sys.stdin.read(), encoding='utf-8')
(log / 'args.json').write_text(json.dumps(args), encoding='utf-8')
(log / 'cwd').write_text(os.getcwd(), encoding='utf-8')
(log / 'home').write_text(os.environ['CODEX_HOME'], encoding='utf-8')
def emit(event):
    print(json.dumps(event), flush=True)
if os.environ.get('FAKE_NO_SESSION') != '1':
    emit({'type': 'thread.started', 'thread_id': 'test-thread-123'})
emit({'type': 'turn.started'})
if os.environ.get('FAKE_CHILD') == '1':
    child_code = "import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nwhile True:\n p.write_text(str(time.time()))\n time.sleep(.1)"
    child = subprocess.Popen([sys.executable, '-c', child_code, str(log / 'heartbeat')])
    (log / 'child.pid').write_text(str(child.pid), encoding='utf-8')
time.sleep(float(os.environ.get('FAKE_SLEEP', '0.3')))
emit({'type': 'item.started', 'item': {'id': 'item_0', 'type': 'command_execution', 'status': 'in_progress'}})
for index, kind in enumerate(('command_execution', 'mcp_tool_call')):
    emit({'type': 'item.completed', 'item': {'id': str(index), 'type': kind, 'status': 'completed'}})
print('{línea inválida}', flush=True)
emit({'type': 'item.completed', 'item': {'id': 'answer', 'type': 'agent_message', 'text': 'Trabajo terminado.\nListo.'}})
if '-o' in args:
    Path(args[args.index('-o') + 1]).write_text('Trabajo terminado.\nListo.', encoding='utf-8')
emit({'type': 'turn.completed', 'usage': {'input_tokens': 42, 'output_tokens': 8}})
sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
FAKE
chmod +x "$TEST_TMP/bin/codex"
export CODEX_RUN_BIN=$TEST_TMP/bin/codex

run() { "$SCRIPTS/codex-run" "$@"; }
assert() { "$@" || { printf 'Falló la comprobación: %s\n' "$*" >&2; return 1; }; }
reject() {
    if run "$@" > "$TEST_TMP/rejected" 2>&1; then
        printf 'Se aceptó una invocación inválida: %s\n' "$*" >&2
        return 1
    fi
}
fixture() {
    OUT=$TEST_TMP/run-$CASE
    export FAKE_LOG=$TEST_TMP/log-$CASE
    unset FAKE_SLEEP FAKE_CHILD FAKE_NO_SESSION FAKE_EXIT
}
start() { run start --model gpt-6.1-sol --effort medium --out "$OUT" "$@" "$TEST_TMP/brief.md"; }
wait_for_file() {
    local count=0
    while [ ! -s "$1" ] && [ "$count" -lt 100 ]; do sleep 0.1; count=$((count + 1)); done
    assert test -s "$1"
}

validation() {
    fixture
    reject start --model sol --effort medium "$TEST_TMP/brief.md"
    reject start --model gpt-6-astra --effort xhigh "$TEST_TMP/brief.md"
    reject start --model gpt-6-astra --effort max "$TEST_TMP/brief.md"
    reject start --effort high "$TEST_TMP/brief.md"
    reject start --model gpt-6-astra "$TEST_TMP/brief.md"
    reject start --model gpt-6-astra --effort high --root "$TEST_TMP" "$TEST_TMP/brief.md"
    reject start --model gpt-6-astra --effort high --network "$TEST_TMP/brief.md"
    reject start --model gpt-6-astra --effort high --timeout 0 "$TEST_TMP/brief.md"
    reject wait absent --timeout nope
    run --help > "$TEST_TMP/help"
    assert grep -q 'gpt-6-astra --effort high --ephemeral' "$TEST_TMP/help"
    assert grep -q 'gpt-6.1-sol --effort medium --write' "$TEST_TMP/help"
}

lifecycle() {
    fixture
    export FAKE_SLEEP=1
    start --cwd "$TEST_TMP/cwd space" > "$TEST_TMP/launch"
    assert grep -Fx "out=$OUT" "$TEST_TMP/launch"
    assert grep -Eq '^pid=[0-9]+$' "$TEST_TMP/launch"
    assert cmp -s "$TEST_TMP/brief.md" "$OUT/brief.md"
    assert grep -q -- '-s read-only' "$OUT/cmd"
    assert grep -q -- '--skip-git-repo-check --json' "$OUT/cmd"
    assert grep -q -- '--signal=KILL 3600' "$OUT/cmd"
    assert grep -Fx 'sandbox=read-only' "$OUT/meta"
    assert grep -Fx 'timeout=3600' "$OUT/meta"
    assert grep -Fx 'ephemeral=0' "$OUT/meta"
    run status "$OUT" > "$TEST_TMP/status"
    assert grep -Fx 'state=running' "$TEST_TMP/status"
    run wait "$OUT" --timeout 15 > "$TEST_TMP/status"
    assert grep -Fx 'state=done exit=0' "$TEST_TMP/status"
    assert grep -Fx 'session=test-thread-123' "$TEST_TMP/status"
    assert grep -Fx 'tools=2' "$TEST_TMP/status"
    assert grep -Fx "last_message=$OUT/last-message.md" "$TEST_TMP/status"
    assert cmp -s "$FAKE_LOG/stdin" "$OUT/brief.md"
    assert test "$(cat "$FAKE_LOG/home")" = "$CODEX_RUN_HOME"
    assert test "$(cat "$FAKE_LOG/cwd")" = "$(cd "$TEST_TMP/cwd space" && pwd -P)"
    python3 - "$FAKE_LOG/args.json" <<'PY'
import json, sys
args = json.load(open(sys.argv[1]))
assert args[-1] == '-'
assert args[args.index('-c') + 1] == 'model_reasoning_effort="medium"'
assert not any('trust_level' in arg for arg in args)
PY
    reject start --model gpt-6.1-sol --effort medium --out "$OUT" "$TEST_TMP/brief.md"
}

write_options() {
    fixture
    start --write --root "$TEST_TMP/root space" --root "$TEST_TMP/cwd space" --network
    run wait "$OUT" --timeout 15
    python3 - "$FAKE_LOG/args.json" "$TEST_TMP/root space" "$TEST_TMP/cwd space" <<'PY'
import json, pathlib, sys
args = json.load(open(sys.argv[1]))
assert args[args.index('-s') + 1] == 'workspace-write'
config = [args[i + 1] for i, arg in enumerate(args) if arg == '-c']
roots = next(item.split('=', 1)[1] for item in config if item.startswith('sandbox_workspace_write.writable_roots='))
assert json.loads(roots) == [str(pathlib.Path(item).resolve()) for item in sys.argv[2:]]
assert 'sandbox_workspace_write.network_access=true' in config
PY
    assert grep -q 'sandbox_workspace_write.writable_roots=' "$OUT/cmd"
    assert grep -q 'sandbox_workspace_write.network_access=true' "$OUT/cmd"
}

resume_run() {
    fixture
    start --write --network --root "$TEST_TMP/root space" --cwd "$TEST_TMP/cwd space"
    run wait "$OUT" --timeout 15
    export FAKE_LOG=$TEST_TMP/resume-log-$CASE
    run resume "$OUT" "$TEST_TMP/brief.md" > "$TEST_TMP/resume-launch"
    local resumed=$OUT/resume-1
    assert grep -Fx "out=$resumed" "$TEST_TMP/resume-launch"
    run wait "$resumed" --timeout 15
    assert cmp -s "$resumed/brief.md" "$FAKE_LOG/stdin"
    assert cmp -s "$resumed/last-message.md" "$OUT/last-message.md"
    assert grep -q 'exec resume test-thread-123' "$resumed/cmd"
    assert test "$(cat "$FAKE_LOG/cwd")" = "$(cd "$TEST_TMP/cwd space" && pwd -P)"
    python3 - "$FAKE_LOG/args.json" <<'PY'
import json, sys
args = json.load(open(sys.argv[1]))
assert args[:3] == ['exec', 'resume', 'test-thread-123']
assert not {'-s', '-m', '-o', '-C'}.intersection(args)
assert 'sandbox_mode="workspace-write"' in args
assert 'model_reasoning_effort="medium"' in args
assert 'sandbox_workspace_write.network_access=true' in args
assert any(arg.startswith('sandbox_workspace_write.writable_roots=') for arg in args)
assert args[-1] == '-'
PY
    run resume "$OUT" "$TEST_TMP/brief.md"
    run wait "$OUT/resume-2" --timeout 15
    reject resume "$OUT/resume-1" "$TEST_TMP/brief.md"
    assert grep -q 'run original' "$TEST_TMP/rejected"
}

resume_rejections() {
    fixture
    start --ephemeral
    run wait "$OUT" --timeout 15
    assert grep -q -- '--ephemeral' "$OUT/cmd"
    reject resume "$OUT" "$TEST_TMP/brief.md"
    OUT=$OUT-no-session
    export FAKE_NO_SESSION=1
    start
    run wait "$OUT" --timeout 15
    reject resume "$OUT" "$TEST_TMP/brief.md"
}

stop_and_wait_timeout() {
    fixture
    export FAKE_SLEEP=120 FAKE_CHILD=1
    start
    wait_for_file "$FAKE_LOG/child.pid"
    wait_for_file "$FAKE_LOG/heartbeat"
    local result=0
    run wait "$OUT" --timeout 1 > "$TEST_TMP/status" || result=$?
    assert test "$result" = 124
    assert grep -Fx 'state=running' "$TEST_TMP/status"
    run stop "$OUT" > "$TEST_TMP/stopped"
    assert grep -Fx 'state=done exit=143' "$TEST_TMP/stopped"
    assert grep -Fx 'exit=143' "$OUT/done"
    run stop "$OUT"
    run wait "$OUT" --timeout 5
    wait_for_file "$FAKE_LOG/heartbeat"
    cp "$FAKE_LOG/heartbeat" "$TEST_TMP/heartbeat-before"
    sleep 0.5
    assert cmp -s "$FAKE_LOG/heartbeat" "$TEST_TMP/heartbeat-before"
}

stop_without_supervisor() {
    fixture
    export FAKE_SLEEP=120 FAKE_CHILD=1
    start
    wait_for_file "$FAKE_LOG/child.pid"
    wait_for_file "$OUT/child-pid"
    kill -KILL "$(cat "$OUT/pid")"
    sleep 0.3
    run status "$OUT" > "$TEST_TMP/status"
    assert grep -Fx 'state=running' "$TEST_TMP/status"
    run stop "$OUT" > "$TEST_TMP/stopped"
    assert grep -Fx 'state=done exit=143' "$TEST_TMP/stopped"
    assert test -z "$(ps -o pid= -p "$(cat "$FAKE_LOG/child.pid")")"
    cp "$FAKE_LOG/heartbeat" "$TEST_TMP/heartbeat-before"
    sleep 0.5
    assert cmp -s "$FAKE_LOG/heartbeat" "$TEST_TMP/heartbeat-before"
}

done_after_leftover_child() {
    fixture
    export FAKE_SLEEP=0.3 FAKE_CHILD=1
    start
    run wait "$OUT" --timeout 15 > "$TEST_TMP/status"
    assert grep -Fx 'state=done exit=0' "$TEST_TMP/status"
    assert test -z "$(ps -o pid= -p "$(cat "$FAKE_LOG/child.pid")")"
}

resume_while_resuming() {
    fixture
    start
    run wait "$OUT" --timeout 15
    export FAKE_LOG=$TEST_TMP/resume-log-$CASE FAKE_SLEEP=120
    run resume "$OUT" "$TEST_TMP/brief.md"
    reject resume "$OUT" "$TEST_TMP/brief.md"
    assert grep -q 'resume-1' "$TEST_TMP/rejected"
    assert test ! -e "$OUT/resume-2/pid"
    run stop "$OUT/resume-1"
}

execution_timeout() {
    fixture
    export FAKE_SLEEP=120
    start --timeout 1
    run wait "$OUT" --timeout 10 > "$TEST_TMP/status"
    assert grep -Fx 'state=done exit=137' "$TEST_TMP/status"
}

lost_and_nonzero() {
    fixture
    export FAKE_EXIT=7
    start
    run wait "$OUT" --timeout 15 > "$TEST_TMP/status"
    assert grep -Fx 'state=done exit=7' "$TEST_TMP/status"
    rm "$OUT/done"
    run wait "$OUT" --timeout 5 > "$TEST_TMP/status"
    assert grep -Fx 'state=lost' "$TEST_TMP/status"
}

default_out_and_home() {
    fixture
    unset CODEX_RUN_HOME
    local launched
    launched=$(TMPDIR=$TEST_TMP run start --model gpt-6-astra --effort high "$TEST_TMP/brief.md")
    OUT=$(printf '%s\n' "$launched" | sed -n 's/^out=//p')
    assert test "${OUT#"$TEST_TMP/codex-run/"}" != "$OUT"
    run wait "$OUT" --timeout 15
    assert test "$(cat "$FAKE_LOG/home")" = "$HOME/.config/codex-cli"
    assert grep -Fx "cwd=$(pwd -P)" "$OUT/meta"
}

hook() {
    printf '%s' "$1" | LAPTOP_GUARD_LAPTOP=${2:-1} "$SCRIPTS/laptop-guard"
}

laptop_warnings() {
    local command payload output
    for command in 'docker run alpine' 'docker compose up' 'docker start app' 'docker build .' orb orbctl 'open -a OrbStack' 'colima start' 'bun dev' 'pnpm run dev' 'npm run dev' 'yarn dev' 'next dev' vite 'bun test' 'pnpm test' 'npm run build' 'yarn e2e' 'pnpm gate' vitest jest 'playwright test' 'turbo run build' xcodebuild ./gradlew 'expo run:ios' postgres 'pg_ctl start' 'pnpm --filter web dev' 'scp file host:/tmp; npm test' 'ssh mini true; bun test' 'scp a mini:/tmp && npm run build' 'printf "ssh x"; docker compose up' $'ssh mini true\nbun test' $'ssh mini true &&\nbun test' $'ssh mini true;\n\nbun test' $'# Comprobar cambios\nbun test' $'echo "a\nb"\nbun test' $'npm run \\\nbuild'; do
        payload=$(python3 -c 'import json,sys; print(json.dumps({"tool_input":{"command":sys.argv[1]}}))' "$command")
        output=$(hook "$payload")
        printf '%s' "$output" | python3 -c '
import json, sys
value = json.load(sys.stdin)
assert "permissionDecision" not in value["hookSpecificOutput"]
assert value["hookSpecificOutput"] == {"hookEventName": "PreToolUse", "additionalContext": "Estás en la MacBook de Felix, que sólo edita y sirve Metro. Corre gates, pruebas, builds, contenedores, bases de datos y servidores en la Mac Mini o en Felix DESKTOP, como diga el AGENTS.md del repositorio. Si este comando es ligero y debe correr aquí, dilo en tu respuesta."}
assert value["systemMessage"] == "laptop-guard: trabajo pesado en la MacBook"
'
    done
}

laptop_silent() {
    local command payload
    for command in 'ssh desktop npm run build' 'remote-run npm test' 'cd repo && ssh mini "bun test"' 'FOO=1 ssh mini bun test' $'ssh mini \\\nbun test' 'expo start' 'npx expo start' 'npm install' 'docker ps' 'echo hola'; do
        payload=$(python3 -c 'import json,sys; print(json.dumps({"tool_input":{"command":sys.argv[1]}}))' "$command")
        assert test -z "$(hook "$payload")"
    done
    assert test -z "$(hook '{"tool_input":{"command":"npm test"}}' 0)"
    assert test -z "$(hook 'invalid json')"
    assert test -z "$(hook '{}')"
    assert test -z "$(hook '{"tool_input":{"command":null}}')"
}

laptop_detection() {
    local stubs=$TEST_TMP/detection
    mkdir -p "$stubs"
    cat > "$stubs/uname" <<'STUB'
#!/bin/bash
printf '%s\n' "$GUARD_OS"
STUB
    cat > "$stubs/pmset" <<'STUB'
#!/bin/bash
printf '%s\n' "$GUARD_BATTERY"
STUB
    chmod +x "$stubs/uname" "$stubs/pmset"
    export GUARD_OS=Linux GUARD_BATTERY=InternalBattery
    assert test -z "$(printf '{}' | env -u LAPTOP_GUARD_LAPTOP PATH="$stubs" /bin/bash "$SCRIPTS/laptop-guard")"
    export GUARD_OS=Darwin GUARD_BATTERY=AC
    assert test -z "$(printf '{}' | env -u LAPTOP_GUARD_LAPTOP PATH="$stubs" /bin/bash "$SCRIPTS/laptop-guard")"
    export GUARD_BATTERY=InternalBattery
    assert test -z "$(printf '{}' | env -u LAPTOP_GUARD_LAPTOP PATH="$stubs" /bin/bash "$SCRIPTS/laptop-guard")"
    assert test -n "$(printf '%s' '{"tool_input":{"command":"bun test"}}' | env -u LAPTOP_GUARD_LAPTOP PATH="$stubs:$PATH" /bin/bash "$SCRIPTS/laptop-guard")"
}

FAILURES=0 CASE=0
TEST_CASES=(validation)
case "$(uname)" in
    MINGW*|MSYS*|CYGWIN*)
        printf 'SKIP daemonización y grupos POSIX en Windows (Git Bash).\n'
        ;;
    *) TEST_CASES+=(lifecycle write_options resume_run resume_rejections stop_and_wait_timeout stop_without_supervisor done_after_leftover_child resume_while_resuming execution_timeout lost_and_nonzero default_out_and_home) ;;
esac
TEST_CASES+=(laptop_warnings laptop_silent laptop_detection)
for test_case in "${TEST_CASES[@]}"; do
    CASE=$((CASE + 1))
    (set -e; "$test_case") > "$TEST_TMP/$test_case.log" 2>&1
    result=$?
    if [ "$result" = 0 ]; then printf 'PASS %s\n' "$test_case";
    else printf 'FAIL %s\n' "$test_case"; cat "$TEST_TMP/$test_case.log"; FAILURES=$((FAILURES + 1)); fi
done
printf '%s casos, %s fallos\n' "$CASE" "$FAILURES"
exit "$FAILURES"
