#!/bin/bash

# Los casos se invocan mediante el bucle de despacho al final.
# shellcheck disable=SC2329
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
SCRIPTS=$REPO/skills/felix-mode/scripts
TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/felix-mode-test.XXXXXX") || exit 1
TEST_TMP=$(cd "$TEST_TMP" && pwd -P) || exit 1
trap 'rm -rf "$TEST_TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

assert() { "$@" || { printf 'Falló la comprobación: %s\n' "$*" >&2; return 1; }; }

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
for test_case in laptop_warnings laptop_silent laptop_detection; do
    CASE=$((CASE + 1))
    (set -e; "$test_case") > "$TEST_TMP/$test_case.log" 2>&1
    result=$?
    if [ "$result" = 0 ]; then printf 'PASS %s\n' "$test_case";
    else printf 'FAIL %s\n' "$test_case"; cat "$TEST_TMP/$test_case.log"; FAILURES=$((FAILURES + 1)); fi
done
printf '%s casos, %s fallos\n' "$CASE" "$FAILURES"
exit "$FAILURES"
