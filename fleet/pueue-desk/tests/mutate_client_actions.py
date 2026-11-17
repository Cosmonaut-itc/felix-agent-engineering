"""CC1 safety mutants for the client coordinator. Run with each supported interpreter; no real Pueue."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
T = 'test_client_actions.ClientTests.'
CASES = [
    ('mutations wait for control.lock', 'control.py',
     '            if not wait_rack_lock(handle):\n', '            if False:\n',
     T + 'test_real_lock_between_processes_without_effect_or_deadlock'),
    ('intent and reservation precede the RPC', 'client_actions.py',
     "        self.save(request, group, 'intent', pending, reservation, rpc=plan['rpc'][0])\n"
     "        self.boundary('intent')\n        outcome = 'ok'\n        try:\n"
     "            self.engine.pueue.call(*plan['rpc'])\n",
     "        outcome = 'ok'\n        try:\n            self.engine.pueue.call(*plan['rpc'])\n"
     "            self.save(request, group, 'intent', pending, reservation, rpc=plan['rpc'][0])\n",
     T + 'test_intent_and_reservation_are_durable_before_every_rpc'),
    ('lost response stays unknown', 'client_actions.py',
     "unknown = ('unknown', 'rpc_unresolved', None)", "unknown = ('deferred', 'rpc_unresolved', None)",
     T + 'test_lost_response_and_surviving_cli_retain_unknown_and_block'),
    ('reservation never expires by clock', 'client_actions.py',
     "            if json.loads(body)['state'] == 'held']",
     "            if json.loads(body)['state'] == 'held'\n"
     "            and json.loads(body)['at'] > __import__('time').time() - 86400]",
     T + 'test_reservation_never_expires_by_clock'),
    ('Locked is never acted on', 'client_actions.py',
     "            raise Refused('locked')  # Locked ajeno: nunca se toca", '            pass',
     T + 'test_locked_task_is_never_touched_by_any_mutating_operation'),
    ('reorder honours client reservations', 'control.py',
     "        if request['expected']['group'] in client_blocks(self):", '        if False:',
     T + 'test_both_orders_hold_versus_reorder'),
    ('hold honours a reorder that owns the group', 'client_actions.py',
     "        reorder = ('reorder' in state['reasons'].get(group, []) or", '        reorder = False and (',
     T + 'test_both_orders_hold_versus_reorder'),
    ('Queued->Running never escalates to a signal', 'client_actions.py',
     "                raise Refused('stale_state')  # Queued->Running: jamás se escala a señal",
     "                return dict(plan, rpc=('kill', '-s', 'SIGTERM', str(run.task_id)))",
     T + 'test_queued_to_running_race_on_each_read_and_on_stash_never_signals'),
    ('pending cancel never opens above manual', 'client_actions.py',
     "        if reasons & {'manual', 'ambiguous', 'handoff'}:\n            return 'manual'",
     "        if False:\n            return 'manual'",
     T + 'test_manual_and_pressure_never_open_and_guard_reopens_with_own_reservation'),
    ('stdin read is bounded before decoding', 'client_actions.py',
     'raw = stream.read(contract.MAX_BYTES + 1)', 'raw = stream.read()',
     T + 'test_cli_bounded_read_and_exit_codes'),
    ('prune keeps the Done task of an open reservation', 'control.py',
     'protected = admission_evidence(directory) | client_reserved_ids(directory)',
     'protected = admission_evidence(directory)',
     T + 'test_prune_keeps_done_task_of_open_reservation'),
    ('finish checks the host-signed retirement receipt', 'client_actions.py',
     "            if proof['receipt'] not in receipts:", '            if False:',
     T + 'test_retire_held_and_interrupted_close_only_with_host_receipt'),
    ('unknown in the group blocks other actions', 'client_actions.py',
     "                return dict(plan, status='deferred', reason='rpc_unresolved')", '                pass',
     T + 'test_lost_response_and_surviving_cli_retain_unknown_and_block'),
    ('identical body repeats its receipt without a second RPC', 'client_actions.py',
     "            if not retryable(record['response']):\n                return record['response']\n",
     "            if False:\n                return record['response']\n",
     T + 'test_lock_free_but_dead_owner_after_reservation_defers_without_rpc'),
    ('orphan client journal fails closed', 'control.py',
     "    return {g: 'unknown' for g in engine.profile} if orphan_client_journal(engine.path) else {}",
     '    return {}',
     T + 'test_control_installed_alone_runs_and_orphan_journal_fails_closed'),
]


def run(root, test):
    return subprocess.run([sys.executable, '-B', '-m', 'unittest', '-v', test], cwd=str(root / 'tests'),
                          env=dict(os.environ, TMPDIR='/tmp', PYTHONDONTWRITEBYTECODE='1'),
                          capture_output=True, text=True, timeout=60)


def main():
    nominal = run(SOURCE, 'test_client_actions')
    if nominal.returncode:
        raise AssertionError(nominal.stdout + nominal.stderr)
    for number, (rule, name, old, new, test) in enumerate(CASES, 1):
        with tempfile.TemporaryDirectory(prefix='cc1-mutant-', dir='/tmp') as folder:
            root = Path(folder) / 'pueue-desk'
            shutil.copytree(SOURCE, root, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            path = root / name
            source = path.read_text()
            if source.count(old) != 1:
                raise AssertionError('Ambiguous anchor: ' + rule)
            path.write_text(source.replace(old, new))
            result = run(root, test)
            if result.returncode != 1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:
                raise AssertionError(rule + ' not killed by assertion:\n' + result.stdout + result.stderr)
            print(str(number) + ' ' + rule + ': assertion', flush=True)
    print(str(len(CASES)) + '/' + str(len(CASES)) + ' mutants; nominal green; ' + sys.version.split()[0], flush=True)


if __name__ == '__main__':
    main()
