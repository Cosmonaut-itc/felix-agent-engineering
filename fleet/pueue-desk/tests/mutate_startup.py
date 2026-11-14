"""U3.4: una mutación por regla nueva de arranque, en copias de /tmp; cada una debe fallar por aserción.

TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/mutate_startup.py
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

FLEET = Path(__file__).resolve().parents[2]
C = 'control.py'
# file (under pueue-desk), original, replacement, targeted test in test_startup
CASES = [
 # S-W1: compatible timestamps and the whole structure before any write.
 (C, 'or off_h is not None and (int(off_h) > 23 or int(off_m) > 59)):', '):',
  'SW1Tests.test_invalid_timestamps_reject_with_zero_writes_and_daemon_stopped'),
 (C, 'datetime(int(year), int(month), int(day))', 'int(year)',
  'SW1Tests.test_invalid_timestamps_reject_with_zero_writes_and_daemon_stopped'),
 (C, "validate_status(detail['previous_status'], depth + 1)  # nested timestamps too", 'pass',
  'SW1Tests.test_invalid_timestamps_reject_with_zero_writes_and_daemon_stopped'),
 (C, 'return validate_queue(value), raw', 'return value, raw',
  'StartupTests.test_absent_or_corrupt_queue_blocks_and_preserves_files'),
 # O-W1: legacy ownership migrates; vanished foreign groups are dropped; retire only empty.
 (C, '        owned &= set(managed_groups)\n', '        raise ValueError("Grupos desconocidos en estado del guardia")\n',
  'OW1Tests.test_legacy_suites_open_empty_three_ticks_attend_other_groups'),
 (C, "gone = [g for g in state['reasons'] if g not in groups and g not in self.profile]", 'gone = []',
  'OW1Tests.test_crashed_retire_is_finished_by_the_guard'),
 (C, "if any(t['group'] == group for t in snapshot['tasks'].values()):\n                raise ValueError('El grupo tiene tareas; no se retira')",
  "if False:\n                raise ValueError('El grupo tiene tareas; no se retira')",
  'OW1Tests.test_authorized_retire_then_ticks_and_tasks_block_it'),
 # O-W2: same validation before any effect, in --seco and real, preparar and installers.
 (C, "if daemon.get('env_vars', {}) not in ({}, None) or daemon.get('shell_command') is not None:", 'if False:',
  'OW2Tests.test_complex_configuration_rejected_before_any_effect'),
 (C, "        if shared.get(key) is not None:\n", '        if False:\n',
  'OW2Tests.test_complex_configuration_rejected_before_any_effect'),
 ('install-desk.py', '    paths = startup_gate(home, pid_file, control)\n', '    paths = None\n',
  'OW2Tests.test_complex_configuration_rejected_before_any_effect'),
 ('remote.py', '        host_gate(home, manager_environment(manager))\n', '',
  'OW2Tests.test_deploy_desk_preflight_rejects_before_linger_or_shutdown'),
 ('install-desk.py', "if pid_path is not None and Path(state['pid_file']) != pid_path:", 'if False:',
  'OW2Tests.test_adopted_pid_path_must_match_preparar'),
 # Startup plan: close everything, fail interrupted work visibly, retain what needs Felix.
 (C, "            group['status'] = 'Paused'\n            events.append", '            events.append',
  'StartupTests.test_restart_closes_every_group_fails_interrupted_and_keeps_queue'),
 (C, "if variant in ('Running', 'Paused') or variant == 'Queued' and key in logged:",
  "if variant == 'Queued' and key in logged:",
  'StartupTests.test_restart_closes_every_group_fails_interrupted_and_keeps_queue'),
 (C, "hold(task['group'], 'log_existente')", 'pass',
  'StartupTests.test_restart_closes_every_group_fails_interrupted_and_keeps_queue'),
 (C, "hold(task['group'], 'bloqueada')", 'pass',
  'StartupTests.test_restart_closes_every_group_fails_interrupted_and_keeps_queue'),
 (C, "hold(name, 'fuera_del_reparto')", "reasons.setdefault(name, set()).add('startup')",
  'StartupTests.test_restart_closes_every_group_fails_interrupted_and_keeps_queue'),
 (C, "elif name in desired and not reasons.get(name):", 'elif False:',
  'StartupTests.test_paused_profile_group_without_reason_is_retained'),
 (C, "        groups[name]['parallel_tasks'] = slots\n", '',
  'StartupTests.test_restart_closes_every_group_fails_interrupted_and_keeps_queue'),
 # Admission: attributed generation, own reasons only, Docker, explicit resolution.
 (C, "if self.host == 'desk' and state['startup']['phase'] == 'prepared':", 'if False:',
  'StartupTests.test_restart_closes_every_group_fails_interrupted_and_keeps_queue'),
 (C, "if self.host == 'desk' and state['startup']['phase'] == 'prepared':",
  "if self.host == 'desk' and state['startup']['phase'] in ('prepared', 'preparing'):",
  'StartupTests.test_unfinished_preparation_never_reopens_on_start'),
 (C, "if reasons <= {'pressure', 'startup'} and status == 'Paused':", "if status == 'Paused':",
  'StartupTests.test_restart_closes_every_group_fails_interrupted_and_keeps_queue'),
 (C, "if not docker:\n                        continue", "if False:\n                        continue",
  'StartupTests.test_docker_gate_keeps_startup_closure_until_ready'),
 (C, "reasons = set(state['reasons'].get(group, [])) - {'manual', 'ambiguous', 'startup'}",
  "reasons = set(state['reasons'].get(group, [])) - {'manual', 'startup'}",
  'StartupTests.test_restart_closes_every_group_fails_interrupted_and_keeps_queue'),
 # Durable intent, repeatable finalization, event crossing, stopped daemon.
 (C, "            with self.connect(write=True) as db:\n                self.store(db, state)\n            if data is not raw:",
  '            if data is not raw:', 'StartupTests.test_crash_at_each_boundary_converges'),
 (C, "            if self.resolve(state) != state:\n                continue  # replan the written queue",
  "            if False:\n                continue  # replan the written queue",
  'StartupTests.test_day_night_crossing_during_preparation'),
 (C, "if name_of(int(text)) == 'pueued':", 'if False:', 'StartupTests.test_live_or_answering_daemon_refuses'),
 (C, "        raise ValueError('Otro pueued responde; preparar exige el daemon detenido')", '        pass',
  'StartupTests.test_live_or_answering_daemon_refuses'),
 (C, "    if version != PUEUED_VERSION:", '    if False:', 'StartupTests.test_live_or_answering_daemon_refuses'),
 (C, "            raise ValueError('Falta state.json con autoridad adoptada; no se arranca una cola vacía')",
  '            return dict(prepared=False)', 'StartupTests.test_absent_or_corrupt_queue_blocks_and_preserves_files'),
 (C, "if isinstance(t['status'], dict) and set(t['status']) <= {'Running', 'Paused'}]", 'if False]',
  'StartupTests.test_stop_records_interrupted_and_snapshot_identity'),
 (C, "\n                         if i['at'] > self.clock() - 86400]", ']',
  'StartupTests.test_stop_records_interrupted_and_snapshot_identity'),
 (C, "    return merged[-INTERRUPTED_KEEP:]", '    return merged',
  'StartupTests.test_interrupted_index_is_bounded'),
 # Service and maintenance.
 (C, "        run('systemctl', '--user', 'stop', *timers)\n        run('systemctl', '--user', 'stop', 'pueue-desk-guard.service', 'pueue-desk-mode.service')\n        run('systemctl', '--user', 'stop', 'pueued.service')",
  "        run('systemctl', '--user', 'stop', 'pueued.service')\n        run('systemctl', '--user', 'stop', *timers)\n        run('systemctl', '--user', 'stop', 'pueue-desk-guard.service', 'pueue-desk-mode.service')",
  'StartupTests.test_maintenance_order_marker_and_no_reopen'),
 ('units/pueue-desk-guard.service', 'Requisite=pueued.service', 'Requires=pueued.service',
  'StartupTests.test_units_order_and_restart_policy'),
 ('units/pueued.service', 'Restart=always', 'Restart=on-failure', 'StartupTests.test_units_order_and_restart_policy'),
 ('units/pueued.service', 'StartLimitIntervalSec=0\n', '', 'StartupTests.test_units_order_and_restart_policy'),
 # Installer on the adopted desk.
 ('install-desk.py', "fields_missing = 'mode_override' not in original or 'startup' not in original",
  "fields_missing = 'mode_override' not in original",
  'OW2Tests.test_seco_and_install_migrate_startup_and_load_unit_without_restart'),
 ('install-desk.py', "if any(a['response']['status'] in ('applying', 'partial', 'unknown') for a in actions):", 'if False:',
  'OW2Tests.test_open_receipts_reject_without_effects_even_without_migration'),
 ('install-desk.py', "after['MainPID'] != before['MainPID'] or ", '',
  'OW2Tests.test_unit_mismatch_after_reload_fails_loudly'),
 ('install-desk.py', "    if os.path.lexists(directory / 'maintenance'):", '    if False:',
  'OW2Tests.test_maintenance_refuses_install_without_effects'),
]


def main():
    python = sys.executable
    first = int(sys.argv[1]) if len(sys.argv) > 1 else 1  # resume a long run from case N
    for number, (relative, original, replacement, test) in enumerate(CASES, 1):
        if number < first:
            continue
        with tempfile.TemporaryDirectory(prefix='u34o-mutation-', dir='/tmp') as folder:
            root = Path(folder) / 'fleet'
            for name in ('rack', 'pueue-desk'):
                shutil.copytree(FLEET / name, root / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            target = root / 'pueue-desk' / relative
            source = target.read_text()
            if source.count(original) != 1:
                raise AssertionError('Nonunique mutation anchor ' + str(number) + ': ' + repr(original))
            target.write_text(source.replace(original, replacement))
            result = subprocess.run([python, '-B', '-m', 'unittest', 'test_startup.' + test, '-v'],
                                    cwd=str(root / 'pueue-desk/tests'), text=True, capture_output=True, timeout=60,
                                    env=dict(os.environ, TMPDIR='/tmp', PYTHONDONTWRITEBYTECODE='1'))
            if result.returncode != 1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:
                raise AssertionError('Mutation ' + str(number) + ' did not fail by assertion:\n' + result.stdout + result.stderr)
            reason = next((s for s in result.stderr.splitlines() if s.startswith('AssertionError:')), 'assertion')
            print(str(number) + ' ' + test + ': caught | ' + reason[:160], flush=True)
    print(str(len(CASES)) + '/' + str(len(CASES)) + ' startup mutations caught; original tree untouched')


if __name__ == '__main__':
    main()
