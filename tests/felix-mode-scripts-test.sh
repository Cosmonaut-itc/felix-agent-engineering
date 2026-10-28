#!/bin/bash

# Los casos se invocan mediante el bucle de despacho al final.
# shellcheck disable=SC2329
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
SCRIPTS=${CODEX_RUN_TEST_SCRIPTS:-$REPO/skills/felix-mode/scripts}
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
    # También limpiar herramientas separadas cuando se prueba una versión rota.
    python3 - "$TEST_TMP" <<'PY_CLEAN'
import os, pathlib, signal, subprocess, sys
for path in list(pathlib.Path(sys.argv[1]).glob('log-*/child.pid')) + list(pathlib.Path(sys.argv[1]).glob('log-*/codex.pid')):
    try:
        pid = int(path.read_text())
        command = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)],
                                 stdout=subprocess.PIPE, text=True).stdout
        if sys.argv[1] in command:
            os.kill(pid, signal.SIGKILL)
    except (OSError, ValueError):
        pass
PY_CLEAN
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
import signal
import subprocess
import sys
import time

if os.environ.get('FAKE_IGNORE_TERM') == '1':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
args = sys.argv[1:]
log = Path(os.environ['FAKE_LOG'])
log.mkdir(parents=True, exist_ok=True)
(log / 'codex.pid').write_text(str(os.getpid()), encoding='utf-8')
(log / 'stdin').write_text(sys.stdin.read(), encoding='utf-8')
(log / 'args.json').write_text(json.dumps(args), encoding='utf-8')
(log / 'cwd').write_text(os.getcwd(), encoding='utf-8')
(log / 'home').write_text(os.environ['CODEX_HOME'], encoding='utf-8')
def emit(event):
    print(json.dumps(event), flush=True)
if os.environ.get('FAKE_NO_SESSION') != '1':
    emit({'type': 'thread.started', 'thread_id': 'test-thread-123'})
emit({'type': 'turn.started'})
if os.environ.get('FAKE_CHILD') == '1' or os.environ.get('FAKE_TOOL') == '1':
    # El latido se reemplaza de forma atómica: matar al hijo nunca lo deja vacío.
    child_code = "import os,pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nt=p.with_name(p.name+'.tmp')\nwhile True:\n t.write_text(str(time.time()))\n os.replace(t,p)\n time.sleep(.1)"
    child = subprocess.Popen([sys.executable, '-c', child_code, str(log / 'heartbeat')],
                             start_new_session=os.environ.get('FAKE_TOOL') == '1')
    (log / 'child.pid').write_text(str(child.pid), encoding='utf-8')
    if '-o' in args:
        # Esperar a que el supervisor registre al hijo: el muestreo es el límite
        # declarado, no lo que estas pruebas comprueban.
        pids = Path(args[args.index('-o') + 1]).parent / 'pids'
        for _ in range(100):
            if pids.exists() and any(line.split(' ', 1)[0] == str(child.pid)
                                     for line in pids.read_text().splitlines()):
                break
            time.sleep(0.05)
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
    unset FAKE_SLEEP FAKE_CHILD FAKE_TOOL FAKE_NO_SESSION FAKE_EXIT FAKE_IGNORE_TERM
}
start() { run start --model gpt-6.1-sol --effort medium --out "$OUT" "$@" "$TEST_TMP/brief.md"; }
wait_for_absent() {
    local count=0
    while [ -e "$1" ] && [ "$count" -lt 100 ]; do sleep 0.1; count=$((count + 1)); done
    assert test ! -e "$1"
}
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
    assert grep -q 'gpt-6.1-sol --effort high --ephemeral' "$TEST_TMP/help"
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
    assert grep -Fx 'sandbox=read-only' "$OUT/meta"
    assert grep -Fx 'timeout=3600' "$OUT/meta"
    assert grep -Fx 'ephemeral=0' "$OUT/meta"
    assert test -s "$OUT/pid-start"
    wait_for_absent "$OUT/launching"
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
    assert_tool_dead
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
    assert grep -Fx 'state=lost' "$TEST_TMP/stopped"
    assert test ! -e "$OUT/done"
    assert_tool_dead
}

done_after_leftover_child() {
    fixture
    export FAKE_SLEEP=0.3 FAKE_CHILD=1
    start
    run wait "$OUT" --timeout 15 > "$TEST_TMP/status"
    assert grep -Fx 'state=done exit=0' "$TEST_TMP/status"
    assert_tool_dead
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

# Cada prueba de herramienta separada comprueba la ausencia antes de stop/cleanup.
assert_tool_dead() {
    python3 - "$FAKE_LOG/child.pid" <<'PY_DEAD'
import os, pathlib, subprocess, sys
pid = int(pathlib.Path(sys.argv[1]).read_text())
result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                        stdout=subprocess.PIPE, text=True)
assert not result.stdout.strip() or result.stdout.strip().startswith('Z'), result.stdout
PY_DEAD
    # Un hijo que murió antes de su primer latido no deja archivo que comparar.
    [ -e "$FAKE_LOG/heartbeat" ] || return 0
    cp "$FAKE_LOG/heartbeat" "$TEST_TMP/heartbeat-before-$CASE"
    sleep 0.4
    assert cmp -s "$FAKE_LOG/heartbeat" "$TEST_TMP/heartbeat-before-$CASE"
}

start_tool() {
    fixture
    export FAKE_SLEEP=120 FAKE_TOOL=1
    start "$@"
    wait_for_file "$FAKE_LOG/child.pid"
    wait_for_file "$FAKE_LOG/heartbeat"
}

stop_tool() {
    start_tool
    run stop "$OUT" > "$TEST_TMP/stopped"
    assert grep -Fx 'state=done exit=143' "$TEST_TMP/stopped"
    assert grep -Fx 'exit=143' "$OUT/done"
    assert_tool_dead
}

timeout_tool() {
    start_tool --timeout 1
    run wait "$OUT" --timeout 15 > "$TEST_TMP/status"
    assert grep -Fx 'state=done exit=137' "$TEST_TMP/status"
    assert_tool_dead
}

normal_exit_tool() {
    fixture
    export FAKE_SLEEP=0.3 FAKE_TOOL=1
    start
    wait_for_file "$OUT/done"
    assert grep -Fx 'exit=0' "$OUT/done"
    assert_tool_dead
}

killed_supervisor_tool() {
    start_tool
    # Esperar a que la identidad de la herramienta quede en el registro atómico.
    wait_for_file "$OUT/pids"
    local count=0 tool_pid
    tool_pid=$(cat "$FAKE_LOG/child.pid")
    while ! grep -q "^$tool_pid " "$OUT/pids" && [ "$count" -lt 100 ]; do
        sleep 0.1
        count=$((count + 1))
    done
    assert grep -q "^$tool_pid " "$OUT/pids"
    kill -KILL "$(cat "$OUT/pid")"
    sleep 0.3
    run status "$OUT" > "$TEST_TMP/status"
    assert grep -Fx 'state=running' "$TEST_TMP/status"
    run stop "$OUT" > "$TEST_TMP/stopped"
    assert grep -Fx 'state=lost' "$TEST_TMP/stopped"
    assert_tool_dead
}

