"""SEN-6487: assertion-killed mutations of emergency pause recovery rules."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

BASE = Path(__file__).resolve().parents[1]
FENCE = 'test_pause_fence.EmergencyFenceTests.'
RESTART = 'test_pause_fence.EmergencyRestartTests.'
CASES = [
    ('unread-state-is-manual', "state is not None and not reasons &", "not reasons &", 2,
     FENCE+'test_protective_marker_requires_state_read_and_no_manual_or_ambiguous'),
    ('manual-reasons-hold', "not reasons & {'manual','ambiguous'}", "not reasons & {'ambiguous'}", 1,
     FENCE+'test_protective_marker_requires_state_read_and_no_manual_or_ambiguous'),
    ('ambiguous-reasons-hold', "not reasons & {'manual', 'ambiguous'}", "not reasons & {'manual'}", 1,
     FENCE+'test_protective_marker_requires_state_read_and_no_manual_or_ambiguous'),
    ('protective-pressure', "stream.write(reason + '\\n')", "stream.write('manual\\n')", 1,
     FENCE+'test_protective_marker_requires_state_read_and_no_manual_or_ambiguous'),
    ('manual-never-downgrades', "reason != 'pressure' or self.fence_reason(group) == 'manual'", "reason != 'pressure'", 1,
     FENCE+'test_conservative_content_and_read_errors_and_no_manual_downgrade'),
    ('exact-bytes-only', "path.read_bytes() == b'pressure\\n'", "path.read_bytes().strip() == b'pressure'", 1,
     FENCE+'test_conservative_content_and_read_errors_and_no_manual_downgrade'),
    ('manual-promotes-existing', 'self.fence_pause(group)  # promote before authority write and pause RPC', 'pass  # lost promotion', 1,
     FENCE+'test_manual_pause_promotes_marker_before_store_and_rpc_then_survives_state_loss'),
    ('marker-fsync', "stream.write(reason + '\\n'); stream.flush(); os.fsync(stream.fileno())", "stream.write(reason + '\\n'); stream.flush()", 1,
     FENCE+'test_manual_pause_promotes_marker_before_store_and_rpc_then_survives_state_loss'),
    ('commit-before-unlink', "        with self.connect(write=True) as db:\n            self.store(db, state)\n        self.pause_fence(group).unlink()",
     "        self.pause_fence(group).unlink()\n        with self.connect(write=True) as db:\n            self.store(db, state)", 1,
     FENCE+'test_pressure_marker_deleted_only_after_committed_reason_and_retries_failed_store'),
    ('pressure-mark-retires', "        self.pause_fence(group).unlink()\n        sync_directory(self.directory)", '        pass  # stale pressure mark', 1,
     FENCE+'test_pressure_marker_deleted_only_after_committed_reason_and_retries_failed_store'),
    ('external-release-manual', 'try:self.fence_pause(group)\n', "try:self.fence_pause(group,'pressure')\n", 1,
     FENCE+'test_failed_external_release_keeps_manual_marker_and_foreign_pause'),
    ('unrecovered-fallback-manual', "if reason is not None:\n", "if False:\n", 4,
     FENCE+'test_unrecovered_pressure_marker_cannot_reclaim_foreign_pause_after_external_start'),
    ('preparer-reads-mark', 'reason = engine.fence_reason(group)', 'reason = None', 1,
     RESTART+'test_incident_managed_restart_without_prior_pressure'),
]


def main():
    for name, before, after, count, test in CASES:
        with tempfile.TemporaryDirectory(prefix='sen6487-mutation-', dir='/tmp') as folder:
            root = Path(folder)/'pueue-desk'
            shutil.copytree(BASE, root, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            target = root/'control.py'
            source = target.read_text()
            assert source.count(before) == count, (name, 'mutation anchor changed')
            target.write_text(source.replace(before, after))
            result = subprocess.run([sys.executable, '-B', '-m', 'unittest', test, '-v'],
                                    cwd=str(root/'tests'), text=True, capture_output=True, timeout=30,
                                    env=dict(os.environ, TMPDIR='/tmp', PYTHONDONTWRITEBYTECODE='1'))
            assert result.returncode == 1 and 'FAIL:' in result.stderr and 'ERROR:' not in result.stderr, (
                name+' survived or failed outside assertion:\n'+result.stdout+result.stderr)
            print(name+': caught by assertion', flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' SEN-6487 safety mutations caught; original tree untouched')


if __name__ == '__main__':
    main()
