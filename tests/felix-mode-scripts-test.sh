#!/bin/bash

# Los casos se invocan mediante el bucle de despacho al final.
# shellcheck disable=SC2329
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
SCRIPTS=${CODEX_RUN_TEST_SCRIPTS:-$REPO/skills/felix-mode/scripts}
TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/felix-mode-test.XXXXXX") || exit 1
TEST_TMP=$(cd "$TEST_TMP" && pwd -P) || exit 1
export HOME=$TEST_TMP/home XDG_STATE_HOME=$TEST_TMP/state
export XDG_CONFIG_HOME=$TEST_TMP/config XDG_CACHE_HOME=$TEST_TMP/cache XDG_DATA_HOME=$TEST_TMP/data
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
start() { run start --model gpt-6.2-sol --effort medium --out "$OUT" "$@" "$TEST_TMP/brief.md"; }
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
    reject start --model gpt-6.2-astra --effort xhigh "$TEST_TMP/brief.md"
    reject start --model gpt-6.2-astra --effort max "$TEST_TMP/brief.md"
    reject start --effort high "$TEST_TMP/brief.md"
    reject start --model gpt-6.2-astra "$TEST_TMP/brief.md"
    reject start --model gpt-6.2-astra --effort high --root "$TEST_TMP" "$TEST_TMP/brief.md"
    reject start --model gpt-6.2-astra --effort high --network "$TEST_TMP/brief.md"
    reject start --model gpt-6.2-astra --effort high --timeout 0 "$TEST_TMP/brief.md"
    reject wait absent --timeout nope
    run --help > "$TEST_TMP/help"
    assert grep -q 'gpt-6.2-sol --effort high --ephemeral' "$TEST_TMP/help"
    assert grep -q 'gpt-6.2-sol --effort medium --write' "$TEST_TMP/help"
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
    reject start --model gpt-6.2-sol --effort medium --out "$OUT" "$TEST_TMP/brief.md"
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
    launched=$(TMPDIR=$TEST_TMP run start --model gpt-6.2-astra --effort high "$TEST_TMP/brief.md")
    OUT=$(printf '%s\n' "$launched" | sed -n 's/^out=//p')
    assert test "${OUT#"$TEST_TMP/codex-run/"}" != "$OUT"
    run wait "$OUT" --timeout 15
    assert test "$(cat "$FAKE_LOG/home")" = "$HOME/.config/codex-cli"
    assert grep -Fx "cwd=$(pwd -P)" "$OUT/meta"
}

hook() {
    printf '%s' "$1" | LAPTOP_GUARD_LAPTOP=${2:-1} "$SCRIPTS/laptop-guard"
}

rerun_guard() {
    python3 - "$SCRIPTS/rerun-guard" "$TEST_TMP/rerun" "$BASH" <<'PY_RERUN'
import json
import os
from pathlib import Path
import subprocess
import sys

script, temporary, bash = sys.argv[1:]
base = Path(temporary)
base.mkdir()
assert Path(script).is_file() and os.access(script, os.X_OK), 'falta rerun-guard ejecutable'

def raw(payload, extra_env=None):
    result = subprocess.run([bash, script], input=payload, text=True, capture_output=True,
                            env=dict(os.environ, **(extra_env or {})))
    assert result.returncode == 0 and result.stderr == '', result
    return result.stdout

def check(command, deny=False, ambiguous=False, tool='Bash'):
    output = raw(json.dumps({'tool_name': tool, 'tool_input': {'command': command}}))
    if not deny:
        assert output == '', (command, output)
        return
    value = json.loads(output)
    decision = value['hookSpecificOutput']
    assert decision['hookEventName'] == 'PreToolUse', value
    assert decision['permissionDecision'] == 'deny', (command, value)
    reason = decision['permissionDecisionReason']
    assert 'gh run rerun' in reason and '--failed' in reason, reason
    if ambiguous:
        assert 'gh run reru[n]' in reason and 'analizar' in reason, reason
    elif 'gh api' in command:
        assert 'rerun-failed-jobs' in reason, reason
    else:
        assert '--job' in reason and 'arranques' in reason, reason
    assert value['systemMessage'].startswith('rerun-guard:'), value

for command in (
    'gh run rerun 123', 'gh run rerun', 'cd "repo space" && gh run rerun 123',
    'gh run rerun --repo owner/repo 123', 'gh run rerun 123 --repo owner/repo',
    'gh run rerun -R owner/repo 123', 'gh -R owner/repo run rerun 123',
    'true; gh run rerun 123; echo listo', 'gh run rerun 123 --failed && gh run rerun 456',
    'true\ngh run rerun 123', 'GH_REPO=owner/repo gh run rerun 123',
    'gh run rerun 123 --log-failed', 'gh run rerun 123 -- --failed',
    'gh run rerun 123 --failed=false', 'gh run rerun 123 --failed --failed=false',
    "cat <<'EOF' && gh run rerun 123\nhola\nEOF\n",
    "bash <<'EOF'\ngh run rerun 123\nEOF\n",
):
    check(command, deny=True)
endpoint = 'repos/owner/repo/actions/runs/123/rerun'
for command in (
    'gh api -X POST ' + endpoint, 'gh api ' + endpoint + ' --method POST',
    'gh api --method=POST /' + endpoint, 'gh api -XPOST ' + endpoint,
    'cd repo && gh api ' + endpoint + ' -X POST; echo listo',
    'gh api ' + endpoint + ' -f hello=world',
    'gh api https://api.github.com/' + endpoint + ' -X POST',
    'gh api -X POST ' + endpoint + ' > result.json',
    'gh api --cache 1h -X POST ' + endpoint,
    'gh api -X POST repos/{owner}/{repo}/actions/runs/123/rerun',
):
    check(command, deny=True)
for command in (
    'gh run rerun 123 --failed', 'gh run rerun --failed -R owner/repo 123',
    'gh run rerun --repo owner/repo --failed', 'gh run rerun 123 --job 456',
    'gh run rerun --job=456 123', 'gh run rerun --job 456 -R owner/repo 123',
    'gh run rerun 123 --failed; gh run rerun 456 --job 789',
    'gh api -X POST repos/owner/repo/actions/runs/123/rerun-failed-jobs',
    'gh api -X POST repos/owner/repo/actions/jobs/456/rerun',
    'gh api -X GET ' + endpoint, 'gh api ' + endpoint,
    'gh workflow run check.yml', 'git status',
    'echo "gh run rerun 123"', "grep 'gh run rerun' README.md",
    "printf '%s\\n' 'gh run rerun 123' > example.txt",
    "cat <<'EOF' > example.txt\ngh run rerun 123\nEOF\n",
    'cat <<"EOF" > example.txt\ngh run rerun 123\nEOF\n',
    'echo "gh api -X POST ' + endpoint + '"',
    '# gh run rerun 123\necho listo',
):
    check(command)
for command in ('bash -c "gh run rerun 123"', 'echo "$(gh run rerun 123)"',
                'gh run rerun 123 "', 'wrapper gh run rerun 123'):
    check(command, deny=True, ambiguous=True)
check('gh run rerun 123', tool='Read')
for payload in ('not json', '{}', '{"tool_input":{"command":null}}', '[]'):
    assert raw(payload) == ''
# No python3, y un intérprete roto: ambos deben fallar abiertos y en silencio.
empty = base / 'empty'
empty.mkdir()
assert raw(json.dumps({'tool_input': {'command': 'gh run rerun 123'}}),
           {'PATH': str(empty)}) == ''
broken = base / 'broken'
broken.mkdir()
(broken / 'python3').write_text('#!/bin/bash\necho fallo >&2\nexit 1\n')
(broken / 'python3').chmod(0o755)
assert raw(json.dumps({'tool_input': {'command': 'gh run rerun 123'}}),
           {'PATH': str(broken)}) == ''
print('rerun-guard: denegaciones, excepciones, citas y fallos abiertos verificados')
PY_RERUN
}

launcher_guard() {
    python3 - "$SCRIPTS/launcher-guard" "$TEST_TMP/launcher" <<'PY_LAUNCHER'
import json
import os
from pathlib import Path
import subprocess
import sys

hook, base = sys.argv[1], Path(sys.argv[2])
base.mkdir()
env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
           GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], env=env,
                                   stderr=subprocess.DEVNULL, text=True).strip()
def commit(repo, path, subject):
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open('a') as stream:
        stream.write(subject + '\n')
    git(repo, 'add', '--', path)
    git(repo, 'commit', '-qm', subject)
    return git(repo, 'rev-parse', 'HEAD')
def raw(payload, extra_env=None):
    result = subprocess.run([hook], input=payload, text=True, capture_output=True,
                            env=dict(env, **(extra_env or {})))
    assert result.returncode == 0, result
    assert result.stderr == '', result.stderr
    return result.stdout

def check(cwd, command, count=0, ref='origin/HEAD', sha=None, subject=None, path='scripts/mini/run.sh'):
    output = raw(json.dumps({'cwd': str(cwd), 'tool_input': {'command': command}}))
    if not count:
        assert output == '', (command, output)
        return
    value = json.loads(output)  # También exige un único aviso.
    noun = 'commit' if count == 1 else 'commits'
    expected = (f'El lanzador de este checkout ({path}) va {count} {noun} detrás de {ref}; '
                f'el más reciente es {sha[:7]} «{subject}». Un lanzador viejo no trae los arreglos '
                'de fila, espera y cancelación, y el host no puede corregirlo. '
                f'Incorpora {ref} en esta rama antes de lanzar, o di en tu respuesta por qué necesitas esta versión.')
    assert value == {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': expected},
                     'systemMessage': f'launcher-guard: lanzador detrás de {ref}'}, value

origin, clone = base / 'origin', base / 'clone space'
origin.mkdir()
git(origin, 'init', '-q', '-b', 'main')
initial = commit(origin, 'scripts/mini/run.sh', 'inicial')
commit(origin, 'scripts/mini/remote-run', 'cliente')
commit(origin, 'scripts/test-hosts/run-remote.sh', 'otro cliente')
commit(origin, 'scripts/remote-run', 'directo')
git(base, 'clone', '-q', str(origin), str(clone))
command = 'bash scripts/mini/run.sh --suite X'
check(clone, command)
commit(origin, 'ajeno.txt', 'ajeno')
git(clone, 'fetch', '-q', 'origin')
check(clone, command)
commit(origin, 'scripts/mini/helper', 'primero')
latest = commit(origin, 'scripts/mini/run.sh', 'espera y cancelación')
git(clone, 'fetch', '-q', 'origin')
for cmd in (command, 'scripts/mini/remote-run start -- cmd',
            'REMOTE_RUN_HOST=desk scripts/mini/remote-run wait ID',
            'bash scripts/mini/run.sh; scripts/mini/remote-run start -- cmd',
            f'cd "{clone}" && bash scripts/mini/run.sh',
            'true\nbash scripts/mini/run.sh',
            f'bash "{clone}/scripts/mini/run.sh"', 'scripts/mini/run.sh; true'):
    check(base if cmd.startswith('cd ') else clone, cmd, 2, sha=latest, subject='espera y cancelación',
          path='scripts/mini/remote-run' if 'scripts/mini/remote-run' in cmd and 'scripts/mini/run.sh' not in cmd else 'scripts/mini/run.sh')
git(clone, 'worktree', 'add', '-qb', 'secondary', str(base / 'secondary'), 'HEAD')
check(base / 'secondary', command, 2, sha=latest, subject='espera y cancelación')
# Un candidato al día precede al primero atrasado.
check(clone, './scripts/test-hosts/run-remote.sh desktop; ' + command, 2,
      sha=latest, subject='espera y cancelación')
other = commit(origin, 'scripts/test-hosts/run-remote.sh', 'hosts')
git(clone, 'fetch', '-q', 'origin')
check(clone, './scripts/test-hosts/run-remote.sh desktop', 1, sha=other,
      subject='hosts', path='scripts/test-hosts/run-remote.sh')
check(clone, 'scripts/remote-run start -- cmd')
latest = commit(origin, 'scripts/remote-run', 'directo nuevo')
git(clone, 'fetch', '-q', 'origin')
check(clone, 'scripts/remote-run start -- cmd', 1, sha=latest,
      subject='directo nuevo', path='scripts/remote-run')
