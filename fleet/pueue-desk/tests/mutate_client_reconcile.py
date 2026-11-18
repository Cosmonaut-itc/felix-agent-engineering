"""CC2 mutations: nominal green; each mutant must die by unittest assertion."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
from mutate_client_actions import run, SOURCE

T='test_client_reconcile.ReconcileTests.'
D='test_client_reconcile.DeskReservationsTests.'
CASES=[
 ('online reorder cannot adopt client Locked','control.py',
  "                        if self.client_blocked(g):", '                        if False:',
  T+'test_client_reservation_prevents_online_and_offline_locked_adoption'),
 ('offline reorder cannot adopt client Locked','control.py',
  "                    if ident in protected or self.client_blocked(task['group']):", '                    if False:',
  T+'test_client_reservation_prevents_online_and_offline_locked_adoption'),
 ('start revalidates authority after unlocked collection','mini.py',
  "            if self.transaction(self.state()) != value:", '            if False:',
  'test_client_reconcile.StartAuthorizationTests.test_start_rechecks_handoff_after_collection_window'),
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
 ('collect outside host lock','client_actions.py',"            proposals = self.bounded_gather(copies, offline)","            with self.lock():\n                proposals = self.bounded_gather(copies, offline)",T+'test_synthetic_settlement_attributes_hold_then_durable_result_finishes'),
 ('clock cannot close','client_actions.py',"            if proposal['kind'] == 'not_sent':","            if self.engine.clock() > 1000000 or proposal['kind'] == 'not_sent':",T+'test_clock_two_equal_reads_and_child_exit_never_exclude_late_rpc'),
 ('equal reads cannot settle','client_actions.py',"        if evidence['settled'] is not True:","        if evidence['settled'] is not True and observed['identity'] != observed['identity']:",T+'test_clock_two_equal_reads_and_child_exit_never_exclude_late_rpc'),
 ('child exit cannot settle','client_actions.py',"        if evidence['settled'] is not True:","        if evidence['settled'] is not True and process.get('state') != 'reaped':",T+'test_clock_two_equal_reads_and_child_exit_never_exclude_late_rpc'),
 ('live birth identity retains','client_actions.py',"self.birth is None or self.birth(process['pid']) == process['birth']",'False',T+'test_birth_matches_live_child_keeps_unknown_even_with_claimed_settlement'),
 ('terminal reservation never resurrects','client_actions.py',"reservation.update(open=False, uncertain=False, identity=after, acceptance=finish['acceptance'])","reservation.update(open=True, uncertain=False, identity=after, acceptance=finish['acceptance'])",T+'test_terminal_never_resurrects_and_effect_receipts_stay_fixed'),
 ('prepare preserves retained reservations','control.py',"        engine.reconcile_clients(offline=queue_path)","        engine.reconcile_clients(offline=queue_path)\n        with engine.connect(write=True) as db:\n            for row in db.execute('SELECT launcher,ref,action_id,body FROM client_reservations'):\n                r = json.loads(row[3]); r['open'] = False\n                db.execute('UPDATE client_reservations SET body=? WHERE launcher=? AND ref=? AND action_id=?',(canonical(r),)+row[:3])",T+'test_preparer_retains_unknown_stashed_across_daemon_restart'),
 ('prepare executes autonomous cycle','control.py',"        engine.reconcile_clients(offline=queue_path)",'        pass',T+'test_every_death_boundary_in_guard_and_preparer'),
 ('guard executes autonomous cycle','control.py',"        self.reconcile_clients()",'        pass',T+'test_every_death_boundary_in_guard_and_preparer'),
 ('desk one checks retained reservation','control.py',"            if self.client_blocked(group, opening=True):\n                block('reason', group)",'            if False:\n                block(\'reason\', group)',D+'test_desk_one_retains_client_reservation'),
 ('desk guard checks retained reservation','control.py',"                    if self.client_blocked(group, opening=True):\n                        block('reason',group,'resume');continue",'                    if False:\n                        continue',D+'test_desk_guard_retains_pressure_reason_and_unknown'),
 ('retry preserves effect idempotency','client_actions.py',"elif not query and record['response']['status'] in c.REEVALUATED:","elif not query and record['response']['status'] in c.REEVALUATED + ('started',):",T+'test_deferred_same_body_retries_but_effect_response_never_replays'),
 ('deferred can retry only exact body','client_actions.py',"            if record['digest'] != digest(r):",'            if False:',T+'test_deferred_same_body_retries_but_effect_response_never_replays'),
 ('deferred can progress','client_actions.py',"elif not query and record['response']['status'] in c.REEVALUATED:",'elif False:',T+'test_deferred_same_body_retries_but_effect_response_never_replays'),
 ('collection has a batch cap','client_actions.py',"    seconds = .4",'    seconds = 3',T+'test_slow_collector_with_many_open_actions_has_one_batch_budget_and_fair_index'),
 ('manual closure retained','client_actions.py',"                if reasons:","                if reasons - {'manual'}:",'test_client_actions.ActionTests.test_fence_real_reason_and_no_manual_open'),
 ('pressure closure retained','client_actions.py',"                if reasons:","                if reasons - {'pressure'}:",'test_client_actions.CorpusTests.test_expected_states_reservations_reasons_and_rpc'),

 ('settlement does not attribute an external effect','client_actions.py',
  "        if type(proof) is not dict or set(proof) != {'launcher', 'ref', 'action_id', 'phase', 'pid', 'birth'}:",
  "        if proof is None:\n            return True\n        if type(proof) is not dict or set(proof) != {'launcher', 'ref', 'action_id', 'phase', 'pid', 'birth'}:",T+'test_settled_external_stash_never_attributes_hold_or_allows_followups'),
 ('rejected no-send reservation closes autonomously','client_actions.py',
  "        if item['rejected_unsent']:","        if False:",T+'test_race_rejected_unsent_hold_closes_without_rewriting_receipt'),
 ('rejected closure never rewrites receipt revision','client_actions.py',
  "                reservation.update(open=False, uncertain=False, revision=reservation['revision'] + 1)",
  "                self.actions.save(current)\n                reservation.update(open=False, uncertain=False, revision=reservation['revision'] + 1)",T+'test_race_rejected_unsent_hold_closes_without_rewriting_receipt'),
 ('rejected uncertain phase retains reservation','client_actions.py',
  "and reservation['uncertain'] is False and reservation.get('hold_identity') is None",
  "and reservation.get('hold_identity') is None",T+'test_rejected_hold_cannot_close_with_uncertainty_or_prior_effect'),
 ('rejected phase with prior effect retains reservation','client_actions.py',
  "all(\n                        phase['process'] is None and phase['response']['effects'] is False\n                        and phase['response']['status'] in ('rejected',) + c.REEVALUATED for phase in phases)", "True",T+'test_rejected_hold_cannot_close_with_uncertainty_or_prior_effect'),
 ('desk cause passes real snapshot contract','control.py',
  "                block('reason', group)","                block('client_reserved', group)",D+'test_actual_guard_block_reaches_rack_snapshot_and_panel'),
 ('desk resume cause passes real snapshot contract','control.py',
  "                    if self.client_blocked(group, opening=True):\n                        block('reason',group,'resume');continue","                    if self.client_blocked(group, opening=True):\n                        block('client_reserved',group,'resume');continue",D+'test_actual_guard_block_reaches_rack_snapshot_and_panel'),
 ('start entry contention has a deadline','control.py',
  "                wait_control_lock(handle, seconds)",
  "                fcntl.flock(handle, fcntl.LOCK_EX)",
  'test_client_reconcile_r1.StartTests.test_start_rejects_with_real_contention_and_keeps_sanitized_log'),
 ('collection never borrows the caller lock','control.py',
  "                handles[-1].client_pending[self.directory.resolve()] = (self, offline)\n                return 0",
  "                fcntl.flock(handles[-1], fcntl.LOCK_UN)\n                try:\n                    return client_module().Reconciler(self, adapter=getattr(self, 'client_adapter', None), birth=priority_helper().birth).run(offline=offline)\n                finally:\n                    wait_control_lock(handles[-1], CLIENT_LOCK_SECONDS)",
  'test_client_reconcile_r1.R1Tests.test_closed_journal_guard_keeps_lock_with_queued_launcher'),
 ('only deferred receipts may reevaluate','client_contract.py',
  "REEVALUATED = ('deferred',)", "REEVALUATED = ('deferred', 'unknown', 'rejected', 'held')",T+'test_non_deferred_receipts_never_reevaluate'),
 ('identical retention has no journal write','client_actions.py',
  "            if current != old or reservation != before:","            if True:",T+'test_identical_retention_skips_journal_write_but_advances_index'),
 ('unchanged retention still advances index','client_actions.py',
  "                db.execute('UPDATE client_open SET checked=? WHERE launcher=? AND ref=? AND action_id=?', (sequence,) + key)",
  "                pass",T+'test_identical_retention_skips_journal_write_but_advances_index'),
 ('finish deferred keeps its original digest','client_actions.py',
  "                    if prior and prior['digest'] != digest(finish):","                    if False:",T+'test_autonomous_finish_preserves_deferred_request_digest'),
 ('retention reason reaches the group view','control.py',
  "            reason = (self.client_block_reason(g) if self.client_blocked(g) else",
  "            reason = (self.client_reservation_reason if self.client_blocked(g) else",
  'rack:test_reorder.ReorderChannelTests.test_retention_reasons_survive_public_sanitizers'),
 ('retention reason reaches sanitized capabilities','../rack/host_action.py',
  "        result['capabilities']['reason_reorder'] = caps['reason_reorder']","        pass",
  'rack:test_reorder.ReorderChannelTests.test_retention_reasons_survive_public_sanitizers'),
 ('daemon-derived boolean cannot prove effect','client_actions.py',
  "        if type(proof) is not dict or set(proof) != {'launcher', 'ref', 'action_id', 'phase', 'pid', 'birth'}:",
  "        if proof is True:\n            return True\n        if type(proof) is not dict or set(proof) != {'launcher', 'ref', 'action_id', 'phase', 'pid', 'birth'}:",
  'test_client_reconcile_r1.R1Tests.test_daemon_derived_effect_never_attributes'),
 ('effect bound to complete phase key','client_actions.py',
  "all(proof[k] == r[k] for k in ('launcher', 'ref', 'action_id', 'phase'))", "True",
  'test_client_reconcile_r1.R1Tests.test_effect_key_and_process_must_match'),
 ('effect bound to child PID','client_actions.py',
  "and proof['pid'] == process.get('pid')", "and True",
  'test_client_reconcile_r1.R1Tests.test_effect_key_and_process_must_match'),
 ('effect bound to child birth','client_actions.py',
  "and proof['birth'] == process.get('birth')", "and True",
  'test_client_reconcile_r1.R1Tests.test_effect_key_and_process_must_match'),
 ('effect rechecked by authority under lock','client_actions.py',
  "                    if not self.effect_matches(proposal.get('effect_applied'), current):",
  "                    if False:",
  'test_client_reconcile_r1.R1Tests.test_apply_checks_effect_binding_under_control_lock'),
 ('launchd wrapper executes independent cycle','mini.py',
  "        self.engine.reconcile_clients(offline=config['data'] / 'state.json')", "        pass",
  'test_client_reconcile_r1.StartTests.test_launchd_wrapper_collects_before_preparing_and_exec'),
 ('start diagnostic does not wait on control lock','mini.py',
  "        with (self.directory / 'arranque.log.lock').open('a') as handle:",
  "        with control.locked(self.directory, create=False) as handle:",
  'test_client_reconcile_r1.StartTests.test_start_rejects_with_real_contention_and_keeps_sanitized_log'),

 ('diagnostic writer contention has a deadline','mini.py',
  "                control.wait_control_lock(handle, control.CLIENT_LOCK_SECONDS)",
  "                __import__('fcntl').flock(handle, __import__('fcntl').LOCK_EX)",
  'test_client_reconcile_r1.StartTests.test_stalled_diagnostic_writer_cannot_delay_start_rejection'),

 ('empty open index skips generation and collector','client_actions.py',
  "            if not keys:\n                return []", "            if False:\n                return []",
  'test_client_reconcile_r1.R1Tests.test_closed_journal_guard_keeps_lock_with_queued_launcher'),

]


def main():
    nominal=run(SOURCE,'test_client_reconcile', timeout=120)
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
