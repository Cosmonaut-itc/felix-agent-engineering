"""U3.5: one assertion-killed mutant per handoff safety rule. Doubles only, tree untouched."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

BASE = Path(__file__).resolve().parents[1]
T = 'test_handoff.'
CASES = [
    # O-W3: without the source's persistent disable, a login can load two daemons.
    ('o-w3-source-disable', 'control.py',
     "            self.launchd.disable(value['source'])\n            self.checkpoint('disable:' + value['source'])",
     "            self.checkpoint('disable:' + value['source'])",
     T + 'HandoffTests.test_o_w3_login_between_bootout_and_bootstrap_loads_no_daemon_then_guard_finishes'),
    ('o-w3-destination-disabled-before-plist', 'install-mini.py',
     "                launchd.disable(control.DAEMON_LABEL)\n", '',
     T + 'InstallerTests.test_real_install_migrates_additively_disables_before_plist_and_completes'),
    ('switch-waits-offline', 'control.py',
     "            if self.launchd.loaded(value['source']) or not self.offline():",
     "            if self.launchd.loaded(value['source']):",
     T + 'HandoffTests.test_source_daemon_lingering_after_bootout_keeps_switch_open'),
    ('guard-ticks-handoff', 'control.py',
     "        if host == 'mini' and handoff_tick(engine, launchd or Launchd()) not in ('idle', 'done', 'unreadable'):",
     "        if False:",
     T + 'HandoffTests.test_o_w3_login_between_bootout_and_bootstrap_loads_no_daemon_then_guard_finishes'),
    ('preparer-fence', 'control.py',
     "            if handoff is not None and handoff['phase'] != 'idle' and not (",
     "            if False and not (",
     T + 'HandoffTests.test_preparer_refuses_while_the_other_manager_owns_the_phase'),
    ('abort-to-guard', 'control.py',
     "            state['reasons'][g] = sorted(reasons | {'pressure'})", "            state['reasons'][g] = sorted(reasons)",
     T + 'HandoffTests.test_busy_task_requires_wait_drains_without_kill_and_deadline_aborts_to_guard'),
    ('busy-needs-wait', 'control.py', "        if busy and not wait_seconds:", "        if False:",
     T + 'HandoffTests.test_busy_task_requires_wait_drains_without_kill_and_deadline_aborts_to_guard'),
    ('mini-pid-reuse', 'control.py',
     "        reused = (host == 'mini' and result.returncode == 0 and result.stdout.strip()",
     "        reused = (False and result.returncode == 0 and result.stdout.strip()",
     T + 'HandoffTests.test_stale_pid_reused_by_other_program_after_reboot_is_offline_on_mini_only'),
    ('unreadable-never-opens', 'control.py',
     "                    and not (self.host == 'mini' and self.handoff_pending()))", ")",
     T + 'HandoffTests.test_unreadable_handoff_row_keeps_every_group_closed_and_no_launchd_effect'),
    ('install-needs-preparation', 'control.py',
     "            if (startup.get('phase') not in ('prepared', 'ready')\n                    or startup.get('previous_generation') != value['previous_generation']):",
     "            if False:",
     T + 'HandoffTests.test_daemon_answering_without_preparation_holds_the_switch'),
    ('withdraw-adopts-generation', 'control.py',
     "            state['generation'] = self.engine.generation(state)\n            # Disabled", "            # Disabled",
     T + 'HandoffTests.test_withdraw_returns_to_homebrew_and_removes_our_plist'),
    ('maintenance-freezes-handoff', 'control.py',
     "            return 'maintenance'  # never stop, start or reopen under maintenance",
     "            pass",
     T + 'HandoffTests.test_maintenance_marker_freezes_handoff_preparer_and_request'),
    ('request-enable-gate', 'control.py',
     "        if self.launchd.disabled(source) or not self.launchd.disabled(destination):", "        if False:",
     T + 'HandoffTests.test_request_gates_reject_without_effects'),
    ('seco-uses-preparer', 'install-mini.py',
     "    control.start_projection(engine, config, config['data'] / 'state.json')", "    pass",
     T + 'InstallerTests.test_seco_validates_with_the_preparer_before_any_effect_and_describes_steps'),
    ('install-rejects-open-receipt', 'install-mini.py',
     "            raise ValueError('Hay recibos applying/partial/unknown; recuperar antes de instalar')", "            pass",
     T + 'InstallerTests.test_seco_and_real_reject_corrupt_queue_profile_or_receipt_without_effects'),
    ('maintenance-disables-daemon', 'install-mini.py',
     "        launchd.disable(control.DAEMON_LABEL)  # a login inside maintenance loads no daemon\n", '',
     T + 'InstallerTests.test_parar_arrancar_maintenance_order_and_restart_through_preparer'),
    ('plain-reinstall-never-switches', 'install-mini.py',
     "    if has_authority and (args.relevo or ours or args.command in ('parar', 'arrancar')):",
     "    if has_authority:",
     T + 'RoutingTests.test_main_routes_handoff_only_with_relevo_maintenance_or_our_agent'),
]


def main():
    for name, filename, before, after, test in CASES:
        with tempfile.TemporaryDirectory(prefix='u35-opus-mut-', dir='/tmp') as folder:
            root = Path(folder) / 'pueue-desk'
            shutil.copytree(BASE, root, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            path = root / filename
            source = path.read_text()
            assert source.count(before) == 1, (name, 'nonunique anchor')
            path.write_text(source.replace(before, after))
            result = subprocess.run([sys.executable, '-B', '-m', 'unittest', test, '-v'], cwd=str(root / 'tests'),
                                    text=True, capture_output=True, timeout=300,
                                    env=dict(os.environ, TMPDIR='/tmp', PYTHONDONTWRITEBYTECODE='1'))
            assert result.returncode == 1 and 'FAIL:' in result.stderr and 'ERROR:' not in result.stderr, \
                name + ' survived or failed outside assertion:\n' + result.stderr[-3000:]
            print(name + ': caught by assertion', flush=True)
    print(str(len(CASES)) + '/' + str(len(CASES)) + ' U3.5 handoff mutations caught; tree untouched')


if __name__ == '__main__':
    main()
