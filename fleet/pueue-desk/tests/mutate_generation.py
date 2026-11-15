"""DESK-GEN mutations (Astra §5, 54–60), in /tmp copies. Nominal tests pass, every mutant fails by assertion."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

FLEET = Path(__file__).resolve().parents[2]
C, I = 'control.py', 'install-desk.py'
SHIFT = 'ClockShiftTests.test_lstart_shift_with_same_process_keeps_generation'
SIGNALS = 'IdentityTests.test_every_restart_signal_changes_generation'
CLOSED = 'IdentityTests.test_unreadable_or_foreign_identity_fails_closed'
RACE = 'IdentityTests.test_change_between_snapshots_or_during_read_fails_closed'
MINI = 'IdentityTests.test_mini_generation_value_unchanged_and_never_reads_proc'
STORED = 'StoredGenerationTests.test_desk_accepts_only_legacy_or_stable_format'
RUNNING = 'ConversionInstallTests.test_every_group_running_converts_and_preserves_reasons_revision_desire_receipts'
SECO = 'ConversionInstallTests.test_seco_reports_conversion_or_stop_without_writes'
DISPATCH = 'self.host == \'mini\' else desk_generation)(Path(state[\'pid_file\']))'
SEED = "seed = canonical([DESK_GENERATION, i['boot'], i['pid'], i['ticks'], i['file']])"
SIGNATURE = 'return [st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size]'
COMMIT = """                if migration is not None or plan['kind'] == 'convert':
                    with engine.connect(write=True) as db:
                        engine.store(db, state)"""
CONVERT = "state = dict(state, generation=plan['current'])"

# file, original, replacement, tests (all run; the mutant must make at least one FAIL and none ERROR)
CASES = [
 # 54: wall clock back in the identity, or an identity component dropped.
 (C, DISPATCH, "self.host in ('mini', 'desk') else desk_generation)(Path(state['pid_file']))", (SHIFT,)),
 (C, SEED, SEED[:-2] + ", mini_generation(pid_file)])", (SHIFT,)),
 (C, SEED, SEED.replace("i['boot'], ", ''), (SIGNALS,)),
 (C, SEED, SEED.replace("i['pid'], ", ''), (SIGNALS,)),
 (C, SEED, SEED.replace("i['ticks'], ", ''), (SIGNALS,)),
 (C, SIGNATURE, SIGNATURE.replace('st.st_dev, ', ''), (SIGNALS,)),
 (C, SIGNATURE, SIGNATURE.replace('st.st_ino, ', ''), (SIGNALS,)),
 (C, SIGNATURE, SIGNATURE.replace('st.st_mtime_ns, ', ''), (SIGNALS,)),
 (C, SIGNATURE, SIGNATURE.replace('st.st_ctime_ns, ', ''), (SIGNALS,)),
 (C, SIGNATURE, SIGNATURE.replace(', st.st_size', ''), (SIGNALS,)),
 (C, 'ticks = fields[19]', 'ticks = fields[18]', (SIGNALS,)),
 # 55: validation, coherence and fail-closed reading.
 (C, "head != str(pid) + ' (pueued'", 'False', (CLOSED,)),
 (C, 'or fields[0] not in LIVE_STATES', 'or False', (CLOSED,)),
 (C, 'if not BOOT_ID.fullmatch(boot):', 'if not boot:', (CLOSED,)),
 (C, 'or int(ticks) >= 2**64', 'or False', (CLOSED,)),
 (C, 'or str(int(text)) != text', 'or False', (CLOSED,)),
 (C, 'first, second = _desk_snapshot(pid_file, proc), _desk_snapshot(pid_file, proc)',
  'first = second = _desk_snapshot(pid_file, proc)', (RACE,)),
 (C, 'if before != after or after != _file_signature(os.stat(str(pid_file))):', 'if before != after:', (RACE,)),
 (C, 'if before != after or after != _file_signature(os.stat(str(pid_file))):',
  'if after != _file_signature(os.stat(str(pid_file))):', (RACE,)),
 (C, "    i = linux_identity(pid_file, proc)\n",
  "    try:\n        i = linux_identity(pid_file, proc)\n    except (OSError, ValueError):\n        return mini_generation(pid_file)\n",
  (CLOSED,)),
 # 56: selection by platform, migration from view, expected alias, relaxed desk format.
 (C, DISPATCH, "sys.platform == 'darwin' else desk_generation)(Path(state['pid_file']))", (SHIFT, MINI)),
 (C, "        generation = self.generation(state)\n        for group in self.profile:",
  "        generation = self.generation(state)\n        if self.host == 'desk' and generation_kind(state['generation']) == 'legacy':\n"
  "            with self.connect(write=True) as db:\n                self.store(db, dict(state, generation=generation))\n"
  "        for group in self.profile:", (SECO,)),
 (C, "if (request['expected'] != expected or expected['generation'] != state['generation']",
  "if (dict(request['expected'], generation=expected['generation']) != expected or expected['generation'] != state['generation']",
  ('ClockShiftTests.test_panel_rejects_expected_from_previous_daemon',)),
 (C, "re.compile(DESK_GENERATION + ':[0-9a-f]{64}')", "re.compile(DESK_GENERATION + ':.+')", (STORED,)),
 (C, "            generation_kind(value['generation'])  # the Mini keeps its opaque value", '            pass', (STORED,)),
 (C, "                generation_kind(startup['previous_generation'])", '                pass', (STORED,)),
 # 57: conversion without the Running proof, or ignoring the durable startup.
 (I, "        if paused:\n            blockers.append(", "        if False:\n            blockers.append(",
  ('ConversionInstallTests.test_paused_default_stops_before_effects',)),
 (I, "if phase not in (None, 'ready'):", 'if False:', ('ConversionInstallTests.test_prepared_startup_stops_before_effects',)),
 (I, "print(describe_generation(planned))\n        refuse_blocked(planned)", 'print(describe_generation(planned))', (SECO,)),
 # 58: conversion adds ambiguous, drops reasons, splits the transaction or rewrites startup history.
 (I, CONVERT, CONVERT[:-1] + ", reasons={g: sorted(set(r) | {'ambiguous'}) for g, r in state['reasons'].items()})", (RUNNING,)),
 (I, CONVERT, CONVERT[:-1] + ', reasons={})', (RUNNING,)),
 (I, COMMIT, """                if migration is not None:
                    with engine.connect(write=True) as db:
                        engine.store(db, migration)
                if plan['kind'] == 'convert':
                    with engine.connect(write=True) as db:
                        engine.store(db, state)""",
  ('ConversionInstallTests.test_table_migration_and_conversion_commit_in_one_transaction',)),
 (I, CONVERT, CONVERT[:-1] + ", **({'startup': dict(state['startup'], previous_generation=plan['current'])} "
  "if state.get('startup') else {}))", ('ConversionInstallTests.test_ready_startup_converts_and_keeps_its_history',)),
 # 59: stale plan, missing fences, daemon change accepted.
 (I, "refuse_blocked(generation_plan(engine, control, current_state(engine, policy_plan(engine)), planned))",
  'policy_plan(engine)', ('ConversionInstallTests.test_group_paused_after_preflight_stops_before_marks',)),
 (I, "                generation_plan(engine, control, current_state(engine, migration), planned)\n", '                pass\n',
  ('ConversionInstallTests.test_group_paused_after_stopping_guard_stops_before_publishing',)),
 (I, "plan = generation_plan(engine, control, state, planned)  # immediately before the commit", 'plan = planned',
  ('ConversionInstallTests.test_group_paused_while_publishing_stops_before_commit',)),
 (I, "            if planned is not None:\n                generation_plan(engine, control, current_state(engine, None), planned)",
  '            if False:\n                pass', ('ConversionInstallTests.test_group_paused_before_lifting_maintenance_keeps_it',)),
 (I, "if planned is not None and current != planned['current']:", 'if False:',
  ('ConversionInstallTests.test_daemon_change_during_install_keeps_maintenance',)),
 (I, "if pid_file.read_text().strip() != engine.install_unit_before['MainPID']:", 'if False:',
  ('ConversionInstallTests.test_pid_file_must_be_main_pid_before_effects',)),
]


def run(root, tests):
    return subprocess.run([sys.executable, '-B', '-m', 'unittest', *['test_generation.' + t for t in tests], '-v'],
                          cwd=str(root / 'pueue-desk/tests'), text=True, capture_output=True, timeout=60,
                          env=dict(os.environ, TMPDIR='/tmp', PYTHONDONTWRITEBYTECODE='1'))


def copy_tree(folder):
    root = Path(folder) / 'fleet'
    for name in ('rack', 'pueue-desk'):
        shutil.copytree(FLEET / name, root / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    return root


def main():
    with tempfile.TemporaryDirectory(prefix='desk-gen-mutation-', dir='/tmp') as folder:
        nominal = run(copy_tree(folder), sorted({t for *_, tests in CASES for t in tests}))
        if nominal.returncode != 0:
            raise AssertionError('Nominal tests are not green:\n' + nominal.stdout + nominal.stderr)
    for number, (relative, original, replacement, tests) in enumerate(CASES, 1):
        with tempfile.TemporaryDirectory(prefix='desk-gen-mutation-', dir='/tmp') as folder:
            root = copy_tree(folder)
            target = root / 'pueue-desk' / relative
            source = target.read_text()
            if source.count(original) != 1:
                raise AssertionError('Nonunique mutation anchor ' + str(number) + ': ' + repr(original))
            target.write_text(source.replace(original, replacement))
            result = run(root, tests)
            if result.returncode != 1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:
                raise AssertionError('Mutation ' + str(number) + ' did not fail by assertion:\n' + result.stdout + result.stderr)
            reason = next((s for s in result.stderr.splitlines() if s.startswith('AssertionError:')), 'assertion')
            print(str(number) + ' ' + ' '.join(tests) + ': caught | ' + reason[:160], flush=True)
    print(str(len(CASES)) + '/' + str(len(CASES)) + ' DESK-GEN mutations caught; original tree untouched')


if __name__ == '__main__':
    main()
