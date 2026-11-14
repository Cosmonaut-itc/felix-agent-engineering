"""One assertion-killed mutant per new restart safety rule. No real binaries."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

BASE=Path(__file__).resolve().parents[1]
CASES=[
 ('chrono-offset','control.py',"if not match or any(int(n or 0) > limit for n, limit in zip(match.groups(), (23, 59, 59, 23, 59))):",
  'if not match:', 'StartupTests.test_s_w1_offsets_calendar_and_nested_reject_without_writes_or_start'),
 ('queue-schema','control.py','            validate_task(task_id, task)','            pass','StartupTests.test_s_w1_offsets_calendar_and_nested_reject_without_writes_or_start'),
 ('queue-duplicate','control.py','object_pairs_hook=unique_object','object_pairs_hook=dict','StartupTests.test_strict_queue_and_missing_initialized_state_preserve_every_file'),
 ('queue-reset','control.py','group.get("status") not in ("Running", "Paused")','group.get("status") not in ("Running", "Paused", "Reset")',
  'StartupTests.test_strict_queue_and_missing_initialized_state_preserve_every_file'),
 ('queue-gzip','control.py','if os.path.lexists(path.with_suffix(".json.gz")):','if False:',
  'StartupTests.test_strict_queue_and_missing_initialized_state_preserve_every_file'),
 ('queue-absent','control.py','value = json.loads(path.read_text(), object_pairs_hook=unique_object)',
  'value = json.loads(path.read_text(), object_pairs_hook=unique_object) if path.exists() else dict(tasks={},groups={})',
  'StartupTests.test_strict_queue_and_missing_initialized_state_preserve_every_file'),
 ('all-groups-closed','control.py',"        row['status'] = 'Paused'","        row['status'] = 'Running' if g == 'default' else 'Paused'",
  'StartupTests.test_task_matrix_and_all_groups_closed_without_requeue_or_cancel_receipts'),
 ('running-never-requeued','control.py',"task['status'] = {'Done': dict(enqueued_at=detail['enqueued_at'], start=detail.get('start', stamp), end=stamp, result='Killed')}",
  "task['status'] = {'Queued': dict(enqueued_at=detail['enqueued_at'])}",
  'StartupTests.test_task_matrix_and_all_groups_closed_without_requeue_or_cancel_receipts'),
 ('log-lexists','control.py',"os.path.lexists(data / 'task_logs' / (ident + '.log'))","(data / 'task_logs' / (ident + '.log')).exists()",
  'StartupTests.test_task_matrix_and_all_groups_closed_without_requeue_or_cancel_receipts'),
 ('queued-identity','control.py',"        variant, detail = next(iter(task['status'].items()))\n        if variant in", "        task['priority'] = 0\n        variant, detail = next(iter(task['status'].items()))\n        if variant in",
  'StartupTests.test_task_matrix_and_all_groups_closed_without_requeue_or_cancel_receipts'),
 ('unknown-locked','control.py',"        elif variant == 'Locked':\n            holds", "        elif variant == 'Locked':\n            task['status'] = {'Queued': {'enqueued_at': stamp}}\n            holds",
  'StartupTests.test_task_matrix_and_all_groups_closed_without_requeue_or_cancel_receipts'),
 ('manual-ownership','control.py',"elif row['status'] == 'Paused' and not reasons:","elif False:",
  'StartupTests.test_manual_ambiguous_resume_uses_same_admission_and_no_panel_button'),
 ('offline-live-pid','control.py',"        if result.returncode != 1:","        if False:",
  'StartupTests.test_live_or_unknown_previous_pid_blocks_and_leaves_bytes_intact'),
 ('intent-before-queue','control.py',"    with engine.connect(write=True) as db:\n        engine.store(db, state)\n    boundary('intent')",
  "    boundary('intent')",'StartupTests.test_crash_at_every_cross_file_boundary_and_repeat_recovery'),
 ('queue-before-completion','control.py',"    atomic_json(queue_path, image)\n    boundary('queue')","    boundary('queue')",
  'StartupTests.test_crash_at_every_cross_file_boundary_and_repeat_recovery'),
 ('completion-repeatable','control.py',"    state['startup']['phase'] = 'prepared'","    state['startup']['phase'] = 'intent'",
  'StartupTests.test_crash_at_every_cross_file_boundary_and_repeat_recovery'),
 ('dry-before-writes','control.py','    if dry_run:\n        return image','    if False:\n        return image',
  'StartupTests.test_ensayo_branches_before_intent_expiry_pid_or_queue_write'),
 ('expiry-during-prepare','control.py','    if engine.resolve(state) != state:\n        return prepare_start','    if False:\n        return prepare_start',
  'StartupTests.test_event_crosses_prepare_and_expired_advance_adjustments_are_not_reinstated'),
 ('startup-generation','control.py',"prepared = (startup.get('phase') == 'prepared' and","prepared = (False and",
  'StartupTests.test_guard_reconciles_before_ready_sensors_then_only_releases_owned_startup'),
 ('docker-gate','control.py',"if self.host == 'desk' and state.get('startup_version') == 1:","if False:",
  'StartupTests.test_docker_failure_and_new_pressure_prevent_admission_and_explicit_resume'),
 ('prepare-maintenance','control.py',"    if os.path.lexists(engine.directory / 'maintenance'):","    if False:",
  'StartupTests.test_unreadable_authority_no_fallback_and_maintenance_no_open_or_prepare'),
 ('cli-zero-write-reject','control.py',"            if args.command != 'preparar':","            if True:",
  'StartupTests.test_cli_invalid_queue_does_not_even_write_diagnostic_log'),
 ('install-preflight','install-desk.py',"    config = control.validate_configuration(home=home, env=manager, cwd=home)","    config = dict(data=home/'data', pid_file=home/'data/pid')",
  'InstallerStartupTests.test_o_w2_install_real_and_seco_use_validator_before_every_effect'),
 ('install-pending-same-schema','install-desk.py',"        actions = [json.loads(row[0]) for row in db.execute('SELECT body FROM host_actions')]",
  "        if state['policy_hash'] == engine.policy_hash and not fields_missing:\n            return None\n        actions = [json.loads(row[0]) for row in db.execute('SELECT body FROM host_actions')]",
  'InstallerStartupTests.test_all_pending_receipts_reject_even_when_schema_already_current'),
 ('install-preserve-maintenance','install-desk.py',"    own_maintenance = False","    own_maintenance = True",
  'InstallerStartupTests.test_preexisting_maintenance_and_broken_symlink_are_never_removed'),
 ('install-stop-order','install-desk.py',"        run('systemctl', '--user', 'stop', *TIMERS)\n        run('systemctl', '--user', 'stop', *SERVICES)",
  "        run('systemctl', '--user', 'stop', *SERVICES)\n        run('systemctl', '--user', 'stop', *TIMERS)",
  'InstallerStartupTests.test_additive_install_keeps_pid_desire_receipts_and_loads_unit_without_restart'),
 ('install-no-restart','install-desk.py',"            run('systemctl', '--user', 'daemon-reload')  # never restart the shared daemon",
  "            run('systemctl', '--user', 'restart', 'pueued.service')",
  'InstallerStartupTests.test_additive_install_keeps_pid_desire_receipts_and_loads_unit_without_restart'),
 ('graceful-stop-identity','control.py',"state.get('stop_tasks', {}).get(ident) == task['created_at']","True",
  'StartupTests.test_graceful_stop_marks_only_active_identities_before_terminal_killed'),
 ('fresh-after-docker','control.py',"                    if decide('desk', current, threshold) != 'reanudar':","                    if False:",
  'StartupTests.test_pressure_and_generation_are_fresh_after_docker'),
 ('validated-reconstructed-image','control.py',("    validate_queue(queue)  # an intent reconstructed from authority is validated too","    validate_queue(image)  # validate the exact bytes to be published, before intent"),("    pass","    pass"),
  'StartupTests.test_reconstructed_intent_must_pass_same_queue_validator_before_writes'),
 ('startup-authority-version','control.py',"        if ('startup_version' in value and (type(value['startup_version']) is not int or value['startup_version'] != 1)","        if (False and (type(value['startup_version']) is not int or value['startup_version'] != 1)",
  'StartupTests.test_corrupt_startup_version_or_unattributed_reason_is_not_authority'),
 ('startup-owner-proof','control.py',"or any('startup' in r for r in value['reasons'].values()) and startup is None","or False",
  'StartupTests.test_corrupt_startup_version_or_unattributed_reason_is_not_authority'),
 ('legacy-retirement-progress','control.py',"            state.pop('retire_intent', None)","            pass  # retain stale retirement",
  'StartupTests.test_explicit_retirement_is_recoverable_and_refuses_tasks'),
 ('legacy-retirement-tasks','control.py',"        if any(t['group'] == group for t in snapshot['tasks'].values()):\n            raise ValueError('Grupo con tareas; no se retira')",
  "        if False:\n            raise ValueError('Grupo con tareas; no se retira')",'StartupTests.test_explicit_retirement_is_recoverable_and_refuses_tasks'),
]


CASES += [
 ('external-running-startup','control.py',"                reasons.discard('startup')\n                state.get('startup', {}).get('holds', {}).pop(g, None)","                state.get('startup', {}).get('holds', {}).pop(g, None)",
  'test_startup_injerto.StartupGraftTests.test_a_w2_external_running_releases_startup_and_second_pause_is_manual'),
 ('external-running-hold','control.py',"                reasons.discard('startup')\n                state.get('startup', {}).get('holds', {}).pop(g, None)","                reasons.discard('startup')",
  'test_startup_injerto.StartupGraftTests.test_a_w2_external_running_releases_startup_and_second_pause_is_manual'),
 ('foreign-resume-cli','control.py',"foreign_resume = args.host == 'desk' and args.command == 'reanudar'",'foreign_resume = False',
  'test_startup_injerto.StartupGraftTests.test_a_w1_cli_resolves_existing_foreign_without_changing_profile'),
 ('foreign-resume-exists','control.py',"if self.host == 'desk' and group not in self.pueue.status()['groups']:",'if False:',
  'test_startup_injerto.StartupGraftTests.test_a_w1_cli_resolves_existing_foreign_without_changing_profile'),
 ('manager-env-gate','install-desk.py',"env=manager, cwd=home",'env={}, cwd=home',
  'test_startup_injerto.InstallerGraftTests.test_a_w3_service_cwd_and_manager_xdg_reject_before_effects'),
 ('manager-cwd-gate','install-desk.py',"env=manager, cwd=home",'env=manager',
  'test_startup_injerto.InstallerGraftTests.test_a_w3_service_cwd_and_manager_xdg_reject_before_effects'),
 ('intent-foreign-image','control.py',"        elif digest != prior['target_hash']:","        elif False:",
  'test_startup_injerto.StartupGraftTests.test_foreign_image_during_intent_blocks_without_writes'),
 ('preexisting-pressure-startup','control.py',"        reasons.add('startup')","        if 'pressure' not in reasons:reasons.add('startup')",
  'test_startup_injerto.StartupGraftTests.test_pressure_manual_and_foreign_pauses_keep_startup_docker_gate'),
 ('install-missing-queue','install-desk.py',"    control.read_queue(config['data'] / 'state.json')",'    pass',
  'test_startup_injerto.InstallerGraftTests.test_adopted_missing_or_corrupt_queue_rejected_without_effects'),
 ('timer-requisite','units/pueue-desk-guard.service','Requisite=pueued.service','Requires=pueued.service',
  'test_startup_injerto.InstallerGraftTests.test_timer_services_require_running_daemon_without_starting_it'),
]
for key,condition in (
 ('pid',"loaded['MainPID'] != engine.install_unit_before['MainPID']"),
 ('active',"loaded['ActiveState'] != engine.install_unit_before['ActiveState']"),
 ('reload',"loaded['NeedDaemonReload'] != 'no'"),
 ('restart',"loaded['Restart'] != 'always'"),
 ('killmode',"loaded['KillMode'] != 'mixed'"),
 ('preparer',"str(home / '.local/lib/pao-pueue-desk/control.py') + ' preparar --host desk' not in loaded['ExecStartPre']")):
 CASES.append(('loaded-unit-'+key,'install-desk.py',condition,'False',
  'test_startup_injerto.InstallerGraftTests.test_loaded_unit_mismatch_retains_maintenance_and_no_timer_restart'))
CASES += [
 ('timer-start-keeps-maintenance','install-desk.py',
  "            if not os.path.lexists(maintenance):\n                control.atomic_json(maintenance, dict(reason='instalación incompleta'))\n            try:","            try:",
  'InstallerStartupTests.test_failed_timer_start_restores_maintenance_and_stops_both_timers'),
 ('timer-start-stops-timers','install-desk.py',
  "                run('systemctl', '--user', 'stop', *TIMERS)\n            except Exception:","                pass\n            except Exception:",
  'InstallerStartupTests.test_failed_timer_start_restores_maintenance_and_stops_both_timers'),
 ('stock-null-shell-command','control.py',"daemon_config.get('shell_command') is not None","'shell_command' in daemon_config",
  'ConfigurationTests.test_stock_4_0_4_configuration_with_null_shell_command_is_accepted'),
 ('custom-shell-command','control.py',"daemon_config.get('shell_command') is not None","False",
  'ConfigurationTests.test_o_w2_complex_configuration_rejects_in_same_validator'),
]


def main():
    for name,filename,before,after,test in CASES:
        with tempfile.TemporaryDirectory(prefix='u34-sol-mut-',dir='/tmp') as folder:
            root=Path(folder)/'pueue-desk';shutil.copytree(BASE,root,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            path=root/filename;source=path.read_text()
            changes=zip(before,after) if isinstance(before,tuple) else [(before,after)]
            for original,replacement in changes:
                assert source.count(original)==1,(name,'nonunique anchor')
                source=source.replace(original,replacement)
            path.write_text(source)
            result=subprocess.run([sys.executable,'-B','-m','unittest',(test if test.startswith('test_') else 'test_startup.'+test),'-v'],cwd=str(root/'tests'),
                                  text=True,capture_output=True,timeout=15,env=dict(os.environ,TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1'))
            assert result.returncode==1 and 'FAIL:' in result.stderr and 'ERROR:' not in result.stderr,name+' survived or failed outside assertion:\n'+result.stderr
            print(name+': caught by assertion',flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' U3.4 safety mutations caught; tree untouched')


if __name__=='__main__':main()
