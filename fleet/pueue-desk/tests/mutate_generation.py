"""Directed DESK-GEN mutants. Nominal green; every mutant must die by assertion."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
D = 'DeskGenerationTests.'
I = 'GenerationInstallTests.'
# file, unique anchor, replacement, directed test
CASES = [
    ('control.py', "seed = ['linux-proc-v1', identity['boot'], identity['pid'], identity['ticks'], identity['file']]",
     "seed = ['linux-proc-v1', identity['boot'], identity['pid'], identity['ticks'], identity['file'], mini_generation(pid_file)]",
     D+'test_wall_clock_jumps_keep_identity'),
    ('control.py', "identity['boot'], identity['pid']", "identity['pid']", D+'test_boot_ticks_and_pid_each_change_identity'),
    ('control.py', "identity['pid'], identity['ticks']", "identity['ticks']", D+'test_pid_alone_changes_identity_with_fixed_file_signature'),
    ('control.py', "identity['ticks'], identity['file']", "identity['file']", D+'test_boot_ticks_and_pid_each_change_identity'),
]
for field in ('st_dev','st_ino','st_mtime_ns','st_ctime_ns','st_size'):
    CASES.append(('control.py', 'info.'+field, '0', D+'test_each_pid_file_signal_changes_identity'))
CASES += [
    ('control.py', "ticks=int(fields[19])", "ticks=int(fields[18])", D+'test_boot_ticks_and_pid_each_change_identity'),
    ('control.py', "head != str(pid) + ' (pueued'", 'False', D+'test_bad_proc_fails_closed'),
    ('control.py', "fields[0] not in ('R', 'S', 'D', 'T', 't', 'W', 'K', 'P', 'I')", 'False', D+'test_bad_proc_fails_closed'),
    ('control.py', "if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot):", 'if False:', D+'test_bad_proc_fails_closed'),
    ('control.py', ("pid_file.open('r', encoding='ascii')", "not re.fullmatch(r'[0-9]+', pid_text)"), ("pid_file.open('r', encoding='utf-8')", 'not pid_text.isdigit()'), D+'test_pid_ascii_and_missing_proc_fail_closed'),
    ('control.py', "or int(fields[19]) >= 2**64", 'or False', D+'test_bad_proc_fails_closed'),
    ('control.py', "first, second = snapshot(), snapshot()", 'first = snapshot(); second = first', D+'test_snapshot_race_and_pid_path_replacement_fail_closed'),
    ('control.py', "or _pid_signature(pid_file.stat()) != signature", 'or False', D+'test_pid_route_metadata_must_match_open_file'),
    ('control.py', "            record = (proc / str(pid) / 'stat').read_text(encoding='ascii')",
     "            try:\n                record = (proc / str(pid) / 'stat').read_text(encoding='ascii')\n            except OSError:\n                record = str(pid) + ' (pueued) ' + ' '.join(['S'] + ['0']*18 + ['956'])",
     D+'test_proc_io_error_never_falls_back_to_ps'),
    ('control.py', "reader = mini_generation if self.host == 'mini' else desk_generation", 'reader = mini_generation', D+'test_wall_clock_jumps_keep_identity'),
    ('control.py', "reader = mini_generation if self.host == 'mini' else desk_generation", "reader = desk_generation if sys.platform == 'linux' else mini_generation", D+'test_mini_golden_never_reads_proc'),
    ('control.py', "            generation_kind(value['generation'])", '            pass', D+'test_desk_formats_strict_mini_unchanged'),
    ('control.py', "                generation_kind(startup['previous_generation'])", '                pass', I+'test_invalid_previous_generation_is_rejected_without_writes'),
    ('install-desk.py', "if kind == 'legacy' or require_running:", 'if False:', I+'test_legacy_paused_any_group_refuses_before_effects'),
    ('install-desk.py', "if phase not in (None, 'ready'):", 'if False:', I+'test_prepared_and_intent_refuse_without_effects'),
    ('install-desk.py', "if kind != 'legacy':", 'if False:', I+'test_new_distinct_generation_uses_normal_restart_reconciliation'),
    ('install-desk.py', "migration = dict(migration if migration is not None else state, generation=current)", "migration = dict(migration if migration is not None else state, generation=current, reasons={})", I+'test_running_conversion_preserves_authority_receipts_and_is_idempotent'),
    ('install-desk.py', "migration = dict(migration if migration is not None else state, generation=current)", "migration = dict(migration if migration is not None else state, generation=current, revision=state['revision']+1)", I+'test_running_conversion_preserves_authority_receipts_and_is_idempotent'),
    ('install-desk.py', "startup.get('previous_generation') == state['generation']", 'False', I+'test_ready_startup_links_convert_only_matching_reference'),
    ('install-desk.py', "startup.get('previous_generation') == state['generation']", 'True', I+'test_ready_startup_links_convert_only_matching_reference'),
    ('install-desk.py', "    kind = control.generation_kind(state['generation'])", "    state['generation'] = engine.generation(state)\n    kind = control.generation_kind(state['generation'])", I+'test_legacy_paused_any_group_refuses_before_effects'),
    ('install-desk.py', "        installation_plan(engine, control, pid_file, expected=engine.install_generation,\n                          require_running=legacy_conversion)  # before even the switch", '        pass  # pre-effect gate lost', I+'test_pause_after_preflight_refuses_before_marks'),
    ('install-desk.py', "            migration, _, action = installation_plan(engine, control, pid_file, expected=engine.install_generation,\n                                                     require_running=legacy_conversion)",
     '            migration, action = engine.install_migration, engine.install_generation_action', I+'test_preflight_plan_is_not_reused_after_workers_stop'),
    ('install-desk.py', "                        generation_plan(engine, control, engine.state(db), require_running=legacy_conversion)", '                        pass', I+'test_paused_at_commit_rolls_back_and_keeps_maintenance'),
    ('install-desk.py', "                        daemon_fence(engine, control, pid_file, engine.install_generation)", '                        pass', I+'test_daemon_change_at_commit_rolls_back'),
    ('install-desk.py', "            installation_plan(engine, control, pid_file, expected=engine.install_generation,\n                              require_running=legacy_conversion)", '            pass  # final fence lost', I+'test_generation_change_before_release_keeps_maintenance'),
    ('install-desk.py', "require_running=legacy_conversion)\n            if own_maintenance:", 'require_running=False)\n            if own_maintenance:', I+'test_paused_before_release_keeps_maintenance'),
    ('install-desk.py', "or loaded['MainPID'] != str(int(pid))", 'or False', I+'test_mainpid_mismatch_refuses_before_effects'),
    ('install-desk.py', "if expected is not None and current != expected:", 'if False:', I+'test_generation_change_before_release_keeps_maintenance'),
    ('control.py', "if op == 'capabilities':\n            return self.view()", "if op == 'capabilities':\n            self.reconcile()\n            return self.view()", I+'test_view_and_ensayo_never_convert_legacy_authority'),
    ('control.py', "request['expected'] != expected or", 'False or', D+'test_expected_survives_clock_jump_and_real_change_rejects'),
]


def run(root, test=None):
    argv = [sys.executable, '-B', '-m', 'unittest', '-v', 'test_generation'+('.'+test if test else '')]
    return subprocess.run(argv,cwd=str(root/'tests'),text=True,capture_output=True,timeout=30,
                          env=dict(os.environ,TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1'))


def main():
    nominal=run(SOURCE)
    if nominal.returncode:
        raise AssertionError('Nominal must pass:\n'+nominal.stdout+nominal.stderr)
    print(nominal.stderr,flush=True)
    for number,(relative,anchor,replacement,test) in enumerate(CASES,1):
        with tempfile.TemporaryDirectory(prefix='desk-gen-mutant-',dir='/tmp') as folder:
            root=Path(folder)/'pueue-desk'
            shutil.copytree(SOURCE,root,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            target=root/relative; source=target.read_text()
            edits = zip(anchor,replacement) if isinstance(anchor,tuple) else [(anchor,replacement)]
            for before,after in edits:
                if source.count(before)!=1:raise AssertionError('Nonunique anchor '+str(number)+': '+repr(before))
                source=source.replace(before,after)
            target.write_text(source)
            result=run(root,test)
            if result.returncode != 1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:
                raise AssertionError('Mutant '+str(number)+' did not fail by assertion:\n'+result.stdout+result.stderr)
            reason=next((line for line in result.stderr.splitlines() if line.startswith('AssertionError:')),'assertion')
            print(str(number)+' '+test+': caught | '+reason,flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' mutants caught by assertion; nominal green',flush=True)


if __name__ == '__main__':main()
