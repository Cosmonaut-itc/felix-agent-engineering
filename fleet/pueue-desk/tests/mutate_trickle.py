"""SEN-6417 directed mutants: each must fail an assertion, nominal first."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE=Path(__file__).resolve().parents[1]
CASES=[('finish_clock_unreadable',
  "        boot, ns = self._boot_sample()\n        if (boot != attempt['boot_id']",
  '        try:\n'
  '            boot, ns = self._boot_sample()\n'
  '        except OSError:\n'
  "            boot, ns = attempt['boot_id'], attempt['reserved_ns']\n"
  "        if (boot != attempt['boot_id']",
  'test_clock_unreadable_at_completion_keeps_uncertainty'),
 ('finish_clock_backwards',
  "            outcome = 'unknown'\n        attempt['outcome']",
  "            outcome = outcome\n        attempt['outcome']",
  'test_clock_backwards_at_completion_keeps_uncertainty'),
 ('evidence_future', ' <= timestamp_key(utc_at(self.clock()))', '', 'test_future_execution_is_not_evidence'),
 ('evidence_prior',
  "timestamp_key(attempt['at']) <= timestamp_key(detail['start'])",
  "timestamp_key(detail['start'])",
  'test_execution_before_reservation_is_not_evidence'),
 ('evidence_requeued',
  "detail.get('enqueued_at') == attempt['enqueued_at']",
  'True',
  'test_requeued_execution_is_not_evidence'),
 ('permit_incoherent',
  "    elif confirmed is not None:\n        raise ValueError('Confirmación sin intento')",
  "    elif False:\n        raise ValueError('Confirmación sin intento')",
  'test_confirmation_without_attempt_is_rejected'),
 ('final_unknown_not_sent',
  "            except ADMISSION_ERRORS:\n                cause = 'task_changed'",
  '            except ADMISSION_ERRORS:\n                return',
  'test_final_unknown_state_finishes_not_sent'),
 ('recovery_maintenance',
  "if self.host != 'desk' or not os.path.lexists(self.directory/'maintenance'):",
  'if False:',
  'test_recovery_requires_maintenance_and_explicit_no_late_effect'),
 ('recovery_certification',
  'if not confirmed and not no_effect:',
  'if False:',
  'test_recovery_other_boot_requires_certification_without_evidence'),
 ('recovery_clock_floor',
  'or minimum is not None and (boot != minimum[0] or ns < minimum[1])',
  'or False',
  'test_recovery_clock_failure_or_rollback_does_not_release'),
 ('reasons_exact',
  "reasons != {'pressure'}",
  "'pressure' not in reasons",
  'test_dependencies_and_mixed_reasons'),
 ('initial_sensor',
  '            block(stage)',
  "            block('authority')",
  'test_initial_sensor_failure_has_no_inferred_operation'),
 ('omit_one',
  "            if normal == 'mantener' and can_open:",
  '            if False:',
  'test_band_positive_fifo_and_property_then_low'),
 ('open_group',
  "self.pueue.call('start',ident)",
  "self.pueue.call('start','-g',group)",
  'test_band_positive_fifo_and_property_then_low'),
 ('gap_58',
  'ADMISSION_GAP_NS = 60_000_000_000',
  'ADMISSION_GAP_NS = 58_000_000_000',
  'test_global_cooldown_instances_59999_60'),
 ('quota',
  "occupied += bool(set(status) & {'Running','Paused','Locked'})",
  "occupied += bool(set(status) & {'Running'})",
  'test_quota_full_plus_other_free_counts_paused_locked'),
 ('dependency',
  "if any(tasks.get(str(d),{}).get('status',{}).get('Done',{}).get('result') != 'Success' for d in "
  'dependencies):',
  'if False:',
  'test_dependencies_and_mixed_reasons'),
 ('completion',
  "attempt['completed_ns'] = ns if outcome in ('confirmed','not_sent') else None",
  "attempt['completed_ns'] = attempt['reserved_ns'] if outcome in ('confirmed','not_sent') else None",
  'test_slow_rpc_cooldown_starts_at_completion'),
 ('unknown',
  "if attempt and attempt['outcome'] in ('reserved','unknown'):",
  'if False:',
  'test_reused_identity_unknown_does_not_expire'),
 ('identity',
  "(('id','id'),('group','grupo'),('created_at','created_at'))",
  "(('id','id'),('group','grupo'))",
  'test_reused_identity_unknown_does_not_expire'),
 ('final_gate',
  'if final_candidate != candidate:',
  'if False:',
  'test_final_gate_failure_consumes_permission_without_replacement'),
 ('generation',
  "if self.generation(state) != state['generation']:\n"
  "                return 'generation', None, None, started",
  "if False:\n                return 'generation', None, None, started",
  'test_retention_generation'),
 ('logging',
  "log(self.directory,'admision_intento',tick_id=tick,grupo=group,id=ident)",
  "log(self.directory,'admision_intento',tick_id=tick,grupo=group,id=ident,detalle=snapshot)",
  'test_lost_response_confirmed_only_by_observation'),
 ('fifo_enqueue',
  'return min(candidates) if candidates else None',
  'return min(candidates,key=lambda c:c[1]) if candidates else None',
  'test_fifo_uses_enqueue_time_before_id_or_group'),
 ('rapidas_excluded',
  "            if group == 'rapidas':\n                continue",
  '            if False:\n                continue',
  'test_rapidas_stays_paused_without_incremental_start_in_memory_band'),
 ('mini_legacy',
  "        if self.host == 'mini':\n            return self._legacy_guard(readings,proc)",
  '        if False:\n            return self._legacy_guard(readings,proc)',
  'test_mini_uses_existing_guard_and_never_reserves'),
 ('resume_intent_budget',
  '                    if time.monotonic()-started > GATE_SECONDS:\n'
  "                        block('fresh_sample',group,'resume');continue",
  "                    if False:\n                        block('fresh_sample',group,'resume');continue",
  'test_resume_intent_expiration_does_not_open_groups'),
 ('main_exception_sanitized',
  'detalle=type(error).__name__',
  'detalle=str(error)',
  'test_main_exception_handler_never_logs_exception_text'),
 ('install_backup_bytes',
  'stream.write(target.read_bytes()); stream.flush(); os.fsync(stream.fileno())\n'
  '            control.sync_directory(library)',
  "stream.write(b'wrong'); stream.flush(); os.fsync(stream.fileno())\n"
  '            control.sync_directory(library)',
  'test_trickle_install.AdmissionInstallTests.test_backup_is_exact_immediately_previous_controller_each_time'),
 ('install_corrupt_permit',
  "    elif confirmed is not None:\n        raise ValueError('Confirmación sin intento')",
  "    elif False:\n        raise ValueError('Confirmación sin intento')",
  'test_trickle_install.AdmissionInstallTests.test_incoherent_confirmation_refuses_even_dry_run_without_writes')]

def run(root,test=None):
    return subprocess.run([sys.executable,'-B','-m','unittest',(test if test and '.' in test else 'test_trickle'+('.TrickleTests.'+test if test else ''))],cwd=str(root/'tests'),
        env=dict(os.environ,TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True,timeout=30)

def main():
    nominal=run(SOURCE)
    if nominal.returncode:raise AssertionError('Nominal failed: '+nominal.stderr)
    print('Nominal: '+nominal.stderr.strip(),flush=True)
    for name,anchor,replacement,test in CASES:
        root=Path(tempfile.mkdtemp(prefix='sen6417-sol-mutant-',dir='/tmp'))/'pueue-desk'
        shutil.copytree(SOURCE,root,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        path=root/('install-desk.py' if name=='install_backup_bytes' else 'control.py');source=path.read_text()
        if source.count(anchor)!=1:raise AssertionError('Anchor '+name)
        source=source.replace(anchor,replacement)
        if name=='unknown':
            source=source.replace("ns-attempt['completed_ns']","ns-(attempt['completed_ns'] or 0)")
        path.write_text(source)
        result=run(root,test)
        if os.environ.get('SEN6417_EVIDENCE_DIR'):
            (Path(os.environ['SEN6417_EVIDENCE_DIR'])/(name+'-rojo.txt')).write_text(result.stderr)
        if result.returncode!=1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:raise AssertionError(name+': '+result.stderr)
        print(name+': AssertionError; '+test,flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' killed by assertion',flush=True)

if __name__=='__main__':main()
