"""CC3-P2: each deliberate change must die by its directed assertion."""
from pathlib import Path
import shutil
import sys
import tempfile

from mutate_client_actions import SOURCE, run


R = 'client_remote_run.py'
A = 'client_actions.py'
T = 'test_client_remote_run.RemoteRunTests.'
CASES = [
    ('observe binds the installed groups', R, 'if binding[\'group\'] not in self.engine.profile:',
     "if binding['group'] == 'caja':", 'test_observe_h1_and_caja'),
    ('stock Q recognizes a force start', R, "if posterior and request['op'] == 'cancel-no-payload':",
     'if False:', 'test_verify_acceptance_and_stock_q'),
    ('acceptance comes from disk', R, "match(originals.acceptance(request['action_id']) == request['acceptance'])",
     'pass', 'test_acceptance_must_match_original'),
    ('recover can produce finish', R, 'finish = self.finish(request, originals, current, reservation)',
     'finish = None', 'test_recover_closes_once_from_live_done'),
    ('a confirmed force start survives autonomous finish', R,
     "if reservation is not None and not reservation['open'] and reservation['phase'] == 'finish':",
     'if False:', 'test_recover_closes_once_from_live_done'),
    ('apply reopens completion', A,
     'if finish is not None and isinstance(self.actions.adapter, InstalledAdapters):',
     'if False:', 'test_apply_revalidates_completion'),
    ('finish waits for Done', R, "and current['state'] in ('Done', 'Absent'):",
     "and current['state'] in ('Running', 'Done', 'Absent'):",
     'test_completion_requires_current_done'),
    ('completion uses the same worker pid', R,
     "and value['pid'] == worker['pid'] and value['birth'] == worker['birth']",
     "and value['birth'] == worker['birth']", 'test_completion_from_other_worker_keeps_open'),
    ('completion requires stock attribution', R,
     """        attribution = recorded.get('attribution')
        c.keys(attribution, 'dispatch seal_receipt exclusion')
        c.digest(attribution['seal_receipt'])
        c.require(attribution['exclusion'] == 'filtered'
                  and attribution['dispatch'] == recorded['process']['dispatch'], 'remote_run_attribution')""",
     '        pass', 'test_completion_without_attribution_keeps_open'),
    ('running worker may predate acceptance', R,
     "match(pair == winner or kind == 'terminated' and pair == (None, None))",
     'match(pair == winner)', 'test_running_worker_can_predate_acceptance'),
    ('completion cannot settle an unsealed RPC', A,
     '        if stock and record[\'response\'][\'status\'] == \'unknown\':\n            return self.collect_stock(item)',
     '''        if stock and record['response']['status'] == 'unknown':
            return dict(kind='retain', reason='evidence_missing')''', 'test_completion_cannot_settle_a_queue_phase'),
    ('retirement Q can witness id reuse', R, "if posterior and request['op'] in RETIRE:",
     'if False:', 'test_stock_retirement_retains_original_id_when_reused'),
    ('an unrelated task is not absence', R,
     "            raise Unavailable('identity')\n        match(task['group'] == group)",
     "            return absent\n        match(task['group'] == group)", 'test_other_creation_does_not_become_absent'),
    ('actual group checked', R, "        match(task['group'] == group)", '        pass', 'test_different_group_is_identity'),
    ('attempt original names current attempt', R,
     "and origin['local_attempt'] == local and origin['identity'] == self.identity",
     "and origin['identity'] == self.identity", 'test_old_attempt_original_is_identity'),
    ('current reread after observation', R,
     "                        match(originals.current == originals.read('pueue.client-current.json'))",
     '                        pass', 'test_current_replaced_mid_read_is_identity'),
    ('a later queued timestamp does not recover attempt', R,
     "        if enqueued is not None and enqueued != identity['attempt']:",
     """        if name == 'Queued':
            current['enqueued_at'] = identity['attempt']
        if current['enqueued_at'] is not None and current['enqueued_at'] != identity['attempt']:""",
     'test_different_queue_attempt_is_identity'),
    ('mode is linked to action id', R,
     "match(value['action_id'] == action_id and all(value[key] == self.links[key] for key in LINKS))",
     "match(all(value[key] == self.links[key] for key in LINKS))", 'test_old_action_original_is_identity'),
    ('generation comes from authority', R,
     "originals.identity['generation'] != hashlib.sha256(raw.encode()).hexdigest()",
     'False', 'test_old_generation_is_startup'),
    ('generation checked after daemon read', R,
     "if generation != state['generation']:", 'if False:',
     'test_daemon_replaced_during_observe_is_startup'),
    ('legacy binding stays unsupported', R, "if envelope.get('schema') == 'remote-run-client-binding@1':",
     'if False:', 'test_legacy_binding_is_unsupported_before_daemon'),
    ('producer must be in allowlist', R,
     "if (value['producer'], value['worker']) not in artifacts:",
     "if not any(worker == value['worker'] for _, worker in artifacts):", 'test_unaccredited_producer_is_unsupported'),
    ('producer must match actual bytes', R, "if hashlib.sha256(source).hexdigest() != value['producer']:",
     'if False:', 'test_producer_copy_is_hashed'),
    ('worker contract must be in allowlist', R, "if (value['producer'], value['worker']) not in artifacts:",
     "if not any(producer == value['producer'] for producer, _ in artifacts):", 'test_unaccredited_worker_is_unsupported'),
    ('birth must name accredited worker', R, "if value['worker'] != WORKER:",
     'if False:', 'test_unknown_worker_original_keeps_open'),
    ('config is a prerequisite', A,
     "            self.adapter(request)\n        except Unavailable:\n            return False",
     "            self.adapter(request)\n        except Unavailable:\n            return True",
     'test_no_allowlist_is_unsupported'),
    ('not_found crosses the wire', A, "if error.reason in ('not_found', 'identity'):",
     "if error.reason == 'identity':", 'test_missing_ref_is_not_found'),
    ('missing reserved ref stays missing evidence', R,
     "'not_found' if not opened and reservation is None else 'evidence_missing'",
     "'not_found' if not opened else 'evidence_missing'", 'test_missing_reserved_directory_keeps_open'),
    ('run inode must remain pinned', R, 'match(inode(current) == inode(info))',
     'pass', 'test_directory_swap_between_lookup_and_use'),
    ('symlink original rejected', R,
     'FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK',
     'FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK', 'test_symlink_original_is_rejected'),
    ('FIFO opened nonblocking', R, 'FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK',
     'FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW', 'test_fifo_is_opened_nonblocking_and_rejected'),
    ('only regular files', R, 'match(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()',
     'match(info.st_uid == os.getuid()', 'test_special_original_is_rejected'),
    ('hardlinks rejected', R,
     ('and info.st_nlink == 1', 'and after.st_nlink == 1'),
     ('', ''), 'test_hardlink_original_is_rejected'),
    ('file owner verified', R, 'match(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()',
     'match(stat.S_ISREG(info.st_mode)', 'test_wrong_file_owner_is_rejected'),
    ('original must be private', R, '(stat.S_IMODE(info.st_mode) == 0o600 if private else not info.st_mode & 0o022)',
     '(not info.st_mode & 0o022)', 'test_open_permissions_are_rejected'),
    ('files are size-bounded before reading', R,
     "        if info.st_size > limit:\n            raise Unavailable('evidence_missing')",
     '        pass', 'test_oversized_original_is_rejected'),
    ('binding must match pre-id anchor', R, "match(raw == self.read('pueue.client-add.json'))",
     'pass', 'test_anchor_is_required_and_must_match'),
    ('duplicate keys must fail closed', R,
     ('        value = c.decode(raw)', '        envelope = c.decode(raw)'),
     ('        value = __import__("json").loads(raw)', '        envelope = __import__("json").loads(raw)'),
     'test_duplicate_fields_are_rejected'),
    ('dispatcher is launcher-specific in both paths', A,
     "if request['launcher'] != 'remote-run' or self.exclusion != 'filtered':",
     "if self.exclusion != 'filtered':", 'test_other_launchers_in_public_and_guard_paths'),
    ('gather defers daemon read until originals pass', A,
     'proposal = self.collect(item, snapshot)', 'proposal = self.collect(item, snapshot())',
     'test_gather_does_not_read_daemon_for_unaccredited_action'),
    ('coordinated profile stays inert', A,
     ("if r['launcher'] == 'remote-run' and r['op'] != 'finish' and self.exclusion != 'filtered':",
      "if request['launcher'] != 'remote-run' or self.exclusion != 'filtered':"),
     ('if False:', "if request['launcher'] != 'remote-run':"), 'test_coordinated_profile_does_not_activate'),
]


