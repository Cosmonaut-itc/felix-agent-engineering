"""Mutaciones SEN-6495: selección, I1 e integración aislada I4."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
CASES=[
    ('M4_exit_zero', "report['podadas'] += len(confirmed)", "report['podadas'] += len(batch)", 'test_M4_partial_success_exit_zero_counts_acknowledged_ids_only'),
    ('M4_failed_batch', "if failed or len(confirmed) != len(batch):", 'if failed:', 'test_M4_partial_success_exit_zero_counts_acknowledged_ids_only'),
    ('I1_live', "                counts['vivas'] += 1\n                continue", "                counts['vivas'] += 1\n                eligible.append((0, int(ident), ident))\n                continue", 'test_I1_only_exact_Done_and_unexpected_shapes'),
    ('I1_multivariant', "type(status) is not dict or len(status) != 1:", "type(status) is not dict or not status:", 'test_I1_only_exact_Done_and_unexpected_shapes'),
    ('I2_general', 'PRUNE_RETENTION = 24 * 60 * 60', 'PRUNE_RETENTION = 23 * 60 * 60', 'test_I2_retention_boundaries_and_results'),
    ('I2_killed', 'PRUNE_KILLED_RETENTION = 7 * 24 * 60 * 60', 'PRUNE_KILLED_RETENTION = 6 * 24 * 60 * 60', 'test_I2_retention_boundaries_and_results'),
    ('I2_boundary', "elif end >= boundary:", "elif end > boundary:", 'test_I2_retention_boundaries_and_results'),
    ('I3_dependency', 'if unknown_dependencies or ident in referenced:', 'if False:', 'test_I3_dependencies_of_every_nonterminal_and_malformed_task'),
    ('I4_propagate', "        except Exception:\n            # The guard has already succeeded and released control.lock.\n            pass", "        except Exception:\n            # The guard has already succeeded and released control.lock.\n            raise", 'test_I4_prune_errors_never_change_guard_result_and_outside_lock'),
    ('I4_omit', "    if args.command == 'guardia':\n        try:\n            prune_history(pueue, directory)", "    if False:\n        try:\n            prune_history(pueue, directory)", 'test_I4_prune_errors_never_change_guard_result_and_outside_lock'),
    ('I5_switch', "if os.path.lexists(directory / 'poda.off'):", 'if False:', 'test_I5_switch_no_Pueue_calls'),
    ('I6_cadence', "now - previous['at'] < policy['interval']", "now - previous['at'] < 1", 'test_I6_cadence_and_cap_retry'),
    ('I6_cap_retry', "stamp(len(ids) == policy['limit'])", 'stamp(False)', 'test_I6_cadence_and_cap_retry'),
    ('I7_protected', "        if ident in protected:", '        if False:', 'test_I7_uncertain_admission_keeps_its_execution_evidence'),
    ('I7_reserved', "attempt['outcome'] in ('reserved', 'unknown') else set()", "attempt['outcome'] in ('unknown',) else set()", 'test_I7_uncertain_admission_keeps_its_execution_evidence'),
    ('I7_resolved', "attempt['outcome'] in ('reserved', 'unknown') else set()", "attempt['outcome'] in ('reserved', 'unknown', 'confirmed') else set()", 'test_I7_uncertain_admission_keeps_its_execution_evidence'),
    ('I7_unreadable', "    try:\n        value = admission_state(db)\n        has_clients = db.execute(\"SELECT 1 FROM sqlite_master WHERE name='client_reservations'\").fetchone() is not None\n    finally:\n        db.close()", "    try:\n        value = admission_state(db)\n        has_clients = db.execute(\"SELECT 1 FROM sqlite_master WHERE name='client_reservations'\").fetchone() is not None\n    except Exception:\n        value, has_clients = None, False\n    finally:\n        db.close()", 'test_I7_unreadable_admission_retains_everything'),
]

def run(root, test=None):
    return subprocess.run([sys.executable,'-B','-m','unittest', 'test_prune'+('.PruneTests.'+test if test else '')],
                          cwd=root/'tests', env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                          text=True,capture_output=True,timeout=60)


def main():
    nominal=run(ROOT)
    print(nominal.stderr,flush=True)
    assert nominal.returncode==0, 'nominal failed'
    for name, anchor, replacement, test in CASES:
        root=Path(tempfile.mkdtemp(prefix='sen6495-mutant-'))/'pueue-desk'
        shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        path=root/'control.py'
        source=path.read_text()
        assert anchor in source,name
        path.write_text(source.replace(anchor,replacement))
        result=run(root,test)
        assert result.returncode==1 and 'FAIL:' in result.stderr and 'ERROR:' not in result.stderr, name+': '+result.stderr
        print(name+': muerta por assertion; '+test,flush=True)
    print(f'{len(CASES)}/{len(CASES)} mutaciones muertas',flush=True)


if __name__=='__main__':main()
