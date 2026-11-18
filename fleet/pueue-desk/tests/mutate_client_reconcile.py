"""CC2 safety mutants: every rejection must be an assertion, not ERROR/timeout."""
import sys
import mutate_client_actions as base

R = 'test_client_reconcile.ReconcileTests.'
SAME = "        return revisions and digests and generation and list(now['phases']) == list(before['phases'])"
GATE = "            if dead is None or dead == now['generation']:"
CASES = [
    ('compare journal revision', 'client_actions.py', SAME, SAME.replace('revisions and ', ''),
     R+'test_three_versions_compared_after_collection'),
    ('compare authority generation', 'client_actions.py', SAME, SAME.replace(' and generation', ''),
     R+'test_three_versions_compared_after_collection'),
    ('compare request digest', 'client_actions.py', SAME, SAME.replace(' digests and', ''),
     R+'test_three_versions_compared_after_collection'),
    ('collect outside control.lock', 'client_actions.py',
     '        outcome = self.decide(now, R, P, evidence)',
     "        outcome = self.decide(now, R, P, self.collect(dict(now, authority=snap['authority'])))",
     R+'test_collection_never_holds_control_lock'),
    ('never close by clock', 'client_actions.py', GATE,
     "            if (dead is None or dead == now['generation']) and self.engine.clock() < 10**8:",
     R+'test_child_frontiers_never_close_by_exit_clock_or_equal_reads'),
    ('never close by two equal reads', 'client_actions.py',
     ("        excluded = R['uncertain'] and proc.get('dead_generation') not in (None, snap['generation'])", GATE),
     ("        excluded = R['uncertain']", "            if evidence['observation'] is None:"),
     R+'test_child_frontiers_never_close_by_exit_clock_or_equal_reads'),
    ('child exit never excludes', 'client_actions.py', GATE,
     "            if dead is None and evidence['child'] != 'dead':",
     R+'test_child_frontiers_never_close_by_exit_clock_or_equal_reads'),
    ('exclusion needs a new daemon generation', 'client_actions.py', GATE, '            if dead is None:',
     R+'test_child_frontiers_never_close_by_exit_clock_or_equal_reads'),
    ('no terminal resurrection', 'client_actions.py',
     ("for v in reservations(self.engine) if v['open'])",
      "        if now is None or not now['reservation']['open'] or not self.same(snap, now):"),
     ("for v in reservations(self.engine))", "        if now is None or not self.same(snap, now):"),
     R+'test_known_reservation_closes_only_with_published_result'),
    ('no opening over manual/pressure without effect proof', 'client_actions.py',
     "                if proc.get('state') != 'excluded':\n                    proc['state'] = 'excluded'",
     "                R['uncertain'] = False\n                if proc.get('state') != 'excluded':\n                    proc['state'] = 'excluded'",
     R+'test_exclusion_without_effect_proof_keeps_unknown_and_closed_groups'),
    ('preparer keeps a dispatched reservation', 'client_actions.py',
     '            if proc is None:', "            if proc is None or 'pid' in proc:",
     'test_client_reconcile.PreparerTests.test_preparer_and_restart_keep_reservations_unknown_and_stashed'),
    ('preparer never drops reservations', 'control.py',
     "    boundary('intent')\n    atomic_json(queue_path, image)",
     "    boundary('intent')\n    with engine.connect(write=True) as db:\n        db.execute('DROP TABLE IF EXISTS client_reservations')\n    atomic_json(queue_path, image)",
     'test_client_reconcile.PreparerTests.test_preparer_and_restart_keep_reservations_unknown_and_stashed'),
    ('wrapper start reconciles', 'mini.py',
     '        control.reconcile_clients(self.engine.pueue, self.directory, engine=self.engine)\n', '',
     'test_client_reconcile.PreparerTests.test_preparer_and_restart_keep_reservations_unknown_and_stashed'),
    ('desk _one consults reservations', 'control.py',
     '        if self.client_blocked(candidate[2], opening=True):', '        if False:',
     'test_client_reconcile.DeskReservationTests.test_desk_one_and_guard_respect_client_reservation'),
    ('desk guard consults reservations', 'control.py',
     "                    if self.client_blocked(group,opening=True):\n                        block('reason',group,'resume')",
     "                    if False:\n                        block('reason',group,'resume')",
     'test_client_reconcile.DeskReservationTests.test_desk_one_and_guard_respect_client_reservation'),
    ('effectful receipt never re-executed', 'client_contract.py',
     'REEVALUATED = ("deferred",)', 'REEVALUATED = ("deferred", "held")',
     R+'test_unsent_stash_closes_from_journal_and_deferred_retry_is_one_rpc'),
    ('unknown receipt never re-executed', 'client_contract.py',
     'REEVALUATED = ("deferred",)', 'REEVALUATED = ("deferred", "unknown")',
     R+'test_deferred_status_query_does_not_reevaluate_and_effects_stay_fixed'),
    ('deferred is re-evaluated', 'client_actions.py',
     "        if record and (query or record['response']['status'] not in c.REEVALUATED or record['digest'] != digest(r)):",
     '        if record:',
     R+'test_unsent_stash_closes_from_journal_and_deferred_retry_is_one_rpc'),
    ('revision monotonic on re-evaluation', 'client_actions.py',
     "revision=prior['revision'] if prior else 0,", 'revision=0,',
     R+'test_unsent_stash_closes_from_journal_and_deferred_retry_is_one_rpc'),
    ('guard tick reconciles after lock release', 'control.py',
     '                guard(pueue, Path("/proc"), directory, host=args.host)',
     '                guard(pueue, Path("/proc"), directory, host=args.host); reconcile_clients(pueue, directory)',
     R+'test_guard_main_reconciles_after_releasing_the_lock'),
    ('visible retained reason', 'control.py',
     '        reason = client_module().retained(self, group)', '        reason = None',
     R+'test_installed_adapters_retain_with_reason_and_no_rpc'),
    ('tick time budget', 'client_actions.py',
     '                if self.clock() >= deadline:', '                if False:',
     R+'test_tick_budget_and_limit_with_many_open_actions'),
    ('tick action limit', 'client_actions.py',
     'keys = (keys[start:] + keys[:start])[:self.LIMIT]', 'keys = keys[start:] + keys[:start]',
     R+'test_tick_budget_and_limit_with_many_open_actions'),
    ('unsent closes only from the journal', 'client_actions.py',
     "                P['response'] = self.actions.reply(r, 'deferred', 'host_action', held=R['open'])\n                self.actions.save(P, R)",
     "                P['response'] = self.actions.reply(r, 'deferred', 'host_action', held=R['open'])",
     R+'test_unsent_stash_closes_from_journal_and_deferred_retry_is_one_rpc'),
]


def main():
    nominal = base.run(base.SOURCE, 'test_client_reconcile')
    if nominal.returncode:
        raise AssertionError(nominal.stdout + nominal.stderr)
    base.CASES[:] = CASES
    base.main()


if __name__ == '__main__':
    main()
