"""Each U3.5 safety mutant must fail its directed test by assertion (3.9/3.12)."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
CASES=[
 ('login-disable','mini.py',"            self.command(state, 'disable', other)",'            pass',
  'test_o_w3_login_reconstructs_all_enabled_plists_at_bootstrap_boundary'),
 ('disable-before-publish','mini.py',"            self.command(state, 'disable', target)",'            pass',
  'test_w6_every_durable_and_effect_boundary_including_job_kill_and_login'),
 ('durable-before-effect','mini.py',"        self.save(state, 'intent:' + name)",'        pass',
  'test_each_manager_effect_has_prior_durable_intent_and_pause_ownership'),
 ('pause-before-rpc','mini.py',"                    self.save(state, 'pause:intent:' + group)",'                    pass',
  'test_each_manager_effect_has_prior_durable_intent_and_pause_ownership'),
 ('no-manual-takeover','mini.py',"sorted((set(reasons) - {'handoff'}) | {'pressure'})","['pressure']",
  'test_running_drains_without_killing_then_recovery_owns_pause_before_rpc'),
 ('drain-before-stop','mini.py',"            if any(set(t['status']) & {'Running', 'Paused'} for t in self.pueue.status()['tasks'].values()):",'            if False:',
  'test_running_drains_without_killing_then_recovery_owns_pause_before_rpc'),
 ('dry-before-writes','mini.py','            if dry:','            if False:',
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
 ('maintenance-stop-order','mini.py',"                if self.loaded(GUARD) is not None:\n                    self.command(state, 'bootout', GUARD)","                pass",
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
                cwd=str(root/'tests'),env=dict(os.environ,TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1'),text=True,capture_output=True,timeout=40)
            assert result.returncode==1 and 'FAIL:' in result.stderr and 'ERROR:' not in result.stderr,name+' survived or failed outside assertion:\n'+result.stderr
            print(name+': caught by assertion',flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' U3.5 mutations caught on '+sys.version.split()[0],flush=True)


if __name__=='__main__':main()
