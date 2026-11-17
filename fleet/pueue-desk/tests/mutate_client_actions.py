"""CC1 safety mutants: every rejection must be an assertion, not ERROR/timeout."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
T = 'test_client_actions.ActionTests.'
CASES = [
    ('stdin bounded before decoding', 'client_actions.py',
     '    raw = stdin.read(c.MAX_BYTES + 1)', '    raw = stdin.read()',
     T+'test_full_corpus_through_coordinator_and_syntax_exit'),
    ('real host lock', 'client_actions.py',
     'fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)', 'pass',
     T+'test_real_processes_control_lock_vs_result_lock'),
    ('reservation committed before RPC', 'client_actions.py',
     ("            self.save(record, reservation)\n            self.boundary('reserved')", "            self.boundary('rpc_returned')"),
     ("            self.boundary('reserved')", "            self.save(record, reservation)\n            self.boundary('rpc_returned')"),
     T+'test_normal_hold_before_rpc_and_restart_idempotence'),
    ('lost reply remains unknown', 'client_actions.py',
     "return saved['response'] if saved else self.reply(r, 'unknown', 'rpc_unresolved', held=True)",
     "return self.reply(r, 'deferred', 'rpc_unresolved', held=True)",
     T+'test_lost_response_live_cli_never_retries_or_expires'),
    ('reservation never expires', 'client_actions.py',
     '    for item in reservations(engine):',
     '    if engine.clock() > 1000000:\n        return False\n    for item in reservations(engine):',
     T+'test_lost_response_live_cli_never_retries_or_expires'),
    ('Locked is untouchable', 'client_actions.py',
     "            if current['state'] == 'Locked':", '            if False:',
     T+'test_locked_every_mutator'),
    ('reorder checks client reservations', 'control.py',
     "        if self.client_blocked(request['expected']['group']):", '        if False:',
     T+'test_hold_blocks_reorder_with_visible_reason'),
    ('hold checks abandoned reorder', 'client_actions.py',
     '            if self.host_busy(group):', '            if False:',
     T+'test_dead_reorder_blocks_clients_without_rpc'),
    ('Queued race cannot signal Running', 'client_actions.py',
     "            if current != r['expected']:\n                return reject('stale_state')",
     "            if current != r['expected']:\n                self.rpc('kill', '-s', 'SIGTERM', current['task_id'])\n                return reject('stale_state')",
     T+'test_races_at_each_read_and_stash_never_escalate'),
    ('manual fences block forced start', 'client_actions.py',
     '                if reasons:', "                if reasons - {'manual'}:",
     T+'test_fence_real_reason_and_no_manual_open'),
    ('safe guard may open with known cancel hold', 'client_actions.py',
     "            if not opening or item['uncertain']:", '            if True:',
     T+'test_manual_pressure_and_cancel_guard_reopening'),
    ('unknown blocks manual opening', 'control.py',
     '            if self.client_blocked(group, opening=True):', '            if False:',
     T+'test_unknown_prevents_manual_resume_without_losing_manual_reason'),
    ('unknown blocks guard opening', 'client_actions.py',
     "            if not opening or item['uncertain']:", '            if not opening:',
     T+'test_unknown_prevents_guard_opening'),
    ('launcher evidence authenticated', 'client_actions.py',
     ('            self.adapter.verify(r, observed, previous)', '            self.adapter.verify(r, latest, reservation)'),
     ('            pass', '            pass'),
     T+'test_untrusted_acceptance_proof_and_prior_normal_result'),
    ('phase digest survives restart', 'client_actions.py',
     "if record['digest'] != digest(r) else record['response']", "if False else record['response']",
     T+'test_normal_hold_before_rpc_and_restart_idempotence'),
    ('first acceptance survives TERM to KILL', 'client_actions.py',
     "                if previous['acceptance'] is not None and previous['acceptance'] != r['acceptance']:",
     '                if False:', T+'test_term_kill_preserve_first_acceptance'),
    ('cross-phase mode fixed', 'client_actions.py',
     "previous['group'] != group or previous['mode'] != r['mode'] or", "previous['group'] != group or",
     T+'test_phase_mode_cannot_change'),
    ('retained ID protected from prune', 'control.py',
     'return protected | client_module().protected_ids(MiniControl(None, directory))', 'return protected',
     T+'test_prune_keeps_done_identity_until_finish'),
    ('child identity durable before release', 'client_actions.py',
     "            self.save(record)\n            child.stdin.write(b'1')", "            child.stdin.write(b'1')",
     T+'test_child_intent_precedes_gate_timeout_unknown'),
    ('identity stable across observations', 'client_actions.py',
     "            if not self.same_attempt(r['expected'], current):", '            if False:',
     T+'test_identity_replaced_between_phases_is_not_adopted'),
]


def run(root, test):
    return subprocess.run([sys.executable, '-B', '-m', 'unittest', '-v', test], cwd=str(root/'tests'),
                          env=dict(os.environ, TMPDIR='/tmp', PYTHONDONTWRITEBYTECODE='1'),
                          capture_output=True, text=True, timeout=30)


def main():
    nominal = run(SOURCE, 'test_client_actions')
    if nominal.returncode:
        raise AssertionError(nominal.stdout + nominal.stderr)
    for number, (rule, name, old, new, test) in enumerate(CASES, 1):
        with tempfile.TemporaryDirectory(prefix='cc1-sol-mutant-', dir='/tmp') as folder:
            root = Path(folder)/'pueue-desk'
            shutil.copytree(SOURCE, root, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            path = root/name
            source = path.read_text()
            edits = zip(old, new) if isinstance(old, tuple) else [(old, new)]
            for before, after in edits:
                if source.count(before) != 1:
                    raise AssertionError('Nonunique anchor: ' + rule)
                source = source.replace(before, after)
            path.write_text(source)
            result = run(root, test)
            if result.returncode != 1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:
                raise AssertionError(rule + ' did not fail by assertion:\n' + result.stdout + result.stderr)
            print(str(number) + ' ' + rule + ': assertion', flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' mutants; nominal green; '+sys.version.split()[0], flush=True)


if __name__ == '__main__':
    main()
