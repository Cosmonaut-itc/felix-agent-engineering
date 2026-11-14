"""Run ONLY via remote-run on Mini, with a private daemon; no tasks are submitted."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-private', action='store_true', required=True)
    args = parser.parse_args()
    base = Path.cwd().resolve()
    root = base/'private-u31-sol'
    root.mkdir()  # refuse reuse
    config = root/'config.json'
    config.write_text(json.dumps(dict(shared=dict(pueue_directory=str(root),runtime_directory=str(root),
        pid_path=str(root/'pid'),unix_socket_path=str(base/'u31-sol.sock'),use_unix_socket=True,
        alias_file=str(root/'aliases.yml'),shared_secret_path=str(root/'secret')),
        daemon=dict(compress_state_file=False))))
    env = dict(os.environ, TMPDIR=str(root), PUEUE_CONFIG_PATH=str(config))
    # The uploaded directory contains exactly this script and the candidate controller.
    spec = importlib.util.spec_from_file_location('candidate', base/'control.py')
    control = importlib.util.module_from_spec(spec); spec.loader.exec_module(control)
    log = (base/'private-slots.jsonl').open('w', buffering=1)
    def emit(event, **values):
        line = json.dumps(dict(event=event, **values), sort_keys=True)
        print(line, flush=True); log.write(line+'\n')
    class PrivatePueue(control.Pueue):
        def call(self, *argv):
            return subprocess.run(['/opt/homebrew/bin/pueue','--config',str(config),*map(str,argv)],
                check=True, capture_output=True, text=True, timeout=4, env=env, cwd=str(base)).stdout
    pueue = PrivatePueue()
    daemon, output = None, (root/'daemon.log').open('a')
    def start():
        nonlocal daemon
        pid = root/'pid'
        if pid.exists(): pid.unlink()  # only after wait() of OUR previous child
        daemon = subprocess.Popen(['/opt/homebrew/bin/pueued','--config',str(config)],
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
        emit('version', version=subprocess.check_output(['/opt/homebrew/bin/pueue','--version'],text=True).strip())
        start()
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


if __name__ == '__main__': main()
