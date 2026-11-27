"""Run ONLY via remote-run on Mini or desk, with a private daemon; only a stashed synthetic task is submitted on desk."""
import argparse
import importlib.util
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private', action='store_true', required=True)
    parser.add_argument('--mode', action='store_true', help='U3.3 private desk mode acceptance')
    parser.add_argument('--host', choices=('mini','desk'), default='mini')
    parser.add_argument('--pueue', default='/opt/homebrew/bin/pueue' if sys.platform == 'darwin' else str(Path.home()/'.local/bin/pueue'))
    parser.add_argument('--pueued', default='/opt/homebrew/bin/pueued' if sys.platform == 'darwin' else str(Path.home()/'.local/bin/pueued'))
    args = parser.parse_args()
    if args.mode and args.host != 'desk':parser.error('--mode sólo en desk')
    base = Path.cwd().resolve()
    root = base/('private-u33-sol' if args.mode else 'private-u32-sol' if args.host == 'desk' else 'private-u31-sol')
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
        if args.mode:
            mode_scenario(control, pueue, root, emit)
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
        # All U3.3 tasks wait on this private gate and also have their own timeout.
        (root/'release').touch()
        if args.mode and daemon is not None and daemon.poll() is None:
            until=time.monotonic()+5
            while time.monotonic()<until:
                live=control.live_tasks(pueue.status())
                if not live:break
                time.sleep(.05)
            else:
                # IDs come exclusively from this run's private daemon.
                for task in live:pueue.call('kill',str(task['id']))
            emit('private_tasks_finished', live=len(control.live_tasks(pueue.status())))
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


def mode_scenario(control, pueue, root, emit):
    """Real 4.0.4 scheduler/parallel, only a private daemon and synthetic clock."""
    now=[datetime(2026,11,14,22,0,0,tzinfo=timezone.utc).timestamp()]
    for g,n in control.mode_profile('dia').items():pueue.call('group','add',g,'--parallel',n)
    pueue.call('pause','--wait','-g','default')
    directory=root/'control';directory.mkdir()
    engine=control.MiniControl(pueue,directory,clock=lambda:now[0],host='desk')
    def request(ident, mode=None, undo_of=None):
        view=engine.view()
        return dict(schema=control.HOST_SCHEMA,op='undo' if undo_of else 'mode',action_id=ident,dry_run=False,
                    expected={k:view[k] for k in ('revision','policy_hash','generation','mode','slots')},
                    **(dict(undo_of=undo_of) if undo_of else dict(change=dict(mode=mode))))
    def apply(ident, mode=None, undo_of=None):
        with control.locked(directory):return engine.handle(request(ident,mode,undo_of))
    with control.locked(directory):
        engine.adopt(root/'pid')
        v=engine.view()
        adjustment=dict(schema=control.HOST_SCHEMA,op='slots',action_id='mode-private-minus',dry_run=False,
                        expected={k:v[k] for k in ('revision','policy_hash','generation','mode','slots')},
                        change=dict(group='otros',delta=-1))
        assert engine.handle(adjustment)['status']=='applied'
        with engine.connect() as db:before=engine.state(db)
    advanced=apply('mode-private-night','noche')
    assert advanced['status']=='applied',advanced
    assert advanced['result']['observed']==control.mode_profile('noche'),advanced
    assert advanced['result']['mode_note']=='Adelantado · vuelve a día a las 08:00',advanced
    assert advanced['result']['mode_detail']=='Ajustes manuales hasta las 23:00',advanced
    emit('mode_advanced',mode='noche',observed=advanced['result']['observed'],expires_at=advanced['result']['expires_at'])
    undone=apply('mode-private-undo1',undo_of='mode-private-night')
    assert undone['status']=='applied',undone
    with engine.connect() as db:restored=engine.state(db)
    for key in ('mode_override','overrides','desired','schedule_event'):assert restored[key]==before[key],key
    emit('undo_restored',mode=engine.view()['mode'],observed=undone['result']['observed'],original_expirations=True)
    assert apply('mode-private-night2','noche')['status']=='applied'
    # Seven bounded Python children in jale; no installed launchers, envs or bodies.
    program="import pathlib,time; gate=pathlib.Path("+repr(str(root/'release'))+"); end=time.monotonic()+45; "
    program+="exec('while not gate.exists() and time.monotonic()<end: time.sleep(0.05)')"
    command=shlex.join([sys.executable,'-c',program])
    ids=[int(pueue.call('add','--print-task-id','--group','jale','--',command).strip()) for _ in range(7)]
    until=time.monotonic()+5
    while True:
        tasks=pueue.status()['tasks']
        running=[i for i in ids if tasks[str(i)]['status']=='Running' or isinstance(tasks[str(i)]['status'],dict) and 'Running' in tasks[str(i)]['status']]
        if len(running)==7:break
        assert time.monotonic()<until,'private tasks did not start'
        time.sleep(.05)
    day=apply('mode-private-day','dia')
    assert day['status']=='applied' and day['result']['observed']==control.mode_profile('dia'),day
    tasks=pueue.status()['tasks']
    for i in ids:assert tasks[str(i)]['status']=='Running' or 'Running' in tasks[str(i)]['status'],i
    emit('20_to_15_with_live_tasks',observed=day['result']['observed'],running_jale=len(ids),jale_parallel=5,no_task_stopped=True)
    undo=apply('mode-private-undo2',undo_of='mode-private-day')
    assert undo['status']=='applied' and engine.view()['mode']=='noche',undo
    emit('undo_live_tasks',observed=undo['result']['observed'],mode=undo['result']['mode'])
    # Same-mode 05Z event removes the advance, including a later slots adjustment.
    with control.locked(directory):
        v=engine.view();adjustment.update(action_id='mode-private-slots2',expected={k:v[k] for k in ('revision','policy_hash','generation','mode','slots')})
        assert engine.handle(adjustment)['status']=='applied'
    now[0]=control.schedule_at(now[0])[1]
    with control.locked(directory):assert engine.reconcile()
    view=engine.view()
    assert view['slots']==control.mode_profile('noche') and view['expires_at'] is None,view
    assert view['mode_note']=='Automático · cambia a día a las 08:00',view
    with engine.connect() as db:
        state=engine.state(db);assert state['mode_override'] is None and state['overrides']=={}
    expired=apply('mode-private-expired',undo_of='mode-private-undo2')
    assert expired['error']['code']=='undo_conflict' and expired['effects'] is False,expired
    emit('05Z_event_retires_advance',mode=view['mode'],note=view['mode_note'],expired_undo='undo_conflict')
    emit('PASS',assertions='destination night table; exact undo; real 20->15 with 7 live tasks unchanged; 05Z clears advance and slots')


if __name__ == '__main__': main()