# origin/HEAD gana incluso cuando main tiene cambios posteriores.
git(clone, 'update-ref', 'refs/remotes/origin/delivery', git(clone, 'rev-parse', 'HEAD'))
git(clone, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/delivery')
check(clone, command)
git(clone, 'symbolic-ref', '--delete', 'refs/remotes/origin/HEAD')
check(clone, command, 2, ref='origin/main', sha=git(origin, 'log', '-1', '--format=%H', '--', 'scripts/mini'), subject='espera y cancelación')
# Estos silencios se prueban con historia pendiente para evitar falsos positivos.
(clone / 'scripts/mini/run-remote.sh').write_text('sin seguimiento\n')
check(clone, 'scripts/mini/run-remote.sh desktop')
check(clone, 'remote-run start -- cmd')
for payload in ('invalid json', '{}', '{"tool_input":{"command":null}}'):
    assert raw(payload) == ''
for cmd in ('bash scripts/mini/run.sh "', 'cd "$UNKNOWN" && ' + command):
    check(clone, cmd)
git(clone, 'merge', '-q', '--ff-only', 'origin/main')
check(clone, command)
git(clone, 'switch', '-qc', 'feature')
commit(clone, 'scripts/mini/run.sh', 'cambio de rama')
check(clone, command)
git(clone, 'checkout', '-q', '--detach', initial)
check(clone, command)
git(clone, 'switch', '-q', 'feature')
git(clone, 'update-ref', '-d', 'refs/remotes/origin/main')
check(clone, command)
# Fuera de git, sin seguimiento, PATH y entrada inválida: silencio y exit 0.
(base / 'remote-run').write_text('cliente\n')
check(base, './remote-run start -- cmd')
check(clone, 'remote-run start -- cmd')
(base / 'bin').mkdir()
(base / 'bin' / 'git').write_text('#!/bin/sh\nprintf called > "$GUARD_GIT_LOG"\nexit 1\n')
(base / 'bin' / 'git').chmod(0o755)
assert raw(json.dumps({'cwd': str(clone), 'tool_input': {'command': 'echo hola'}}),
           {'PATH': str(base / 'bin') + os.pathsep + env['PATH'],
            'GUARD_GIT_LOG': str(base / 'git-called')}) == ''
assert not (base / 'git-called').exists()
empty = base / 'empty'
empty.mkdir()
git(empty, 'init', '-q')
(empty / 'run.sh').write_text('sin commits\n')
check(empty, 'bash run.sh')
assert raw(json.dumps({'cwd': str(clone), 'tool_input': {'command': command}}),
           {'PATH': str(base / 'bin')}) == ''  # Sin python3.
(base / 'bin' / 'python3').symlink_to(sys.executable)
assert raw(json.dumps({'cwd': str(clone), 'tool_input': {'command': command}}),
           {'PATH': str(base / 'bin'), 'GUARD_GIT_LOG': str(base / 'git-called')}) == ''  # git falla.
assert (base / 'git-called').read_text() == 'called'
(base / 'only-python').mkdir()
(base / 'only-python' / 'python3').symlink_to(sys.executable)
assert raw(json.dumps({'cwd': str(clone), 'tool_input': {'command': command}}),
           {'PATH': str(base / 'only-python')}) == ''  # Sin git.
print('launcher-guard: avisos, rutas, referencias y tolerancia a errores verificados')
PY_LAUNCHER
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
import json, os, shlex, subprocess, sys
value = json.load(sys.stdin)
context = value["hookSpecificOutput"]["additionalContext"]
assert value["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
assert "no tiene units.tsv ni decisions.tsv." in context
header = shlex.quote("id\\ttrack\\tstate\\tbranch\\tpr\\tsha\\tbrief\\n")
units = os.path.join(os.environ["STORE"], "units.tsv")
step = f"printf {header} > {shlex.quote(units)}"
assert f"`{step}`" in context
subprocess.run(["bash", "-c", step], check=True)
with open(units) as handle:
    assert handle.read() == "id\ttrack\tstate\tbranch\tpr\tsha\tbrief\n"
assert "`/p/0.9.95/skills/show-me-your-work/scripts/log.sh " in context and "/decisions.tsv start" in context
assert "/.claude/encargos/mev-1018, no en el store." in context
assert value["systemMessage"] == "store-guard: store de programa sin units.tsv o decisions.tsv"
'
    touch "$store/units.tsv"
    # shellcheck disable=SC2016
    output=$(store_hook Bash command 'printf x >> "$HOME"/.claude/orchestrate/mev-1018/todo.md')
    assert grep -q 'mev-1018 no tiene decisions.tsv. ' <<< "$output"
    assert test -z "$(grep 'printf' <<< "$output")"
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
    assert grep -q "'$base/sesión ñ/units.tsv'" <<< "$output"
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
if os.environ.get("MERGE_TEST_ORIGIN"):
    # These controls must resolve an actual unenrolled repo, not an unknown cwd.
    subprocess.run(["git", "init", "-q", str(base)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(base), "remote", "add", "origin",
                    os.environ["MERGE_TEST_ORIGIN"]], check=True, capture_output=True)
    (base / "repo").mkdir()
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
        if columns[1] == "fleet/tren/pao-tren":
            # El CLI instalado ejecuta también sus módulos y adaptadores.
            paths += ["fleet/tren/" + m for m in ("dominio.py", "ledger.py", "observador.py",
                                                  "instalar.py", "adaptadores/hasito.py")]
        for path in paths:
            files([path])
            reason = decision(invoke(f"gh pr merge 30 {head}"), "sensible")
            assert "gobernanza de agentes: " + path + "." in reason, reason
else:
    raise AssertionError(mode)
PY_MERGE
}

merge_train() {
    python3 - "$SCRIPTS/merge-guard" "$TEST_TMP" "$BASH" <<'PY_TRAIN'
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

script, temporary, bash = sys.argv[1:]
base = Path(temporary) / "train"
repo = base / "repo"
repo.mkdir(parents=True)
env = dict(os.environ, HOME=str(base / "home"), XDG_CONFIG_HOME=str(base / "config"),
           XDG_STATE_HOME=str(base / "state"))
registry = Path(env["XDG_CONFIG_HOME"]) / "pao-tren" / "inscritos"
registry.parent.mkdir(parents=True)

def git(*args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

git("init", "-q", "-b", "main")
git("remote", "add", "origin", "git@github.com:SEN-HASO/Hasito.git")

def invoke(command, denied):
    payload = json.dumps({"cwd": str(repo), "tool_input": {"command": command}})
    result = subprocess.run([bash, script], input=payload, text=True, capture_output=True, env=env)
    assert result.returncode == 0 and not result.stderr, result
    if denied:
        output = json.loads(result.stdout)["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny", (command, output)
        assert "repo inscrito en el tren: usa pao-tren entregar" in output["permissionDecisionReason"], (command, output)
    else:
        assert not result.stdout, (command, result.stdout)

pushes = ["git push origin main", "git push origin HEAD:main",
          "git push origin " + "a" * 40 + ":refs/heads/main",
          "git push --force-with-lease origin HEAD:main",
          "git push --force-with-lease=refs/heads/main:" + "b" * 40 + " origin HEAD:refs/heads/main",
          "git push origin +HEAD:main", "git push origin :main",
          "git push --delete origin main", "git push origin refs/heads/main",
          "git push origin", "git push --all origin", "git push --mirror origin",
          "git -C " + shlex.quote(str(repo)) + " push origin HEAD:main",
          "cd " + shlex.quote(str(repo)) + " && git push origin HEAD:main",
          "cd " + shlex.quote(str(repo)) + "\ngit push origin HEAD:main",
          "bash -c 'git push origin HEAD:main'",
          "sh -ec 'git push --force-with-lease origin HEAD:main'"]
# Sin archivo no interviene en pushes, ni siquiera si falta origin.
for command in pushes:
    invoke(command, False)
registry.write_text("# piloto\n  sen-haso/HASITO  # inscrito\n\n", encoding="utf-8")
for command in pushes:
    invoke(command, True)
for command in ["gh pr merge 1 --repo SEN-HASO/Hasito --squash --match-head-commit approved",
                "gh pr merge 1 -Rsen-haso/hasito", "gh pr merge 1 --repo=sen-haso/hasito",
                "gh pr merge 1 --squash --match-head-commit approved"]:
    invoke(command, True)
invoke("git push origin HEAD:feature", False)
invoke("git push origin feature", False)
invoke("git push --tags origin", False)
# El repo explícito prevalece sobre origin y no requiere consultar GitHub.
invoke("gh pr merge 1 --repo otro/repo --disable-auto", False)
git("remote", "set-url", "origin", "https://github.com/otro/repo.git")
for command in pushes:
    invoke(command, False)
invoke("gh pr merge 1 --disable-auto", False)
git("remote", "remove", "origin")
invoke("git push origin HEAD:main", True)
invoke("git push origin HEAD:feature", True)
# El fallback de HOME funciona cuando XDG_CONFIG_HOME no está fijado.
env.pop("XDG_CONFIG_HOME")
fallback = Path(env["HOME"]) / ".config" / "pao-tren" / "inscritos"
fallback.parent.mkdir(parents=True)
fallback.write_text("sen-haso/hasito\n", encoding="utf-8")
git("remote", "add", "origin", "ssh://git@github.com/SEN-HASO/Hasito.git")
invoke("git push origin HEAD:main", True)
PY_TRAIN
}

# Reproducciones Sol U5-1..5 y Opus 1,1b,2,2b,3,4,5,7,8,9.
# Se acumulan los fallos para que la comparación con 523799a6 muestre cada fuga.
merge_train_regressions() {
    python3 - "$SCRIPTS/merge-guard" "$TEST_TMP" "$BASH" <<'PY_TRAIN_R1'
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

script, temporary, bash = sys.argv[1:]
base = Path(temporary) / "train-r1"
base.mkdir()
env = dict(os.environ, HOME=str(base / "home"), XDG_CONFIG_HOME=str(base / "config"),
           XDG_STATE_HOME=str(base / "state"), XDG_CACHE_HOME=str(base / "cache"),
           XDG_DATA_HOME=str(base / "data"))
for key in list(env):
    if key.startswith("GIT_") or key == "GH_REPO":
        env.pop(key)
for name in ("home", "config", "state", "cache", "data", "bin"):
    (base / name).mkdir()
real_git = shutil.which("git")
registry = base / "config/pao-tren/inscritos"
registry.parent.mkdir()
# No GitHub writes: only the hook runs, with gh pr view simulated.
gh = base / "bin/gh"
gh.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps({\"url\":\"https://github.com/otro/repo/pull/1\",\"files\":[{\"path\":\"src/plain.ts\"}],\"changedFiles\":1}))\n")
gh.chmod(0o755)
env["PATH"] = str(base / "bin") + ":" + env["PATH"]

def git(repo, *args, input=None):
    return subprocess.run([real_git, "-C", str(repo), *args], env=env, input=input,
                          text=True, capture_output=True, check=True).stdout.strip()

def mkrepo(name, remote, branch="feature"):
    repo = base / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", branch)
    git(repo, "remote", "add", "origin", remote)
    return repo

enrolled = mkrepo("enrolled", "git@github.com:SEN-HASO/Hasito.git")
other = mkrepo("other", "https://github.com/otro/repo.git")
hasito = mkrepo("hasito", "git@github.com:SEN-HASO/Hasito.git", "main")
head = "a" * 40
merge = "gh pr merge 1 --squash --match-head-commit " + head
failures = []

def probe(name, command, cwd=enrolled, expected="deny", reason=None, extra=None):
    result = subprocess.run([bash, script], input=json.dumps({"cwd":str(cwd),
                            "tool_input":{"command":command}}), text=True,
                            capture_output=True, env=dict(env, **(extra or {})), timeout=20)
    try:
        output = json.loads(result.stdout)["hookSpecificOutput"] if result.stdout else {}
        actual = output.get("permissionDecision", "allow")
        assert result.returncode == 0 and not result.stderr, result
        assert actual == expected, (actual, expected, output)
        if reason:
            assert reason in output.get("permissionDecisionReason", ""), output
    except (AssertionError, ValueError, KeyError) as error:
        failures.append(name)
        print("FAIL", name, repr(command), cwd.name, str(error), flush=True)
    else:
        print("PASS", name, flush=True)

# Controls: no registry retains the old guard, including head/flag/sensitive gates.
probe("no-file-push", "git push origin HEAD:main", expected="allow")
probe("no-file-merge-head", merge + " --repo SEN-HASO/Hasito", expected="allow")
probe("no-file-merge-no-head", "gh pr merge 1 --repo SEN-HASO/Hasito --squash", reason="--match-head-commit")
registry.write_text("sen-haso/hasito\n")
probe("other-control-push", "git push origin HEAD:main", other, "allow")
probe("other-control-merge", merge + " --repo otro/repo", other, "allow")
probe("other-merge-no-head", "gh pr merge 1 --repo otro/repo --squash", other, reason="--match-head-commit")
for name, command in [
    ("Sol-U5-1-Opus-2-url-ssh", "git push git@github.com:SEN-HASO/Hasito.git HEAD:main"),
    ("Opus-2b-url-https", "git push https://github.com/SEN-HASO/Hasito.git HEAD:main"),
    ("Sol-U5-1-explicit-repo", "git push --repo=git@github.com:SEN-HASO/Hasito.git HEAD:main"),
    ("explicit-repo-separated", "git push --repo git@github.com:SEN-HASO/Hasito.git HEAD:main"),
    ("Sol-U5-2-compact-C", "git -C" + str(enrolled) + " push origin HEAD:main"),
    ("Sol-U5-2-env-git-dir", "GIT_DIR=" + str(enrolled / ".git") + " git push origin HEAD:main"),
    ("git-dir-separated", "git --git-dir " + str(enrolled / ".git") + " push origin HEAD:main"),
    ("git-dir-equal", "git --git-dir=" + str(enrolled / ".git") + " push origin HEAD:main"),
    ("work-tree-options", "git --git-dir=" + str(enrolled / ".git") + " --work-tree " + str(enrolled) + " push origin HEAD:main"),
    ("work-tree-equal", "git --git-dir=" + str(enrolled / ".git") + " --work-tree=" + str(enrolled) + " push origin HEAD:main"),
    ("env-wrapper", "env GIT_DIR=" + str(enrolled / ".git") + " GIT_WORK_TREE=" + str(enrolled) + " git push origin HEAD:main"),
    ("env-work-tree", "GIT_WORK_TREE=" + str(enrolled) + " git --git-dir=" + str(enrolled / ".git") + " push origin HEAD:main"),
    ("C-separated", "git -C " + str(enrolled) + " push origin HEAD:main"),
]:
    probe(name, command, other, reason="repo inscrito en el tren")
probe("Sol-U5-1-other-destination", "git push https://github.com/otro/repo.git HEAD:main", expected="allow")
git(other, "remote", "add", "train", "git@github.com:SEN-HASO/Hasito.git")
git(enrolled, "remote", "add", "other", "https://github.com/otro/repo.git")
probe("named-enrolled-remote", "git push train HEAD:main", other)
probe("named-unenrolled-remote", "git push other HEAD:main", expected="allow")
# U5-r3-1: a configured remote name may contain a slash; its URL decides.
git(other, "remote", "add", "team/hasito", "git@github.com:SEN-HASO/Hasito.git")
git(enrolled, "remote", "add", "team/otro", "https://github.com/otro/repo.git")
probe("U5-r3-1-slash-enrolled-remote", "git push team/hasito HEAD:main", other)
probe("U5-r3-1-slash-unenrolled-remote", "git push team/otro HEAD:main", expected="allow")
# Push URL rather than fetch URL defines enrollment.
git(other, "remote", "set-url", "--push", "origin", "git@github.com:SEN-HASO/Hasito.git")
probe("push-url", "git push origin HEAD:main", other)
git(other, "remote", "set-url", "--push", "origin", "https://github.com/otro/repo.git")
probe("Sol-U5-3-Opus-3-pr-url", "gh pr merge https://github.com/SEN-HASO/Hasito/pull/1 --squash --match-head-commit " + head, other)
probe("Sol-U5-3-GH_REPO", "GH_REPO=SEN-HASO/Hasito " + merge, other)
probe("inherited-GH_REPO", merge, other, extra={"GH_REPO":"SEN-HASO/Hasito"})
for flag in ("--repo github.com/SEN-HASO/Hasito", "-R SEN-HASO/Hasito", "--repo https://github.com/SEN-HASO/Hasito"):
    probe("Opus-4-host-repo-" + flag, merge + " " + flag, other)
probe("Opus-1-head", "git push origin HEAD", hasito)
probe("Opus-1b-arroba", "git push -u origin @", hasito)
probe("Opus-5-cd-comment", "cd " + str(hasito) + " # entra al repo\ngit push origin HEAD:main", other)
# H8: quoted text that spans lines is ordinary work, not an unparseable push.
heredoc_commit = "git commit -m \"$(cat <<'EOF'\nfix: merge-guard niega push a main\n\nEOF\n)\""
heredoc_pr = "gh pr create --title t --body \"$(cat <<'EOF'\nNo hace push ni merge.\nEOF\n)\""
for cwd in (other, hasito):
    probe("H8-commit-heredoc-" + cwd.name, heredoc_commit, cwd, "allow")
    probe("H8-pr-create-heredoc-" + cwd.name, heredoc_pr, cwd, "allow")
    probe("H8-commit-multiline-" + cwd.name, "git commit -m \"uno\ndos sobre push\"", cwd, "allow")
# A quoted or mid-word # is not a comment and must not hide the next command.
probe("H8-quoted-hash", "git commit -m \"a # b\"\ngit push origin HEAD:main", hasito)
probe("H8-echo-hash", "echo '#'; git push origin HEAD:main", hasito)
probe("H8-word-hash", "echo a#b; git push origin HEAD:main", hasito)
probe("feature-HEAD", "git push origin HEAD", expected="allow")
probe("unknown-merge", merge, base, reason="No puedo resolver")
probe("unknown-push", "git push origin HEAD:main", base, reason="No puedo resolver")

# Sol block B: contrast actual Git push destinations through a local dry-run.
tree = git(enrolled, "hash-object", "-t", "tree", "-w", "--stdin", input="")
commit = git(enrolled, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit-tree", tree, input="fixture\n")
git(enrolled, "update-ref", "refs/heads/feature", commit)
bare = base / "transport.git"
subprocess.run([real_git, "init", "-q", "--bare", str(bare)], check=True, env=env)
git(enrolled, "config", "push.default", "upstream")
git(enrolled, "config", "branch.feature.remote", "origin")
git(enrolled, "config", "branch.feature.merge", "refs/heads/main")
def dry_case(name, command, *args):
    probe(name, command)
    result = subprocess.run([real_git, "-C", str(enrolled), "-c", "url.file://" + str(bare) + ".insteadOf=git@github.com:SEN-HASO/Hasito.git", "push", "--dry-run", *args], env=env, text=True, capture_output=True, check=True)
    assert "-> main" in result.stderr, result
    print("GIT DRY-RUN", name, result.stderr.strip(), flush=True)
dry_case("Sol-U5-4-implicit-upstream", "git push")
dry_case("Sol-U5-4-implicit-upstream-origin", "git push origin", "origin")
git(enrolled, "symbolic-ref", "HEAD", "refs/heads/main")
git(enrolled, "update-ref", "refs/heads/main", commit)
dry_case("Sol-U5-4-HEAD-on-main", "git push origin HEAD", "origin", "HEAD")
git(enrolled, "symbolic-ref", "HEAD", "refs/heads/feature")
git(enrolled, "config", "push.default", "current")
probe("current-feature", "git push", expected="allow")
git(enrolled, "config", "remote.origin.push", "HEAD:main")
probe("remote-push-config", "git push")
git(enrolled, "config", "--unset", "remote.origin.push")
git(enrolled, "config", "push.default", "matching")
probe("matching", "git push", reason="No puedo resolver")
git(enrolled, "checkout", "--detach", "-q", commit)
probe("detached-default", "git push", reason="No puedo resolver")
probe("detached-HEAD", "git push origin HEAD", reason="No puedo resolver")
git(enrolled, "symbolic-ref", "HEAD", "refs/heads/feature")
# U12: sombra deja pasar por el guard habitual; activo/desconocido prevalece.
registry_before_shadow = registry.read_text()
shadow_repo = mkrepo("shadow-u12", "https://github.com/owner/project.git", "main")
shadow_merge = merge + " --repo owner/project"
for contents, denied in (
    ("OWNER/PROJECT SoMbRa # doble camino\n", False),
    ("owner/project\n", True),
    ("owner/project activo\n", True),
    ("owner/project desconocido\n", True),
    ("owner/project sombra extra\n", True),
    ("owner/project sombra\nowner/project\n", True),
    ("owner/project otro\nowner/project sombra\n", True),
):
    registry.write_text(contents)
    expected = "deny" if denied else "allow"
    for command in ("git push origin HEAD:main", "git push", "git push origin main",
                    "git push https://github.com/owner/project.git HEAD:main",
                    "git -C " + str(shadow_repo) + " push origin HEAD:main", shadow_merge):
        probe("U12-modo-" + repr(contents) + "-" + command, command, shadow_repo, expected)
registry.write_text("owner/project sombra\n")
probe("U12-sombra-head-obligatorio", "gh pr merge 1 --repo owner/project --squash",
      shadow_repo, reason="--match-head-commit")
probe("U12-sombra-flags", shadow_merge + " --admin", shadow_repo, reason="flag")
gh.write_text(gh.read_text().replace("src/plain.ts", ".github/workflows/ci.yml"))
probe("U12-sombra-sensible", shadow_merge, shadow_repo, "ask", reason="toca CI")
gh.write_text(gh.read_text().replace(".github/workflows/ci.yml", "src/plain.ts"))
# Un destino activo entre varias push URLs sigue negado aunque otro sea sombra.
registry.write_text("owner/project sombra\nactive/project\n")
git(shadow_repo, "remote", "set-url", "--add", "--push", "origin", "https://github.com/owner/project.git")
git(shadow_repo, "remote", "set-url", "--add", "--push", "origin", "https://github.com/active/project.git")
probe("U12-destino-mixto", "git push origin HEAD:main", shadow_repo)
registry.write_text("owner/project sombra\n")
registry.chmod(0)
probe("U12-sombra-ilegible-push", "git push origin HEAD:main", shadow_repo, reason="ilegible")
probe("U12-sombra-ilegible-merge", shadow_merge, shadow_repo, reason="ilegible")
registry.chmod(0o600)
registry.write_text(registry_before_shadow)
# Every unreadable form must deny mutations and leave ordinary commands alone.
for state in ("000", "utf8", "directory"):
    if state == "000":
        registry.chmod(0)
    elif state == "utf8":
        registry.chmod(0o600)
        registry.write_bytes(b"sen-haso/hasito\n\xff")
    else:
        registry.rename(registry.with_name("saved"))
        registry.mkdir()
    probe("Sol-U5-5-Opus-7-8-9-" + state + "-push", "git push origin HEAD:main", hasito, reason="ilegible")
    probe("Sol-U5-5-" + state + "-push", "git push origin HEAD:main", reason="ilegible")
    probe("Sol-U5-5-" + state + "-merge-no-head", "gh pr merge 1 --repo SEN-HASO/Hasito --squash", reason="--match-head-commit")
    probe(state + "-feature-push", "git push origin HEAD:feature", hasito, reason="ilegible")
    probe(state + "-merge-head", merge + " --repo otro/repo", other, reason="ilegible")
    # Legacy denies still run: their specific reason must remain in the output.
    probe(state + "-other-merge-no-head", "gh pr merge 1 --repo otro/repo --squash", other, reason="--match-head-commit")
    probe(state + "-unknown-flag", merge + " --repo otro/repo --admin", other, reason="flag")
    probe(state + "-ordinary", "git status", other, "allow")
# Preserve the directory fixture and restore a readable registry without deletion.
registry.rename(registry.with_name("directory-fixture"))
registry.with_name("saved").rename(registry)
registry.write_text("sen-haso/hasito\n")
gh.write_text(gh.read_text().replace("src/plain.ts", ".github/workflows/ci.yml"))
registry.chmod(0)
probe("unreadable-sensitive-keeps-legacy", merge + " --repo otro/repo", other, reason="toca CI")
registry.chmod(0o600)
gh.write_text(gh.read_text().replace(".github/workflows/ci.yml", "src/plain.ts"))
# Timeout reproductions use the same sleeping git wrapper as Sol block A.
wrapper = base / "bin/git"
wrapper.write_text("#!/bin/sh\nsleep 3\nexit 1\n")
wrapper.chmod(0o755)
probe("Sol-U5-3-timeout-merge", merge, reason="No puedo resolver")
probe("Sol-U5-3-timeout-push", "git push origin HEAD:main", reason="No puedo resolver")
print(str(len(failures)) + " regresiones fallidas: " + ", ".join(failures), flush=True)
assert not failures, failures
PY_TRAIN_R1
}

merge_unenrolled() (
    export TEST_TMP=$TEST_TMP/unenrolled
    export XDG_CONFIG_HOME=$TEST_TMP/config
    mkdir -p "$XDG_CONFIG_HOME/pao-tren"
    printf 'otro/repo\n' > "$XDG_CONFIG_HOME/pao-tren/inscritos"
    export MERGE_TEST_ORIGIN=https://github.com/Cosmonaut-itc/felix-agent-engineering.git
    merge_cases silent
    merge_cases ask
    merge_cases deny
)

apto_vigente_scratch() {
    local repo state output scratch
    repo=$(apto_repo)
    state=$TEST_TMP/scratch-state
    output=$(XDG_STATE_HOME=$state apto "$repo" destino revisado revisado)
    assert test "${output%%:*}" = 'APTO vigente'
    for scratch in "$state"/apto-vigente/*; do
        assert test -f "$scratch/HEAD"
        assert test -d "$scratch/objects"
        assert test "$(git --git-dir="$scratch" rev-parse --is-bare-repository)" = true
    done
    # Una segunda invocación conserva ambos scratch.
    XDG_STATE_HOME=$state apto "$repo" destino revisado revisado > "$TEST_TMP/scratch-output"
    assert test "$(find "$state/apto-vigente" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" = 2
    output=$(unset XDG_STATE_HOME; apto "$repo" destino revisado revisado)
    assert test "${output%%:*}" = 'APTO vigente'
    for scratch in "$HOME"/.local/state/apto-vigente/*; do
        assert test -f "$scratch/HEAD"
    done
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
risk_repo() {
    local repo="$TEST_TMP/risk repo"
    rm -rf "$repo"
    mkdir -p "$repo/convex/payments" "$repo/migrations/old" "$repo/docs"
    git -C "$repo" init -q
    git -C "$repo" config user.email test@example.com
    git -C "$repo" config user.name test
    printf 'x\n' > "$repo/convex/payments/charge.ts"
    printf 'x\n' > "$repo/migrations/old/1.sql"
    printf 'x\n' > "$repo/docs/a.md"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m base
    git -C "$repo" update-ref refs/remotes/origin/main HEAD
    printf '%s' "$repo"
}

risk_classify() {
    local repo output index
    repo=$(risk_repo)
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert test "$output" = 'Rutas: el repositorio no declara .agents/high-risk-paths'

    mkdir -p "$repo/.agents"
    printf '# pagos\r\n\r\n  convex/payments/**  \r\n**/*.icg.ts\nmigrations/*.sql\n' > "$repo/.agents/high-risk-paths"
    printf 'y\n' >> "$repo/docs/a.md"
    printf 'y\n' >> "$repo/migrations/old/1.sql"
    output=$(cd "$repo/docs" && "$SCRIPTS/risk-classify")
    assert test "$output" = "Rutas: el cambio frente a $(git -C "$repo" rev-parse --short origin/main) no toca ninguna ruta de .agents/high-risk-paths"
    output=$("$SCRIPTS/risk-classify" --help)
    assert test "$(tail -n 1 <<< "$output")" = 'Uso: risk-classify [DESTINO]'
    assert test "$(wc -l <<< "$output" | tr -d ' ')" = 11

    git -C "$repo" add -A
    git -C "$repo" commit -q -m docs
    printf 'y\n' >> "$repo/convex/payments/charge.ts"
    mkdir -p "$repo/src/sync dir"
    printf 'x\n' > "$repo/src/sync dir/pedido.icg.ts"
    printf 'x\n' > "$repo/docs/c.md"
    git -C "$repo" add docs/c.md
    index=$(cksum < "$repo/.git/index")
    mkdir "$TEST_TMP/risk tmp"
    output=$(cd "$repo" && TMPDIR="$TEST_TMP/risk tmp" "$SCRIPTS/risk-classify")
    assert test "$(cksum < "$repo/.git/index")" = "$index"
    assert test -z "$(ls -A "$TEST_TMP/risk tmp")"
    assert test "$(git -C "$repo" diff --cached --name-only)" = docs/c.md
    assert test "$(head -n 1 <<< "$output")" = 'Nivel: alto por convex/payments/**, **/*.icg.ts'
    assert grep -qxF $'convex/payments/**\tconvex/payments/charge.ts' <<< "$output"
    assert grep -qxF $'**/*.icg.ts\tsrc/sync dir/pedido.icg.ts' <<< "$output"
    assert test "$(wc -l <<< "$output" | tr -d ' ')" = 3

    output=$(cd "$repo" && "$SCRIPTS/risk-classify" HEAD)
    assert test "$(head -n 1 <<< "$output")" = 'Nivel: alto por convex/payments/**, **/*.icg.ts'
    output=$(cd "$repo" && GIT_LITERAL_PATHSPECS=1 "$SCRIPTS/risk-classify" HEAD)
    assert test "$(head -n 1 <<< "$output")" = 'Nivel: alto por convex/payments/**, **/*.icg.ts'
}

risk_classify_moves_and_errors() {
    local repo output status moved
    repo=$(risk_repo)
    mkdir -p "$repo/.agents" "$repo/lib"
    printf 'convex/payments/**\n' > "$repo/.agents/high-risk-paths"
    git -C "$repo" mv convex/payments/charge.ts lib/charge.ts
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert grep -qxF $'convex/payments/**\tconvex/payments/charge.ts' <<< "$output"

    status=0
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" HEAD HEAD 2>&1) || status=$?
    assert test "$status" = 2
    assert test "$output" = 'risk-classify: uso: risk-classify [DESTINO]'

    status=0
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" no-existe 2>&1) || status=$?
    assert test "$status" = 2
    assert test "$output" = 'risk-classify: destino inválido: no-existe'

    git -C "$repo" update-ref -d refs/remotes/origin/main
    status=0
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" 2>&1) || status=$?
    assert test "$status" = 2
    assert test "$output" = 'risk-classify: destino inválido: origin/main'

    status=0
    output=$(cd "$TEST_TMP" && "$SCRIPTS/risk-classify" 2>&1) || status=$?
    assert test "$status" = 2
    assert test "$output" = 'risk-classify: no estás en un repositorio git'

    git -C "$repo" commit -q -m mueve
    moved=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" switch -q -c choque
    printf 'choque\n' > "$repo/docs/a.md"
    git -C "$repo" commit -q -am choque
    git -C "$repo" switch -q -
    printf 'tronco\n' > "$repo/docs/a.md"
    git -C "$repo" commit -q -am tronco
    git -C "$repo" merge -q choque >/dev/null 2>&1 || true
    status=0
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$moved" 2>&1) || status=$?
    assert test "$status" = 2
    assert test "$output" = 'risk-classify: el stage tiene conflictos sin resolver'
    git -C "$repo" merge --abort

    printf 'secreto\n' > "$repo/docs/secreto.md"
    chmod 0 "$repo/docs/secreto.md"
    if [ ! -r "$repo/docs/secreto.md" ]; then
        status=0
        output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$moved" 2>&1) || status=$?
        assert test "$status" = 2
        assert test "$output" = 'risk-classify: no puedo leer el working tree'
    fi
    rm -f "$repo/docs/secreto.md"

    git -C "$repo" switch -q --orphan suelta
    status=0
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$moved" 2>&1) || status=$?
    assert test "$status" = 2
    assert test "$output" = 'risk-classify: el repositorio no tiene commits'
    git -C "$repo" commit -q --allow-empty -m suelta
    status=0
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$moved" 2>&1) || status=$?
    assert test "$status" = 2
    assert test "$output" = 'risk-classify: la rama no comparte historia con el destino'
}

risk_classify_sources() {
    local repo output status
    repo=$(risk_repo)
    mkdir -p "$repo/.agents"
    printf 'convex/payments/**\n' > "$repo/.agents/high-risk-paths"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m lista
    git -C "$repo" update-ref refs/remotes/origin/main HEAD
    printf 'y\n' >> "$repo/convex/payments/charge.ts"
    git -C "$repo" commit -q -am cambio
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert test "$output" = $'Nivel: alto por convex/payments/**\nconvex/payments/**\tconvex/payments/charge.ts'

    git -C "$repo" show HEAD~1:convex/payments/charge.ts > "$repo/convex/payments/charge.ts"
    git -C "$repo" add convex/payments/charge.ts
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert grep -qxF $'convex/payments/**\tconvex/payments/charge.ts' <<< "$output"

    git -C "$repo" commit -q -am revierte
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert grep -q '^Rutas: el cambio frente a' <<< "$output"
    git -C "$repo" switch -q -c descartado
    printf 'descartado\n' >> "$repo/convex/payments/charge.ts"
    git -C "$repo" commit -q -am descartado
    git -C "$repo" switch -q -
    git -C "$repo" merge -q --no-ff -s ours descartado -m descarta
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert grep -q '^Rutas: el cambio frente a' <<< "$output"

    git -C "$repo" update-ref refs/remotes/origin/main HEAD
    printf 'z\n' >> "$repo/convex/payments/charge.ts"
    git -C "$repo" add convex/payments/charge.ts
    git -C "$repo" show HEAD:convex/payments/charge.ts > "$repo/convex/payments/charge.ts"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert grep -qxF $'convex/payments/**\tconvex/payments/charge.ts' <<< "$output"

    git -C "$repo" reset -q --hard
    git -C "$repo" switch -q -c lado
    printf 'lado\n' >> "$repo/docs/a.md"
    git -C "$repo" commit -q -am lado
    git -C "$repo" switch -q -
    printf 'tronco\n' > "$repo/docs/b.md"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m tronco
    git -C "$repo" merge -q --no-commit --no-ff lado
    printf 'merge\n' >> "$repo/convex/payments/charge.ts"
    git -C "$repo" commit -q -am merge
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert grep -qxF $'convex/payments/**\tconvex/payments/charge.ts' <<< "$output"

    printf '# ok\nconvex/payments/**z.ts\n' > "$repo/.agents/high-risk-paths"
    status=0
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" 2>&1) || status=$?
    assert test "$status" = 2
    assert test "$output" = 'risk-classify: .agents/high-risk-paths:2: patrón inválido: convex/payments/**z.ts'
    printf '../fuera\n' > "$repo/.agents/high-risk-paths"
    status=0
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" 2>&1) || status=$?
    assert test "$status" = 2
    assert test "$output" = 'risk-classify: .agents/high-risk-paths:1: patrón inválido: ../fuera'
    # Para git toda línea que empieza con : lleva marca de pathspec. Sin este
    # rechazo, :(glob) delante la vuelve un nombre literal que no existe y el
    # cambio sale como normal.
    local prefixed
    for prefixed in ':(glob)convex/payments/**' ':(top)docs/**' ':!docs/**' ':^docs/**' ':/docs/**' '::docs/**' ':docs/**' ':#docs/**'; do
        printf 'docs/**\n  %s\r\n' "$prefixed" > "$repo/.agents/high-risk-paths"
        status=0
        output=$(cd "$repo" && "$SCRIPTS/risk-classify" 2>&1) || status=$?
        assert test "$status" = 2
        assert test "$output" = "risk-classify: .agents/high-risk-paths:2: quita el prefijo, risk-classify ya antepone :(glob); un nombre que empieza con : va como \\:nombre: $prefixed"
    done
    # El escape no se rechaza: \:x es el nombre literal :x. Windows no admite
    # : en nombres de archivo y su git lee \ como separador.
    case "$(uname)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; esac
    printf '\\:reportes/**\n\\:(glob)raro/**\n' > "$repo/.agents/high-risk-paths"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert test "${output%% *}" = 'Rutas:'
}

risk_classify_target() {
    local repo output
    repo=$(risk_repo)
    mkdir -p "$repo/.agents" "$repo/lib"
    printf 'convex/payments/**\n' > "$repo/.agents/high-risk-paths"
    printf 'old\n' > "$repo/lib/refund.ts"
    printf 'otro\n' > "$repo/lib/otro.ts"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m lista
    git -C "$repo" switch -q -c tema
    printf 'new\n' > "$repo/lib/refund.ts"
    git -C "$repo" commit -q -am tema
    git -C "$repo" switch -q -
    printf 'x\n' > "$repo/convex/payments/new.ts"
    git -C "$repo" add convex/payments/new.ts
    git -C "$repo" rm -q lib/otro.ts
    git -C "$repo" commit -q -m nuevo
    git -C "$repo" update-ref refs/remotes/origin/main HEAD
    git -C "$repo" switch -q tema
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert test "$output" = $'Nivel: alto por convex/payments/**\nconvex/payments/**\tconvex/payments/new.ts'
    git -C "$repo" merge -q --no-edit origin/main
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert grep -q '^Rutas: el cambio frente a' <<< "$output"
    printf 'y\n' >> "$repo/convex/payments/charge.ts"
    git -C "$repo" commit -q -am tema

    git -C "$repo" switch -q -
    git -C "$repo" mv lib/refund.ts convex/payments/refund.ts
    printf 'y\n' >> "$repo/convex/payments/charge.ts"
    git -C "$repo" commit -q -am destino
    git -C "$repo" update-ref refs/remotes/origin/main HEAD
    git -C "$repo" switch -q tema
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert grep -qxF $'convex/payments/**\tconvex/payments/refund.ts' <<< "$output"
    assert grep -qxF $'convex/payments/**\tconvex/payments/charge.ts' <<< "$output"

    git -C "$repo" switch -q -
    mkdir -p "$repo/lib"
    git -C "$repo" mv convex/payments/refund.ts lib/refund.ts
    git -C "$repo" commit -q -m vuelve
    printf 'z\n' > "$repo/convex/payments/after.ts"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m despues
    git -C "$repo" update-ref refs/remotes/origin/main HEAD
    git -C "$repo" switch -q -c carpeta HEAD~1
    git -C "$repo" mv convex/payments convex/cobros
    git -C "$repo" commit -q -m carpeta
    printf 'convex/payments/after.ts\n' > "$repo/.agents/high-risk-paths"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert grep -qxF $'convex/payments/after.ts\tconvex/payments/after.ts' <<< "$output"
    git -C "$repo" checkout -q -- .agents/high-risk-paths
    git -C "$repo" switch -q tema

    git -C "$repo" rm -q convex/payments/charge.ts
    git -C "$repo" commit -q -m borra
    output=$(cd "$repo" && "$SCRIPTS/risk-classify")
    assert grep -qxF $'convex/payments/**\tconvex/payments/charge.ts' <<< "$output"
}

risk_classify_edges() {
    local repo output base target flag status other one kind
    repo=$(risk_repo)
    mkdir -p "$repo/.agents"
    printf 'convex/payments/charge.ts\nvendor/**' > "$repo/.agents/high-risk-paths"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m lista
    base=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" mv convex/payments/charge.ts left.ts
    git -C "$repo" commit -q -m izquierda
    target=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" checkout -q --detach "$base"
    git -C "$repo" mv convex/payments/charge.ts right.ts
    git -C "$repo" commit -q -m derecha
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target")
    assert grep -qxF $'convex/payments/charge.ts\tconvex/payments/charge.ts' <<< "$output"

    git -C "$repo" checkout -q --detach "$base"
    git -C "$repo" update-index --add --cacheinfo "160000,$base,vendor/payments"
    git -C "$repo" commit -q -m submodulo
    target=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" update-index --cacheinfo "160000,$target,vendor/payments"
    git -C "$repo" commit -q -m sube
    git -C "$repo" config diff.ignoreSubmodules all
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target")
    assert grep -qxF $'vendor/**\tvendor/payments' <<< "$output"

    for flag in assume-unchanged skip-worktree; do
        git -C "$repo" checkout -q --detach "$base"
        git -C "$repo" update-index "--$flag" convex/payments/charge.ts
        printf 'oculto\n' >> "$repo/convex/payments/charge.ts"
        output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$base")
        assert grep -qxF $'convex/payments/charge.ts\tconvex/payments/charge.ts' <<< "$output"
        git -C "$repo" update-index "--no-$flag" convex/payments/charge.ts
        git -C "$repo" checkout -q -- convex/payments/charge.ts
    done
    # Dos rutas con flags: update-index sin -z sólo leería la primera.
    git -C "$repo" update-index --assume-unchanged .agents/high-risk-paths convex/payments/charge.ts
    git -C "$repo" update-index --skip-worktree .agents/high-risk-paths convex/payments/charge.ts
    assert test "$(git -C "$repo" ls-files -v convex/payments/charge.ts)" = 's convex/payments/charge.ts'
    printf 'oculto\n' >> "$repo/convex/payments/charge.ts"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$base")
    assert grep -qxF $'convex/payments/charge.ts\tconvex/payments/charge.ts' <<< "$output"
    git -C "$repo" update-index --no-assume-unchanged .agents/high-risk-paths convex/payments/charge.ts
    git -C "$repo" update-index --no-skip-worktree .agents/high-risk-paths convex/payments/charge.ts
    git -C "$repo" checkout -q -- convex/payments/charge.ts

    git -C "$repo" config merge.mio.driver true
    printf 'destino\n' > "$repo/convex/payments/charge.ts"
    git -C "$repo" commit -q -am destino
    target=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" checkout -q --detach "$base"
    printf 'convex/payments/charge.ts merge=mio\n' > "$repo/.gitattributes"
    printf 'rama\n' > "$repo/convex/payments/charge.ts"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m rama
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target")
    assert grep -qxF $'convex/payments/charge.ts\tconvex/payments/charge.ts' <<< "$output"

    printf '.agents/\n' > "$repo/.gitignore"
    git -C "$repo" rm -q --cached .agents/high-risk-paths
    chmod 0 "$repo/.agents/high-risk-paths"
    if [ ! -r "$repo/.agents/high-risk-paths" ]; then
        status=0
        output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target" 2>&1) || status=$?
        assert test "$status" = 2
        assert test "$output" = 'risk-classify: no puedo leer .agents/high-risk-paths'
    fi
    chmod 644 "$repo/.agents/high-risk-paths"
    rm "$repo/.agents/high-risk-paths"
    for kind in symlink directorio; do
        if [ "$kind" = symlink ]; then
            # Git Bash de Windows no crea symlinks a un destino que no existe.
            ln -s falta "$repo/.agents/high-risk-paths" 2>/dev/null || continue
        else
            mkdir "$repo/.agents/high-risk-paths"
        fi
        status=0
        output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target" 2>&1) || status=$?
        assert test "$status" = 2
        assert test "$output" = 'risk-classify: no puedo leer .agents/high-risk-paths'
        rm -rf "${repo:?}/.agents/high-risk-paths"
    done

    repo=$(risk_repo)
    mkdir -p "$repo/.agents" "$repo/lib"
    printf 'convex/payments/**\n' > "$repo/.agents/high-risk-paths"
    seq 1 20 > "$repo/lib/refund.ts"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m lista
    base=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" config merge.mio.driver true
    printf 'lib/refund.ts merge=mio\nconvex/payments/refund.ts merge=mio\n' > "$repo/.gitattributes"
    printf 'rama\n' >> "$repo/lib/refund.ts"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m rama
    git -C "$repo" checkout -q --detach "$base"
    git -C "$repo" mv lib/refund.ts convex/payments/refund.ts
    { printf 'destino\n'; seq 2 20; } > "$repo/convex/payments/refund.ts"
    git -C "$repo" commit -q -am destino
    target=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" checkout -q -
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target")
    assert grep -qxF $'convex/payments/**\tconvex/payments/refund.ts' <<< "$output"

    git -C "$repo" checkout -q --detach "$base"
    git -C "$repo" sparse-checkout set docs .agents
    printf 'convex/payments/**\nmigrations/**\n' > "$repo/.agents/high-risk-paths"
    mkdir -p "$repo/convex/payments"
    printf 'fuera del cono\n' > "$repo/convex/payments/charge.ts"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$base")
    assert test "$(sed -n 2p <<< "$output")" = $'convex/payments/**\tconvex/payments/charge.ts'
    assert test "$(wc -l <<< "$output" | tr -d ' ')" = 2

    repo=$(risk_repo)
    mkdir -p "$repo/.agents" "$repo/lib"
    printf 'convex/payments/**\n' > "$repo/.agents/high-risk-paths"
    printf 'x\n' > "$repo/lib/refund.ts"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m lista
    base=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" mv lib convex/payments/lib
    git -C "$repo" commit -q -m destino
    target=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" checkout -q --detach "$base"
    git -C "$repo" config merge.directoryRenames false
    printf 'x\n' > "$repo/lib/nuevo.ts"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target")
    assert grep -qxF $'convex/payments/**\tconvex/payments/lib/refund.ts' <<< "$output"

    rm -rf "${repo:?}/lib"
    git -C "$repo" checkout -q --detach "$target"
    printf 'archivo\n' > "$repo/lib"
    git -C "$repo" add lib
    git -C "$repo" commit -q -m archivo
    target=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" checkout -q --detach "$base"
    printf 'x\n' > "$repo/lib/nuevo.ts"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target")
    assert grep -qxF $'convex/payments/**\tconvex/payments/lib/refund.ts' <<< "$output"

    repo=$(risk_repo)
    mkdir -p "$repo/.agents"
    printf 'convex/payments/**\n' > "$repo/.agents/high-risk-paths"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m lista
    one=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" update-index --add --cacheinfo "160000,$one,lib/sub"
    git -C "$repo" commit -q -m submodulo
    base=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" update-index --force-remove lib/sub
    git -C "$repo" update-index --add --cacheinfo "160000,$one,convex/payments/sub"
    git -C "$repo" commit -q -m mueve
    target=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" checkout -q --detach "$base"
    git -C "$repo" update-index --cacheinfo "160000,$target,lib/sub"
    git -C "$repo" commit -q -m sube
    git -C "$repo" config diff.ignoreSubmodules all
    git -C "$repo" config merge.renames false
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target")
    assert grep -qxF $'convex/payments/**\tconvex/payments/sub' <<< "$output"

    repo=$(risk_repo)
    mkdir -p "$repo/.agents" "$repo/old"
    printf 'convex/payments/*.icg.ts\n' > "$repo/.agents/high-risk-paths"
    printf 'x\n' > "$repo/old/keep.txt"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m lista
    base=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" mv old convex/payments/old
    git -C "$repo" commit -q -m destino
    target=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" checkout -q --detach "$base"
    git -C "$repo" mv old/keep.txt fuera.txt
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target")
    assert grep -qxF $'rename de directorio\told/keep.txt' <<< "$output"
    git -C "$repo" mv fuera.txt old/keep.txt
    printf 'x\n' > "$repo/old/nuevo.icg.ts"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target" 2>&1)
    assert test "$output" = $'Nivel: alto por rename de directorio\nrename de directorio\told/nuevo.icg.ts'
    git -C "$repo" add old/nuevo.icg.ts
    git -C "$repo" commit -q -m otro
    other=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" checkout -q --detach "$target"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$other")
    assert test "$output" = $'Nivel: alto por rename de directorio\nrename de directorio\told/nuevo.icg.ts'
    printf 'archivo\n' > "$repo/old"
    git -C "$repo" add old
    git -C "$repo" commit -q -m archivo
    target=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" checkout -q --detach "$other"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target")
    assert grep -qxF $'rename de directorio\told/nuevo.icg.ts' <<< "$output"

    git -C "$repo" checkout -q -f --detach "$base"
    git -C "$repo" clean -qfd
    git -C "$repo" config diff.ignoreSubmodules all
    git -C "$repo" update-index --add --cacheinfo "160000,$base,vendor/sub"
    git -C "$repo" commit -q -m gitlink
    one=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" update-index --force-remove vendor/sub
    git -C "$repo" commit -q -m quita
    target=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" checkout -q --detach "$one"
    mkdir -p "$repo/vendor"
    printf 'x\n' > "$repo/vendor/x.txt"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target")
    assert grep -qxF $'rename de directorio\tvendor/x.txt' <<< "$output"
    rm "$repo/vendor/x.txt"
    git -C "$repo" update-index --cacheinfo "160000,$target,vendor/sub"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target")
    assert grep -qxF $'rename de directorio\tvendor/sub' <<< "$output"
    git -C "$repo" config --unset diff.ignoreSubmodules

    git -C "$repo" checkout -q -f --detach "$base"
    git -C "$repo" clean -qfd
    printf 'old/keep.txt\n' > "$repo/.agents/high-risk-paths"
    git -C "$repo" update-index --assume-unchanged old/keep.txt
    rm "$repo/old/keep.txt"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$base")
    assert test "$output" = $'Nivel: alto por old/keep.txt\nold/keep.txt\told/keep.txt'

    repo=$(risk_repo)
    mkdir -p "$repo/.agents"
    printf 'convex/payments/**\n' > "$repo/.agents/high-risk-paths"
    if ln -s uno "$repo/convex/payments/enlace" 2>/dev/null; then
        git -C "$repo" add -A
        git -C "$repo" commit -q -m enlace
        base=$(git -C "$repo" rev-parse HEAD)
        git -C "$repo" update-index --skip-worktree convex/payments/enlace
        rm "$repo/convex/payments/enlace"
        ln -s dos "$repo/convex/payments/enlace"
        output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$base")
        assert grep -qxF $'convex/payments/**\tconvex/payments/enlace' <<< "$output"
    fi

    # Windows no admite \ en nombres de archivo.
    case "$(uname)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; esac
    repo=$(risk_repo)
    mkdir -p "$repo/.agents" "$repo/o\\ld"
    printf 'x\n' > "$repo/convex/payments/a\\b.ts"
    printf 'x\n' > "$repo/o\\ld/keep.txt"
    printf '%s\n' 'convex/payments/a\\b.ts' 'docs/a\*\*' > "$repo/.agents/high-risk-paths"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m barras
    base=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" update-index --assume-unchanged 'convex/payments/a\b.ts'
    printf 'y\n' >> "$repo/convex/payments/a\\b.ts"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$base" 2>&1)
    assert test "$output" = "$(printf 'Nivel: alto por %s\n%s\t%s' 'convex/payments/a\\b.ts' 'convex/payments/a\\b.ts' '"convex/payments/a\\b.ts"')"
    git -C "$repo" update-index --no-assume-unchanged 'convex/payments/a\b.ts'
    git -C "$repo" checkout -q -- 'convex/payments/a\b.ts'
    git -C "$repo" rm -q -r 'o\ld'
    git -C "$repo" commit -q -m quita
    target=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" checkout -q --detach "$base"
    printf 'x\n' > "$repo/o\\ld/nuevo.txt"
    output=$(cd "$repo" && "$SCRIPTS/risk-classify" "$target")
    assert grep -qxF "$(printf '%s\t%s' 'rename de directorio' 'o\ld/nuevo.txt')" <<< "$output"
}

apto_repo() {
    local repo="$TEST_TMP/apto repo"
    rm -rf "$repo"
    mkdir -p "$repo/origen"
    git -C "$repo" init -q -b destino --object-format="${1:-sha1}"
    git -C "$repo" config user.email test@example.com
    git -C "$repo" config user.name test
    printf 'const m = "a b";\n' > "$repo/cadena.js"
    printf 'x\n' > "$repo/otro.txt"
    printf 'x\n' > "$repo/origen/existente.js"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m base
    git -C "$repo" checkout -q -b revisado
    printf 'parche\n' > "$repo/parche.js"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m parche
    git -C "$repo" checkout -q destino
    printf '%s' "$repo"
}
apto() { (cd "$1" && shift && "$SCRIPTS/apto-vigente" "$@"); }
apto_short() { git -C "$1" rev-parse --short "$2"; }
# Argumentos de `git diff` que apto-vigente imprime en su línea `Resolución:`.
apto_resolution() { read -r -a resolution <<< "$(sed -n 's/^Resolución: git diff //p' <<< "$1")"; }

apto_vigente() {
    local repo output tree status left right
    repo=$(apto_repo)
    printf 'y\n' >> "$repo/otro.txt"
    git -C "$repo" commit -q -am destino
    git -C "$repo" checkout -q -b rebasado revisado
    git -C "$repo" rebase -q destino
    tree=$(git -C "$repo" rev-parse 'rebasado^{tree}')
    output=$(apto "$repo" destino revisado rebasado)
    assert test "$output" = "APTO vigente: $(apto_short "$repo" rebasado) es $(apto_short "$repo" revisado) más $(apto_short "$repo" destino), árbol $tree"
    git -C "$repo" checkout -q -b mezclado revisado
    git -C "$repo" merge -q --no-edit destino
    output=$(apto "$repo" destino revisado mezclado)
    assert test "$output" = "APTO vigente: $(apto_short "$repo" mezclado) es $(apto_short "$repo" revisado) más $(apto_short "$repo" destino), árbol $tree"
    output=$(apto "$repo" destino revisado revisado)
    assert test "${output%%:*}" = 'APTO vigente'

    # El destino avanza después del rebase: el veredicto habla de lo que el head incorporó.
    git -C "$repo" checkout -q destino
    printf 'tarde\n' > "$repo/tarde.txt"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m tarde
    output=$(apto "$repo" destino revisado rebasado)
    assert test "$output" = "APTO vigente: $(apto_short "$repo" rebasado) es $(apto_short "$repo" revisado) más $(apto_short "$repo" 'destino~1'), árbol $tree"

    git -C "$repo" checkout -q -b editado rebasado
    printf 'extra\n' >> "$repo/parche.js"
    git -C "$repo" commit -q -am extra
    output=$(apto "$repo" destino revisado editado)
    assert test "$output" = 'Ronda: el head nuevo no es la mezcla del head revisado con el destino'

    status=0
    output=$(apto "$repo" destino revisado noexiste 2>&1) || status=$?
    assert test "$status" = 2
    assert test "$output" = 'apto-vigente: commit inválido: noexiste'
    status=0
    output=$(apto "$repo" destino revisado 2>&1) || status=$?
    assert test "$status" = 2
    assert test "$output" = 'apto-vigente: uso: apto-vigente DESTINO HEAD_REVISADO HEAD_NUEVO'
    output=$("$SCRIPTS/apto-vigente" --help)
    assert test "$(tail -n 1 <<< "$output")" = 'Uso: apto-vigente DESTINO HEAD_REVISADO HEAD_NUEVO'
    assert test "$(wc -l <<< "$output" | tr -d ' ')" = 8

    # Dos merges con los mismos padres dejan dos bases óptimas para B.
    repo=$(apto_repo)
    left=$(git -C "$repo" rev-parse revisado)
    printf 'destino\n' >> "$repo/otro.txt"
    git -C "$repo" commit -q -am destino
    right=$(git -C "$repo" rev-parse destino)
    tree=$(git -C "$repo" rev-parse 'destino^{tree}')
    git -C "$repo" branch cruce-revisado "$(git -C "$repo" commit-tree "$tree" -p "$left" -p "$right" -m revisado)"
    git -C "$repo" branch cruce-destino "$(git -C "$repo" commit-tree "$tree" -p "$right" -p "$left" -m destino)"
    status=0
    output=$(apto "$repo" cruce-destino cruce-revisado cruce-destino 2>&1) || status=$?
    assert test "$status" = 2
    assert test "$output" = 'apto-vigente: la base común del head revisado y la base nueva no es única'
}

apto_vigente_interactions() {
    local repo output tree mode resolution
    # Un espacio dentro de una cadena cambia el árbol aunque el parche se parezca.
    repo=$(apto_repo)
    git -C "$repo" checkout -q revisado
    printf 'const m = "a b";\nconst n = "c d";\n' > "$repo/cadena.js"
    git -C "$repo" commit -q -am cadena
    git -C "$repo" checkout -q destino
    printf 'y\n' >> "$repo/otro.txt"
    git -C "$repo" commit -q -am destino
    git -C "$repo" checkout -q -b rebasado revisado
    git -C "$repo" rebase -q destino
    printf 'const m = "a b";\nconst n = "cd";\n' > "$repo/cadena.js"
    git -C "$repo" commit -q --amend -a --no-edit
    output=$(apto "$repo" destino revisado rebasado)
    assert test "$output" = 'Ronda: el head nuevo no es la mezcla del head revisado con el destino'

    # El rebase mueve la edición del parche y coincide con la mezcla automática limpia.
    repo=$(apto_repo)
    git -C "$repo" checkout -q revisado
    printf 'x\nparche\n' > "$repo/otro.txt"
    git -C "$repo" commit -q -am toca-otro
    git -C "$repo" checkout -q destino
    git -C "$repo" mv otro.txt movido.txt
    git -C "$repo" commit -q -m rename
    git -C "$repo" checkout -q -b rebasado revisado
    git -C "$repo" rebase -q destino
    assert test "$(git -C "$repo" merge-tree --write-tree destino revisado)" = "$(git -C "$repo" rev-parse 'rebasado^{tree}')"
    output=$(apto "$repo" destino revisado rebasado)
    assert test "$output" = "$(printf 'Ronda delta: el destino cambió archivos del parche\notro.txt')"

    # Los dos lados cambian zonas distintas del mismo archivo y git los mezcla sin conflicto.
    repo=$(apto_repo)
    git -C "$repo" checkout -q revisado
    printf 'x\n\n\n\n\n\n\n\nfin parche\n' > "$repo/otro.txt"
    git -C "$repo" commit -q -am cola
    git -C "$repo" checkout -q destino
    printf 'inicio destino\nx\n' > "$repo/otro.txt"
    git -C "$repo" commit -q -am cabeza
    git -C "$repo" checkout -q -b rebasado revisado
    git -C "$repo" rebase -q destino
    output=$(apto "$repo" destino revisado rebasado)
    assert test "$output" = "$(printf 'Ronda delta: el destino cambió archivos del parche\notro.txt')"

    # El destino renombra un directorio donde el parche añade un archivo. Con
    # directoryRenames=true git lo mueve sin avisar y las listas no lo comparten.
    for mode in conflict true; do
        repo=$(apto_repo)
        git -C "$repo" config merge.directoryRenames "$mode"
        git -C "$repo" checkout -q revisado
        printf 'reviewed\n' > "$repo/origen/nuevo.js"
        git -C "$repo" add -A
        git -C "$repo" commit -q -m en-origen
        git -C "$repo" checkout -q destino
        git -C "$repo" mv origen nuevo
        git -C "$repo" commit -q -m dir-rename
        tree=$(git -C "$repo" merge-tree --write-tree destino revisado | head -n 1) || true
        git -C "$repo" branch candidato "$(git -C "$repo" commit-tree "$tree" -p destino -m candidato)"
        output=$(apto "$repo" destino revisado candidato)
        if git -C "$repo" cat-file -e "$tree:nuevo/nuevo.js" 2>/dev/null; then
            assert test "$(head -n 1 <<< "$output")" = 'Ronda: el head nuevo no es la mezcla del head revisado con el destino'
            assert grep -Fq 'difiere de la referencia: nuevo/nuevo.js' <<< "$output"
        else
            # Algunas versiones dejan el alta en su ruta original.
            assert test "$(head -n 1 <<< "$output")" = 'Ronda delta: la mezcla del head revisado con el destino tiene conflictos'
            assert grep -Fxq 'origen/nuevo.js' <<< "$output"
            apto_resolution "$output"
            assert git -C "$repo" diff --stat "${resolution[@]}"
        fi
    done

    # Un merge con conflictos también escribe un árbol, con los marcadores dentro.
    repo=$(apto_repo)
    git -C "$repo" checkout -q revisado
    printf 'reviewed\n' > "$repo/origen/nuevo.js"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m en-origen
    git -C "$repo" checkout -q destino
    git -C "$repo" mv origen nuevo
    printf 'target\n' > "$repo/nuevo/nuevo.js"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m dir-rename-y-add
    # Con los SHA, los marcadores llevan las mismas etiquetas que pone apto-vigente.
    tree=$(git -C "$repo" merge-tree --write-tree "$(git -C "$repo" rev-parse destino)" "$(git -C "$repo" rev-parse revisado)" | head -n 1) || true
    git -C "$repo" branch candidato "$(git -C "$repo" commit-tree "$tree" -p destino -m candidato)"
    assert grep -q '^<<<<<<<' <(git -C "$repo" show candidato:nuevo/nuevo.js)
    output=$(apto "$repo" destino revisado candidato)
    # Los marcadores cambian una ruta cuya referencia es el blob del destino.
    assert test "$(head -n 1 <<< "$output")" = 'Ronda: el head nuevo no es la mezcla del head revisado con el destino'
    assert grep -Fq 'difiere de la referencia: nuevo/nuevo.js' <<< "$output"
}

# Catálogos reales en historias separadas; se prueba el veredicto público.
apto_vigente_catalogs() {
    python3 - "$SCRIPTS/apto-vigente" "$TEST_TMP" "$BASH" <<'PY_CATALOGS'
import copy
import json
from pathlib import Path
import subprocess
import sys

script, temporary, bash = sys.argv[1:]


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


cases = ('disjuntas', 'translations', 'varios', 'comun', 'valor-parche', 'valor-destino',
         'borrada', 'invalido', 'duplicada', 'lista', 'numero', 'objeto-vacio',
         'puntos', 'modo', 'no-catalogo', 'otro-archivo', 'editado', 'conflicto', 'catalogo-destino-borra')
for case in cases:
    repo = Path(temporary) / ('catalogo-' + case)
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'destino')
    git(repo, 'config', 'user.email', 'test@example.com')
    git(repo, 'config', 'user.name', 'test')
    path = ('apps/mobile/lib/translations/es.json' if case == 'translations' else
            'config/es.json' if case == 'no-catalogo' else 'apps/web/messages/es.json')
    paths = [path, 'messages/en.json'] if case == 'varios' else [path]
    base = {'inicio': {'base': 'Inicio'}, 'medio': {'uno': 'Uno', 'dos': 'Dos', 'tres': 'Tres'},
            'final': {'base': 'Final'}}
    if case == 'objeto-vacio':
        base['inicio']['vacio'] = {}
    if case == 'puntos':
        base['inicio']['a.b'] = 'Original'
    mine, theirs = copy.deepcopy(base), copy.deepcopy(base)
    mine['inicio']['parche'] = 'Nuevo del parche'
    theirs['final']['destino'] = 'Nuevo del destino'
    if case == 'comun':
        theirs['inicio']['parche'] = mine['inicio']['parche']
    if case == 'valor-parche':
        mine['inicio']['base'] = 'Cambiado'
    if case == 'valor-destino':
        theirs['final']['base'] = 'Cambiado'
    if case == 'borrada':
        del theirs['final']['base']
    if case == 'lista':
        theirs['final']['destino'] = ['Cadena']
    if case == 'numero':
        theirs['final']['destino'] = 1
    if case == 'objeto-vacio':
        del mine['inicio']['vacio']
    if case == 'puntos':
        del mine['inicio']['a.b']
        mine['inicio']['a'] = {'b': 'Original'}
    if case == 'conflicto':
        mine['medio']['dos'], theirs['medio']['dos'] = 'Parche', 'Destino'

    def write_catalogs(data, side):
        for name in paths:
            file = repo / name
            file.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
            if case == 'invalido' and side == 'destino':
                text = text.replace('"destino": "Nuevo del destino"', '"destino": indefinido')
            if case == 'duplicada' and side == 'destino':
                text = text.replace('"destino": "Nuevo del destino"', '"destino": "Uno", "destino": "Dos"')
            file.write_text(text, encoding='utf-8')

    def write(data, side):
        write_catalogs(data, side)
        # Mismo archivo de código, con cambios distantes para no causar conflicto.
        (repo / 'codigo.txt').write_text(('parche' if side == 'parche' else 'base') +
                                       '\n' * 12 + ('destino' if side == 'destino' else 'base') + '\n')

    write(base, 'base')
    rename_delete = case == 'catalogo-destino-borra'
    if rename_delete:
        (repo / 'src').mkdir()
        (repo / 'src/legado.ts').write_text('export const legado = 1;\n')
        (repo / 'src/index.ts').write_text('export {};\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'base')
    git(repo, 'checkout', '-qb', 'revisado')
    write(mine, 'parche' if case == 'otro-archivo' else 'base')
    if rename_delete:
        git(repo, 'mv', 'src/legado.ts', 'src/movido.ts')
    git(repo, 'commit', '-qam', 'parche')
    git(repo, 'checkout', '-q', 'destino')
    write(theirs, 'destino')
    if rename_delete:
        git(repo, 'rm', 'src/legado.ts')
    if case == 'modo':
        (repo / path).chmod(0o755)
    git(repo, 'add', '.')
    if case == 'modo':
        git(repo, 'update-index', '--chmod=+x', path)
    git(repo, 'commit', '-qm', 'destino')
    git(repo, 'checkout', '-qb', 'nuevo', 'revisado')
    merge = subprocess.run(['git', '-C', str(repo), 'merge', '-q', '--no-edit', 'destino'],
                           capture_output=True, text=True)
    if case == 'conflicto':
        assert merge.returncode == 1, merge
        write_catalogs(mine, 'resuelto')
        assert (repo / 'codigo.txt').read_text().endswith('destino\n'), merge
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'resuelto')
    elif rename_delete:
        assert merge.returncode == 1, merge
        joined = copy.deepcopy(theirs)
        joined['inicio'] = mine['inicio']
        write_catalogs(joined, 'resuelto')
        assert (repo / 'src/movido.ts').is_file(), merge
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'resuelto')
    else:
        assert merge.returncode == 0, merge
    if case == 'editado':
        (repo / path).write_text('{}\n')
        git(repo, 'commit', '-qam', 'extra')
    result = subprocess.run([bash, script, 'destino', 'revisado', 'nuevo'], cwd=repo,
                            capture_output=True, text=True, encoding='utf-8')
    expected = ('APTO vigente' if case in ('disjuntas', 'translations', 'varios') else
                'Ronda delta')
    assert result.returncode == 0 and result.stdout.startswith(expected), (case, result)
    if expected == 'APTO vigente':
        assert 'catálogo' in result.stdout and 'claves disjuntas' in result.stdout, result
    if case == 'conflicto':
        assert 'Excepción de catálogos: el head nuevo no es la unión de altas disjuntas' in result.stdout, result
    if rename_delete:
        lines = result.stdout.splitlines()
        assert 'src/movido.ts' in lines and lines[1].startswith('Resolución: git diff --no-relative '), result
        assert 'Excepción: conflicto de la mezcla en una ruta no compartida: src/movido.ts' in lines, result
    print('catálogo ' + case + ': ' + result.stdout.splitlines()[0])
PY_CATALOGS
}

apto_vigente_migrations() {
    python3 - "$SCRIPTS/apto-vigente" "$TEST_TMP" "$BASH" <<'PY_MIGRATIONS'
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

script, temporary, bash = sys.argv[1:]
origin = '00000000-0000-0000-0000-000000000000'
journal = 'meta/_journal.json'


def git(repo, *args, check=True, env=None):
    return subprocess.run(['git', '-C', str(repo), *args], check=check, env=env,
                          capture_output=True, text=True)


def commit(repo, name):
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', name)


def entry(idx, when, name):
    return dict(idx=idx, when=when, tag=f'{idx:04d}_{name}', version='7', breakpoints=True)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def write_migration(root, item, sql, identifier, parent, tables):
    tag = item['tag']
    (root / f'{tag}.sql').write_bytes(sql)
    if identifier is not None:
        write(root / f'meta/{tag[:4]}_snapshot.json',
              dict(id=identifier, prevId=parent, version='7', dialect='postgresql', tables=tables))


def write_journal(root, entries):
    write(root / journal, dict(version='7', dialect='postgresql', entries=entries))


cases = {
    'una': None, 'dos': None, 'apilada': None, 'sin-conflicto': None,
    'directorio-unicode': None, 'solo-sql': None, 'sin-snapshot': None, 'when-maximo': None,
    'mixta': None, 'mixta-invalida': (None, 'unión de altas disjuntas'),
    'sql': (3, 'cambió el SQL'), 'when': (2, 'when debe crecer'), 'idx': (2, 'idx o tag'),
    'prevId': (4, 'prevId'), 'id-base': (4, 'id repetido'), 'id-alta': (4, 'id repetido'),
    'perdio-tabla': (4, 'esquema difiere'), 'perdio-columna': (4, 'esquema difiere'),
    'fuera': (1, 'árbol fuera'), 'snapshot-base': (4, 'snapshot de la base editado'),
    'misma-clave': (4, 'cambios incompatibles'), 'extra': (5, 'otros archivos'),
    'compartido-fuera': (1, 'compartido.txt'),
    'sin-conflicto-sql': (3, 'cambió el SQL'), 'sql-base': (3, 'otros cambios de SQL'),
    'sql-viejo': (3, 'aún existe el nombre anterior'),
    'conflicto-fuera': (1, 'parche.txt'), 'when-entre-altas': (2, 'when debe crecer'),
    'version': (2, 'versión'), 'breakpoints': (2, 'breakpoints'),
    'retroceso': (2, 'las altas del journal difieren'),
    'sin-conflicto-fuera': (1, 'árbol fuera'), 'id-base-nombrado': (4, 'id repetido'),
    'snapshot-agregado': (4, 'presencia del snapshot'), 'snapshot-borrado': (4, 'presencia del snapshot'),
    'renombre': ('vaciado', 'legacy/ajuste.sql'),
    'renombre-fuera': (1, 'ajustes/ajuste.sql'), 'modo-fuera': (1, 'parche.txt'),
    'when-no-seguro': (2, 'rango de enteros seguros'),
    'when-negativo': (2, 'rango de enteros seguros'), 'error-python': None,
    'renombre-conservado': ('vaciado', 'legacy/ajuste.sql'), 'renombre-dividido': ('vaciado', 'legacy/ajuste.sql'),
    'vaciado-por-parche': ('vaciado', 'aux/nuevo.txt'), 'vaciado-sin-alta': None,
    'vaciado-con-edicion': (1, 'parche.txt'), 'journal-solo-nuevo': (1, 'otro/esquema.txt'),
    'drizzle-destino-borra': ('conflicto-git', 'src/movido.ts'),
    'drizzle-revisado-borra': ('conflicto-git', 'src/movido.ts'),
    'drizzle-revisado-borra-honra': (1, 'src/movido.ts'),
    'drizzle-renombre-diverge': ('conflicto-git', 'src/movido.ts'),
    'base-nueva-ambigua': None,
    'control': ('conflicto-git', 'deploy/jobs/movido.sql'),
    'journal-revisado': ('conflicto-git', 'deploy/jobs/movido.sql'),
    'journal-destino': ('conflicto-git', 'deploy/jobs/movido.sql'),
    'hacia-drizzle': ('conflicto-git', 'packages/db/schema-history/0001_primera.sql'),
    'snapshot': ('conflicto-git', 'packages/db/schema-history/meta/0001_snapshot.json'),
}
for case, failure in cases.items():
    repo = Path(temporary) / ('migracion-' + case)
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'destino')
    git(repo, 'config', 'user.email', 'test@example.com')
    git(repo, 'config', 'user.name', 'test')
    directory = 'packages/db/histórico' if case == 'directorio-unicode' else 'packages/db/schema-history'
    root = repo / directory
    root.mkdir(parents=True)
    branch_sql, second_sql = b'ALTER TABLE original ADD COLUMN first TEXT;\n', b'SELECT 2;\n'
    base = entry(0, 100, 'base')
    incoming = entry(1, 200, 'destino')
    clean = case.startswith('sin-conflicto')
    split = case == 'renombre-dividido'
    directory_rename = split or case in ('renombre', 'renombre-conservado', 'renombre-fuera', 'vaciado-con-edicion')
    file_conflict = case in ('drizzle-destino-borra', 'drizzle-revisado-borra',
                             'drizzle-revisado-borra-honra', 'drizzle-renombre-diverge')
    job_conflict = case in ('control', 'journal-revisado', 'journal-destino', 'hacia-drizzle')
    jobs = repo / 'deploy/jobs'
    emptied_dir = root / 'aux' if case == 'vaciado-por-parche' else repo / 'vaciado'
    old_file = emptied_dir / 'anterior.txt'
    added_file = emptied_dir / 'nuevo.txt'
    retreat = case == 'retroceso'
    first = entry(2 if clean or retreat else 1, 250 if retreat else 150, 'primera')
    second, third = entry(2, 175, 'segunda'), entry(3, 180, 'tercera')
    stacked = case == 'apilada'
    gap = case == 'sin-snapshot'
    three = stacked or gap
    two = three or case in ('dos', 'id-alta', 'when-entre-altas', 'when-no-seguro')
    mixed = case in ('mixta', 'mixta-invalida')
    catalog = repo / 'messages/es.json'

    base_tables = {'original': {'columns': {'id': 'integer'}}}
    branch_tables = deepcopy(base_tables)
    branch_tables['original']['columns']['first'] = 'text'
    branch_last = deepcopy(branch_tables)
    branch_last['original']['columns']['second'] = 'text'
    incoming_tables = deepcopy(base_tables)
    if case != 'snapshot':
        incoming_tables['destino'] = {'columns': {'id': 'integer'}}
    if case == 'misma-clave':
        branch_tables['original']['columns']['id'] = 'bigint'
        incoming_tables['original']['columns']['id'] = 'text'

    write_migration(root, base, b'SELECT 0;\n', 'base', origin, base_tables)
    write_journal(root, [base])
    if case == 'snapshot':
        write(repo / 'planes/borrador.json', dict(id='primera', prevId='base', version='7',
                                                  dialect='postgresql', tables=branch_tables))
        (repo / 'planes/README.md').write_text('planes\n')
    if case == 'id-base-nombrado':
        write(root / 'meta/archivado_snapshot.json', dict(id='archivado', prevId=origin, tables={}))
    if clean:
        # Cada lado inserta al final del mismo array pero en lados opuestos
        # de las líneas en blanco. Git mezcla el texto sin conflicto.
        def padded(item=None, after=False):
            extra = ', ' + json.dumps(item) if item is not None else ''
            (root / journal).write_text(
                '{"version":"7","dialect":"postgresql","entries":[\n'
                + json.dumps(base) + '\n' + (extra + '\n' if not after else '')
                + '\n' * 12 + (extra + '\n' if after else '') + ']}\n')
        padded()
    if mixed:
        # Un objeto anidado de cadenas admite inserciones en lugares separados.
        catalog_base = {'inicio': {'base': 'Inicio'}, 'medio': {'uno': 'Uno', 'dos': 'Dos',
                                                               'tres': 'Tres'}, 'fin': {'base': 'Fin'}}
        write(catalog, catalog_base)
    if case == 'compartido-fuera':
        (repo / 'compartido.txt').write_text('base\n' + '\n' * 12 + 'base\n')
    if directory_rename:
        (repo / 'legacy').mkdir()
        (repo / 'legacy/marcador.txt').write_text('marcador\n')
    if file_conflict:
        (repo / 'src').mkdir()
        (repo / 'src/legado.ts').write_text('export const legado = 1;\n')
        (repo / 'src/index.ts').write_text('export {};\n')
    if job_conflict:
        jobs.mkdir(parents=True)
        (jobs / 'conservar.txt').write_text('ancla\n')
        (jobs / 'legado.sql').write_bytes(branch_sql if case == 'hacia-drizzle' else b'SELECT 99;\n')
    if case == 'base-nueva-ambigua':
        (repo / 'politica.txt').write_text('original\n')
    if case == 'journal-solo-nuevo':
        (repo / 'otro').mkdir()
        (repo / 'otro/esquema.txt').write_text('original\n')
    if case in ('vaciado-por-parche', 'vaciado-sin-alta'):
        emptied_dir.mkdir()
        old_file.write_text('anterior\n')
    if split:
        (repo / 'legacy/esquema.txt').write_text('esquema\n')
        (repo / 'aplicar-ajustes.sh').write_text('for sql in legacy/*.sql; do\n  printf "%s\\n" "$sql"\ndone\n')
    commit(repo, 'base')
    old_base = git(repo, 'rev-parse', 'HEAD').stdout.strip()
    original_entries = [base]
    original_parent = 'base'
    if retreat:
        write_migration(root, incoming, b'SELECT 1;\n', 'destino', 'base', incoming_tables)
        original_entries.append(incoming)
        original_parent = 'destino'
        write_journal(root, original_entries)
        commit(repo, 'destino')
        branch_tables['destino'] = incoming_tables['destino']

    git(repo, 'checkout', '-qb', 'revisado')
    if case == 'solo-sql':
        branch_sql = b'UPDATE original SET id = 1 WHERE id IS NULL;\n'
    original_id = None if case in ('solo-sql', 'snapshot-agregado') else 'primera'
    if job_conflict:
        git(repo, 'mv', 'deploy/jobs/legado.sql', failure[1])
    if case == 'snapshot':
        git(repo, 'mv', 'planes/borrador.json', f'{directory}/meta/0001_snapshot.json')
    if case == 'journal-revisado':
        write_journal(jobs, [])
    write_migration(root, first, branch_sql, original_id, original_parent, branch_tables)
    write_journal(root, [*original_entries, first])
    if clean:
        padded(first)
    (repo / 'parche.txt').write_text('cambio revisado\n')
    if directory_rename:
        (repo / 'legacy/ajuste.sql').write_bytes(b'UPDATE original SET id = 1 WHERE id IS NULL;\n')
    if file_conflict:
        if case in ('drizzle-destino-borra', 'drizzle-renombre-diverge'):
            git(repo, 'mv', 'src/legado.ts', 'src/movido.ts')
        else:
            git(repo, 'rm', 'src/legado.ts')
    if case == 'vaciado-por-parche':
        old_file.unlink()
    if mixed:
        mine = deepcopy(catalog_base)
        mine['inicio']['rama'] = 'Rama'
        if case == 'mixta-invalida':
            mine['inicio']['base'] = 'Editado'
        write(catalog, mine)
    if case == 'compartido-fuera':
        (repo / 'compartido.txt').write_text('parche\n' + '\n' * 12 + 'base\n')
    if stacked:
        commit(repo, 'padre-apilado')
        git(repo, 'branch', 'padre')
    if two:
        write_migration(root, second, second_sql, None if gap else 'segunda', 'primera', branch_last)
        write_journal(root, [base, first, second])
    if three:
        # La tercera alta se generó sobre la primera, aunque la segunda ya
        # está en el journal. Su replay debe conservar la columna de la segunda.
        third_tables = deepcopy(branch_tables)
        third_tables['original']['columns']['third'] = 'text'
        write_migration(root, third, b'SELECT 3;\n', 'tercera', 'primera', third_tables)
        write_journal(root, [base, first, second, third])
    commit(repo, 'revisado')

    git(repo, 'checkout', '-q', 'destino')
    incoming_id = None if case == 'snapshot' else 'destino'
    write_migration(root, incoming, b'SELECT 1;\n', incoming_id, 'base', incoming_tables)
    if case == 'snapshot':
        git(repo, 'rm', 'planes/borrador.json')
    write_journal(root, [base, incoming])
    if clean:
        padded(incoming, after=True)
    if mixed:
        theirs = deepcopy(catalog_base)
        theirs['fin']['destino'] = 'Destino'
        write(catalog, theirs)
    if case == 'compartido-fuera':
        (repo / 'compartido.txt').write_text('base\n' + '\n' * 12 + 'destino\n')
    if case == 'conflicto-fuera':
        (repo / 'parche.txt').write_text('cambio incompatible\n')
    if split:
        (repo / 'ajustes/datos').mkdir(parents=True)
        (repo / 'ajustes/esquema').mkdir()
        git(repo, 'mv', 'legacy/marcador.txt', 'ajustes/datos/marcador.txt')
        git(repo, 'mv', 'legacy/esquema.txt', 'ajustes/esquema/esquema.txt')
        deploy = repo / 'aplicar-ajustes.sh'
        deploy.write_text(deploy.read_text().replace('legacy/', 'ajustes/datos/'))
    elif case == 'renombre-fuera':
        git(repo, 'mv', 'legacy', 'ajustes')
    elif directory_rename:
        git(repo, 'mv', 'legacy/marcador.txt', f'{directory}/marcador.txt')
    if file_conflict:
        if case == 'drizzle-destino-borra':
            git(repo, 'rm', 'src/legado.ts')
        else:
            destination = 'src/alterno.ts' if case == 'drizzle-renombre-diverge' else 'src/movido.ts'
            git(repo, 'mv', 'src/legado.ts', destination)
    if job_conflict:
        git(repo, 'rm', 'deploy/jobs/legado.sql')
    if case == 'journal-destino':
        write_journal(jobs, [])
    if case == 'vaciado-por-parche':
        added_file.write_text('alta del destino\n')
    if case == 'vaciado-sin-alta':
        old_file.unlink()
    if not retreat:
        commit(repo, 'destino')
    candidate_base = old_base if retreat else 'destino'
    auto = git(repo, 'merge-tree', '--write-tree', candidate_base, 'revisado', check=False)
    assert auto.returncode == (0 if clean or retreat else 1), (case, auto)
    if case == 'renombre-fuera':
        moved = git(repo, '-c', 'merge.directoryRenames=true', 'merge-tree',
                    '--write-tree', 'destino', 'revisado', check=False)
        paths = git(repo, 'ls-tree', '-r', '--name-only', moved.stdout.splitlines()[0]).stdout.splitlines()
        assert 'ajustes/ajuste.sql' in paths and 'legacy/ajuste.sql' not in paths, moved
        patch, incoming_paths = (set(git(repo, 'diff', '--no-renames', '--name-only',
                                         old_base, ref).stdout.splitlines())
                                 for ref in ('revisado', 'destino'))
        assert all(path.startswith(directory + '/') for path in patch & incoming_paths)

    git(repo, 'checkout', '-qb', 'nuevo', candidate_base)
    new_first, new_second, new_third = (entry(2, 201, 'primera'), entry(3, 202, 'segunda'),
                                       entry(4, 203, 'tercera'))
    if case == 'when-no-seguro':
        new_first['when'], new_second['when'] = 2**53, 2**53 + 1
    if case == 'when-negativo':
        new_first['when'] = -1
    if case == 'when-maximo':
        new_first['when'] = 2**53 - 1
    if case == 'when':
        new_first['when'] = 200
    if case == 'when-entre-altas':
        new_second['when'] = new_first['when']
    if case == 'idx':
        new_first['idx'] = 3
    if case == 'version':
        new_first['version'] = '6'
    if case == 'breakpoints':
        new_first['breakpoints'] = False
    if retreat:
        new_first = entry(1, 250, 'primera')
    integrated = deepcopy(base_tables if retreat else incoming_tables)
    integrated['original']['columns']['first'] = 'text'
    if case == 'perdio-tabla':
        del integrated['destino']
    if case == 'perdio-columna':
        del integrated['original']['columns']['first']
    first_id = {'id-base': 'destino', 'id-base-nombrado': 'archivado'}.get(case, 'integrada-primera')
    if case in ('solo-sql', 'snapshot-borrado'):
        first_id = None
    if case in ('sql', 'sin-conflicto-sql'):
        branch_sql += b' '
    write_migration(root, new_first, branch_sql,
                    first_id, 'base' if case in ('prevId', 'snapshot') or retreat else 'destino', integrated)
    if two:
        second_id = None if gap else first_id if case == 'id-alta' else 'integrada-segunda'
        if not gap:
            integrated['original']['columns']['second'] = 'text'
        write_migration(root, new_second, second_sql, second_id, first_id, integrated)
    if three:
        integrated['original']['columns']['third'] = 'text'
        write_migration(root, new_third, b'SELECT 3;\n', 'integrada-tercera',
                        first_id if gap else second_id, integrated)
    write_journal(root, [base] + ([] if retreat else [incoming]) + [new_first]
                  + ([new_second] if two else []) + ([new_third] if three else []))
    outside_edit = case in ('fuera', 'sin-conflicto-fuera', 'vaciado-con-edicion')
    (repo / 'parche.txt').write_text('cambio distinto\n' if outside_edit else 'cambio revisado\n')
    if case == 'snapshot-base':
        with (root / 'meta/0001_snapshot.json').open('a') as file:
            file.write('\n')
    if case == 'sql-base':
        (root / '0000_base.sql').write_bytes(b'SELECT 9;\n')
    if case == 'sql-viejo':
        (root / f'{first["tag"]}.sql').write_bytes(branch_sql)
    if case == 'extra':
        (root / 'extra.txt').write_text('cambio adicional\n')
    if case == 'compartido-fuera':
        (repo / 'compartido.txt').write_text('parche\n' + '\n' * 12 + 'destino\n')
    if mixed:
        theirs['inicio'] = mine['inicio']
        write(catalog, theirs)
    if case in ('renombre-conservado', 'renombre-dividido', 'renombre-fuera', 'vaciado-con-edicion'):
        path = repo / ('ajustes' if case == 'renombre-fuera' else 'legacy') / 'ajuste.sql'
        path.parent.mkdir(exist_ok=True)
        path.write_text(git(repo, 'show', 'revisado:legacy/ajuste.sql').stdout)
    if case in ('drizzle-destino-borra', 'drizzle-renombre-diverge'):
        (repo / 'src/movido.ts').write_text(git(repo, 'show', 'revisado:src/movido.ts').stdout)
    if case == 'drizzle-revisado-borra-honra':
        git(repo, 'rm', 'src/movido.ts')
    if case == 'journal-solo-nuevo':
        write_journal(repo / 'otro', [])
        (repo / 'otro/esquema.txt').write_text('edición extra\n')
    if job_conflict and case != 'hacia-drizzle':
        (jobs / 'movido.sql').write_text(git(repo, 'show', 'revisado:deploy/jobs/movido.sql').stdout)
    if case == 'journal-revisado':
        write_journal(jobs, [])
    if case == 'vaciado-por-parche':
        old_file.unlink()
    commit(repo, 'renumerado')
    if case == 'modo-fuera':
        (repo / 'parche.txt').chmod(0o755)
        git(repo, 'update-index', '--chmod=+x', 'parche.txt')
        git(repo, 'commit', '-qm', 'cambia sólo el modo')

    command = [bash, script, 'destino', 'revisado', 'nuevo']
    if case == 'base-nueva-ambigua':
        # Los dos merges comparten la migración y la corrección como bases
        # óptimas, pero el árbol del candidato conserva la política original.
        target_migration = git(repo, 'rev-parse', 'destino').stdout.strip()
        candidate_tree = git(repo, 'rev-parse', 'nuevo^{tree}').stdout.strip()
        git(repo, 'checkout', '-qb', 'correccion', old_base)
        (repo / 'politica.txt').write_text('corregida\n')
        git(repo, 'commit', '-qam', 'corrección',
            env=dict(os.environ, GIT_COMMITTER_DATE='2026-01-01T00:00:00Z'))
        git(repo, 'checkout', '-q', 'destino')
        git(repo, 'merge', '-q', '--no-ff', '-m', 'destino', 'correccion')
        candidate = git(repo, 'commit-tree', candidate_tree, '-p', target_migration,
                        '-p', 'correccion', '-m', 'candidato sin la corrección').stdout.strip()
        git(repo, 'branch', '-f', 'nuevo', candidate)
        assert len(git(repo, 'merge-base', '--all', 'destino', 'nuevo').stdout.splitlines()) == 2
        assert git(repo, 'merge-base', 'destino', 'nuevo').stdout.strip() == target_migration
        result = subprocess.run(command, cwd=repo, capture_output=True, encoding='utf-8')
        assert result.returncode == 2 and not result.stdout, (case, result)
        assert result.stderr == 'apto-vigente: la base común del destino y el head nuevo no es única\n', result
        print(f'migración {case} → {result.stderr.strip()} (2)')
        continue
    if case == 'error-python':
        shim = repo / 'shim'
        shim.mkdir()
        python = shim / 'python3'
        env = dict(os.environ, PATH=str(shim) + os.pathsep + os.environ['PATH'])
        for exit_code in (2, 1):
            python.write_text(f'#!/bin/sh\nexit {exit_code}\n')
            python.chmod(0o755)
            result = subprocess.run(command, cwd=repo, env=env, capture_output=True, encoding='utf-8')
            assert result.returncode == 2 and not result.stdout, (case, result)
            assert 'falló el validador' in result.stderr, (case, result)
            print(f'migración {case} → Python sale con {exit_code}, apto-vigente con 2')
        continue
    result = subprocess.run(command, cwd=repo, capture_output=True, encoding='utf-8')
    assert result.returncode == 0, (case, result)
    lines = result.stdout.splitlines()
    if failure:
        point, reason = failure
        if outside_edit or retreat or case in ('modo-fuera', 'renombre-fuera', 'journal-solo-nuevo',
                                               'drizzle-revisado-borra-honra'):
            assert lines[0].startswith('Ronda:'), (case, result)
        else:
            assert lines[0].startswith('Ronda delta:'), (case, result)
            assert f'{directory}/{journal}' in lines, (case, result)
        if point == 'vaciado':
            added = reason if directory_rename else f'{directory}/{reason}'
            emptied = added.rpartition('/')[0]
            assert added in lines, (case, result)
            assert f'Directorio vaciado: {emptied}. Archivo añadido: {added}' in lines, (case, result)
            assert lines[1].startswith('Resolución: git diff --no-relative '), (case, result)
        elif point == 'conflicto-git':
            assert reason in lines and lines[1].startswith('Resolución: git diff --no-relative '), result
            assert f'Excepción: conflicto de la mezcla en una ruta no compartida: {reason}' in lines, result
        else:
            diagnostic = f'Excepción de migraciones: punto {point}:' if point else 'Excepción de catálogos:'
            assert lines[-1].startswith(diagnostic) and reason in lines[-1], (case, result)
    else:
        assert lines[0].startswith('APTO vigente'), (case, result)
        assert 'Renumeración de Drizzle' in lines[1], (case, result)
        assert f'{directory}/{first["tag"]} → {directory}/{new_first["tag"]}' in lines[1], (case, result)
        if two:
            assert f'{directory}/{second["tag"]} → {directory}/{new_second["tag"]}' in lines[1], (case, result)
        if three:
            assert f'{directory}/{third["tag"]} → {directory}/{new_third["tag"]}' in lines[1], (case, result)
        if mixed:
            assert lines[2:] == ['El catálogo se juntó por claves disjuntas:', 'messages/es.json'], result
    print(f'migración {case} → {lines[0]}')
    if failure:
        print(lines[-1])
PY_MIGRATIONS
}

