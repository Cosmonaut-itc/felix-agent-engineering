"""CC2 mutations: nominal green; each mutant must die by unittest assertion."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
from mutate_client_actions import run, SOURCE

T='test_client_reconcile.ReconcileTests.'
D='test_client_reconcile.DeskReservationsTests.'
S='test_client_reconcile.StartAuthorizationTests.'
CASES=[
 ('online reorder cannot adopt client Locked','control.py',
  "                        if self.client_blocked(g):", '                        if False:',
  T+'test_client_reservation_prevents_online_and_offline_locked_adoption'),
 ('offline reorder cannot adopt client Locked','control.py',
  "                    if ident in protected or self.client_blocked(task['group']):", '                    if False:',
  T+'test_client_reservation_prevents_online_and_offline_locked_adoption'),
 ('start revalidates authority after preparation','mini.py',
  "            if self.transaction(self.state()) != value:", '            if False:',
  S+'test_start_rechecks_handoff_after_collection_window'),
 ('rejected terminal is immutable','client_actions.py',
  "if prior and prior['response']['status'] not in c.REEVALUATED + ('unknown', 'pending'):",'if False:',T+'test_rejected_finish_receipt_cannot_be_replaced_by_reconciler'),
 ('dead reorder respects client reserve','control.py',
  '                        and not self.client_blocked(g, opening=True)','',T+'test_dead_reorder_owner_never_opens_over_retained_client_reservation'),
 ('CAS generation','client_actions.py',
  "state['generation'] != item['state']['generation'] or generation != item['generation']",'False',T+'test_generation_revision_digest_and_reservation_cas'),
 ('CAS journal revision','client_actions.py',"or current['revision'] != old['revision']",'',T+'test_generation_revision_digest_and_reservation_cas'),
 ('CAS request digest','client_actions.py',"or current['digest'] != old['digest']",'',T+'test_generation_revision_digest_and_reservation_cas'),
 ('CAS reservation revision','client_actions.py',"or reservation['revision'] != before['revision']",'',T+'test_generation_revision_digest_and_reservation_cas'),
 ('CAS reservation digest','client_actions.py',"or reservation['digest'] != before['digest']",'',T+'test_generation_revision_digest_and_reservation_cas'),
 ('collect outside host lock','client_actions.py',"            proposals = self.bounded_gather(copies, offline)",
  "            with self.lock():\n                proposals = self.bounded_gather(copies, offline)",T+'test_real_result_lock_and_client_lock_have_no_deadlock'),
 ('clock cannot close','client_actions.py',"        if proposal['kind'] == 'not_sent':","        if self.engine.clock() > 1000000 or proposal['kind'] == 'not_sent':",T+'test_clock_two_equal_reads_and_child_exit_never_exclude_late_rpc'),
 ('equal reads cannot settle','client_actions.py',"        if evidence['settled'] is not True:","        if evidence['settled'] is not True and observed['identity'] != observed['identity']:",T+'test_clock_two_equal_reads_and_child_exit_never_exclude_late_rpc'),
 ('child exit cannot settle','client_actions.py',"        if evidence['settled'] is not True:","        if evidence['settled'] is not True and process.get('state') != 'reaped':",T+'test_clock_two_equal_reads_and_child_exit_never_exclude_late_rpc'),
 ('live birth identity retains','client_actions.py',"self.birth is None or self.birth(process['pid']) == process['birth']",'False',T+'test_birth_matches_live_child_keeps_unknown_even_with_claimed_settlement'),
 ('terminal reservation never resurrects','client_actions.py',"reservation.update(open=False, uncertain=False, identity=after, acceptance=finish['acceptance'])","reservation.update(open=True, uncertain=False, identity=after, acceptance=finish['acceptance'])",T+'test_terminal_never_resurrects_and_effect_receipts_stay_fixed'),
 ('prepare preserves retained reservations','control.py',"    queue_path = queue_path or config['data'] / 'state.json'\n","    queue_path = queue_path or config['data'] / 'state.json'\n    with engine.connect(write=True) as db:\n        for row in db.execute('SELECT launcher,ref,action_id,body FROM client_reservations'):\n            r = json.loads(row[3]); r['open'] = False\n            db.execute('UPDATE client_reservations SET body=? WHERE launcher=? AND ref=? AND action_id=?',(canonical(r),)+row[:3])\n",T+'test_preparer_retains_unknown_stashed_across_daemon_restart'),
 ('preparar executes autonomous cycle first','control.py',"        reconcile_before_start(engine, lambda: start_configuration(engine))",'        pass',S+'test_every_productive_entry_reconciles_before_its_locked_work'),
 ('preparar --seco never reconciles','control.py',"    elif args.command == 'preparar' and not args.seco:","    elif args.command == 'preparar':",S+'test_every_productive_entry_reconciles_before_its_locked_work'),
 ('launchd wrapper executes autonomous cycle first','mini.py',"        control.reconcile_before_start(self.engine, self.configuration_for_host)",'        pass',S+'test_every_productive_entry_reconciles_before_its_locked_work'),
 ('recuperar executes autonomous cycle first','mini.py',"        control.reconcile_before_lock(manager.engine)",'        pass',S+'test_every_productive_entry_reconciles_before_its_locked_work'),
 ('pre-lock phase executes autonomous cycle','control.py',"        return engine.reconcile_clients(offline=offline)",'        return 0',T+'test_every_death_boundary_in_guard_and_preparer'),
 ('guardia executes autonomous cycle first','control.py',"        reconcile_before_lock(MiniControl(pueue, directory, host=args.host))",'        pass',S+'test_every_productive_entry_reconciles_before_its_locked_work'),
 ('desk one checks retained reservation','control.py',"            if self.client_blocked(group, opening=True):\n                block('reason', group)",'            if False:\n                block(\'reason\', group)',D+'test_desk_one_retains_client_reservation'),
 ('desk guard checks retained reservation','control.py',"                    if self.client_blocked(group, opening=True):\n                        block('reason',group,'resume');continue",'                    if False:\n                        continue',D+'test_desk_guard_retains_pressure_reason_and_unknown'),
 ('retry preserves effect idempotency','client_actions.py',"elif not query and record['response']['status'] in c.REEVALUATED:","elif not query and record['response']['status'] in c.REEVALUATED + ('started',):",T+'test_deferred_same_body_retries_but_effect_response_never_replays'),
 ('deferred can retry only exact body','client_actions.py',"            if record['digest'] != digest(r):",'            if False:',T+'test_deferred_same_body_retries_but_effect_response_never_replays'),
 ('deferred can progress','client_actions.py',"elif not query and record['response']['status'] in c.REEVALUATED:",'elif False:',T+'test_deferred_same_body_retries_but_effect_response_never_replays'),
 ('collection has a batch cap','client_actions.py',"    seconds = .4",'    seconds = 3',T+'test_slow_collector_with_many_open_actions_has_one_batch_budget_and_fair_index'),
 ('manual closure retained','client_actions.py',"                if reasons:","                if reasons - {'manual'}:",'test_client_actions.ActionTests.test_fence_real_reason_and_no_manual_open'),
 ('pressure closure retained','client_actions.py',"                if reasons:","                if reasons - {'pressure'}:",'test_client_actions.CorpusTests.test_expected_states_reservations_reasons_and_rpc'),

 ('settlement does not attribute an external effect','client_actions.py',
  "(self.bound_effect(old) is None or proposal.get('effect') != self.bound_effect(old))",
  "False",T+'test_settled_external_stash_never_attributes_hold_or_allows_followups'),
 ('effect proof is never derived from the daemon','client_actions.py',
  "(self.bound_effect(old) is None or proposal.get('effect') != self.bound_effect(old))",
  "not proposal.get('effect')",T+'test_effect_derived_from_daemon_never_attributes_hold'),
 ('effect proof binds key, phase and child','client_actions.py',
  "proposal.get('effect') != self.bound_effect(old))",
  "(proposal.get('effect') or {}).get('action_id') != self.bound_effect(old)['action_id'])",T+'test_effect_bound_to_key_phase_and_child_identity'),
 ('rejected no-send reservation closes autonomously','client_actions.py',
  "        if item['rejected_unsent']:","        if False:",T+'test_race_rejected_unsent_hold_closes_without_rewriting_receipt'),
 ('rejected closure never rewrites receipt revision','client_actions.py',
  "            reservation.update(open=False, uncertain=False, revision=reservation['revision'] + 1)",
  "            self.actions.save(current)\n            reservation.update(open=False, uncertain=False, revision=reservation['revision'] + 1)",T+'test_race_rejected_unsent_hold_closes_without_rewriting_receipt'),
 ('rejected uncertain phase retains reservation','client_actions.py',
  "and reservation['uncertain'] is False and reservation.get('hold_identity') is None",
  "and reservation.get('hold_identity') is None",T+'test_rejected_hold_cannot_close_with_uncertainty_or_prior_effect'),
 ('rejected phase with prior effect retains reservation','client_actions.py',
  "all(\n                        phase['process'] is None and phase['response']['effects'] is False\n                        and phase['response']['status'] in ('rejected',) + c.REEVALUATED for phase in phases)", "True",T+'test_rejected_hold_cannot_close_with_uncertainty_or_prior_effect'),
 ('desk cause passes real snapshot contract','control.py',
  "                block('reason', group)","                block('client_reserved', group)",D+'test_actual_guard_block_reaches_rack_snapshot_and_panel'),
 ('desk resume cause passes real snapshot contract','control.py',
  "                    if self.client_blocked(group, opening=True):\n                        block('reason',group,'resume');continue","                    if self.client_blocked(group, opening=True):\n                        block('client_reserved',group,'resume');continue",D+'test_actual_guard_block_reaches_rack_snapshot_and_panel'),
 ('only deferred receipts may reevaluate','client_contract.py',
  "REEVALUATED = ('deferred',)", "REEVALUATED = ('deferred', 'unknown', 'rejected', 'held')",T+'test_non_deferred_receipts_never_reevaluate'),
 ('identical retention has no journal write','client_actions.py',
  "        if current != old or reservation != before:","        if True:",T+'test_identical_retention_skips_journal_write_but_advances_index'),
 ('unchanged retention still advances index','client_actions.py',
  "                db.execute('UPDATE client_open SET checked=? WHERE launcher=? AND ref=? AND action_id=?', (sequence,) + key)",
  "                pass",T+'test_identical_retention_skips_journal_write_but_advances_index'),
 ('finish deferred keeps its original digest','client_actions.py',
  "                if prior and prior['digest'] != digest(finish):","                if False:",T+'test_autonomous_finish_preserves_deferred_request_digest'),
 ('retention reason reaches the group view','control.py',
  "            reason = (self.client_block_reason(g) if self.client_blocked(g) else",
  "            reason = (self.client_reservation_reason if self.client_blocked(g) else",
  'rack:test_reorder.ReorderChannelTests.test_retention_reasons_survive_public_sanitizers'),
 ('retention reason reaches sanitized capabilities','../rack/host_action.py',
  "        result['capabilities']['reason_reorder'] = caps['reason_reorder']","        pass",
  'rack:test_reorder.ReorderChannelTests.test_retention_reasons_survive_public_sanitizers'),
 ('guard never cedes control.lock to a queued launcher','control.py',
  "    def guard(self, readings, proc=Path('/proc')):\n        if self.host == 'mini':",
  "    def guard(self, readings, proc=Path('/proc')):\n        for _, held in _CONTROL_HANDLES.get():\n            fcntl.flock(held, fcntl.LOCK_UN)\n        time.sleep(.05)\n        for _, held in _CONTROL_HANDLES.get():\n            fcntl.flock(held, fcntl.LOCK_EX)\n        if self.host == 'mini':",
  T+'test_queued_launcher_never_preempts_protective_guard'),
 ('empty index launches no collector','client_actions.py',"            if not copies:\n                return 0","            if False:\n                return 0",T+'test_queued_launcher_never_preempts_protective_guard'),
 ('reconciler never releases a caller lock','control.py',
  "            return client_module().Reconciler(self, adapter=getattr(self, 'client_adapter', None),",
  "            for _, held in _CONTROL_HANDLES.get():\n                fcntl.flock(held, fcntl.LOCK_UN)\n            return client_module().Reconciler(self, adapter=getattr(self, 'client_adapter', None),",
  T+'test_queued_launcher_never_preempts_protective_guard'),
 ('reconciler failure never suppresses the guard','control.py',
  "    except Exception:\n        return 0  # Collection can never suppress the caller's own duties.",
  "    except ADMISSION_ERRORS:\n        return 0",S+'test_whole_guard_tick_runs_protective_guard_despite_reconciler_failure_or_contention'),
 ('reconciler never waits for a busy control.lock','client_actions.py',
  "            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)","            fcntl.flock(handle, fcntl.LOCK_EX)",
  S+'test_whole_guard_tick_runs_protective_guard_despite_reconciler_failure_or_contention'),
 ('start enters control.lock with a deadline','mini.py',
  "        with control.locked(self.directory, create=False, seconds=START_LOCK_SECONDS):",
  "        with control.locked(self.directory, create=False):",S+'test_contended_start_rejects_within_deadline_with_diagnostic'),
 ('start diagnostic never waits for control.lock','mini.py',
  "        target = self.directory / 'arranque.log'\n",
  "        control.wait_rack_lock(open(self.directory / 'control.lock', 'rb'), 5)\n        target = self.directory / 'arranque.log'\n",
  S+'test_contended_start_rejects_within_deadline_with_diagnostic'),
 ('start diagnostic lock has a deadline','mini.py',
  "                if not control.wait_rack_lock(stream, deadline - time.monotonic()):",
  "                if control.fcntl.flock(stream, control.fcntl.LOCK_EX):",
  S+'test_contended_start_rejects_within_deadline_with_diagnostic'),
]


def main():
    nominal=run(SOURCE,'test_client_reconcile')
    if nominal.returncode:raise AssertionError(nominal.stdout+nominal.stderr)
    for number,(rule,name,old,new,test) in enumerate(CASES,1):
        with tempfile.TemporaryDirectory(prefix='cc2-sol-mutant-',dir='/tmp') as folder:
            root=Path(folder)/'pueue-desk'
            shutil.copytree(SOURCE,root,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            shutil.copytree(SOURCE.parent/'rack',root.parent/'rack',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            path=root/name;source=path.read_text()
            if source.count(old)!=1:raise AssertionError('Nonunique anchor: '+rule)
            path.write_text(source.replace(old,new))
            result=run(root.parent/'rack',test[len('rack:'):]) if test.startswith('rack:') else run(root,test)
            if result.returncode!=1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:
                raise AssertionError(rule+' did not fail by assertion:\n'+result.stdout+result.stderr)
            print(str(number)+' '+rule+': assertion',flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' mutants; nominal green; '+sys.version.split()[0],flush=True)


if __name__=='__main__':main()
