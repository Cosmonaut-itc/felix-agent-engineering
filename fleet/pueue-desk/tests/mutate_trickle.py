"""SEN-6417: one assertion-killed mutant per trickle-admission and panel rule. No real binaries.

TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/mutate_trickle.py
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

FLEET = Path(__file__).resolve().parents[2]
D, R = 'pueue-desk', 'rack'
T, TI, TP = 'test_trickle.', 'test_trickle_install.TrickleInstallTests.', 'test_trickle_panel.'
CASES = [
    # (name, file under fleet/, original, replacement, tests directory, test)
    # The chooser and the final gate each check ownership; both must fall for the rule to break.
    ('only-pressure-owned', D + '/control.py',
     ("            if reasons != {'pressure'}:", "        if set(state['reasons'].get(g, [])) != {'pressure'} or os.path.lexists(self.pause_fence(g)):"),
     ("            if 'pressure' not in reasons:", "        if False:"),
     D, T + 'TrickleRegressionTests.test_rapidas_manual_and_ambiguous_never_trickle'),
    ('rapidas-excluded', D + '/control.py', "TRICKLE_EXCLUDED = ('rapidas',)", 'TRICKLE_EXCLUDED = ()',
     D, T + 'TrickleRegressionTests.test_rapidas_manual_and_ambiguous_never_trickle'),
    ('quota-full-skipped', D + '/control.py',
     "            if sum(n in ('Running', 'Paused', 'Locked') for n in names.values()) >= groups[g]['parallel_tasks']:",
     '            if False:', D, T + 'TrickleRedTests.test_quota_full_group_is_skipped_and_free_group_admits_once'),
    ('fifo-order', D + '/control.py', 'return [row[3] for row in sorted(found, key=lambda row: row[:3])]', 'return [row[3] for row in sorted(found, key=lambda row: row[1:3])]',
     D, T + 'TrickleRedTests.test_mid_band_admits_one_fifo_task_and_keeps_pause_and_pressure'),
    ('dependencies-done', D + '/control.py', "        return all(done(dep) for dep in item['dependencies'])", '        return True',
     D, T + 'TrickleRedTests.test_pending_dependency_is_skipped_and_satisfied_dependency_admits'),
    ('start-keeps-pause', D + '/control.py', "            self.pueue.call('start', candidate['id'])", "            self.pueue.call('start', '-g', candidate['grupo'])",
     D, T + 'TrickleRedTests.test_mid_band_admits_one_fifo_task_and_keeps_pause_and_pressure'),
    ('global-gap', D + '/control.py', "        left = (attempt['completed_ns'] if attempt['boot_id'] == boot else 0) + ADMISSION_GAP_NS - now",
     '        left = 0', D, T + 'TrickleRedTests.test_global_cap_survives_new_instance_and_counts_from_completion'),
    ('gap-from-completion', D + '/control.py', "        left = (attempt['completed_ns'] if", "        left = (attempt['reserved_ns'] if",
     D, T + 'TrickleRedTests.test_global_cap_survives_new_instance_and_counts_from_completion'),
    ('unknown-never-released', D + '/control.py',
     ("                self.finish(body, 'unknown')  # a crashed pass never retries without evidence",
      "            return report.limit('admission_unknown')"),
     ('                pass', '            return boot, now'),
     D, T + 'TrickleRedTests.test_crash_after_reservation_is_never_retried_without_evidence'),
    ('failed-start-unknown', D + '/control.py', "            self.finish(body, 'unknown')\n            return report.hold(None, 'one', 'start_failed')",
     "            self.finish(body, 'not_sent', reserved)\n            return report.hold(None, 'one', 'start_failed')",
     D, T + 'TrickleRedTests.test_failed_start_stays_unknown_and_blocks_until_evidence'),
    ('evidence-creation', D + '/control.py', "if item is None or item.get('group') != attempt['grupo'] or item.get('created_at') != attempt['created_at']:",
     "if item is None or item.get('group') != attempt['grupo']:", D, T + 'TrickleRedTests.test_reused_id_with_other_creation_is_not_evidence'),
    ('evidence-start-window', D + '/control.py',
     "            return pueue_instant(detail['start']) >= pueue_instant(attempt['utc']) - ADMISSION_START_SLACK_NS", '            return True',
     D, T + 'TrickleRedTests.test_failed_start_stays_unknown_and_blocks_until_evidence'),
    ('clock-backwards', D + '/control.py',
     ("        if attempt['boot_id'] == boot and now < attempt['reserved_ns']:\n            return report.hold(None, 'one', 'clock')",
      "        if attempt['boot_id'] == boot and now < attempt['completed_ns']:\n            return report.hold(None, 'one', 'clock')"),
     ('        pass', '        pass'),
     D, T + 'TrickleRedTests.test_clock_rules_backwards_reboot_and_civil_time'),
    ('fresh-mantener', D + '/control.py', "                    if decide('desk', current, PRESSURE_THRESHOLDS) != 'mantener':", '                    if False:',
     D, T + 'TrickleRedTests.test_every_hold_cause_is_logged_for_resume_and_for_one'),
    ('gate-receipts', D + '/control.py', "            if set(pending) & {'applying', 'partial', 'unknown'}:", '            if False:',
     D, T + 'TrickleRedTests.test_every_hold_cause_is_logged_for_resume_and_for_one'),
    ('high-says-so', D + '/control.py', "            report.hold(None, 'one', 'pressure_high')", '            pass',
     D, T + 'TrickleRedTests.test_high_pressure_admits_nothing_and_says_so_when_queue_and_slot_wait'),
    ('summary-every-pass', D + '/control.py', '            raise\n        report.write()', '            raise',
     D, T + 'TrickleRedTests.test_high_pressure_admits_nothing_and_says_so_when_queue_and_slot_wait'),
    ('no-secret-in-log', D + '/control.py', "        report.fields.update(grupo=candidate['grupo'], id=candidate['id'])",
     "        report.fields.update(grupo=candidate['grupo'], id=candidate['id'], tarea=self.snapshot['tasks'][str(candidate['id'])])",
     D, T + 'TrickleRedTests.test_nothing_secret_reaches_the_log'),
    ('mini-unchanged', D + '/control.py', "TickReport(self.directory, self.host == 'desk')", 'TickReport(self.directory, True)',
     D, T + 'TrickleRegressionTests.test_mini_mid_band_is_unchanged'),
    ('resolve-needs-maintenance', D + '/control.py', "        if self.host != 'desk' or not os.path.lexists(self.directory / 'maintenance'):",
     "        if self.host != 'desk':", D, T + 'TrickleRecoveryTests.test_needs_maintenance_and_evidence_or_explicit_no_effect'),
    ('resolve-explicit-no-effect', D + '/control.py', "        elif boot != attempt['boot_id'] or no_effect:", '        elif True:',
     D, T + 'TrickleRecoveryTests.test_needs_maintenance_and_evidence_or_explicit_no_effect'),
    ('install-keeps-permit', D + '/install-desk.py', "'INSERT OR IGNORE INTO guard_admission VALUES (1,?)'", "'INSERT OR REPLACE INTO guard_admission VALUES (1,?)'",
     D, TI + 'test_install_creates_empty_permit_and_reinstall_keeps_it'),
    ('install-dated-backup', D + '/install-desk.py', '            dated_backup(library, target.read_bytes())', '            pass',
     D, TI + 'test_dated_backup_keeps_the_immediately_previous_controller'),
    ('install-dry-plan', D + '/install-desk.py', '        engine.install_admission_action = admission_plan(engine, control)', '        pass',
     D, TI + 'test_dry_run_reports_the_permit_plan_without_writing'),
    ('install-validates-permit', D + '/install-desk.py', "    attempt = engine.admission()['last_attempt']  # an unreadable permit stops the installation",
     '    attempt = None',
     D, TI + 'test_corrupt_permit_stops_before_effects_and_in_dry_run'),
    ('panel-memory-first', R + '/index.html', 'if(lim.memory!=null && (p.memory<lim.memory || lim.cpu==null))', 'if(lim.memory!=null && lim.cpu==null)',
     R, TP + 'GuardCellTests.test_high_pressure_says_held_by_the_binding_metric_and_how_it_trickles'),
    ('panel-io-before-cpu', R + '/index.html', 'if(lim.io!=null && (pausing ? p.io>lim.io : p.io>=lim.io))', 'if(false)',
     R, TP + 'GuardCellTests.test_mid_band_says_trickle_reopen_threshold_and_last_admission'),
    ('panel-last-own-group', R + '/index.html', 'ago=last?.grupo===n ?', 'ago=last ?',
     R, TP + 'GuardCellTests.test_mid_band_says_trickle_reopen_threshold_and_last_admission'),
    ('panel-gate-cause', R + '/index.html', "  if(blocked) return ['Retenido',esc(CAUSE_TEXT[blocked.causa]),'warn'];\n", '',
     R, TP + 'GuardCellTests.test_blocking_gate_states_its_cause_instead_of_pressure'),
    ('panel-stale-unknown', R + '/index.html', 'NOW-Date.parse(g.at)/1000<=GUARD_FRESH && ', '',
     R, TP + 'GuardCellTests.test_without_guard_data_the_state_is_unknown_never_free'),
    ('panel-unknown-not-free', R + '/index.html', "  if(!g) return ['Retenido por presión','estado de la guardia desconocido','warn'];",
     "  if(!g) return ['Libre','','warn'];", R, TP + 'GuardCellTests.test_without_guard_data_the_state_is_unknown_never_free'),
    ('panel-full-silent', R + '/index.html', "g.status==='Paused' && free ? r.length+1", "g.status==='Paused' ? r.length+1",
     R, TP + 'GuardCellRegressionTests.test_full_group_says_nothing_about_the_pause'),
    ('serve-whitelist', R + '/serve.py', "    data['guard'] = valid_guard(data.get('guard'))", "    data['guard'] = data.get('guard')",
     R, TP + 'GuardServeTests.test_invalid_core_drops_guard_and_invalid_parts_drop_alone'),
    ('serve-hold-causes', R + '/serve.py', " and b['causa'] in GUARD_CAUSES for b in result['bloqueos']", " for b in result['bloqueos']",
     R, TP + 'GuardServeTests.test_invalid_core_drops_guard_and_invalid_parts_drop_alone'),
    ('snapshot-same-tick', R + '/snapshot.py', '    return decision, summaries.get(tick) if isinstance(tick, str) else None',
     '    return decision, (list(summaries.values()) or [None])[-1]',
     R, TP + 'GuardSnapshotTests.test_result_of_another_pass_is_never_attributed_to_the_last_decision'),
    ('snapshot-rapidas-desk', R + '/snapshot.py', "if isinstance(k, ast.Constant) and k.value == 'desk':", "if isinstance(k, ast.Constant) and k.value == 'mini':",
     R, TP + 'GuardSnapshotTests.test_mid_band_publishes_pressure_installed_thresholds_same_tick_result_and_last_admission'),
    ('snapshot-last-admission', R + '/snapshot.py',
     """        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='guard_admission'").fetchone():""",
     '        if True:', R, TP + 'GuardSnapshotTests.test_mid_band_publishes_pressure_installed_thresholds_same_tick_result_and_last_admission'),
    ('snapshot-part-errors', R + '/snapshot.py', "pressure=part('guard_pressure', lambda: desk_pressure(proc, now)),", 'pressure=desk_pressure(proc, now),',
     R, TP + 'GuardSnapshotTests.test_unreadable_pressure_is_null_with_its_error_and_keeps_the_rest'),
]


