"""U3.1 safety mutations, in /tmp copies; every failure must be an assertion."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

FLEET = Path(__file__).resolve().parents[2]
# directory, file, original, replacement, targeted test
CASES = [
 ('pueue-desk','control.py','deadline = time.monotonic() + RACK_LOCK_SECONDS','deadline = time.monotonic()', 'SlotsTests.test_slots_and_undo_wait_for_short_guard_or_snapshot_tick'),
 ('pueue-desk','control.py',"status = ('rejected' if request['op'] in ('slots', 'undo') else", "status = ('unknown' if request['op'] in ('slots', 'undo') else", 'SlotsTests.test_expired_lock_is_terminal_without_effects_for_slots_and_undo'),
 ('rack','host_action.py',"if value.get('status') == 'busy' and (value.get('error') or {}).get('code') == 'lock_busy':",'if False:', 'ChannelTests.test_lock_busy_is_terminal_in_rack_and_snapshot_without_effects'),
 ('pueue-desk','control.py',"if status == 'Running' and 'pressure' in reasons:",'if False:', 'SlotsTests.test_external_start_drops_adopted_pressure_ownership'),
 ('pueue-desk','control.py',"self.store(db, state)\n                except errors as error:\n                    can_open, failure = False, error\n                    try:\n                        self.fence_pause(g)", "self.store(db, state)\n                except errors as error:\n                    can_open, failure = False, error\n                    try:\n                        pass", 'SlotsTests.test_failed_external_start_release_never_reclaims_foreign_pause'),
 ('pueue-desk','control.py','self.timeout = None','self.timeout = 3', 'SlotsTests.test_slow_pueue_still_protects_both_guards_and_applies_mode'),
 ('pueue-desk','control.py','pueue.timeout = 3','pueue.timeout = None', 'SlotsTests.test_panel_rpc_stays_bounded_and_uncertain_status_never_replays'),
 ('rack','index.html',"response.status===403 && data.status==='rejected' && data.error?.code==='disabled' && data.effects===false", 'false', 'ChannelTests.test_lost_disabled_rejection_releases_ui_but_auth_403_stays_closed'),
 ('rack','index.html',"response.status===403 && data.status==='rejected' && data.error?.code==='disabled' && data.effects===false", 'response.status===403', 'ChannelTests.test_lost_disabled_rejection_releases_ui_but_auth_403_stays_closed'),
 ('pueue-desk','control.py','MINI_PROFILE = {"suites": 3, "default": 1, "rapidas": 3}','MINI_PROFILE = {"suites": 3, "default": 1, "rapidas": 2}','SlotsTests.test_served_profile_includes_default_and_seven_places'),
 ('pueue-desk','control.py',"self.store(db, state)\n            self.pueue.call('pause', '--wait', '-g', group)","pass\n            self.pueue.call('pause', '--wait', '-g', group)",'SlotsTests.test_manual_pause_intention_durable_before_effect'),
 ('pueue-desk','control.py',"                    if observed != state['desired'] or self.generation(state) != state['generation']:\n                        raise ValueError('Deseo o daemon cambiaron antes de admitir')",'                    if False:\n                        pass','SlotsTests.test_daemon_change_between_sensor_and_start_retains_pause'),

 ('rack','accion.py',"if status not in (*CANCEL_OPEN, 'cancelled', 'already_finished', 'rejected'):","if status not in (*OPEN, 'cancelled', 'already_finished', 'rejected'):",'ChannelTests.test_public_host_variants_closed_without_breaking_cancel'),

 ('pueue-desk','control.py','os.path.lexists(directory / name)','(directory / name).exists()','SlotsTests.test_broken_authority_marker_never_restores_legacy_guard'),

 ('rack','accion.py',"if op == 'status' and response['status'] not in ('applied', 'applying', 'partial', 'unknown') and not verified:",'if False:','ChannelTests.test_host_downgrade_cannot_turn_uncertain_status_into_terminal_unsupported'),

 ('pueue-desk','install-mini.py','if observed != control.MINI_PROFILE:','if False:','SlotsInstallTests.test_profile_mismatch_refuses_before_bootout_or_writes'),
 ('pueue-desk','install-mini.py','if not os.path.lexists(switch):','if False:','SlotsInstallTests.test_atomic_adoption_enables_only_after_bootstrap_and_reinstall_preserves_receipts'),
 ('pueue-desk','install-mini.py','enable_after = False','enable_after = True','SlotsInstallTests.test_existing_switch_is_preserved_and_uninstall_cannot_discard_desire'),
 ('pueue-desk','control.py',"target.lstat()",'target.stat()','SlotsTests.test_host_switch_rejects_new_effect_but_accepted_recovery_continues'),

 ('pueue-desk','control.py',"self.store(db, state)  # ownership/intention precedes pause RPC",'pass','SlotsTests.test_pause_intention_durable_before_effect'),
 ('pueue-desk','control.py',"if decide('mini', current, RAPIDAS_THRESHOLDS['mini'] if g == 'rapidas' else MINI_THRESHOLDS) != 'reanudar':",'if False:','SlotsTests.test_fresh_pressure_prevents_reopen_after_low_initial_sample'),
 ('pueue-desk','control.py',"can_open = failure is None",'can_open = True','SlotsTests.test_guard_never_opens_before_desire_verified'),

 ('pueue-desk','control.py',"if observed != state['desired']:\n                validate_target(observed, state['desired'])",'if False:\n                pass','SlotsTests.test_recovery_never_increases_above_cap_even_for_grandfathered_desire'),
 ('pueue-desk','control.py','type(value) is not int or not 1 <= value <= 8','not isinstance(value, int) or not 1 <= value <= 8','SlotsTests.test_host_limits_boolean_zero_nine_and_complete_vector'),
 ('pueue-desk','control.py','not 1 <= value <= 8','not 0 <= value <= 9','SlotsTests.test_host_limits_boolean_zero_nine_and_complete_vector'),
 ('pueue-desk','control.py','if sum(target.values()) > cap and not monotonic:', 'if False:', 'SlotsTests.test_host_cap_counts_paused_groups'),
 ('pueue-desk','control.py','and not monotonic:', 'and False:', 'SlotsTests.test_minimum_maximum_and_monotonic_repair'),
 ('pueue-desk','control.py',"request['expected'] != expected or",'False or','SlotsTests.test_revision_vector_policy_generation_and_reset_checked_on_host'),
 ('pueue-desk','control.py',"expected['generation'] != state['generation']",'False','SlotsTests.test_new_generation_requires_reconciliation_before_accepting_fresh_request'),
 ('pueue-desk','control.py',"or before != state['desired']",'or False','SlotsTests.test_drift_visible_immediately_and_dry_run_does_not_reconcile'),
 ('pueue-desk','control.py',"if row[0] != digest",'if False','SlotsTests.test_id_digest_duplicate_and_rejections_are_durable'),
 ('pueue-desk','control.py',"if request['dry_run']:",'if False:','SlotsTests.test_dry_run_plan_queries_and_unadopted_have_zero_filesystem_or_rpc_effects'),
 ('pueue-desk','control.py',"if op == 'capabilities':\n            return self.view()", "if op == 'capabilities':\n            self.reconcile()\n            return self.view()",'SlotsTests.test_pending_queries_and_ensayo_do_not_recover'),
 ('pueue-desk','control.py',"if op == 'status':\n            with self.connect() as db:","if op == 'status':\n            self.reconcile()\n            with self.connect() as db:",'SlotsTests.test_pending_queries_and_ensayo_do_not_recover'),
 ('pueue-desk','control.py',"self.store(db, state)\n            db.execute('INSERT INTO host_actions", "self.store(db, state)\n            db.commit()\n            db.execute('INSERT INTO host_actions",'SlotsTests.test_atomic_desire_and_intent_when_receipt_insert_fails'),
 ('pueue-desk','control.py',"return [(g, target[g]) for g in sorted(target) if target[g] < before[g]] + [\n        (g, target[g]) for g in sorted(target) if target[g] > before[g]]", "return [(g, target[g]) for g in sorted(target) if target[g] > before[g]] + [\n        (g, target[g]) for g in sorted(target) if target[g] < before[g]]",'SlotsTests.test_reductions_verified_before_increases'),
 ('pueue-desk','control.py',"if observed[g] != n or self.generation(state) != generation:",'if False:','SlotsTests.test_unconfirmed_reduction_never_runs_increases'),
 ('pueue-desk','control.py',"previous['revision'] != state['revision']",'False','SlotsTests.test_undo_strict_revision_and_no_inverse_when_state_changed'),
 ('pueue-desk','control.py',"'ambiguous'})","'manual'})",'SlotsTests.test_restart_lost_parallel_is_reconciled_without_opening_ambiguous_or_foreign_groups'),
 ('pueue-desk','control.py',"if host == 'mini' and has_authority(directory):",'if False:','SlotsTests.test_actual_guard_dispatch_never_falls_back_when_authority_corrupt'),
 ('pueue-desk','control.py',"if calculated != value['desired']:",'if False:','SlotsTests.test_invalid_desire_metadata_does_not_apply_default'),
 ('pueue-desk','control.py',"'pressure' if g in owned and g in MINI_PROFILE else 'manual'","'pressure' if g in owned else 'manual'",'SlotsTests.test_import_ownership_separate_profile_including_missing_legacy_groups'),
 ('pueue-desk','control.py',"decision = rapid if g == 'rapidas' else normal",'decision = normal','SlotsTests.test_guard_pressure_rapidas_and_manual_takeover'),
 ('rack','accion.py',"if request['op'] == 'host_capabilities' or request.get('dry_run') is True:","if request['op'] == 'host_capabilities':",'ChannelTests.test_dry_run_and_capabilities_do_not_create_receipts_or_recover'),
 ('rack','host_action.py',"type(c['delta']) is not int or",'False or','ChannelTests.test_public_host_variants_closed_without_breaking_cancel'),
 ('rack','index.html',"&& action.request.op==='cancel'",'','ChannelTests.test_production_ui_slots'),
 ('rack','index.html',"if(['slots','undo'].includes(action.request.op)) return acceptSlots(key,action,data);",'','ChannelTests.test_production_ui_slots'),
 ('pueue-desk', 'control.py', 'if not can_open:\n                        self.fence_pause(g)', 'if not can_open:\n                        continue', 'SlotsTests.test_corrupt_quota_authority_still_closes_running_groups'),
 ('pueue-desk', 'control.py', 'if g not in groups:\n                continue', "if g not in groups:\n                raise ValueError('missing')", 'SlotsTests.test_missing_profile_member_three_ticks_preserve_rapidas_ladder'),
 ('pueue-desk', 'control.py', 'except errors as error:\n                    can_open, failure = False, error\n                continue', 'except errors as error:\n                    raise\n                continue', 'SlotsTests.test_failed_pause_rpc_does_not_skip_other_groups'),
 ('pueue-desk', 'control.py', 'self.store(db, state)  # ownership/intention precedes pause RPC\n                except errors as error:\n                    can_open, failure = False, error\n                    try:', 'self.store(db, state)  # ownership/intention precedes pause RPC\n                except errors as error:\n                    can_open, failure = False, error\n                    continue\n                    try:', 'SlotsTests.test_failed_authority_write_closes_but_never_grants_future_reopen'),
 ('pueue-desk', 'control.py', 'if os.path.lexists(self.pause_fence(g)):', 'if False:', 'SlotsTests.test_failed_authority_write_closes_but_never_grants_future_reopen'),
 ('rack', 'host_action.py', "response.get('receipt_digest') != digest or response.get('effects') is not False", 'False', 'ChannelTests.test_unbound_or_effectful_terminal_and_generic_unsupported_remain_unknown'),
 ('rack', 'accion.py', 'and not verified:', 'and True:', 'ChannelTests.test_lost_durable_rejection_recovers_terminal_without_resend'),
 ('rack', 'index.html', '(!response.ok && !hostTerminal)', '!response.ok', 'ChannelTests.test_production_ui_slots'),
 ('rack', 'tests/safety.py', ", 'control.py'", '', 'ChannelTests.test_installed_controller_is_forbidden_by_audit_hook'),
]


def main():
    for number,(directory,relative,original,replacement,test) in enumerate(CASES,1):
        with tempfile.TemporaryDirectory(prefix='u31-mutation-sol-',dir='/tmp') as folder:
            root=Path(folder)/'fleet'
            for name in ('rack','pueue-desk'):
                shutil.copytree(FLEET/name,root/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            target=root/directory/relative
            source=target.read_text()
            if source.count(original)!=1:
                raise AssertionError('Nonunique mutation anchor '+str(number)+': '+repr(original))
            target.write_text(source.replace(original,replacement))
            result=subprocess.run(['/usr/bin/python3','-B','-m','unittest',('test_slots_install.' if test.startswith('SlotsInstallTests.') else 'test_slots.')+test,'-v'],
                cwd=str(root/directory/'tests'),text=True,capture_output=True,timeout=15,
                env=dict(os.environ,TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1'))
            if result.returncode!=1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:
                raise AssertionError('Mutation '+str(number)+' did not fail by assertion:\n'+result.stdout+result.stderr)
            reason=next((s for s in result.stderr.splitlines() if s.startswith('AssertionError:')),'assertion')
            print(str(number)+' '+test+': caught | '+reason,flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' safety mutations caught; original tree untouched')


if __name__=='__main__': main()