apto_vigente_conflict() {
    local repo output resolution kind
    for kind in rebase merge; do
        repo=$(apto_repo)
        git -C "$repo" checkout -q revisado
        printf 'x del parche\n' > "$repo/otro.txt"
        git -C "$repo" commit -q -am parche-otro
        git -C "$repo" checkout -q destino
        printf 'x del destino\n' > "$repo/otro.txt"
        printf 'y\n' > "$repo/solo-destino.txt"
        git -C "$repo" add -A
        git -C "$repo" commit -q -m destino
        git -C "$repo" checkout -q -b resuelto revisado
        if [ "$kind" = rebase ]; then
            git -C "$repo" rebase -q destino > /dev/null 2>&1 && return 1
            printf 'x resuelta\n' > "$repo/otro.txt"
            git -C "$repo" add otro.txt
            GIT_EDITOR=true git -C "$repo" rebase --continue > /dev/null 2>&1
        else
            git -C "$repo" merge -q --no-edit destino > /dev/null 2>&1 && return 1
            printf 'x resuelta\n' > "$repo/otro.txt"
            git -C "$repo" add otro.txt
            git -C "$repo" commit -q --no-edit
        fi
        output=$(apto "$repo" destino revisado resuelto)
        assert test "$kind: $(sed -n 1p <<< "$output")" = "$kind: Ronda delta: la mezcla del head revisado con el destino tiene conflictos"
        assert test "$kind: $(sed -n '3,$p' <<< "$output")" = "$kind: otro.txt"
        apto_resolution "$output"
        assert test "$kind: ${resolution[${#resolution[@]}-1]}" = "$kind: $(git -C "$repo" rev-parse resuelto)"
        # El diff que imprime muestra sólo la resolución del conflicto.
        output=$(git -C "$repo" diff --name-only "${resolution[@]}")
        assert test "$kind: $output" = "$kind: otro.txt"
        assert grep -qx '+x resuelta' <(git -C "$repo" diff "${resolution[@]}")
        assert grep -q '^-<<<<<<<' <(git -C "$repo" diff "${resolution[@]}")
        # Desde un subdirectorio y con diff.relative=true sigue mostrando la resolución.
        output=$(apto_with diff.relative=true "$repo/origen" destino revisado resuelto)
        apto_resolution "$output"
        output=$(cd "$repo/origen" && GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=diff.relative GIT_CONFIG_VALUE_0=true git diff --name-only "${resolution[@]}")
        assert test "$kind relativo: $output" = "$kind relativo: otro.txt"
    done

    # El journal introducido sólo en el candidato no activa una excepción.
    printf 'edición independiente\n' >> "$repo/parche.js"
    mkdir -p "$repo/otro/meta"
    printf '{"entries":[]}\n' > "$repo/otro/meta/_journal.json"
    git -C "$repo" add -A
    git -C "$repo" commit -qm sin-excepcion
    output=$(apto "$repo" destino revisado resuelto)
    assert test "$output" = 'Ronda: el head nuevo no es la mezcla del head revisado con el destino'
}