def run(root, directory, test):
    return subprocess.run([sys.executable, '-B', '-m', 'unittest', test, '-v'], cwd=str(root / directory / 'tests'), text=True,
                          capture_output=True, timeout=60, env=dict(os.environ, TMPDIR='/tmp', PYTHONDONTWRITEBYTECODE='1'))


def copy(folder):
    root = Path(folder) / 'fleet'
    for part in (D, R):
        shutil.copytree(str(FLEET / part), str(root / part), ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    return root


def main():
    with tempfile.TemporaryDirectory(prefix='sen-6417-mut-', dir='/tmp') as folder:
        root = copy(folder)
        for directory in sorted({c[4] for c in CASES}):
            tests = sorted({c[5] for c in CASES if c[4] == directory})
            nominal = subprocess.run([sys.executable, '-B', '-m', 'unittest', *tests], cwd=str(root / directory / 'tests'), text=True,
                                     capture_output=True, timeout=300, env=dict(os.environ, TMPDIR='/tmp', PYTHONDONTWRITEBYTECODE='1'))
            assert nominal.returncode == 0, 'nominal run is not green:\n' + nominal.stderr
            print('nominal ' + directory + ': ' + nominal.stderr.strip().splitlines()[-1], flush=True)
    for name, filename, before, after, directory, test in CASES:
        with tempfile.TemporaryDirectory(prefix='sen-6417-mut-', dir='/tmp') as folder:
            root = copy(folder)
            path = root / filename
            source = path.read_text()
            for original, replacement in zip(*((before, after) if isinstance(before, tuple) else ((before,), (after,)))):
                assert source.count(original) == 1, (name, 'nonunique anchor', source.count(original))
                source = source.replace(original, replacement)
            path.write_text(source)
            result = run(root, directory, test)
            assert result.returncode == 1 and 'FAIL:' in result.stderr and 'ERROR:' not in result.stderr, \
                name + ' survived or failed outside assertion:\n' + result.stderr
            print(name + ': caught by assertion · ' + next(l for l in result.stderr.splitlines() if 'Error:' in l).strip()[:140], flush=True)
    print(str(len(CASES)) + '/' + str(len(CASES)) + ' SEN-6417 mutations caught; tree untouched')


if __name__ == '__main__':
    main()
