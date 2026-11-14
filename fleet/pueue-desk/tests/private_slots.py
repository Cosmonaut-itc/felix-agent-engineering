"""Run ONLY via remote-run on Mini or desk, with a private daemon; only a stashed synthetic task is submitted on desk."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private', action='store_true', required=True)
    parser.add_argument('--host', choices=('mini','desk'), default='mini')
    parser.add_argument('--scenario', choices=('slots','mode'), default='slots')
    parser.add_argument('--pueue', default='/opt/homebrew/bin/pueue' if sys.platform == 'darwin' else str(Path.home()/'.local/bin/pueue'))
    parser.add_argument('--pueued', default='/opt/homebrew/bin/pueued' if sys.platform == 'darwin' else str(Path.home()/'.local/bin/pueued'))
    args = parser.parse_args()
    base = Path.cwd().resolve()
    root = base/('private-u33-opus' if args.scenario == 'mode' else 'private-u32-sol' if args.host == 'desk' else 'private-u31-sol')
    root.mkdir()  # refuse reuse
    config = root/'config.json'
    config.write_text(json.dumps(dict(shared=dict(pueue_directory=str(root),runtime_directory=str(root),
        pid_path=str(root/'pid'),unix_socket_path=str(base/'u31-sol.sock'),use_unix_socket=True,
        alias_file=str(root/'aliases.yml'),shared_secret_path=str(root/'secret')),
        daemon=dict(compress_state_file=False))))
    env = dict(os.environ, TMPDIR=str(root), PUEUE_CONFIG_PATH=str(config))
    controller = base/'control.py'
    if not controller.is_file():
        controller = Path(__file__).resolve().parents[1]/'control.py'
    spec = importlib.util.spec_from_file_location('candidate', controller)
    control = importlib.util.module_from_spec(spec); spec.loader.exec_module(control)
    log = (base/'private-slots.jsonl').open('w', buffering=1)
    def emit(event, **values):
        line = json.dumps(dict(event=event, **values), sort_keys=True)
        print(line, flush=True); log.write(line+'\n')
    class PrivatePueue(control.Pueue):
        def call(self, *argv):
            return subprocess.run([args.pueue,'--config',str(config),*map(str,argv)],
                check=True, capture_output=True, text=True, timeout=4, env=env, cwd=str(base)).stdout
    pueue = PrivatePueue()
    daemon, output = None, (root/'daemon.log').open('a')
    def start():
        nonlocal daemon
        pid = root/'pid'
        if pid.exists(): pid.unlink()  # only after wait() of OUR previous child
        daemon = subprocess.Popen([args.pueued,'--config',str(config)],
            cwd=str(base),env=env,stdout=output,stderr=output)
        until = time.monotonic()+5
        while time.monotonic()<until:
            if daemon.poll() is not None: raise RuntimeError('private daemon exited')
            try: pueue.status(); return
            except subprocess.SubprocessError: time.sleep(.05)
        raise RuntimeError('private daemon unavailable')
    def crash():
        daemon.kill(); daemon.wait(timeout=4)
    group = 'jale' if args.host == 'desk' else 'suites'
    now = [datetime(2026,11,14,13,59,59,tzinfo=timezone.utc).timestamp()]
    profile = control.desk_profile(now[0]) if args.host == 'desk' else control.MINI_PROFILE
    def request(engine, ident, delta):
        view = engine.view()
        return dict(schema=control.HOST_SCHEMA,op='slots',action_id=ident,dry_run=False,
                    expected={k:view[k] for k in ('revision','policy_hash','generation','mode','slots')},
                    change=dict(group=group,delta=delta))
    try:
        emit('version', version=subprocess.check_output([args.pueue,'--config',str(config),'--version'],text=True).strip())
        start()
        if args.host == 'desk' and args.scenario == 'mode':
            def add_task(group, *argv):
                # Minimal environment: the task record never inherits the runner's variables.
                clean = dict(PATH='/usr/bin:/bin', HOME=str(root), TMPDIR=str(root), PUEUE_CONFIG_PATH=str(config))
                subprocess.run([args.pueue,'--config',str(config),'add','--group',group,'--',*argv],
                               check=True, capture_output=True, text=True, timeout=4, env=clean, cwd=str(root))
            desk_mode_scenario(control, pueue, root, emit, add_task)
            return
        if args.host == 'desk':
            desk_scenario(control, pueue, root, start, crash, emit)
            return
        for name, slots in profile.items():
            if name == 'default': pueue.parallel(name,slots)
            else: pueue.call('group','add',name,'--parallel',slots)
        pueue.call('pause','--wait','-g','default')  # save exact baseline
        directory = root/'control'; directory.mkdir()
        engine = control.MiniControl(pueue,directory,host=args.host,clock=lambda:now[0])
        with control.locked(directory):
            engine.adopt(root/'pid')
            before = engine.view()
            down = engine.handle(request(engine,'private-down-0001',-1))
            assert down['status']=='applied', down
            emit('unsaved_parallel',before=before['slots'],after=down['result']['observed'])
            crash(); start()
            restored = engine.view()
            emit('after_restart_without_save',observed=restored['slots'],desired=restored['desired'])
            assert restored['slots'][group]==profile[group], restored
            assert engine.reconcile()
            recovered = engine.view()
            assert recovered['slots'][group]==profile[group]-1
            emit('reconciled', observed=recovered['slots'],desired=recovered['desired'],
                 paused=recovered['pause_reasons'])
            up = engine.handle(request(engine,'private-up-00001',1))
            assert up['status']=='applied',up
            pueue.call('pause','--wait','-g',group)  # this operation saves parallel too
            crash(); start()
            persisted = engine.view()
            assert persisted['slots'][group]==profile[group]
            assert engine.reconcile()
            emit('saved_parallel_survives',observed=persisted['slots'],desired=persisted['desired'])
            emit('PASS', assertions='parallel unsaved lost; saved survives; durable candidate reconciles both; no tasks')
    finally:
        if daemon is not None and daemon.poll() is None:
            try: pueue.call('shutdown')
            except subprocess.SubprocessError: pass
            finally:
                try: daemon.wait(timeout=4)
                except subprocess.TimeoutExpired: daemon.kill(); daemon.wait(timeout=4)
        emit('cleanup', daemon_reaped=daemon is None or daemon.poll() is not None)
        output.close(); log.close()


def desk_scenario(control, pueue, root, start, crash, emit):
    """Private stock daemon, real timer/guard dispatch, synthetic clock/sensors only."""
    now = [datetime(2026,11,14,13,59,59,tzinfo=timezone.utc).timestamp()]
    table = control.desk_profile(now[0])
    for group, slots in table.items():
        pueue.call('group', 'add', group, '--parallel', slots)
    pueue.call('pause', '--wait', '-g', 'default')
    directory = root/'control'; directory.mkdir()
    engine = control.MiniControl(pueue, directory, clock=lambda:now[0], host='desk')
    low = dict(io=0, cpu=0, memory=64*1024**3)
    def request(ident, delta=-1, undo_of=None):
        v = engine.view()
        return dict(schema=control.HOST_SCHEMA, op='undo' if undo_of else 'slots', action_id=ident,
                    dry_run=False, expected={k:v[k] for k in ('revision','policy_hash','generation','mode','slots')},
                    **(dict(undo_of=undo_of) if undo_of else dict(change=dict(group='jale',delta=delta))))
    def entry(command):
        # main takes the actual lock and dispatches aplicar/guardia. Never real HOME.
        with patch.object(control, 'STATE_DIR', directory), patch.object(control, 'Pueue', return_value=pueue), \
             patch.object(control, 'MiniControl', return_value=engine), patch.object(control, 'pressure', return_value=low), \
             patch.object(control.sys, 'argv', ['control.py',command,'--host','desk']):
            control.main()
    with control.locked(directory):
        adopted = engine.adopt(root/'pid')
        down = engine.handle(request('private-desk-down1'))
    assert down['status'] == 'applied', down
    assert down['result']['expires_at'] == 1794664800., down
    emit('adjusted', before=table, observed=down['result']['observed'], expires_at=down['result']['expires_at'])
    # Actual timer dispatch first preserves the live override, then applies the next event.
    entry('aplicar')
    assert engine.view()['slots']['jale'] == 6
    now[0] += 1
    entry('aplicar')
    after = engine.view(); day = control.desk_profile(now[0])
    assert after['slots'] == day and after['desired'] == day and after['expires_at'] is None, after
    with engine.connect() as db:
        assert engine.state(db)['overrides'] == {}
    with control.locked(directory):
        undo = engine.handle(request('private-desk-undo1', undo_of='private-desk-down1'))
    assert (undo['status'], (undo.get('error') or {}).get('code'), undo.get('effects')) == ('conflict','undo_conflict',False), undo
    assert engine.view()['slots'] == day
    emit('event_applied_by_timer', mode=after['mode'], observed=after['slots'], undo=undo['error']['code'], overrides_retired=True)
    # Save this baseline before the deliberately unsaved parallel operation.
    pueue.call('pause','--wait','-g','default')
    with control.locked(directory):
        down = engine.handle(request('private-desk-down2'))
    assert down['status'] == 'applied', down
    desired = dict(day, jale=4)
    crash(); start()
    lost = engine.view()
    assert lost['slots'] == day and lost['recovery_pending'], lost
    emit('after_restart_without_save', observed=lost['slots'], desired=lost['desired'], recovery_pending=True)
    entry('guardia')
    recovered = engine.view()
    assert recovered['slots'] == desired and recovered['generation'] != adopted['generation'], recovered
    emit('reconciled_by_guard', observed=recovered['slots'], paused=recovered['pause_reasons'])
    # An undo after a missed timer must execute no RPC. Count our private transport calls.
    now[0] = control.schedule_at(now[0])[1] + 120
    with patch.object(pueue, 'parallel', wraps=pueue.parallel) as parallel, control.locked(directory):
        expired = engine.handle(request('private-expired-undo1', undo_of='private-desk-down2'))
    assert expired['status'] == 'conflict' and expired.get('effects') is False, expired
    assert parallel.call_count == 0, parallel.call_args_list
    assert engine.view()['slots'] == desired
    emit('missed_timer_undo_no_rpc', status=expired['status'], effects=expired['effects'], rpc_count=parallel.call_count)
    # A stashed task in default must not veto the 05:00Z durable night table.
    pueue.call('add', '--stashed', '--group', 'default', '--', 'true')
    entry('guardia')
    missed = engine.view()
    assert missed['slots'] == table and missed['mode'] == 'noche' and missed['expires_at'] is None, missed
    assert missed['foreign_work'] == 1, missed
    emit('missed_timer_applied_by_guard', mode=missed['mode'], observed=missed['slots'], foreign_work=missed['foreign_work'])
    # A further missed event is recovered by an ordinary real slots click, without accepting it.
    with control.locked(directory):
        adjusted = engine.handle(request('private-desk-down3'))
    assert adjusted['status'] == 'applied', adjusted
    now[0] = control.schedule_at(now[0])[1]
    with control.locked(directory):
        click = engine.handle(request('private-missed-slots'))
    assert click['status'] == 'conflict' and click.get('effects') is True, click
    assert engine.view()['slots'] == control.desk_profile(now[0]) and not engine.view()['drift']
    emit('missed_timer_applied_by_slots', status=click['status'], observed=engine.view()['slots'])
    emit('PASS', assertions='timer preserves override then expires; guard restores lost parallel; missed timer; '
         'expired undo no RPC; ordinary click recovers; 05:00Z with stashed default task')


def desk_mode_scenario(control, pueue, root, emit, add_task):
    """U3.3 Día/Noche against a private stock pueued: synthetic clock, real parallel RPCs."""
    utc = lambda text: datetime.strptime(text, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
    now = [utc('2026-11-14T22:00:00')]  # 16:00 en México, horario de día
    dia, noche = control.mode_table('dia'), control.mode_table('noche')
    for group, slots in dia.items():
        pueue.call('group', 'add', group, '--parallel', slots)
    pueue.call('pause', '--wait', '-g', 'default')
    directory = root/'control'; directory.mkdir()
    engine = control.MiniControl(pueue, directory, clock=lambda: now[0], host='desk')
    low = dict(io=0, cpu=0, memory=64*1024**3)
    def observed():
        groups = pueue.status()['groups']
        return {g: groups[g]['parallel_tasks'] for g in dia}
    def running(group='jale'):
        return sum(1 for t in pueue.status()['tasks'].values() if t.get('group') == group and 'Running' in (t.get('status') or {}))
    def send(ident, mode=None, undo_of=None):
        v = engine.view()
        request = dict(schema=control.HOST_SCHEMA, op='undo' if undo_of else 'mode', action_id=ident, dry_run=False,
                       expected={k: v[k] for k in ('revision', 'policy_hash', 'generation', 'mode', 'slots')},
                       **(dict(undo_of=undo_of) if undo_of else dict(change=dict(mode=mode))))
        with control.locked(directory):
            return engine.handle(request)
    def entry(command):
        with patch.object(control, 'STATE_DIR', directory), patch.object(control, 'Pueue', return_value=pueue), \
             patch.object(control, 'MiniControl', return_value=engine), patch.object(control, 'pressure', return_value=low), \
             patch.object(control.sys, 'argv', ['control.py', command, '--host', 'desk']):
            control.main()
    def notes():
        return engine.view()['notes']
    with control.locked(directory):
        engine.adopt(root/'pid')
    v = engine.view()
    assert v['capabilities']['mode'] is True, 'sin datos de zona horaria'
    assert notes() == dict(mode='Automático · cambia a noche a las 23:00', detail=None), notes()
    emit('zona_horaria', zone=control.MODE_ZONE, notes=notes())
    first = send('u33-desk-noche-01', 'noche')
    assert first['status'] == 'applied' and observed() == noche, first
    assert first['result']['expires_at'] == utc('2026-11-15T05:00:00'), first
    assert notes() == dict(mode='Adelantado · vuelve a día a las 08:00', detail='Ajustes manuales hasta las 23:00'), notes()
    emit('adelanto_noche', observed=observed(), cap=engine.view()['cap'], notes=notes(), rpc=first['result']['rpc'])
    for _ in range(7):
        add_task('jale', 'sleep', '90')
    deadline = time.monotonic() + 10
    while running() < 7 and time.monotonic() < deadline: time.sleep(.1)
    assert running() == 7, running()
    back = send('u33-desk-deshacer1', undo_of='u33-desk-noche-01')
    assert back['status'] == 'applied' and observed() == dia, back
    assert running() == 7, 'una reducción no detiene tareas vivas'
    with engine.connect() as db:
        assert engine.state(db)['mode_override'] is None
    emit('deshacer_18_a_14_con_tareas_vivas', observed=observed(), running_jale=running(), notes=notes())
    again = send('u33-desk-noche-02', 'noche')
    assert again['status'] == 'applied', again
    now[0] = utc('2026-11-15T05:00:00')  # 23:00 en México; noche sigue, el adelanto vence
    entry('aplicar')
    with engine.connect() as db:
        state = engine.state(db)
    assert state['mode_override'] is None and observed() == noche, state
    assert notes() == dict(mode='Automático · cambia a día a las 08:00', detail=None), notes()
    late = send('u33-desk-deshacer2', undo_of='u33-desk-noche-02')
    assert (late['status'], (late.get('error') or {}).get('code'), late.get('effects')) == ('conflict', 'undo_conflict', False), late
    assert observed() == noche
    emit('evento_retira_adelanto', observed=observed(), notes=notes(), undo=late['error']['code'])
    now[0] = utc('2026-11-15T06:00:00')
    day = send('u33-desk-dia-0001', 'dia')
    assert day['status'] == 'applied' and observed() == dia and running() == 7, day
    assert notes() == dict(mode='Adelantado · vuelve a noche a las 23:00', detail='Ajustes manuales hasta las 08:00'), notes()
    emit('adelanto_dia_con_tareas_vivas', observed=observed(), running_jale=running(), notes=notes())
    now[0] = utc('2026-11-15T14:00:00')
    entry('aplicar')
    assert observed() == dia and notes() == dict(mode='Automático · cambia a noche a las 23:00', detail=None), notes()
    pueue.call('add', '--stashed', '--group', 'default', '--', 'true')
    before = observed()
    vetoed = send('u33-desk-ajeno-01', 'noche')
    assert (vetoed['status'], (vetoed.get('error') or {}).get('code'), vetoed.get('effects')) == ('rejected', 'foreign_work', False), vetoed
    assert observed() == before
    emit('trabajo_ajeno_veta_subida', status=vetoed['status'], observed=observed())
    pueue.call('kill', '-g', 'jale')  # only the synthetic sleeps of this private daemon
    deadline = time.monotonic() + 10
    while running() and time.monotonic() < deadline: time.sleep(.1)
    assert running() == 0, running()
    emit('PASS', assertions='zona horaria; adelanto con tabla completa; deshacer 18→14 sin detener tareas; '
         'evento retira adelanto y deshacer tardío sin RPC; adelanto a día; timer vuelve al horario; trabajo ajeno veta subir')


if __name__ == '__main__': main()
