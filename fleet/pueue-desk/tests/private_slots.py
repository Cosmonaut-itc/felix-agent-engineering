"""Run ONLY via remote-run on Mini or desk, with a private daemon; no tasks are submitted."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private', action='store_true', required=True)
    parser.add_argument('--host', choices=['mini', 'desk'], default='mini')
    parser.add_argument('--pueue', default='/opt/homebrew/bin/pueue' if sys.platform == 'darwin' else str(Path.home()/'.local/bin/pueue'))
    parser.add_argument('--pueued', default='/opt/homebrew/bin/pueued' if sys.platform == 'darwin' else str(Path.home()/'.local/bin/pueued'))
    args = parser.parse_args()
    base = Path.cwd().resolve()
    root = base/('private-u31-sol' if args.host == 'mini' else 'private-u32-opus')
    root.mkdir()  # refuse reuse
    config = root/'config.json'
    socket = base/('u31-sol.sock' if args.host == 'mini' else 'u32-opus.sock')
    config.write_text(json.dumps(dict(shared=dict(pueue_directory=str(root),runtime_directory=str(root),
        pid_path=str(root/'pid'),unix_socket_path=str(socket),use_unix_socket=True,
        alias_file=str(root/'aliases.yml'),shared_secret_path=str(root/'secret')),
        daemon=dict(compress_state_file=False))))
    env = dict(os.environ, TMPDIR=str(root), PUEUE_CONFIG_PATH=str(config))
    # The candidate controller next to this script (fleet/pueue-desk/control.py), or an
    # uploaded directory holding exactly both files.
    source = Path(__file__).resolve().parent
    source = source.parent if (source.parent/'control.py').exists() else source
    spec = importlib.util.spec_from_file_location('candidate', source/'control.py')
    control = importlib.util.module_from_spec(spec); spec.loader.exec_module(control)
    log = (base/('private-slots.jsonl' if args.host == 'mini' else 'private-desk-slots.jsonl')).open('w', buffering=1)
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
    def request(engine, ident, delta):
        view = engine.view()
        return dict(schema=control.HOST_SCHEMA,op='slots',action_id=ident,dry_run=False,
                    expected={k:view[k] for k in ('revision','policy_hash','generation','mode','slots')},
                    change=dict(group='suites',delta=delta))
    try:
        emit('version', version=subprocess.check_output([args.pueue,'--version'],text=True).strip())
        start()
        if args.host == 'desk':
            desk_scenario(control, pueue, root, start, crash, emit)
            return
        for group, slots in control.MINI_PROFILE.items():
            if group == 'default': pueue.parallel(group,slots)
            else: pueue.call('group','add',group,'--parallel',slots)
        pueue.call('pause','--wait','-g','default')  # save exact baseline 3/1/3
        directory = root/'control'; directory.mkdir()
        engine = control.MiniControl(pueue,directory)
        with control.locked(directory):
            engine.adopt(root/'pid')
            before = engine.view()
            down = engine.handle(request(engine,'private-down-0001',-1))
            assert down['status']=='applied', down
            emit('unsaved_parallel',before=before['slots'],after=down['result']['observed'])
            crash(); start()
            restored = engine.view()
            emit('after_restart_without_save',observed=restored['slots'],desired=restored['desired'])
            assert restored['slots']['suites']==3, restored
            assert engine.reconcile()
            recovered = engine.view()
            assert recovered['slots']['suites']==2
            emit('reconciled', observed=recovered['slots'],desired=recovered['desired'],
                 paused=recovered['pause_reasons'])
            up = engine.handle(request(engine,'private-up-00001',1))
            assert up['status']=='applied',up
            pueue.call('pause','--wait','-g','suites')  # this operation saves parallel too
            crash(); start()
            persisted = engine.view()
            assert persisted['slots']['suites']==3
            assert engine.reconcile()
            emit('saved_parallel_survives',observed=persisted['slots'],desired=persisted['desired'])
            emit('PASS', assertions='parallel unsaved lost; saved survives; durable candidate reconciles both; no tasks')
    finally:
        if daemon is not None and daemon.poll() is None:
            try: pueue.call('shutdown')
            finally:
                try: daemon.wait(timeout=4)
                except subprocess.TimeoutExpired: daemon.kill(); daemon.wait(timeout=4)
        emit('cleanup', daemon_reaped=daemon is None or daemon.poll() is not None)
        output.close(); log.close()


def desk_scenario(control, pueue, root, start, crash, emit):
    """Event with an active adjustment, missed timer and pueued restart, on a private daemon."""
    first = control.last_event(time.time())
    now = [first + 60]
    mode = control.event_mode(first)
    table = {g: row[mode] for g, row in control.GROUP_LIMITS.items()}
    for group, slots in table.items():
        pueue.call('group', 'add', group, '--parallel', slots)
    pueue.call('pause', '--wait', '-g', 'default')  # saves the exact baseline table
    directory = root/'control'; directory.mkdir()
    # Real desk generation from /proc and the private pid_path; only the clock is injected.
    engine = control.DeskControl(pueue, directory, clock=lambda: now[0])
    low = dict(io=0, cpu=0, memory=64 * 1024**3)
    engine.sample = lambda: low
    def request(ident, group, delta, op='slots', **extra):
        view = engine.view()
        body = dict(schema=control.HOST_SCHEMA, op=op, action_id=ident, dry_run=False,
                    expected={k: view[k] for k in ('revision', 'policy_hash', 'generation', 'mode', 'slots')}, **extra)
        if op == 'slots':
            body['change'] = dict(group=group, delta=delta)
        return body
    with control.locked(directory):
        adopted = engine.adopt(root/'pid')
        assert adopted['slots'] == table and adopted['mode'] == mode, adopted
        emit('adopted', mode=mode, slots=adopted['slots'], cap=adopted['cap'], generation=adopted['generation'][:12])
        down = engine.handle(request('private-desk-down1', 'jale', -1))
        assert down['status'] == 'applied', down
        assert down['result']['expires_at'] == control.next_event(first), down
        emit('adjusted', observed=down['result']['observed'], expires_at=down['result']['expires_at'])
        # The timer entry (aplicar) at the event: new mode table, adjustment retired.
        now[0] = control.next_event(first) + 1
        second = control.last_event(now[0])
        other = control.event_mode(second)
        target = {g: row[other] for g, row in control.GROUP_LIMITS.items()}
        engine.apply()
        after = engine.view()
        assert after['slots'] == target and after['desired'] == target, after
        assert after['expires_at'] is None and after['mode'] == other, after
        undo = engine.handle(request('private-desk-undo1', None, None, op='undo', undo_of='private-desk-down1'))
        assert (undo['status'], undo['error']['code']) == ('conflict', 'undo_expired'), undo
        assert engine.view()['slots'] == target
        emit('event_applied_by_timer', mode=other, observed=after['slots'], undo=undo['error']['code'])
        # A new adjustment, never saved by Pueue, then a restart of our own daemon.
        up = engine.handle(request('private-desk-down2', 'pesadas', -1))
        assert up['status'] == 'applied', up
        desired = dict(target, pesadas=target['pesadas'] - 1)
        crash(); start()
        lost = engine.view()
        emit('after_restart_without_save', observed=lost['slots'], desired=lost['desired'],
             recovery_pending=lost['recovery_pending'])
        assert lost['slots'] != desired and lost['recovery_pending'], lost
        # The periodic guard entry is the single recuperador.
        engine.guard(low)
        recovered = engine.view()
        assert recovered['slots'] == desired and recovered['generation'] != adopted['generation'], recovered
        emit('reconciled_by_guard', observed=recovered['slots'], paused=recovered['pause_reasons'])
        # Missed timer: the next event is applied by the guard alone.
        now[0] = control.next_event(second) + 120
        engine.guard(low)
        missed = engine.view()
        assert missed['slots'] == table and missed['mode'] == mode and missed['expires_at'] is None, missed
        emit('missed_timer_applied_by_guard', mode=mode, observed=missed['slots'])
        status = engine.handle(dict(schema=control.HOST_SCHEMA, op='status', action_id='private-desk-down2'))
        emit('receipt_after_event', status=status['status'])
        emit('PASS', assertions='event with adjustment applies new table and retires it; undo after event rejected; '
             'restart loses parallel and guard restores it; missed timer applied by guard; no tasks')


if __name__ == '__main__': main()
