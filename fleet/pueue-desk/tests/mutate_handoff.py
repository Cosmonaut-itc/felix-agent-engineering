"""Each U3.5 safety mutant must fail its directed test by assertion (3.9/3.12)."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
CASES=[
 ('mini-authority-dispatch','control.py',"if host == 'mini' and has_authority(directory):",'if False:',
  'test_slots.SlotsTests.test_actual_guard_dispatch_never_falls_back_when_authority_corrupt'),
 ('login-disable','mini.py',"            self.command(state, 'disable', other)",'            pass',
  'test_o_w3_login_reconstructs_all_enabled_plists_at_bootstrap_boundary'),
 ('disable-before-publish','mini.py',"            self.command(state, 'disable', target)",'            pass',
  'test_w6_every_durable_and_effect_boundary_including_job_kill_and_login'),
 ('durable-before-effect','mini.py',"        self.save(state, 'intent:' + name)",'        pass',
  'test_each_manager_effect_has_prior_durable_intent_and_pause_ownership'),
 ('pause-before-rpc','mini.py',"                    self.save(state, 'pause:intent:' + group)",'                    pass',
  'test_each_manager_effect_has_prior_durable_intent_and_pause_ownership'),
 ('no-manual-takeover','mini.py',"state['reasons'][group] = sorted((set(reasons) - {'handoff'}) | {self.release_reason(group)})\n            if target", "state['reasons'][group] = ['pressure']\n            if target",
  'test_running_drains_without_killing_then_recovery_owns_pause_before_rpc'),
 ('drain-before-stop','mini.py',"            if self.active(self.pueue.status()):",'            if False:',
  'test_running_drains_without_killing_then_recovery_owns_pause_before_rpc'),
 ('dry-before-writes','mini.py',"            if dry:\n                print('SECO: validar","            if False:\n                print('SECO: validar",
  'test_seco_and_pending_receipts_reject_before_any_effect_or_write'),
 ('pending-blocks-install','mini.py',"in ('applying', 'partial', 'unknown')","in ()",
  'test_seco_and_pending_receipts_reject_before_any_effect_or_write'),
 ('shared-preflight','mini.py','        config = self.configuration_for_host()\n        queue =','        config = control.validate_configuration()\n        queue =',
  'test_shared_validator_in_seco_and_real_precedes_all_effects'),
 ('maintenance-recovery','mini.py',"        if os.path.lexists(self.directory / 'maintenance') or value and value['phase'] == 'maintenance':\n            return False",'        if False:\n            return False',
  'test_maintenance_during_bootout_gap_blocks_every_manager_effect'),
 ('maintenance-wrapper','mini.py',"        if os.path.lexists(self.directory / 'maintenance'):\n            raise ValueError('Host en mantenimiento')",'        if False:\n            raise ValueError(\'Host en mantenimiento\')',
  'test_maintenance_stops_installer_recoverer_wrapper_and_guard_without_reopening'),
 ('single-daemon','mini.py',"or self.loaded(BREW) is not None or not self.disabled(BREW)","or not self.disabled(BREW)",
  'test_two_daemons_and_alive_pid_are_rejected_without_overwriting_queue'),
 ('keepalive','launchagents/local.pao.pueue-mini.daemon.plist','<key>KeepAlive</key><true/>','<key>KeepAlive</key><false/>',
  'test_delivered_plists_fixed_preparer_keepalive_throttle_and_existing_job'),
 ('throttle','launchagents/local.pao.pueue-mini.daemon.plist','<integer>10</integer>','<integer>0</integer>',
  'test_delivered_plists_fixed_preparer_keepalive_throttle_and_existing_job'),
 ('fixed-preparer','mini.py',"            control.prepare_start(self.engine, configuration=config)\n            self.checkpoint('start:prepared')","            self.checkpoint('start:prepared')",
  'test_delivered_plists_fixed_preparer_keepalive_throttle_and_existing_job'),
 ('fullfsync','control.py','        fcntl.fcntl(stream.fileno(), 51)','        pass # fcntl.fcntl(stream.fileno(), 51)',
  'test_fullfsync_is_required_for_own_files_on_macos'),
 ('maintenance-reconcile','control.py',"    def reconcile(self):\n        if os.path.lexists(self.directory / 'maintenance'):\n            return False",'    def reconcile(self):\n        if False:\n            return False',
  'test_maintenance_stops_installer_recoverer_wrapper_and_guard_without_reopening'),
 ('maintenance-stop-order','mini.py',"                self.bootout(state, GUARD)","                pass",
  'test_maintenance_command_stops_guard_first_and_resumes_only_own_marker'),
 ('maintenance-foreign-marker','mini.py',"or not marker.is_file() or marker.read_bytes() != signature",'or not marker.is_file()',
  'test_maintenance_marker_changed_after_publication_is_never_removed'),
 ('launchd-disable-words','mini.py',"match[2] in ('true', 'disabled')","match[2] == 'true'",
  'test_launchd_disabled_words_and_booleans_and_unknown_fail_closed'),
 ('maintenance-replay','mini.py',"value['phase'] not in ('maintenance', 'switch', 'verify')","value['phase'] != 'maintenance'",
  'test_maintenance_stop_and_continue_replay_after_each_boundary'),
 ('handoff-authority','control.py',"if self.host == 'mini' and 'mini_handoff' in value:","if False:",
  'test_corrupt_handoff_blocks_queries_and_recovery_without_effects'),
 ('protect-on-recovery-failure','control.py',"            return MiniControl(pueue, directory)._legacy_guard(None, proc=proc, recovery_error=error)",'            raise',
  'test_recovery_error_still_closes_pressure_but_never_opens'),
 ('never-open-on-recovery-failure','control.py',"failure, state = recovery_error, None","failure, state = None, None",
  'test_recovery_error_still_closes_pressure_but_never_opens'),
 ('mini-config-environment','control.py',"        env.pop(key, None)",'        pass',
  'test_mini_clients_and_fixed_exec_share_home_cwd_and_clean_config_environment'),
 ('mini-client-cwd','control.py',"        pueue.context = dict(env=mini_environment(Path.home()), cwd=Path.home())",'        pass',
  'test_mini_clients_and_fixed_exec_share_home_cwd_and_clean_config_environment'),
 ('wrapper-mainpid','mini.py',"        if info is None or not re.search(r'^\\s*pid = ' + str(os.getpid()) + r'\\s*$', info, re.M):",'        if False:',
  'test_unowned_wrapper_cannot_prepare_or_exec'),
 ('wait-for-bootout','mini.py',"            except ValueError:\n                return False  # bootout may return before its verified PID/socket has exited",'            except ValueError:\n                pass',
  'test_async_bootout_waits_for_dead_pid_before_bootstrap'),
 ('legacy-publishes-recoverer','install-mini.py','shutil.copy2(SOURCE / "mini.py", path)','shutil.copy2(SOURCE / "control.py", path)',
  'test_pueue_mini.InstallerTests.test_install_twice_plist_and_withdraw_twice_resumes_owned_groups'),
]

CASES += [
 ('aborted-source-override','mini.py',"        self.save(state, 'handoff:aborted')",
  "        self.save(state, 'handoff:aborted')\n        self.command(state, 'disable', BREW if source['owner'] == 'homebrew' else LABEL)",
  'test_handoff.InjertoTests.test_withdraw_after_aborted_install_retains_source_enable_and_login'),
 ('wait-bounded','mini.py','or not 0 <= wait <= 7200','or False',
  'test_handoff.InjertoTests.test_wait_budget_invalid_rejects_without_writes'),
 ('deadline-validator','control.py',"or value['deadline'] is not None and (type(value['deadline']) not in (int, float) or not math.isfinite(value['deadline']))",'or False',
  'test_handoff.InjertoTests.test_explicit_wait_deadline_survives_job_death_aborts_without_killing_or_opening'),
 ('package-templates','mini.py',"names + ['mini.py', 'control.py']","['mini.py', 'control.py']",
  'test_handoff.InjertoTests.test_package_complete_before_intent_and_only_guard_code_updated'),
 ('package-before-intent','mini.py','            self.install_recoverer()','            pass',
  'test_handoff.InjertoTests.test_package_complete_before_intent_and_only_guard_code_updated'),
 ('projection-preflight','mini.py','        control.start_projection(self.engine, config, allow_pending=False)','        pass',
  'test_handoff.InjertoTests.test_shared_projection_rejects_pending_and_bad_images_before_effects'),
 ('projection-exact-image','control.py','    validate_queue(image)  # validate the exact bytes to be published, before intent','    pass',
  'test_handoff.InjertoTests.test_shared_projection_rejects_pending_and_bad_images_before_effects'),
 ('relevo-opt-in','install-mini.py',"if '--relevo' in sys.argv:","if '--solo-guardia' not in sys.argv:",
  'test_handoff.InjertoTests.test_installer_relevo_opt_in_routes_explicitly_without_legacy_reload'),
 ('wait-explicit','mini.py','            if not wait and self.active(self.pueue.status()):','            if False:',
  'test_handoff.InjertoTests.test_explicit_wait_deadline_survives_job_death_aborts_without_killing_or_opening'),
 ('drain-deadline-durable','mini.py','deadline=self.engine.clock() + wait','deadline=self.engine.clock() + 7200',
  'test_handoff.InjertoTests.test_explicit_wait_deadline_survives_job_death_aborts_without_killing_or_opening'),
 ('drain-deadline-abort','mini.py',"            if value['deadline'] is not None and self.engine.clock() >= value['deadline']:",'            if False:',
  'test_handoff.InjertoTests.test_explicit_wait_deadline_survives_job_death_aborts_without_killing_or_opening'),
 ('abort-no-manual-takeover','mini.py',"state['reasons'][group] = sorted((set(reasons) - {'handoff'}) | {self.release_reason(group)})\n        state.pop", "state['reasons'][group] = ['pressure']\n        state.pop",
  'test_handoff.InjertoTests.test_explicit_wait_deadline_survives_job_death_aborts_without_killing_or_opening'),
 ('guard-upgrade-stays-loaded','install-mini.py',"keep_guard = args.command == 'instalar' and args.cuotas and guard_loaded",'keep_guard = False',
  'test_slots_install.SlotsInstallTests.test_atomic_adoption_enables_only_after_bootstrap_and_reinstall_preserves_receipts'),
]


CASES += [
 ('r2c-bootout-accepts-3','mini.py',"result.returncode not in (0, 3, 113)","result.returncode not in (0, 113)",
  'test_continue_bootout_absent_3_prepared'),
 ('r2c-bootout-waits-unloaded','mini.py',"while self.loaded(label) is not None:","while False:",
  'test_continue_bootout_waits_prepared'),
 ('r2b-verify-retries-switch','mini.py',
  "            if self.loaded(target) is None:\n                value['phase'] = 'switch'\n                self.save(state, 'handoff:retry-switch')\n                return False\n",
  "",
  'test_continue_stale_bootout_retries_prepared_owner'),
 ('r2-origin-not-target','mini.py',"value.update(phase='aborted', effect=None,\n                     retained_owner=source['owner'])",
  "value.update(phase='aborted', effect=None,\n                     retained_owner='homebrew' if value['target'] == 'install' else 'prepared')",
  'test_abort_retains_verified_source_independently_of_target_and_validates_identity'),
 ('r2-live-target-converges','mini.py',"            if same_owner and previous:","            if False:",
  'test_opposite_withdraw_install_running_converges_and_login'),
 ('r1-retained-owner','mini.py',
  "value.update(phase='aborted', effect=None,\n                     retained_owner=source['owner'])",
  "value.update(phase='aborted', effect=None)",
  'test_handoff_r1.R1Tests.test_aborted_withdraw_restarts_prepared_owner_and_login'),
 ('r1-aborted-wrapper','mini.py',"and value.get('retained_owner') == 'prepared'","and False",
  'test_handoff_r1.R1Tests.test_aborted_withdraw_restarts_prepared_owner_and_login'),
 ('r1-legacy-owner-repair','mini.py',"if value and value['phase'] == 'aborted' and 'retained_owner' not in value:","if False:",
  'test_handoff_r1.R1Tests.test_legacy_aborted_withdraw_is_repaired_by_existing_job'),
 ('r1-owner-validation','control.py',"or 'retained_owner' in value and value['retained_owner'] not in ('homebrew', 'prepared')","or False",
  'test_handoff_r1.R1Tests.test_retained_owner_is_validated_and_never_authorizes_brew_or_inflight_withdraw'),
 ('r1-final-drain-record','mini.py',"            state.pop('stop_tasks', None)\n            value['phase'] = 'switch'","            value['phase'] = 'switch'",
  'test_handoff_r1.R1Tests.test_cancel_between_stop_record_and_final_drain_read_has_no_restart_reason'),
 ('r1-maintenance-retains-manager','mini.py',"                if value['phase'] == 'aborted':","                if False:",
  'test_handoff_r1.R1Tests.test_maintenance_after_either_abort_returns_same_owner'),
 ('r1-brew-verbose','mini.py',"len(argv) not in (1, 2)","len(argv) != 1",
  'test_handoff_r1.R1Tests.test_homebrew_404_exact_plist_is_accepted_and_other_arguments_rejected'),
 ('r1-brew-cwd','mini.py',"brew_cwd not in (self.home, Path('/opt/homebrew/var'))","brew_cwd != self.home",
  'test_handoff_r1.R1Tests.test_homebrew_404_exact_plist_is_accepted_and_other_arguments_rejected'),
 ('r1-brew-fixed-argv','mini.py',"or argv[1:] not in ([], ['--verbose'])","or False",
  'test_handoff_r1.R1Tests.test_homebrew_404_exact_plist_is_accepted_and_other_arguments_rejected'),
 ('r1-brew-fixed-binary','mini.py',"or Path(argv[0]).resolve() != Path(DAEMON).resolve()","or False",
  'test_handoff_r1.R1Tests.test_homebrew_404_exact_plist_is_accepted_and_other_arguments_rejected'),
 ('r1-brew-no-env','mini.py',"or 'EnvironmentVariables' in brew","or False",
  'test_handoff_r1.R1Tests.test_homebrew_404_exact_plist_is_accepted_and_other_arguments_rejected'),
 ('r1-same-config-source','mini.py',"if configuration_source(self.home, brew_cwd) != configuration_source(self.home, self.home):","if False:",
  'test_handoff_r1.R1Tests.test_cwd_config_selection_must_match_source_and_destination'),
 ('r1-start-reason','mini.py',"                self.log_start_rejection(error)","                pass",
  'test_handoff_r1.R1Tests.test_blocked_start_records_safe_reason_and_bounded_log'),
 ('r1-start-log-bounded','mini.py',"START_LOG_BYTES = 65536","START_LOG_BYTES = 655360",
  'test_handoff_r1.R1Tests.test_blocked_start_records_safe_reason_and_bounded_log'),
 ('r1-start-log-private','mini.py',"'Preparación o autorización rechazada; revisar cola, PID y autoridad')","message)",
  'test_handoff_r1.R1Tests.test_blocked_start_records_safe_reason_and_bounded_log'),
 ('r1-json-detail-private','mini.py',"and error.msg in JSON_REASONS:","and True:",
  'test_handoff_r1.R1Tests.test_blocked_start_records_safe_reason_and_bounded_log'),
 ('r1-foreign-pause','mini.py',"return 'pressure' if group in self.engine.profile else 'ambiguous'","return 'pressure'",
  'test_handoff_r1.R1Tests.test_foreign_handoff_pause_is_resolvable_on_completion_and_abort'),
]

def main():
    for name,relative,before,after,test in CASES:
        with tempfile.TemporaryDirectory(prefix='u35-mut-',dir='/tmp') as folder:
            root=Path(folder)/'pueue-desk'
            shutil.copytree(ROOT,root,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            path=root/relative;source=path.read_text()
            assert source.count(before)==1,(name,'nonunique anchor')
            path.write_text(source.replace(before,after))
            target_test=test if '.' in test else 'test_handoff.HandoffTests.'+test
            result=subprocess.run([sys.executable,'-B','-m','unittest',target_test,'-v'],
                cwd=str(root/'tests'),env=dict(os.environ,TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1'),text=True,capture_output=True,timeout=120)
            assert result.returncode==1 and 'FAIL:' in result.stderr and 'ERROR:' not in result.stderr,name+' survived or failed outside assertion:\n'+result.stderr
            print(name+': caught by assertion',flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' U3.5 mutations caught on '+sys.version.split()[0],flush=True)


if __name__=='__main__':main()
