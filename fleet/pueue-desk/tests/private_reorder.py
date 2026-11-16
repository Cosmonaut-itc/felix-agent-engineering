"""U3.6 en un pueued 4.0.4 privado de la Mini. Sólo por remote-run (ver private_reorder.sh).

Cada corrida crea su propio directorio, configuración, socket y daemon; nunca usa el
pueued real, ni ~/.local, ni LaunchAgents. Las tareas son `/usr/bin/true` y
bloqueadores que salen solos. Sólo se termina un proceso por su PID verificado.

Cubre: secuencia normal y deshacer, alias (edit_unsafe sin pausa), roundtrip del
ayudante con comandos difíciles, caída del cliente de edición (Locked) recuperada
con `enqueue` por id, reinicio del daemon con diario pendiente (preparar + guardia),
y la medición del punto 11 con 5 y 64 filas, sin y con el historial MO2.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import patch

LOW = dict(load=0.0, memory=1)
PUEUE, PUEUED = '/opt/homebrew/bin/pueue', '/opt/homebrew/bin/pueued'
BLOCKER = '''i=0
echo $$ >> "$1/bloqueadores.pids"
while [ ! -e "$1/liberar" ] && [ $i -lt 1600 ]; do sleep 0.25; i=$((i+1)); done
'''
HANG = '''import importlib.util, os, sys, time
_spec = importlib.util.spec_from_file_location('editor_real', {real!r})
_real = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_real)
fingerprint = _real.fingerprint
if __name__ == '__main__':
    marker = {marker!r}
    if not os.path.exists(marker):
        with open(marker, 'w') as stream:
            stream.write(str(os.getpid()))
        time.sleep(2.5)  # el cliente muere antes; esta copia sale sola sin tocar el TOML
        sys.exit(9)
    sys.exit(_real.main(sys.argv))
'''


class Crash(BaseException):
    """control.py muere aquí: nada lo atrapa como error ordinario."""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private', action='store_true', required=True)
    parser.parse_args()
    base = Path.cwd().resolve()
    root = base / ('u36o-' + os.urandom(3).hex())
    root.mkdir()  # nunca reutiliza
    data, run, directory = root / 'd', root / 'r', root / 'control'
    for folder in (data, run, directory):
        folder.mkdir()
    config = root / 'pueue.json'
    pid_file = run / 'pueue.pid'
    config.write_text(json.dumps(dict(
        client=dict(edit_mode='toml', restart_in_place=False, show_confirmation_questions=False),
        daemon=dict(compress_state_file=False, pause_group_on_failure=False, pause_all_on_failure=False),
        shared=dict(pueue_directory=str(data), runtime_directory=str(run), pid_path=str(pid_file),
                    use_unix_socket=True, unix_socket_path=str(run / 's'), alias_file=str(data / 'aliases.yml')))))
    # Entorno mínimo: Pueue copia el entorno del cliente en cada tarea y lo guarda en state.json.
    env = dict(PATH='/usr/bin:/bin:/usr/sbin:/sbin', HOME=os.environ['HOME'], LANG='en_US.UTF-8',
               TMPDIR=str(root), PUEUE_CONFIG_PATH=str(config))
    source = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('candidate', source / 'control.py')
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    blocker = root / 'bloquea.sh'
    blocker.write_text(BLOCKER)
    log = (base / 'private-reorder.jsonl').open('a', buffering=1)

    def emit(event, **values):
        line = json.dumps(dict(event=event, **values), sort_keys=True, ensure_ascii=False)
        print(line, flush=True)
        log.write(line + '\n')

    class PrivatePueue(control.Pueue):
        def command(self, *args):
            return [self.binary, '--config', str(config), *map(str, args)]

    pueue = PrivatePueue(PUEUE, env=env, cwd=str(root))
    pueue.timeout = 30
    daemon = dict(proc=None, out=(root / 'daemon.log').open('a'))
    children = []  # Popen propios que deben terminar antes de salir

    def call(*args):
        return pueue.call(*args)

    def ours(pid, needle):
        """PID vivo cuyo comando contiene nuestra ruta privada."""
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='], text=True, capture_output=True)
        return result.returncode == 0 and needle in result.stdout

    def start_daemon(tag):
        assert not pid_file.exists() or not ours(int(pid_file.read_text().strip() or 0), str(config))
        if pid_file.exists():
            pid_file.unlink()  # sólo el de nuestro daemon anterior, ya esperado
        proc = subprocess.Popen([PUEUED, '--config', str(config)], cwd=str(root), env=env,
                                stdout=daemon['out'], stderr=daemon['out'])
        daemon['proc'] = proc
        until = time.monotonic() + 8
        while time.monotonic() < until:
            if proc.poll() is not None:
                raise RuntimeError('pueued privado salió')
            try:
                pueue.status()
                break
            except subprocess.SubprocessError:
                time.sleep(.05)
        else:
            raise RuntimeError('pueued privado no responde')
        comm = subprocess.run(['/bin/ps', '-p', str(proc.pid), '-o', 'comm='], text=True, capture_output=True).stdout
        assert pid_file.read_text().strip() == str(proc.pid) and Path(comm.strip()).name == 'pueued'
        assert ours(proc.pid, str(config))
        emit('daemon', tag=tag, pid=proc.pid)

    def kill_daemon():
        proc = daemon['proc']
        # Verificado: hijo propio, pid file propio, comm pueued y nuestro --config.
        assert proc.poll() is None and pid_file.read_text().strip() == str(proc.pid) and ours(proc.pid, str(config))
        proc.kill()
        proc.wait(timeout=5)

    def stop_daemon():
        proc = daemon['proc']
        if proc is None or proc.poll() is not None:
            return
        try:
            call('shutdown')
            proc.wait(timeout=6)
        except (subprocess.SubprocessError, OSError):
            if proc.poll() is None and ours(proc.pid, str(config)):
                proc.kill()
                proc.wait(timeout=5)

    def tasks(group='suites'):
        snapshot = pueue.status()
        return snapshot, control.queued_rows(snapshot, group)

    def add(*argv, group='suites', stashed=False, label=None):
        extra = (['--stashed'] if stashed else []) + (['--label', label] if label else [])
        out = call('add', '--print-task-id', '-g', group, *extra, '--', *argv)
        return int(out.strip())

    def wait_running(count, group='suites'):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            snapshot = pueue.status()
            running = [t for t in snapshot['tasks'].values() if t['group'] == group
                       and control.task_state(t)[0] == 'Running']
            if len(running) == count:
                return
            time.sleep(.05)
        raise RuntimeError('bloqueadores sin arrancar')

    def engine_for():
        engine = control.MiniControl(pueue, directory, host='mini')
        engine.pueue_data, engine.pueue_config = data, config
        engine.alias_default = data / 'aliases.yml'
        return engine

    def instrument(engine):
        """Marca de tiempo de cada RPC del reordenar (pause/start/enqueue) y de cada edición."""
        marks = []
        rpc, edit = engine._rpc, engine._reorder_edit

        def timed_rpc(*args):
            marks.append((args[0], time.monotonic()))
            try:
                return rpc(*args)
            finally:
                marks.append((args[0] + ':fin', time.monotonic()))

        def timed_edit(key, task, target, phase):
            marks.append(('edit', time.monotonic()))
            try:
                return edit(key, task, target, phase)
            finally:
                marks.append(('edit:fin', time.monotonic()))
        engine._rpc, engine._reorder_edit = timed_rpc, timed_edit
        return marks

    def window(marks):
        pause = next((t for n, t in marks if n == 'pause'), None)
        start = next((t for n, t in reversed(marks) if n == 'start:fin'), None)
        edits = [b - a for (n, a), (m, b) in zip(marks, marks[1:]) if n == 'edit' and m == 'edit:fin']
        return dict(pause_ms=round((start - pause) * 1000) if pause and start else None,
                    edits=len(edits), edit_ms_max=round(max(edits) * 1000) if edits else None,
                    edit_ms_avg=round(sum(edits) / len(edits) * 1000) if edits else None,
                    rpcs=[n for n, _ in marks if not n.endswith(':fin')])

    counter = [0]

    def request(engine, order, op='reorder', undo_of=None, group='suites'):
        counter[0] += 1
        view = engine.view()
        info = view['reorder'][group]
        expected = dict(group=group, generation=view['generation'], queue_etag=info.get('queue_etag', 'a' * 64))
        body = dict(schema=control.HOST_SCHEMA, op=op, action_id='u36opus-%04d' % counter[0], dry_run=False,
                    expected=expected)
        if op == 'reorder':
            expected['queue'] = info.get('queue', [[1, '0' * 32, 0], [2, '0' * 32, 0]])
            body['change'] = dict(order=order)
        else:
            body['undo_of'] = undo_of
        return body

    def handle(engine, body):
        started = time.monotonic()
        with control.locked(directory):
            result = engine.handle(body)
        return result, round((time.monotonic() - started) * 1000)

    def reasons(engine):
        with engine.connect() as db:
            return engine.state(db)['reasons']

    def identity(snapshot, ids):
        return {i: [snapshot['tasks'][str(i)][k] for k in ('command', 'original_command', 'path', 'label')]
                for i in ids}

    def released_wait(pids, needle, seconds=6):
        """Espera a que salgan los PIDs propios; mata sólo los verificados que sigan."""
        until = time.monotonic() + seconds
        alive = list(pids)
        while alive and time.monotonic() < until:
            alive = [p for p in alive if ours(p, needle)]
            time.sleep(.1)
        for pid in alive:
            if ours(pid, needle):
                os.kill(pid, signal.SIGKILL)
        return alive

    try:
        emit('version', pueue=subprocess.check_output([PUEUE, '--version'], text=True).strip(),
             pueued=subprocess.check_output([PUEUED, '--version'], text=True).strip(),
             python=sys.version.split()[0], root=root.name)
        try:
            emit('presion_real_informativa', **control.pressure_mini())
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            emit('presion_real_informativa', error=type(error).__name__)
        start_daemon('inicial')
        call('group', 'add', 'suites', '--parallel', control.MINI_PROFILE['suites'])
        call('group', 'add', 'rapidas', '--parallel', control.MINI_PROFILE['rapidas'])
        call('parallel', control.MINI_PROFILE['default'], '-g', 'default')
        call('group', 'add', 'banco', '--parallel', 1)
        call('pause', '--wait', '-g', 'banco')  # su única tarea nunca corre: sólo se edita
        for _ in range(control.MINI_PROFILE['suites']):
            add('/bin/sh', str(blocker), str(root), label='pao.u36.opus.bloqueador')
        wait_running(control.MINI_PROFILE['suites'])
        queued = [add('/usr/bin/true', 'fila-%d' % k, label='pao.u36.opus.fila') for k in range(1, 6)]
        with patch.object(control, 'pressure_mini', return_value=dict(LOW)):
            with control.locked(directory):
                engine_for().adopt(pid_file)
            run_scenarios(control, emit, engine_for, instrument, window, request, handle, reasons, tasks, add,
                          call, identity, queued, root, data, pid_file, kill_daemon, start_daemon, released_wait,
                          children, config)
        emit('PASS', assertions='normal+deshacer; alias sin pausa; ayudante roundtrip; Locked→enqueue; '
                                'reinicio con diario; medición 5/64/MO2')
    finally:
        (root / 'liberar').touch()
        for child in children:
            if child.poll() is None:
                child.kill()  # hijo Popen propio, aún sin cosechar: su PID no pudo reutilizarse
                child.wait(timeout=5)
        stop_daemon()
        pids = [int(p) for p in (root / 'bloqueadores.pids').read_text().split()] if (root / 'bloqueadores.pids').exists() else []
        survivors = released_wait(pids, str(blocker))
        emit('limpieza', daemon_cosechado=daemon['proc'] is None or daemon['proc'].poll() is not None,
             bloqueadores=len(pids), bloqueadores_forzados=len(survivors),
             hijos_vivos=sum(c.poll() is None for c in children))
        daemon['out'].close()
        log.close()


def run_scenarios(control, emit, engine_for, instrument, window, request, handle, reasons, tasks, add, call,
                  identity, queued, root, data, pid_file, kill_daemon, start_daemon, released_wait, children, config):
    directory = root / 'control'

    def settled(result, marks, ids, priorities):
        """Aplicado dentro de la pausa, rechazado antes de pausar, o abortado y restaurado."""
        rows = tasks()[1]
        if result['status'] == 'applied':
            assert window(marks)['pause_ms'] <= control.REORDER_PAUSE_SECONDS * 1000, window(marks)
            assert [t['id'] for t in rows] == [ids[-1]] + ids[:-1], rows
        elif result['status'] == 'rejected':
            assert result['error']['code'] == 'too_long' and marks == [], result
        else:
            assert result['status'] == 'aborted', result
            assert priorities == {t['id']: t['priority'] for t in rows}, rows
        assert tasks()[0]['groups']['suites']['status'] == 'Running'

    bench_task = add('/usr/bin/true', 'banco', group='banco', label='pao.u36.opus.banco')

    def breakdown(tag):
        """Dónde se va una edición: Pueue solo, arranque de Python, cargar el ayudante, SQLite."""
        def timed(fn, n=8):
            samples = []
            for _ in range(n):
                t = time.monotonic()
                fn()
                samples.append(time.monotonic() - t)
            samples.sort()
            return dict(med=round(samples[len(samples) // 2] * 1000), max=round(samples[-1] * 1000))
        env = dict(pueue.context['env'])

        def bare(editor):
            def run():
                subprocess.run(pueue.command('edit', bench_task), env=dict(env, EDITOR=editor), check=True,
                               capture_output=True, timeout=10, cwd=str(root))
            return run
        engine = engine_for()
        emit('desglose', tag=tag, carga=os.getloadavg()[0], state_bytes=(data / 'state.json').stat().st_size,
             pueue_edit_true_ms=timed(bare('/usr/bin/true')),
             pueue_edit_python_ms=timed(bare(sys.executable + ' -I -B -c pass')),
             python_arranque_ms=timed(lambda: subprocess.run([sys.executable, '-I', '-B', '-c', 'pass'], check=True)),
             cargar_ayudante_ms=timed(lambda: control.load_editor(engine.editor)),
             sqlite_full_ms=timed(lambda: reasons_write(engine)),
             status_json_ms=timed(lambda: pueue.status(), n=4))

    def reasons_write(engine):
        with engine.connect(write=True) as db:
            engine.store(db, engine.state(db))

    breakdown('historial-pequeño')

    # ---- 1. Secuencia normal (5 filas) y deshacer ----------------------------
    engine = engine_for()
    info = engine.view()['reorder']['suites']
    assert info['enabled'], info
    _, rows = tasks()
    original = [t['id'] for t in rows]
    order = [original[0], original[4]] + original[1:4]
    marks = instrument(engine)
    result, total = handle(engine, request(engine, order))
    snapshot, rows = tasks()
    assert result['status'] == 'applied', result
    assert [t['id'] for t in rows] == order, rows
    assert snapshot['groups']['suites']['status'] == 'Running'
    assert not reasons(engine).get('suites'), reasons(engine)
    assert {t['id']: t['priority'] for t in rows} == dict(result['result']['after'])
    assert window(marks)['rpcs'] == ['pause'] + ['edit'] * result['result']['writes'] + ['start'], marks
    emit('normal', filas=5, carga=os.getloadavg()[0], writes=result['result']['writes'], total_ms=total,
         estimate_ms=result['result']['estimate_ms'], **window(marks))
    applied = result
    engine = engine_for()
    marks = instrument(engine)
    undo = request(engine, None, op='reorder_undo', undo_of=applied['action_id'])
    result, total = handle(engine, undo)
    snapshot, rows = tasks()
    assert result['status'] == 'applied', result
    assert [t['id'] for t in rows] == original
    assert {t['id']: t['priority'] for t in rows} == dict(applied['result']['before'])
    assert snapshot['groups']['suites']['status'] == 'Running'
    emit('deshacer', total_ms=total, **window(marks))
    # Repetir el mismo action_id devuelve el mismo recibo sin efectos nuevos.
    engine = engine_for()
    marks = instrument(engine)
    again, _ = handle(engine, undo)
    assert again == result and marks == [], again
    emit('idempotente', status=again['status'])

    # ---- 2. Alias: edit_unsafe antes de cualquier pausa ----------------------
    (data / 'aliases.yml').write_text('u36alias: /usr/bin/true\n')
    alias = add('u36alias', 'fila-alias', label='pao.u36.opus.alias')
    snapshot, _ = tasks()
    expanded = snapshot['tasks'][str(alias)]
    engine = engine_for()
    info = engine.view()['reorder']['suites']
    assert info['enabled'] is False and info['code'] == 'edit_unsafe', info
    marks = instrument(engine)
    _, rows = tasks()
    body = request(engine, None)
    body['expected']['queue'] = [[t['id'], control.task_identity(t)[:32], t['priority']] for t in rows]
    body['change'] = dict(order=[t['id'] for t in rows][::-1])
    result, _ = handle(engine, body)
    assert result['status'] == 'rejected' and result['error']['code'] == 'edit_unsafe', result
    assert marks == [], marks
    assert tasks()[0]['groups']['suites']['status'] == 'Running'
    emit('alias', code=result['error']['code'], rpcs=len(marks),
         expandido=expanded['command'] != expanded['original_command'])
    call('remove', alias)
    (data / 'aliases.yml').unlink()

    # ---- 3. Roundtrip del ayudante con comandos difíciles ------------------
    hard = [add('/usr/bin/true', 'priority = 9', '"comillas"', "'simples'", 'ñandú', '#', '\\', label='pao.u36.opus.dificil'),
            add('/usr/bin/true', 'línea\npriority = 7\n[[tabla]]', label='pao.u36.opus.multilinea'),
            add('/usr/bin/true', 'sin-etiqueta')]
    engine = engine_for()
    assert engine.view()['reorder']['suites']['enabled']
    snapshot, rows = tasks()
    ids = [t['id'] for t in rows]
    before = identity(snapshot, ids)
    order = list(reversed(ids))
    marks = instrument(engine)
    result, total = handle(engine, request(engine, order))
    snapshot, rows = tasks()
    assert result['status'] == 'applied', result
    assert [t['id'] for t in rows] == order
    assert identity(snapshot, ids) == before, 'el ayudante sólo cambia priority'
    emit('ayudante_roundtrip', filas=len(ids), writes=result['result']['writes'], total_ms=total, **window(marks))
    for ident in hard:
        call('remove', ident)

    # ---- 4. Caída del cliente de edición: Locked → enqueue por id ----------
    hang = root / 'editor_cuelga.py'
    marker = root / 'cuelga.pid'
    hang.write_text(HANG.format(real=str(engine.editor), marker=str(marker)))
    engine = engine_for()
    engine.editor = hang
    snapshot, rows = tasks()
    ids = [t['id'] for t in rows]
    priorities = {t['id']: t['priority'] for t in rows}
    marks = instrument(engine)
    result, total = handle(engine, request(engine, [ids[-1]] + ids[:-1]))
    snapshot, rows = tasks()
    assert result['status'] == 'aborted' and result['error']['code'] == 'interrupted', result
    assert [t['id'] for t in rows] == ids and {t['id']: t['priority'] for t in rows} == priorities
    assert snapshot['groups']['suites']['status'] == 'Running'
    assert not any(control.task_state(t)[0] in ('Locked', 'Stashed') for t in snapshot['tasks'].values()
                   if t['group'] == 'suites')
    names = window(marks)['rpcs']
    assert 'enqueue' in names, names
    hung = int(marker.read_text())
    leftover = released_wait([hung], str(hang))
    emit('locked_enqueue', status=result['status'], code=result['error']['code'], rpcs=names, total_ms=total,
         editor_colgado_forzado=bool(leftover))

    # ---- 5. 64 filas sin historial grande ----------------------------------
    extra = [add('/usr/bin/true', 'larga-%d' % k, label='pao.u36.opus.larga')
             for k in range(control.REORDER_MAX - len(tasks()[1]))]
    engine = engine_for()
    info = engine.view()['reorder']['suites']
    assert info['enabled'] and len(info['queue']) == control.REORDER_MAX, info
    rows = tasks()[1]
    ids, priorities = [t['id'] for t in rows], {t['id']: t['priority'] for t in rows}
    marks = instrument(engine)
    result, total = handle(engine, request(engine, [ids[-1]] + ids[:-1]))
    emit('medicion', historial='pequeño', carga=os.getloadavg()[0], state_bytes=(data / 'state.json').stat().st_size, filas=len(ids),
         status=result['status'], code=(result.get('error') or {}).get('code'),
         writes=(result.get('result') or {}).get('writes'), max_writes=info['max_writes'], total_ms=total,
         estimate_ms=(result.get('result') or {}).get('estimate_ms'), **window(marks))
    settled(result, marks, ids, priorities)
    one_more = add('/usr/bin/true', 'sobra', label='pao.u36.opus.larga')
    info = engine_for().view()['reorder']['suites']
    assert info['enabled'] is False and info['code'] == 'too_long', info
    emit('demasiado_larga', filas=control.REORDER_MAX + 1, code=info['code'])
    for ident in extra + [one_more]:
        call('remove', ident)

    # ---- 6. Historial MO2: 1500 Stashed de 2 KiB en un grupo ajeno ---------
    call('group', 'add', 'big', '--parallel', 1)
    long = 'x' * 2048
    started = time.monotonic()
    for _ in range(1500):
        add('/usr/bin/true', long, group='big', stashed=True)
    call('pause', '--wait', '-g', 'big')  # guarda el estado completo
    emit('mo2', state_bytes=(data / 'state.json').stat().st_size, alta_s=round(time.monotonic() - started, 1))
    breakdown('MO2')
    for size in (5, 20, control.REORDER_MAX):
        rows = tasks()[1]
        if len(rows) < size:
            extra = [add('/usr/bin/true', 'mo2-%d' % k, label='pao.u36.opus.mo2') for k in range(size - len(rows))]
        rows = tasks()[1]
        while len(rows) > size:
            call('remove', rows[-1]['id'])
            rows = tasks()[1]
        engine = engine_for()
        info = engine.view()['reorder']['suites']
        ids, priorities = [t['id'] for t in rows], {t['id']: t['priority'] for t in rows}
        marks = instrument(engine)
        result, total = handle(engine, request(engine, [ids[-1]] + ids[:-1]))
        emit('medicion', historial='MO2', carga=os.getloadavg()[0], state_bytes=(data / 'state.json').stat().st_size, filas=size,
             status=result['status'], code=(result.get('error') or {}).get('code'),
             writes=(result.get('result') or {}).get('writes'), max_writes=info.get('max_writes'),
             estimate_ms=(result.get('result') or {}).get('estimate_ms'), total_ms=total, **window(marks))
        settled(result, marks, ids, priorities)
    for row in tasks()[1][5:]:
        call('remove', row['id'])  # el reinicio parte de 5 filas

    # ---- 7. Reinicio del daemon con diario pendiente (Locked) ---------------
    marker.unlink()
    engine = engine_for()
    engine.editor = hang
    assert engine.view()['reorder']['suites']['enabled'], engine.view()['reorder']['suites']
    snapshot, rows = tasks()
    ids = [t['id'] for t in rows]
    priorities = {t['id']: t['priority'] for t in rows}
    spawn = engine.pueue.edit_process

    def dying_edit(ident, env):
        process = spawn(ident, env)
        children.append(process)

        class Dies:
            # Muerte dura de control.py: nadie mata ni espera al cliente de edición.
            pid = process.pid

            def kill(self):
                pass

            def wait(self, timeout=None):
                pass

            def communicate(self, data, timeout=None):
                process.stdin.write(data)
                process.stdin.flush()
                time.sleep(.6)  # Pueue ya bloqueó la tarea y el editor cuelga
                raise Crash('control.py muere con la edición abierta')
        return Dies()
    engine.pueue.edit_process = dying_edit
    body = request(engine, [ids[-1]] + ids[:-1])
    try:
        try:
            handle(engine, body)
            raise AssertionError('debía morir')
        except Crash:
            pass
    finally:
        del engine.pueue.edit_process
    snapshot, _ = tasks()
    locked = [t['id'] for t in snapshot['tasks'].values() if control.task_state(t)[0] == 'Locked']
    with engine.connect() as db:
        journal = control.reorder_journal(db)
    emit('diario_pendiente', locked=locked, diario=sorted(journal), razones=reasons(engine).get('suites'))
    assert locked and [str(i) for i in locked] == sorted(journal), (locked, journal)
    kill_daemon()
    for child in children:
        try:
            child.wait(timeout=6)  # el cliente pierde el socket y sale solo
        except subprocess.TimeoutExpired:
            pass
    released_wait([int(marker.read_text())], str(hang))
    with control.locked(directory):
        image = control.prepare_start(engine_for(), configuration=dict(data=data, pid_file=pid_file))
    restored = {k: control.task_state(v)[0] for k, v in image['tasks'].items() if int(k) in ids}
    emit('preparar', estados=sorted(set(restored.values())), grupo=image['groups']['suites']['status'])
    assert 'Locked' not in restored.values() and 'Stashed' not in restored.values(), restored
    start_daemon('tras-reinicio')
    for tick in range(4):
        engine = engine_for()
        with control.locked(directory):
            engine.guard(dict(LOW))  # la guardia de 60 s: el único recuperador junto a preparar
        receipt = engine.handle(dict(schema=control.HOST_SCHEMA, op='status', action_id=body['action_id']))
        snapshot, rows = tasks()
        emit('guardia', tick=tick, recibo=receipt.get('status'), grupo=snapshot['groups']['suites']['status'],
             razones=reasons(engine).get('suites'))
        if receipt.get('status') != 'applying' and snapshot['groups']['suites']['status'] == 'Running':
            break
        time.sleep(1)
    assert receipt['status'] == 'aborted', receipt
    assert {t['id']: t['priority'] for t in rows} == priorities, rows
    assert not any(control.task_state(t)[0] in ('Locked', 'Stashed') for t in snapshot['tasks'].values()
                   if t['group'] == 'suites')
    emit('reinicio_con_diario', recibo=receipt['status'], code=(receipt.get('error') or {}).get('code'),
         grupo=snapshot['groups']['suites']['status'], razones=reasons(engine).get('suites'))


if __name__ == '__main__':
    main()