# Sin supervisor, dos stop a la vez terminan lo registrado sin escribir en OUT.
concurrent_stop() {
    start_tool
    local count=0 tool_pid first=0 second=0
    tool_pid=$(cat "$FAKE_LOG/child.pid")
    while ! grep -q "^$tool_pid " "$OUT/pids" 2>/dev/null && [ "$count" -lt 100 ]; do
        sleep 0.1
        count=$((count + 1))
    done
    kill -KILL "$(cat "$OUT/pid")"
    sleep 0.3
    ls -A "$OUT" > "$TEST_TMP/files-before"
    cp "$OUT/pids" "$TEST_TMP/pids-before"
    run stop "$OUT" > "$TEST_TMP/stop-first" 2>&1 & first=$!
    run stop "$OUT" > "$TEST_TMP/stop-second" 2>&1 & second=$!
    wait "$first"
    wait "$second"
    ls -A "$OUT" > "$TEST_TMP/files-after"
    assert cmp -s "$TEST_TMP/files-before" "$TEST_TMP/files-after"
    assert cmp -s "$TEST_TMP/pids-before" "$OUT/pids"
    assert grep -Fx 'state=lost' "$TEST_TMP/stop-first"
    assert grep -Fx 'state=lost' "$TEST_TMP/stop-second"
    assert_tool_dead
}