def main():
    nominal = run(SOURCE, 'test_client_remote_run', timeout=120)
    if nominal.returncode:
        raise AssertionError(nominal.stdout + nominal.stderr)
    failures = []
    for number, (rule, name, old, new, test) in enumerate(CASES, 1):
        with tempfile.TemporaryDirectory(prefix='cc3-p2-mutant-', dir='/tmp') as folder:
            root = Path(folder) / 'pueue-desk'
            shutil.copytree(SOURCE, root, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            path = root / name
            source = path.read_text()
            edits = zip(old, new) if isinstance(old, tuple) else [(old, new)]
            for before, after in edits:
                if source.count(before) != 1:
                    failures.append('Nonunique anchor: ' + rule)
                    break
                source = source.replace(before, after)
            else:
                path.write_text(source)
                result = run(root, T + test)
                if (result.returncode != 1 or 'FAIL: ' + test + ' (' not in result.stderr
                        or 'AssertionError:' not in result.stderr or 'ERROR:' in result.stderr):
                    failures.append(rule + ' did not fail by assertion:\n' + result.stdout + result.stderr)
                    continue
                print(f'{number} {rule}: assertion', flush=True)
    if failures:
        raise AssertionError('\n'.join(failures))
    print(f'{len(CASES)}/{len(CASES)} mutants; nominal green; {sys.version.split()[0]}', flush=True)


if __name__ == '__main__':
    main()