# Corre apto-vigente con una clave de configuración de git del usuario, sin
# escribirla en el repositorio.
apto_with() {
    local pair=$1
    shift
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=${pair%%=*} GIT_CONFIG_VALUE_0=${pair#*=} apto "$@"
}
APTO_CONFIGS=(diff.relative=true diff.ignoreSubmodules=all diff.renames=copies diff.noPrefix=true color.ui=always core.quotePath=false merge.renames=false merge.directoryRenames=true merge.renormalize=true)

apto_vigente_config() {
    local repo output pair expected tree base other
    # Los dos lados cambian zonas distintas del mismo archivo. Ni la
    # configuración ni el directorio desde el que corre esconden ese archivo.
    repo=$(apto_repo)
    git -C "$repo" checkout -q revisado
    printf 'x\n\n\n\n\n\n\n\nfin parche\n' > "$repo/otro.txt"
    git -C "$repo" commit -q -am cola
    git -C "$repo" checkout -q destino
    printf 'inicio destino\nx\n' > "$repo/otro.txt"
    git -C "$repo" commit -q -am cabeza
    git -C "$repo" checkout -q -b rebasado revisado
    git -C "$repo" rebase -q destino
    expected=$(printf 'Ronda delta: el destino cambió archivos del parche\notro.txt')
    for pair in "${APTO_CONFIGS[@]}"; do
        output=$(apto_with "$pair" "$repo" destino revisado rebasado)
        assert test "$pair: $output" = "$pair: $expected"
        output=$(apto_with "$pair" "$repo/origen" destino revisado rebasado)
        assert test "$pair desde origen/: $output" = "$pair desde origen/: $expected"
    done

    # Los dos lados mueven el mismo submódulo, y `.gitmodules` pide ignorarlo.
    repo=$(apto_repo)
    base=$(git -C "$repo" rev-parse destino)
    other=$(git -C "$repo" rev-parse revisado)
    printf '[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n\tignore = all\n' > "$repo/.gitmodules"
    git -C "$repo" add .gitmodules
    git -C "$repo" update-index --add --cacheinfo "160000,$base,sub"
    git -C "$repo" commit -q -m submodulo
    git -C "$repo" checkout -q -B revisado
    printf 'parche\n' > "$repo/parche.js"
    git -C "$repo" add parche.js
    git -C "$repo" update-index --cacheinfo "160000,$other,sub"
    git -C "$repo" commit -q -m parche-y-sub
    git -C "$repo" checkout -q destino
    printf 'y\n' >> "$repo/otro.txt"
    git -C "$repo" add otro.txt
    git -C "$repo" update-index --cacheinfo "160000,$other,sub"
    git -C "$repo" commit -q -m destino-y-sub
    tree=$(git -C "$repo" merge-tree --write-tree destino revisado)
    git -C "$repo" branch candidato "$(git -C "$repo" commit-tree "$tree" -p destino -m candidato)"
    expected=$(printf 'Ronda delta: el destino cambió archivos del parche\nsub')
    for pair in "${APTO_CONFIGS[@]}"; do
        output=$(apto_with "$pair" "$repo" destino revisado candidato)
        assert test "$pair: $output" = "$pair: $expected"
    done

    # Un rebase limpio sobre archivos distintos conserva el APTO con cualquiera de ellas.
    repo=$(apto_repo)
    printf 'y\n' >> "$repo/otro.txt"
    git -C "$repo" commit -q -am destino
    git -C "$repo" checkout -q -b rebasado revisado
    git -C "$repo" rebase -q destino
    for pair in "${APTO_CONFIGS[@]}"; do
        output=$(apto_with "$pair" "$repo/origen" destino revisado rebasado)
        assert test "$pair: ${output%%:*}" = "$pair: APTO vigente"
    done
}

# Un head que mezcla el destino y descarta lo que trajo no conserva el APTO, y
# el que lo mezcla limpio sí, con cualquier estado local.
apto_local_state() {
    local label=$1 repo=$2 output
    output=$(apto "$repo" destino revisado candidato 2>/dev/null)
    assert test "$label: $output" = "$label: Ronda: el head nuevo no es la mezcla del head revisado con el destino"
    output=$(apto "$repo" destino revisado limpio 2>/dev/null)
    assert test "$label: ${output%%:*}" = "$label: APTO vigente"
}

apto_vigente_local_state() {
    local repo base incorporated later
    repo=$(apto_repo)
    base=$(git -C "$repo" rev-parse destino)
    printf 'y\n' >> "$repo/otro.txt"
    git -C "$repo" commit -q -am destino
    incorporated=$(git -C "$repo" rev-parse destino)
    printf 'tarde\n' > "$repo/tarde.txt"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m tarde
    later=$(git -C "$repo" rev-parse destino)
    git -C "$repo" checkout -q -b lado "$base"
    printf 'lado\n' > "$repo/lado.txt"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m lado
    git -C "$repo" checkout -q destino
    git -C "$repo" merge -q --no-edit lado
    git -C "$repo" checkout -q -b limpio revisado
    git -C "$repo" merge -q --no-edit "$incorporated"
    git -C "$repo" checkout -q -b candidato revisado
    git -C "$repo" merge -q --no-edit -s ours "$incorporated"
    apto_local_state 'sin estado local' "$repo"

    # `git replace` muestra el commit limpio en lugar del candidato.
    git -C "$repo" replace candidato limpio
    apto_local_state 'refs/replace' "$repo"
    git -C "$repo" replace -d candidato > /dev/null

    # Un injerto pone al head revisado encima del destino que el candidato descartó.
    printf '%s %s\n' "$(git -C "$repo" rev-parse revisado)" "$incorporated" > "$repo/injertos"
    cp "$repo/injertos" "$repo/.git/info/grafts"
    apto_local_state 'info/grafts' "$repo"
    rm "$repo/.git/info/grafts"
    GIT_GRAFT_FILE=$repo/injertos apto_local_state 'GIT_GRAFT_FILE' "$repo"

    # Un corte de historia esconde que el destino ya contiene lo que el candidato descartó.
    printf '%s\n' "$later" > "$repo/corte"
    cp "$repo/corte" "$repo/.git/shallow"
    apto_local_state 'shallow' "$repo"
    rm "$repo/.git/shallow"
    GIT_SHALLOW_FILE=$repo/corte apto_local_state 'GIT_SHALLOW_FILE' "$repo"

    # La configuración del repositorio tampoco entra, ni el checkout desde el que corre.
    git -C "$repo" config merge.directoryRenames true
    git -C "$repo" config diff.relative true
    git -C "$repo" worktree add -q --detach "$TEST_TMP/apto worktree" candidato
    apto_local_state 'worktree' "$TEST_TMP/apto worktree"
    git -C "$repo" worktree remove --force "$TEST_TMP/apto worktree"
}

# El repositorio temporal lee el mismo almacén de objetos que el local: su
# formato y los almacenes alternos que nombra el entorno.
apto_vigente_object_store() {
    local repo objects other ref output
    repo=$(apto_repo sha256)
    printf 'y\n' >> "$repo/otro.txt"
    git -C "$repo" commit -q -am destino
    git -C "$repo" checkout -q -b candidato revisado
    git -C "$repo" merge -q --no-edit destino
    output=$(apto "$repo" destino revisado candidato)
    assert test "sha256: ${output%%:*}" = 'sha256: APTO vigente'

    # Los commits viven sólo en el almacén que nombra la variable de entorno.
    objects=$(git -C "$repo" rev-parse --path-format=absolute --git-path objects)
    other="$TEST_TMP/apto alterno"
    rm -rf "$other"
    git init -q --object-format=sha256 "$other"
    for ref in destino revisado candidato; do
        GIT_ALTERNATE_OBJECT_DIRECTORIES=$objects git -C "$other" update-ref "refs/heads/$ref" "$(git -C "$repo" rev-parse "$ref")"
    done
    output=$(GIT_ALTERNATE_OBJECT_DIRECTORIES=$objects apto "$other" destino revisado candidato)
    assert test "alterno: ${output%%:*}" = 'alterno: APTO vigente'
}

TEST_CASES=(validation)
case "$(uname)" in
    MINGW*|MSYS*|CYGWIN*)
        printf 'SKIP daemonización, grupos POSIX, store-guard y regresiones POSIX del tren en Windows (Git Bash).\n'
        ;;
    *) TEST_CASES+=(lifecycle write_options resume_run resume_rejections stop_and_wait_timeout stop_without_supervisor done_after_leftover_child resume_while_resuming execution_timeout stop_tool timeout_tool normal_exit_tool killed_supervisor_tool concurrent_stop impossible_confirmation post_launch_exception popen_failure codex_without_snapshot state_during_launch foreign_group launch_window foreign_supervisor_pid concurrent_resume lost_and_nonzero default_out_and_home store_warnings store_silent launcher_guard merge_train_regressions) ;;
esac
TEST_CASES+=(rerun_guard merge_train merge_unenrolled apto_vigente_scratch risk_classify risk_classify_moves_and_errors risk_classify_sources risk_classify_target risk_classify_edges apto_vigente apto_vigente_interactions apto_vigente_catalogs apto_vigente_migrations apto_vigente_conflict apto_vigente_config apto_vigente_local_state apto_vigente_object_store laptop_warnings laptop_silent laptop_detection merge_deny merge_silent merge_ask merge_scope merge_arguments merge_manifest merge_unknown_flags merge_wrappers merge_deadline merge_differential merge_invalid_utf8)
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
        case "$test_case" in
            merge_differential|apto_vigente_migrations) cat "$TEST_TMP/$test_case.log" ;;
        esac
    else printf 'FAIL %s\n' "$test_case"; cat "$TEST_TMP/$test_case.log"; FAILURES=$((FAILURES + 1)); fi
done
printf '%s casos, %s fallos\n' "$CASE" "$FAILURES"
exit "$FAILURES"