# Ejecutar el supervisor real con fallos inyectados, sin modificar el módulo.
injected_worker() {
    python3 - "$SCRIPTS/codex-run-support.py" "$OUT" "$TEST_TMP/brief.md" "$1" <<'PY_INJECT'
import importlib.util, json, os, pathlib, signal, subprocess, sys, time
sys.dont_write_bytecode = True
module_path, directory, brief, mode = sys.argv[1:]
out = pathlib.Path(directory)
out.mkdir()
(out / 'brief.md').write_bytes(pathlib.Path(brief).read_bytes())
(out / 'meta').write_text('timeout=30\n')
(out / 'pid').write_text(str(os.getpid()))
started = subprocess.check_output(['ps', '-o', 'lstart=', '-p', str(os.getpid())],
                                  text=True, env=dict(os.environ, LC_ALL='C')).strip()
(out / 'pid-start').write_text(started + '\n')
(out / 'run.json').write_text(json.dumps(dict(
    command=['/nonexistent/codex-run-test'] if mode == 'enoent' else [os.environ['CODEX_RUN_BIN']],
    cwd=os.getcwd(),
    home=os.environ['CODEX_RUN_HOME'], timeout=30, resume=False, ephemeral=False)))
spec = importlib.util.spec_from_file_location('codex_run_support', module_path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
if mode == 'launch-window':
    original_popen = subprocess.Popen
    def die_after_launch(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        if args[0][0] == os.environ['CODEX_RUN_BIN']:
            deadline = time.monotonic() + 5
            while not (pathlib.Path(os.environ['FAKE_LOG']) / 'heartbeat').exists():
                assert time.monotonic() < deadline
                time.sleep(.02)
            os.kill(os.getpid(), signal.SIGKILL)
        return child
    subprocess.Popen = die_after_launch
elif mode == 'stuck':
    # El proceso registrado permanece vivo según la comprobación inyectada.
    m.process_alive = lambda pid, started, snapshot: True
elif mode == 'blind':
    # Fallan la foto inicial y la primera de limpieza; Codex ignora SIGTERM.
    original_snapshot = m.process_snapshot
    failures = [2]
    def failing_snapshot():
        if failures[0]:
            # Dejar que Codex ignore SIGTERM y lance su herramienta antes del fallo.
            deadline = time.monotonic() + 5
            while not (pathlib.Path(os.environ['FAKE_LOG']) / 'heartbeat').exists():
                assert time.monotonic() < deadline
                time.sleep(.02)
            failures[0] -= 1
            raise OSError('fallo inyectado ps')
        return original_snapshot()
    m.process_snapshot = failing_snapshot
if mode in ('exception', 'blind'):
    original_finish = m.finish
    def checked_finish(directory, code):
        # Comprobar la promesa justo antes de publicar done, sin esperar al return.
        for name in ('codex.pid', 'child.pid'):
            path = pathlib.Path(os.environ['FAKE_LOG']) / name
            if not path.exists():
                continue
            pid = int(path.read_text())
            result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                                    stdout=subprocess.PIPE, text=True)
            assert not result.stdout.strip() or result.stdout.strip().startswith('Z'), (
                'done antes de terminar ' + name + ': ' + result.stdout)
        original_finish(directory, code)
    m.finish = checked_finish
if mode == 'exception':
    original = pathlib.Path.write_text
    def broken_write(self, *args, **kwargs):
        if self == out / 'child-pid':
            # Dejar que Codex y su herramienta arranquen antes del fallo.
            deadline = time.monotonic() + 5
            while not (pathlib.Path(os.environ['FAKE_LOG']) / 'heartbeat').exists():
                assert time.monotonic() < deadline
                time.sleep(.02)
            raise OSError('fallo inyectado child-pid')
        return original(self, *args, **kwargs)
    pathlib.Path.write_text = broken_write
with (out / 'stderr.log').open('w') as log:
    original_stderr = sys.stderr
    sys.stderr = log
    try:
        m.worker(out)
    finally:
        sys.stderr = original_stderr
if mode == 'enoent':
    assert (out / 'done').read_text() == 'exit=127\n', 'falta done tras un Popen fallido'
    assert not (out / 'launching').exists() and not (out / 'stuck').exists()
    sys.exit(0)
if mode == 'stuck':
    assert (out / 'stuck').exists(), 'falta stuck'
    assert not (out / 'done').exists(), 'done sin confirmar terminación'
else:
    assert (out / 'done').read_text() == 'exit=127\n'
    assert 'fallo inyectado ' + ('ps' if mode == 'blind' else 'child-pid') in (out / 'stderr.log').read_text()
# La comprobación es posterior a worker, pero anterior a cualquier limpieza.
for name in ('codex.pid', 'child.pid'):
    path = pathlib.Path(os.environ['FAKE_LOG']) / name
    if path.exists():
        result = subprocess.run(['ps', '-o', 'stat=', '-p', path.read_text()],
                                stdout=subprocess.PIPE, text=True)
        assert not result.stdout.strip() or result.stdout.strip().startswith('Z'), result.stdout
PY_INJECT
}

impossible_confirmation() {
    fixture
    export FAKE_TOOL=1 FAKE_SLEEP=0.3
    injected_worker stuck
    run status "$OUT" > "$TEST_TMP/status"
    assert grep -Fx 'state=stuck' "$TEST_TMP/status"
    assert grep -Eq '^survivors=[0-9]+' "$TEST_TMP/status"
    run wait "$OUT" --timeout 1 > "$TEST_TMP/status"
    assert grep -Fx 'state=stuck' "$TEST_TMP/status"
    reject resume "$OUT" "$TEST_TMP/brief.md"
    assert grep -q 'quedó stuck' "$TEST_TMP/rejected"
    # stuck es definitivo: stop no lo convierte en done.
    reject stop "$OUT"
    assert grep -Fx 'state=stuck' "$TEST_TMP/rejected"
    assert test ! -e "$OUT/done"
}

popen_failure() {
    fixture
    injected_worker enoent
}

# Popen cuenta como superviviente aunque ninguna foto haya registrado a Codex.
codex_without_snapshot() {
    fixture
    export FAKE_TOOL=1 FAKE_SLEEP=120 FAKE_IGNORE_TERM=1
    injected_worker blind
    assert grep -Fx 'exit=127' "$OUT/done"
    assert_tool_dead
}

# Una consulta que coincide con el lanzamiento no puede concluir lost.
state_during_launch() {
    fixture
    mkdir -p "$OUT"
    printf 'timeout=30\n' > "$OUT/meta"
    printf '%s\n' "$$" > "$OUT/pid"
    python3 - "$SCRIPTS/codex-run-support.py" "$OUT" <<'PY_RACE'
import importlib.util, pathlib, sys
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('codex_run_support', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
out = pathlib.Path(sys.argv[2])
def launching_then_dead(directory):
    # El supervisor escribe launching y muere justo mientras state consulta.
    (directory / 'launching').write_text('launching\n')
    return False
m.supervisor_alive = launching_then_dead
assert m.state_detail(out) == ('stuck', 'survivors=unknown'), 'state concluyó sin ver launching'
assert not (out / 'stuck').exists(), 'state escribió en OUT'
PY_RACE
}

# Un grupo cuyo número coincide con child-pid no se atribuye al run.
foreign_group() {
    fixture
    mkdir -p "$OUT"
    python3 -c 'import os,time; os.setsid(); time.sleep(30)' &
    local foreign=$!
    # shellcheck disable=SC2064
    trap "kill -KILL $foreign 2>/dev/null || true" EXIT
    printf 'timeout=30\n' > "$OUT/meta"
    sleep 30 & local dead=$!; kill -KILL "$dead"; wait "$dead" 2>/dev/null || true
    printf '%s\n' "$dead" > "$OUT/pid"
    printf 'no-es-el-supervisor\n' > "$OUT/pid-start"
    printf '%s\n' "$foreign" > "$OUT/child-pid"
    printf '%s Thu Jan  1 00:00:00 1970\n' "$dead" > "$OUT/pids"
    sleep 0.3
    run status "$OUT" > "$TEST_TMP/status"
    assert grep -Fx 'state=lost' "$TEST_TMP/status"
    run stop "$OUT" > /dev/null
    local result=0
    kill -0 "$foreign" 2>/dev/null || result=1
    kill -KILL "$foreign" 2>/dev/null
    assert test "$result" = 0
}

post_launch_exception() {
    fixture
    export FAKE_TOOL=1 FAKE_SLEEP=120
    injected_worker exception
    assert grep -Fx 'exit=127' "$OUT/done"
    assert_tool_dead
}

launch_window() {
    fixture
    export FAKE_TOOL=1 FAKE_SLEEP=120
    local result=0
    injected_worker launch-window || result=$?
    assert test "$result" = 137
    assert test ! -e "$OUT/child-pid"
    assert test ! -e "$OUT/pids"
    run status "$OUT" > "$TEST_TMP/status"
    assert grep -Fx 'state=stuck' "$TEST_TMP/status"
    assert grep -Fx 'survivors=unknown' "$TEST_TMP/status"
    result=0
    run stop "$OUT" > "$TEST_TMP/stopped" 2>&1 || result=$?
    assert test "$result" != 0
    assert grep -Fx 'state=stuck' "$TEST_TMP/stopped"
    assert grep -Fx 'survivors=unknown' "$TEST_TMP/stopped"
    assert test ! -e "$OUT/done"
    run wait "$OUT" --timeout 1 > "$TEST_TMP/status"
    assert grep -Fx 'state=stuck' "$TEST_TMP/status"
}

foreign_supervisor_pid() {
    fixture
    python3 - "$SCRIPTS/codex-run-support.py" "$OUT" <<'PY_FOREIGN'
import importlib.util, os, pathlib, signal, subprocess, sys
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('supervisor', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
out = pathlib.Path(sys.argv[2])
out.mkdir()
(out / 'meta').write_text('timeout=30\n')
(out / 'pids').touch()
foreign = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', str(out)])
original_kill = os.kill
signals = []
def shield(pid, sig):
    if pid == foreign.pid and sig != 0:
        signals.append(sig)  # Proteger el proceso ajeno y registrar cualquier intento.
        return
    return original_kill(pid, sig)
try:
    (out / 'pid').write_text(str(foreign.pid))
    (out / 'pid-start').write_text('Thu Jan  1 00:00:00 1970\n')
    os.kill = shield
    observed = m.state(out)
    m.stop(out)
    assert observed == 'lost', observed
    assert not signals, signals
    assert foreign.poll() is None, 'señalizó el proceso ajeno'
finally:
    os.kill = original_kill
    foreign.kill()
    foreign.wait()
PY_FOREIGN
}

concurrent_resume() {
    fixture
    start
    run wait "$OUT" --timeout 15
    export FAKE_SLEEP=120
    python3 - "$SCRIPTS/codex-run-support.py" "$OUT" "$TEST_TMP/brief.md" "$TEST_TMP" <<'PY_CONCURRENT'
import os, pathlib, subprocess, sys, time
module, directory, brief, temporary = sys.argv[1:]
out = pathlib.Path(directory)
tmp = pathlib.Path(temporary)
# Pausar launch después de la comprobación. En la versión sin flock, los dos
# contendientes llegan aquí; con flock, el segundo espera antes de comprobar.
code = r"""
import fcntl, importlib.util, os, pathlib, sys, time
sys.dont_write_bytecode = True
module, directory, brief, temporary, label = sys.argv[1:]
tmp = pathlib.Path(temporary)
spec = importlib.util.spec_from_file_location('supervisor', module)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
original_flock = fcntl.flock
def observed_flock(fd, operation):
    (tmp / (label + '-lock')).touch()
    return original_flock(fd, operation)
fcntl.flock = observed_flock
original_launch = m.launch
def paused_launch(*args, **kwargs):
    (tmp / (label + '-launch')).touch()
    deadline = time.monotonic() + 15
    while not (tmp / 'release-resumes').exists():
        assert time.monotonic() < deadline, 'no se liberó launch'
        time.sleep(.02)
    return original_launch(*args, **kwargs)
m.launch = paused_launch
sys.argv = ['codex-run', 'resume', directory, brief]
try:
    sys.exit(m.main())
except (OSError, ValueError, KeyError) as error:
    print('codex-run: ' + str(error), file=sys.stderr)
    sys.exit(1)
"""
def wait_for_any(*paths):
    deadline = time.monotonic() + 15
    while not any(path.exists() for path in paths):
        assert time.monotonic() < deadline, paths
        time.sleep(.02)
processes = []
logs = []
try:
    for label in ('first', 'second'):
        log = (tmp / (label + '-resume.log')).open('w')
        logs.append(log)
        env = dict(os.environ, FAKE_LOG=str(tmp / ('resume-log-' + label)))
        processes.append(subprocess.Popen(
            [sys.executable, '-c', code, module, directory, brief, temporary, label],
            env=env, stdout=log, stderr=log))
        if label == 'first':
            wait_for_any(tmp / 'first-launch')
        else:
            wait_for_any(tmp / 'second-lock', tmp / 'second-launch')
    (tmp / 'release-resumes').touch()
    results = [process.wait(timeout=25) for process in processes]
    for log in logs:
        log.flush()
    details = '\n'.join((tmp / (label + '-resume.log')).read_text()
                        for label in ('first', 'second'))
    assert sorted(results) == [0, 1], (results, details)
    assert len(list(out.glob('resume-*/pid'))) == 1, details
    assert 'detén o espera' in details, details
finally:
    (tmp / 'release-resumes').touch()
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait()
    for log in logs:
        log.close()
PY_CONCURRENT
    run status "$OUT/resume-1" > "$TEST_TMP/status"
    assert grep -Fx 'state=running' "$TEST_TMP/status"
    assert test ! -e "$OUT/resume-2"
    run stop "$OUT/resume-1"
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

store_hook() {
    python3 -c 'import json,sys; print(json.dumps({"tool_name":sys.argv[1],"tool_input":{sys.argv[2]:sys.argv[3]},"cwd":sys.argv[4]}))' "$1" "$2" "$3" "${4:-$TEST_TMP}" \
        | "$SCRIPTS/store-guard"
}

store_warnings() {
    local base=$HOME/.claude/orchestrate store output
    store=$base/mev-1018
    mkdir -p "$store" "$base/sesión ñ" "$HOME/.claude/plugins"
    printf '{"plugins":{"pstack@pstack-claude":[{"installPath":"/p/0.9.95"}]}}' > "$HOME/.claude/plugins/installed_plugins.json"
    output=$(store_hook Write file_path "$store/state.md")
    printf '%s' "$output" | STORE="$store" python3 -c '
import json, os, shlex, sys
value = json.load(sys.stdin)
context = value["hookSpecificOutput"]["additionalContext"]
store = shlex.quote(os.environ["STORE"])
assert value["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
assert "no tiene units.tsv ni decisions.tsv." in context
assert f"`bun /p/0.9.95/skills/poteto-mode/scripts/orch/orch.ts --store {store} init`" in context
assert "`/p/0.9.95/skills/show-me-your-work/scripts/log.sh " in context and "/decisions.tsv start" in context
assert "/.claude/encargos/mev-1018, no en el store." in context
assert value["systemMessage"] == "store-guard: store de programa sin units.tsv o decisions.tsv"
'
    touch "$store/units.tsv"
    # shellcheck disable=SC2016
    output=$(store_hook Bash command 'printf x >> "$HOME"/.claude/orchestrate/mev-1018/todo.md')
    assert grep -q 'mev-1018 no tiene decisions.tsv. ' <<< "$output"
    assert test -z "$(grep 'orch.ts' <<< "$output")"
    assert grep -q 'mev-1018 no tiene' <<< "$(store_hook Bash command 'printf x > state.md' "$store/runs")"
    assert grep -q 'mev-1018 no tiene' <<< "$(store_hook Bash command 'touch mev-1018/state.md' "$base")"
    assert grep -q 'mev-1018 no tiene' <<< "$(store_hook Bash command 'touch .claude/orchestrate/mev-1018/state.md' "$HOME")"
    assert grep -q 'mev-1018 no tiene' <<< "$(store_hook Bash command 'bun orch.ts --store ~/.claude/orchestrate/mev-1018 unit set')"
    # shellcheck disable=SC2016
    assert grep -q 'mev-1018 no tiene' <<< "$(store_hook Bash command 'bun orch.ts --store="${HOME}/.claude/orchestrate/mev-1018" status')"
    assert grep -q 'mev-1018 no tiene' <<< "$(store_hook Bash command 'cat ~/.claude/orchestrate/mev-1018/units.tsv > /dev/null')"
    # shellcheck disable=SC2016
    output=$(store_hook Bash command 'printf x > "$HOME/.claude/orchestrate/sesión ñ/state.md"')
    assert grep -q 'sesión ñ no tiene' <<< "$output"
    assert grep -q "'$base/sesión ñ'" <<< "$output"
    assert grep -q 'sesión ñ no tiene' <<< "$(store_hook Write file_path "$base/sesión ñ/state.md")"
}

store_silent() {
    local base=$HOME/.claude/orchestrate
    mkdir -p "$base/completo" "$base/viejo" "$base/sesión"
    touch "$base/completo/units.tsv" "$base/completo/decisions.tsv"
    assert test -z "$(store_hook Edit file_path "$base/completo/status.md")"
    assert test -z "$(store_hook Write file_path "$base/no-existe/state.md")"
    assert test -z "$(store_hook Write file_path "$base/viejo/../fuera.md")"
    assert test -z "$(store_hook Write file_path "$HOME/repo/.claude/orchestrate.md")"
    assert test -z "$(store_hook Bash command 'printf x > ~/.claude/orchestrate/viejo/../completo/state.md')"
    # shellcheck disable=SC2016
    assert test -z "$(store_hook Bash command 'printf x > "$HOME/.claude/orchestrate/sesión-nuevo/state.md"')"
    assert test -z "$(store_hook Bash command 'cat ~/.claude/orchestrate/completo/units.tsv 2>/dev/null')"
    assert test -z "$(store_hook Bash command 'ls ~/.claude/orchestrate/')"
    assert test -z "$(store_hook Bash command 'mkdir -p nuevo && touch completo/state.md' "$base")"
    assert test -z "$(store_hook Bash command 'touch viejo/state.md' "$HOME")"
    assert test -z "$(store_hook Bash command 'echo "sin cerrar')"
    assert test -z "$(printf 'invalid json' | "$SCRIPTS/store-guard")"
    assert test -z "$(printf '{}' | "$SCRIPTS/store-guard")"
    assert test -z "$(printf '{"tool_input":{"command":null}}' | "$SCRIPTS/store-guard")"
}

merge_cases() {
    python3 - "$SCRIPTS/merge-guard" "$TEST_TMP" "$REPO" "$1" "$BASH" <<'PY_MERGE'
import json
import os
from pathlib import Path
import subprocess
import sys
import time

script, temporary, repo, mode, bash = sys.argv[1:]
base = Path(temporary) / ("merge-" + mode)
stubs = base / "bin"
stubs.mkdir(parents=True)
(base / "sub").mkdir()
fake = stubs / "gh-fake.py"
fake.write_text("""
import json, os, pathlib, sys, time
args = sys.argv[1:]
executed = False
if args[:2] == ["pr", "merge"]:
    disabled = False
    for arg in args[2:]:
        flag, equal, value = arg.partition("=")
        if flag == "--disable-auto":
            if not equal or value in ("1", "t", "T", "TRUE", "true", "True"):
                disabled = True
            elif value in ("0", "f", "F", "FALSE", "false", "False"):
                disabled = False
            else:
                sys.exit(1)
    executed = not disabled
with pathlib.Path(os.environ["MERGE_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd(),
                          "repo": os.environ.get("GH_REPO"), "executed": executed}) + "\\n")
time.sleep(float(os.environ.get("MERGE_SLEEP", "0")))
if os.environ.get("MERGE_INVALID_UTF8"):
    sys.stdout.buffer.write(bytes([255]))
    sys.exit(0)
print(os.environ["MERGE_JSON"])
sys.exit(int(os.environ.get("MERGE_EXIT", "0")))
""", encoding="utf-8")
if os.name != "nt":
    (stubs / "gh").write_text('#!/bin/bash\nexec python3 "$(dirname "$0")/gh-fake.py" "$@"\n', encoding="utf-8")
    (stubs / "gh").chmod(0o755)
(stubs / "gh.cmd").write_text('@echo off\r\npython "%~dp0gh-fake.py" %*\r\n', encoding="utf-8")
log = base / "calls"
env = dict(os.environ, PATH=str(stubs) + os.pathsep + os.environ["PATH"],
           MERGE_LOG=str(log), MERGE_EXIT="0", MERGE_SLEEP="0")
env.pop("GH_REPO", None)
env.pop("CDPATH", None)
env.pop("approved_head", None)
url = "https://github.com/Cosmonaut-itc/felix-agent-engineering/pull/30"
head = "--match-head-commit approved"
canonical = ("Ejecuta el merge en una llamada propia, con valores literales y sin redirecciones, "
             "tuberías, variables ni otros comandos: [cd <dir> &&] gh pr merge <pr> --repo <owner/repo> "
             "--squash --match-head-commit <head aprobado>.")
unparsed = ("Este comando menciona gh pr merge y merge-guard no puede analizarlo. " + canonical +
            " Si sólo buscas o citas el texto, escríbelo de otra forma, como gh pr merg[e], o pásalo en un archivo.")
crash = ("merge-guard falló al revisar este comando, que menciona gh pr merge. Revisa que lleve "
         "el head aprobado y que el PR tenga tu autorización si toca CI, dependencias o gobernanza "
         "de agentes. ¿Lo ejecuto?")
no_head = ("gh pr merge necesita --match-head-commit <head aprobado>, para que GitHub rechace "
           "el merge si el head cambió después de la review. Usa el head que aprobaron "
           "la review y el CI, no el que leas ahora del PR. Procedimiento: references/merge.md de felix-mode.")
outcomes = {
    "formato": ("deny", "merge-guard: comando bloqueado por formato; el agente debe corregirlo sin tu intervención"),
    "head": ("deny", "merge-guard: merge sin --match-head-commit"),
    "sensible": ("ask", "merge-guard: necesita la confirmación de Felix porque el PR toca CI, dependencias o gobernanza de agentes"),
    "lectura": ("ask", "merge-guard: necesita la confirmación de Felix porque no pude comprobar los archivos del PR"),
    "fallo": ("ask", "merge-guard: necesita la confirmación de Felix porque el hook falló"),
    "mixto": ("ask", "merge-guard: necesita la confirmación de Felix porque el PR toca CI, dependencias "
              "o gobernanza de agentes y no pude comprobar los archivos del PR"),
}

def files(paths, pr_url=url, count=None):
    env["MERGE_JSON"] = json.dumps({"url": pr_url, "files": [{"path": p} for p in paths],
                                   "changedFiles": len(paths) if count is None else count})

def invoke(command=None, raw=None):
    log.unlink(missing_ok=True)
    payload = raw if raw is not None else json.dumps({"cwd": str(base), "tool_input": {"command": command}})
    result = subprocess.run([bash, script], input=payload, text=True, capture_output=True, env=env)
    assert result.returncode == 0 and not result.stderr, result
    return json.loads(result.stdout) if result.stdout else None

def calls():
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

def decision(value, outcome, reason=None):
    assert value is not None, (outcome, reason)
    output = value["hookSpecificOutput"]
    kind, message = outcomes[outcome]
    expected = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": kind,
                "permissionDecisionReason": reason if reason is not None else output["permissionDecisionReason"]},
                "systemMessage": message}
    assert value == expected, value
    return output["permissionDecisionReason"]

files(["fleet/fleet/runner.py"])
if mode == "deny":
    for command in ("gh pr merge 30 --squash", "gh pr merge --squash --delete-branch",
                    "cd repo; gh pr merge 30 --squash", "GH_REPO=a/b gh pr merge 1",
                    f"gh pr merge 30 {head}; gh pr merge 31", "gh pr merge 30\n",
                    "cd sub && gh pr merge 30 --squash",
                    "gh pr merge 30 --match-head-commit", "gh pr merge 30 --match-head-commit=",
                    "/usr/bin/gh pr merge 30", "gh pr merge 30 \\\n--squash"):
        decision(invoke(command), "head", no_head)
        assert calls() == [], command
    decision(invoke("echo listo\ngh pr merge 30"), "formato", unparsed)
    assert calls() == []
    decision(invoke("gh pr merge 1; gh pr merge 2"), "head", no_head + "\n" + no_head)
    assert calls() == []
elif mode == "silent":
    for command in ("gh pr view 30", "git log --grep 'pr merge'", "git merge main",
                    "gh pr merge 30 --disable-auto"):
        assert invoke(command) is None, command
        assert calls() == [], command
    for command in ('echo "gh pr merge 30"', "echo gh pr merge 30", "ssh mini gh pr merge 30"):
        decision(invoke(command), "formato", unparsed)
        assert calls() == [], command
    for raw in ("invalid json", "{}", '{"tool_input":{"command":null}}',
                '{"tool_input":{"command":42}}'):
        assert invoke(raw=raw) is None
        assert calls() == []
    assert invoke(f"gh pr merge 30 --squash {head}") is None
    assert len(calls()) == 1
elif mode == "ask":
    for path, category in ((".github/workflows/check.yml", "CI"),
                           ("apps/web/package.json", "dependencias"), ("AGENTS.md", "gobernanza de agentes"),
                           ("apps/api/CLAUDE.md", "gobernanza de agentes"),
                           (".claude/settings.json", "gobernanza de agentes"),
                           ("skills/felix-mode/SKILL.md", "gobernanza de agentes")):
        files([path])
        reason = f"El PR {url} toca {category}: {path}. felix-mode lo detiene en merge-ready hasta tu autorización explícita. ¿Autorizas este merge?"
        decision(invoke(f"gh pr merge 30 {head}"), "sensible", reason)
    paths = [".github/workflows/check.yml", "apps/web/package.json", "AGENTS.md",
             "apps/api/CLAUDE.md", ".claude/settings.json", "skills/x/run.sh", "manifest"]
    files(paths)
    decision(invoke(f"gh pr merge 30 {head}"), "sensible",
             f"El PR {url} toca CI, dependencias, gobernanza de agentes: " + ", ".join(paths[:5]) +
             " (y 2 más). felix-mode lo detiene en merge-ready hasta tu autorización explícita. ¿Autorizas este merge?")
    env["MERGE_EXIT"] = "1"
    reason = decision(invoke(f"gh pr merge 30 {head}"), "lectura")
    assert reason == ("No pude comprobar si este PR toca CI, dependencias o gobernanza de agentes "
                      "(gh pr view falló). felix-mode pide tu autorización explícita para esos PR. ¿Autorizas este merge?")
    env["MERGE_EXIT"] = "0"
    files(["runner.py"], count=2)
    assert "(lista de archivos incompleta)" in decision(invoke(f"gh pr merge 30 {head}"), "lectura")
    for response in ("invalid", "{}", '{"url":null,"files":[],"changedFiles":0}'):
        env["MERGE_JSON"] = response
        assert "(respuesta JSON incompleta o inválida)" in decision(invoke(f"gh pr merge 30 {head}"), "lectura")
    files(["AGENTS.md"])
    reason = decision(invoke(f"gh pr merge 30 {head}; gh pr merge 31 {head}"), "sensible")
    assert len(calls()) == 2 and len(reason.splitlines()) == 2
elif mode == "invalid_utf8":
    env["MERGE_INVALID_UTF8"] = "1"
    reason = decision(invoke(f"gh pr merge 30 {head}"), "lectura")
    assert "(respuesta JSON incompleta o inválida)" in reason, reason
    assert len(calls()) == 1
elif mode == "scope":
    files(["skills/x/run.sh"], "https://github.com/other/repo/pull/1")
    assert invoke(f"gh pr merge 1 {head}") is None
    files(["manifest"], url.replace("Cosmonaut-itc", "cosmonaut-ITC"))
    decision(invoke(f"gh pr merge 30 {head}"), "sensible")
elif mode == "arguments":
    for flag in ("--repo a/b", "--repo=a/b", "-R a/b"):
        assert invoke(f"gh pr merge {flag} 30 {head}") is None
        assert calls()[0]["args"] == ["pr", "view", "30", "--repo", "a/b", "--json", "url,files,changedFiles"], calls()
    assert invoke(f"GH_REPO=a/b gh pr merge branch {head}") is None
    assert calls()[0]["repo"] == "a/b"
    assert calls()[0]["args"] == ["pr", "view", "branch", "--json", "url,files,changedFiles"]
    for command in (f"cd sub; gh pr merge 30 {head}",
                    f"cd sub; cd ..; gh pr merge {head}",
                    f"cd sub; GH_REPO=a/b gh pr merge 30 {head}",
                    f"cd sub; gh pr merge 30 --repo= {head}"):
        decision(invoke(command), "formato", unparsed)
        assert calls() == [], command
    assert invoke(f"cd sub; gh pr merge 30 --repo a/b {head}") is None
    assert calls()[0]["args"] == ["pr", "view", "30", "--repo", "a/b", "--json", "url,files,changedFiles"]
    assert Path(calls()[0]["cwd"]).resolve() == base.resolve()
    assert invoke(f'gh pr merge --body "some body" -b body --body-file file -F file --subject subject -t title --author-email a@b -A a@b --match-head-commit=approved https://github.com/a/b/pull/1') is None
    assert calls()[0]["args"] == ["pr", "view", "https://github.com/a/b/pull/1", "--json", "url,files,changedFiles"]
    for command in (f'gh pr merge 30 {head} > output',
                    'gh pr merge 30 --match-head-commit "$approved_head"',
                    'gh pr merge 30 --match-head-commit `printf approved`',
                    f"gh pr merge 30 {head} --body *",
                    f"gh pr merge 30 {head} --body ?",
                    f"gh pr merge 30 {head} --body [ab]",
                    f"gh pr merge 30 {head} --body {{a,b}}",
                    f"gh pr merge 30 {head} --body ~",
                    f"gh pr merge 30 {head} || gh pr merge 31 {head}",
                    f"gh pr merge 30 {head} & gh pr merge 31 {head}",
                    f"gh pr merge 30 {head} |& gh pr merge 31 {head}",
                    f"gh pr merge 30 {head} ;; gh pr merge 31 {head}",
                    f'"GH_REPO=a/b" gh pr merge 30 {head}'):
        decision(invoke(command), "formato", unparsed)
        assert calls() == [], command
    for body in ("'$literal `text` * ? [a] {a,b} ~ < >'", '"* ? [a] {a,b} ~ < >"'):
        assert invoke(f"gh pr merge 30 {head} --body {body}") is None
        assert len(calls()) == 1
    env["CDPATH"] = "/tmp"
    decision(invoke(f"cd sub; gh pr merge 30 {head}"), "formato", unparsed)
    assert calls() == []
    decision(invoke(f"cd ./sub; gh pr merge 30 {head}"), "formato", unparsed)
    assert calls() == []
    assert invoke(f"cd sub; gh pr merge 30 --repo a/b {head}") is None
    assert Path(calls()[0]["cwd"]).resolve() == base.resolve()
    env.pop("CDPATH")
    for space in ("\r", "\v", "\f", "\u00a0", "\u0085", "\u2028", "\u2029"):
        decision(invoke(f"gh pr merge 30 {head}{space}"), "formato", unparsed)
        assert calls() == [], repr(space)
    for command in ("gh pr merge 30 --disable-auto --foo",
                    f"gh pr merge 30 --disable-auto --disable-auto=true {head}"):
        decision(invoke(command), "formato", unparsed)
        assert calls() == [], command
elif mode == "unknown_flags":
    for flag in ("-Rowner/repo", "-sd", "--foo", "--auto=yes", "--disable-auto=false", "--admin", "--auto"):
        for command in (f"gh pr merge {flag} 30 {head}", f"gh pr merge {flag} 30"):
            decision(invoke(command), "formato",
                     f"gh pr merge lleva un flag que merge-guard no admite: {flag}. " + canonical)
            assert calls() == [], command
    for flag in ("--delete-branch", "-d", "--merge", "-m",
                 "--rebase", "-r", "--squash", "-s"):
        assert invoke(f"gh pr merge 30 {flag} {head}") is None, flag
        assert len(calls()) == 1
    for flag in ("--body", "-b", "--body-file", "-F", "--subject", "-t", "--author-email", "-A"):
        for value in (f"{flag} --foo", f"{flag}=--foo"):
            assert invoke(f"gh pr merge 30 {value} {head}") is None, value
            assert calls()[0]["args"][2] == "30", calls()
elif mode == "wrappers":
    for command in ("env GH_REPO=a/b gh pr merge 1", "command gh pr merge 1",
                    'bash -c "gh pr merge 1"', "exec gh pr merge 1", "nohup gh pr merge 1",
                    "time gh pr merge 1", 'sh -ec "gh pr merge 1"',
                    'zsh -c "gh pr merge 1"', "command env GH_REPO=a/b exec gh pr merge 1"):
        decision(invoke(command), "formato", unparsed)
        assert calls() == [], command
    for command in (f"env GH_REPO=a/b gh pr merge 1 {head}",
                    f'bash -lc "cd sub && gh pr merge 30 {head}"',
                    f'env GH_REPO=a/b bash -c "gh pr merge 30 {head}"',
                    f"env -u GH_REPO gh pr merge 30 {head}",
                    "env -i GH_REPO=a/b gh pr merge 1",
                    f'bash -c "cd sub"; gh pr merge 30 {head}'):
        decision(invoke(command), "formato", unparsed)
        assert calls() == [], command
elif mode == "deadline":
    env["MERGE_SLEEP"] = "6"
    files(["AGENTS.md"])
    for total in (2, 3):
        started = time.monotonic()
        command = "; ".join(f"gh pr merge {30 + index} {head}" for index in range(total))
        reason = decision(invoke(command), "mixto")
        elapsed = time.monotonic() - started
        assert elapsed < 12, elapsed
        assert len(calls()) == 2, calls()
        assert reason.count("(tiempo agotado)") == total - 1, reason
elif mode == "differential":
    files(["AGENTS.md"])
    (base / "work").mkdir()
    (stubs / "gh").write_text('#!/bin/bash\nexec python3 "$(dirname "$0")/gh-fake.py" "$@"\n', encoding="utf-8")
    (stubs / "gh").chmod(0o755)
    corpus = [
        ('gh pr merge 30 --squash', 'head'),
        ('gh pr merge 30 --squash --match-head-commit abc', 'sensible'),
        ('cd work && gh pr merge 30 --squash', 'head'),
        ('gh pr merge 30 --squash --body "primera\nsegunda"', 'formato'),
        ('gh pr mer\\\nge 30 --squash', 'head'),
        ('(echo listo); gh pr merge 30 --squash', 'formato'),
        ('command -- gh pr merge 30 --squash', 'formato'),
        ('time -p gh pr merge 30 --squash', 'formato'),
        ('if true; then gh pr merge 30 --squash; fi', 'formato'),
        ('for p in 30; do gh pr merge $p --squash; done', 'formato'),
        ('{ gh pr merge 30 --squash; }', 'formato'),
        ('true && gh pr merge 30 --squash', 'formato'),
        ('echo x|gh pr merge 30 --squash', 'formato'),
        ('bash -c "gh pr merge 30 --squash"', 'formato'),
        ('eval gh pr merge 30 --squash', 'formato'),
        ('f(){ gh pr merge 30 --squash; }; f', 'formato'),
        ('echo ";" gh pr merge 30', 'formato'),
        ("cat <<'EOT'\ngh pr merge 30\nEOT", 'formato'),
        ('echo "gh pr merge 30"', 'formato'),
        ('gh pr view 30', 'silent'),
        ("git log --grep 'pr merge'", 'silent'),
        ('gh pr merge 30 --body "primera\nsegunda"', 'formato'),
        ('bash -c "echo listo\ngh pr merge 30"', 'formato'),
        ('gh pr mer\\\nge 30', 'head'),
        ('(echo listo); gh pr merge 30', 'formato'),
        ('command -- gh pr merge 30', 'formato'),
        ('time -p gh pr merge 30', 'formato'),
        ('if true; then gh pr merge 30 --squash; fi', 'formato'),
        ('echo "\\\n"; gh pr merge 30', 'formato'),
        ('gh pr merge 30 --body "; & | ( )"', 'head'),
        ('gh pr merge 30 --body=";"', 'head'),
        ('gh pr merge 30 --disable-auto', 'silent'),
        ('bash -c "gh pr merge 30 --disable-auto"', 'formato'),
        ('echo "gh pr merge 30"; gh pr merge 30', 'formato'),
        ('gh pr merge 30 --body "unfinished', 'formato'),
        ('gh pr merge 30 --match-head-commit X', 'sensible'),
        ('gh pr merge 30 > --disable-auto', 'formato'),
        ('gh pr merge 30 --match-head-commit "$approved_head"', 'formato'),
        ('(cd sub); gh pr merge 30 --match-head-commit X', 'formato'),
        ('cd sub | cat; gh pr merge 30 --match-head-commit X', 'formato'),
        ('false && cd sub; gh pr merge 30 --match-head-commit X', 'formato'),
        ('export GH_REPO=sensitive/repo; gh pr merge 30 --match-head-commit X', 'formato'),
        ('f() {\n gh pr merge 30\n}\necho done', 'formato'),
        ('cd sub && gh pr merge 30 --match-head-commit X', 'formato'),
        ('cd sub; gh pr merge 30 --match-head-commit X', 'formato'),
        ('GH_REPO=a/b gh pr merge 30 --match-head-commit X', 'sensible'),
        ('gh pr merge 30 --squash --disable-auto --disable-auto=false', 'formato'),
        ('cd missing; cd ..; gh pr merge 30 --squash --match-head-commit X', 'formato'),
        ('cd sub; cd -; gh pr merge 30 --squash --match-head-commit X', 'formato'),
        ('cd missing && gh pr merge 30 --squash', 'head'),
        ('gh pr merge 30 --squash --disable-auto=false', 'formato'),
        ('cd missing; cd ..; gh pr merge 30 --repo a/b --squash --match-head-commit X', 'sensible'),
        ('cd sub; cd -; gh pr merge 30 -R a/b --match-head-commit X', 'sensible'),
        ('cd sub && gh pr merge 30 --repo a/b --match-head-commit X', 'sensible'),
        ('cd sub; gh pr merge 30 --repo a/b --match-head-commit X', 'sensible'),
        ('gh pr merge 30 --squash --match-head-commit X', 'formato'),
        ('gh pr merge 30 --squash --match-head-commit X\r', 'formato'),
        ('cd sub; gh pr merge 30 --repo old/repo --repo=a/b --match-head-commit X', 'sensible'),
        ('cd sub; gh pr merge 30 --repo=old/repo -R a/b --match-head-commit X', 'sensible'),
        ('cd sub; gh pr merge 30 -R old/repo --repo a/b --match-head-commit X', 'sensible'),
    ]
    payload = json.dumps({"cwd": ["invalid"], "tool_input": {"command": "cd sub; gh pr merge 30"}})
    decision(invoke(raw=payload), "fallo", crash)
    assert calls() == []
    def identity(args):
        selector, repo, index = None, None, 2
        valued = {"--repo", "-R", "--match-head-commit", "--body", "-b",
                  "--body-file", "-F", "--subject", "-t", "--author-email", "-A", "--json"}
        while index < len(args):
            arg = args[index]
            flag, equal, value = arg.partition("=")
            if flag in valued:
                if not equal:
                    index += 1
                    value = args[index]
                if flag in {"--repo", "-R"}:
                    repo = value
            elif not arg.startswith("-") and selector is None:
                selector = arg
            index += 1
        return selector, repo

    for values, expected_merge, expected_exit in (
            *(([value], False, 0) for value in ("1", "t", "T", "TRUE", "true", "True")),
            *(([value], True, 0) for value in ("0", "f", "F", "FALSE", "false", "False")),
            (["invalid"], False, 1), (["false", None], False, 0),
            ([None, "false"], True, 0), (["invalid", "false"], False, 1)):
        log.unlink(missing_ok=True)
        flags = ["--disable-auto" + ("=" + value if value is not None else "") for value in values]
        result = subprocess.run([bash, "-c", "gh pr merge 30 " + " ".join(flags)],
                                cwd=base, env=env, capture_output=True, timeout=10)
        assert result.returncode == expected_exit, (values, result)
        assert any(call["executed"] for call in calls()) == expected_merge, (values, calls())

    for command, expected in corpus:
        log.unlink(missing_ok=True)
        subprocess.run([bash, "-c", command], cwd=base, env=env, capture_output=True, timeout=10)
        bash_calls = [call for call in calls() if call["args"][:2] == ["pr", "merge"]]
        executed = any(call["executed"] for call in bash_calls)
        value = invoke(command)
        actual = value["hookSpecificOutput"]["permissionDecision"] if value else "silent"
        assert not executed or value is not None, ("FUGA", command)
        if expected == "silent":
            assert value is None, command
        else:
            reason = decision(value, expected)
            assert expected != "formato" or reason.endswith(canonical) or reason == unparsed, (command, reason)
        for view, merge in zip(calls(), bash_calls):
            assert view["args"][:2] == ["pr", "view"], view
            selector, repo = identity(merge["args"])
            if repo is not None:
                assert identity(view["args"]) == (selector, repo), (command, view, merge)
            else:
                assert Path(view["cwd"]).resolve() == Path(merge["cwd"]).resolve(), (command, view, merge)
                assert view["repo"] == merge["repo"], (command, view, merge)
        assert not calls() or len(calls()) == len(bash_calls), (command, calls(), bash_calls)
        if expected == "formato":
            assert calls() == [], command
        print(json.dumps({"command": command, "merge": executed, "decision": actual}, ensure_ascii=False))
elif mode == "manifest":
    for line in (Path(repo) / "manifest").read_text().splitlines():
        columns = line.split()
        if not columns or columns[0] not in {"file", "skill"}:
            continue
        paths = [columns[1]] if columns[0] == "file" else [columns[1] + "/SKILL.md", columns[1] + "/otro.md"]
        for path in paths:
            files([path])
            reason = decision(invoke(f"gh pr merge 30 {head}"), "sensible")
            assert "gobernanza de agentes: " + path + "." in reason, reason
else:
    raise AssertionError(mode)
PY_MERGE
}

merge_deny() { merge_cases deny; }
merge_silent() { merge_cases silent; }
merge_ask() { merge_cases ask; }
merge_scope() { merge_cases scope; }
merge_arguments() { merge_cases arguments; }
merge_manifest() { merge_cases manifest; }
merge_unknown_flags() { merge_cases unknown_flags; }
merge_wrappers() { merge_cases wrappers; }
merge_deadline() { merge_cases deadline; }
merge_differential() { merge_cases differential; }
merge_invalid_utf8() { merge_cases invalid_utf8; }

FAILURES=0 CASE=0
TEST_CASES=(validation)
case "$(uname)" in
    MINGW*|MSYS*|CYGWIN*)
        printf 'SKIP daemonización, grupos POSIX y store-guard en Windows (Git Bash).\n'
        ;;
    *) TEST_CASES+=(lifecycle write_options resume_run resume_rejections stop_and_wait_timeout stop_without_supervisor done_after_leftover_child resume_while_resuming execution_timeout stop_tool timeout_tool normal_exit_tool killed_supervisor_tool concurrent_stop impossible_confirmation post_launch_exception popen_failure codex_without_snapshot state_during_launch foreign_group launch_window foreign_supervisor_pid concurrent_resume lost_and_nonzero default_out_and_home store_warnings store_silent) ;;
esac
TEST_CASES+=(laptop_warnings laptop_silent laptop_detection merge_deny merge_silent merge_ask merge_scope merge_arguments merge_manifest merge_unknown_flags merge_wrappers merge_deadline merge_differential merge_invalid_utf8)
# Permite repetir sólo las regresiones contra una copia temporal de otra versión.
if [ -n "${CODEX_RUN_TEST_CASES:-}" ]; then
    read -r -a TEST_CASES <<< "$CODEX_RUN_TEST_CASES"
fi
for test_case in "${TEST_CASES[@]}"; do
    CASE=$((CASE + 1))
    (set -e; "$test_case") > "$TEST_TMP/$test_case.log" 2>&1
    result=$?
    if [ "$result" = 0 ]; then
        printf 'PASS %s\n' "$test_case"
        if [ "$test_case" = merge_differential ]; then cat "$TEST_TMP/$test_case.log"; fi
    else printf 'FAIL %s\n' "$test_case"; cat "$TEST_TMP/$test_case.log"; FAILURES=$((FAILURES + 1)); fi
done
printf '%s casos, %s fallos\n' "$CASE" "$FAILURES"
exit "$FAILURES"
