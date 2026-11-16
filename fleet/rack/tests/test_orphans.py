"""Huérfanos: /proc y cgroup privados; ningún comando del host."""
import safety
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_rack import snapshot, serve, sample


class OrphanTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='rack-orphans-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.proc, self.cgroup = self.root/'proc', self.root/'cgroup'
        self.proc.mkdir()
        (self.proc/'stat').write_text('cpu 1 2 3 4\nbtime 1000\n')
        self.members = self.cgroup/'private.service'/'cgroup.procs'
        self.members.parent.mkdir(parents=True)
        self.ids = []
        self.process(10, 1, comm='pueued')
        (self.proc/'10'/'cgroup').write_text('0::/private.service\n')
        self.data = dict(tasks={'7': dict(group='otros', status={'Done': {'result': 'Success'}},
                                            command='SECRET', envs={'SECRET': 'SECRET'}, path='SECRET')})
        self.addCleanup(patch.stopall)
        self.command = patch.object(snapshot, 'command', return_value='10\n').start()
        patch.object(snapshot.os, 'sysconf', side_effect=lambda name: {'SC_CLK_TCK': 100, 'SC_PAGE_SIZE': 4096}[name]).start()

    def process(self, pid, ppid, start=100, rss=2, comm='worker ) safe', fd=None):
        folder = self.proc/str(pid)
        folder.mkdir(exist_ok=True)
        fields = ['0']*22
        for index, value in {0:'S', 1:ppid, 11:5, 12:2, 19:start, 21:rss}.items():
            fields[index] = str(value)
        (folder/'stat').write_text(str(pid)+' ('+comm+') '+' '.join(fields))
        if pid not in self.ids:
            self.ids.append(pid)
        self.members.write_text('\n'.join(map(str, self.ids)))
        if fd:
            (folder/'fd').mkdir(exist_ok=True)
            (folder/'fd'/fd).symlink_to('/private/task_logs/7.log')

    def read(self):
        return snapshot.desk_orphans(self.data, self.proc, self.cgroup, 'private.service')

    def test_descendants_excluded_and_reparented_attributed_by_fd1(self):
        self.process(20, 10)
        self.process(21, 20, fd='1')
        self.process(30, 1, fd='1')
        rows, total = self.read()
        self.assertEqual([r['pid'] for r in rows], [30])
        self.assertEqual(total, 1)
        row = rows[0]
        self.assertEqual((row['origin_task'], row['group'], row['origin_state']), ('7', 'otros', 'Done'))
        self.assertEqual((row['comm'], row['rss_bytes'], row['started_at']), ('worker ) safe', 8192, '1970-01-01T00:16:41Z'))
        self.assertEqual(row['cpu_percent'], 0)
        self.assertNotIn('SECRET', json.dumps(rows))
        self.command.assert_called_once_with(['systemctl', '--user', 'show', 'private.service', '-p', 'MainPID', '--value'], timeout=.5)

    def test_fd2_unknown_origin_and_closed_origin_states(self):
        self.process(30, 1, fd='2')
        self.process(31, 1)
        for status, state in (({'Done': {'result': 'Killed'}}, 'Killed'), ({'Running': {}}, 'Running'), ('Paused', 'desconocido')):
            self.data['tasks']['7']['status'] = status
            rows, total = self.read()
            self.assertEqual(total, 2)
            self.assertEqual(rows[0]['origin_state'], state)
            self.assertEqual((rows[1]['origin_task'], rows[1]['group'], rows[1]['origin_state']), (None, None, 'desconocido'))
        self.data = None
        self.assertEqual(self.read()[0][0]['origin_state'], 'desconocido')

    def test_identity_change_and_disappearance_discarded(self):
        self.process(30, 1, fd='1')
        self.process(31, 1)
        original = snapshot.orphan_stat
        calls = {}
        def changing(proc, pid):
            calls[pid] = calls.get(pid, 0) + 1
            if calls[pid] == 2 and pid == 31:
                raise FileNotFoundError()
            row = original(proc, pid)
            if calls[pid] == 2 and pid == 30:
                row['start'] += 100
            return row
        with patch.object(snapshot, 'orphan_stat', side_effect=changing):
            self.assertEqual(self.read(), ([], 0))

    def test_membership_change_and_main_identity_change(self):
        self.process(30, 1)
        with patch.object(snapshot.time, 'sleep', side_effect=lambda _: self.members.write_text('10\n')):
            self.assertEqual(self.read(), ([], 0))
        self.members.write_text('10\n30\n')
        with patch.object(snapshot.time, 'sleep', side_effect=lambda _: self.process(10, 1, start=999)):
            with self.assertRaises(ValueError):
                self.read()

    def test_real_total_limit_and_rss_order(self):
        for pid in range(100, 170):
            self.process(pid, 1, rss=pid)
        rows, total = self.read()
        self.assertEqual((len(rows), total), (64, 70))
        self.assertEqual([row['pid'] for row in rows], list(range(169, 105, -1)))

    def test_cpu_shared_sample_and_no_private_reads(self):
        self.process(30, 1)
        clock = [0.0]
        original = Path.read_text
        def public_only(path, *args, **kwargs):
            self.assertNotIn(path.name, ('cmdline', 'environ'))
            return original(path, *args, **kwargs)
        def tick(_):
            path = self.proc/'30'/'stat'
            fields = path.read_text().rsplit(') ', 1)[1].split()
            fields[11] = '15'
            path.write_text('30 (worker) '+' '.join(fields))
            clock[0] = .1
        with patch.object(Path, 'read_text', public_only), patch.object(snapshot.time, 'sleep', side_effect=tick) as sleep, \
                patch.object(snapshot.time, 'monotonic', side_effect=lambda: clock[0]):
            rows, _ = self.read()
        self.assertAlmostEqual(rows[0]['cpu_percent'], 100)
        sleep.assert_called_once()
        clock = [0.0]
        with patch.object(snapshot.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(snapshot.time, 'sleep', side_effect=lambda _: clock.__setitem__(0, .6)):
            self.assertIsNone(self.read()[0][0]['cpu_percent'])
        with patch.object(snapshot.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(snapshot.time, 'sleep', side_effect=lambda _: clock.__setitem__(0, 2)):
            with self.assertRaises(TimeoutError):
                self.read()

    def test_read_failure_is_partial_snapshot_error(self):
        original = Path.read_text
        for source in ('systemctl', 'cgroup'):
            def commands(argv, **kwargs):
                if argv[0] == 'systemctl':
                    if source == 'systemctl':
                        raise PermissionError('private text must not leak')
                    return '10\n'
                return json.dumps(dict(groups={}, tasks={}))
            def reading(path, *args, **kwargs):
                if source == 'cgroup' and path == self.members:
                    raise PermissionError('private path must not leak')
                return original(path, *args, **kwargs)
            with self.subTest(source=source), patch.object(snapshot, 'command', side_effect=commands), \
                    patch.object(Path, 'read_text', reading), \
                    patch.object(snapshot, 'linux_capacity', return_value=None), \
                    patch.object(snapshot, 'disk', return_value=None), \
                    patch.object(snapshot, 'desk_guard', return_value=None):
                result = snapshot.collect('linux', 'desk', pueue='/fake/pueue', home=self.root,
                                          proc=self.proc, cgroup=self.cgroup, pueue_unit='private.service')
            self.assertEqual((result['orphans'], result['orphans_total']), ([], 0))
            self.assertEqual(result['errors'], [dict(stage='orphans', message='Lectura fallida: PermissionError')])
            self.assertEqual(serve.validate_snapshot(result, 'desk'), result)

    def test_snapshot_validator_strict(self):
        self.process(30, 1, fd='1')
        rows, total = self.read()
        data = dict(sample(), host='desk', os='linux', orphans=rows, orphans_total=total)
        self.assertEqual(serve.validate_snapshot(data, 'desk'), data)
        self.assertEqual(serve.validate_snapshot(sample(), 'mini'), sample())
        bad_values = dict(pid=[True, 0, '30'], comm=[None, 'x'*33], origin_task=[3, 'x', '1'*21],
                          group=[3, 'x'*129], origin_state=[None, 'Paused', []],
                          rss_bytes=[True, -1, 1.5], cpu_percent=[True, -1, float('nan'), float('inf'), '1'],
                          started_at=[None, 'x'*1000, '2026-02-30T00:00:00Z', '2026-01-01T00:00:00+01:00'])
        variants = []
        for key, values in bad_values.items():
            for value in values:
                row = dict(rows[0], **{key: value})
                variants.append(dict(data, orphans=[row]))
        variants += [dict(data, orphans=[dict(rows[0], command='secret')]),
                     dict(data, orphans=[{k:v for k,v in rows[0].items() if k != 'comm'}]),
                     dict(data, orphans=None), dict(data, orphans_total=True), dict(data, orphans_total=-1),
                     dict(data, orphans_total=2), dict(data, orphans=[rows[0]]*65, orphans_total=65),
                     dict(data, orphans=[rows[0]]*2, orphans_total=2),
                     dict(data, orphans=[rows[0], dict(rows[0], pid=31, rss_bytes=99999)], orphans_total=2)]
        for bad in variants:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                serve.validate_snapshot(copy.deepcopy(bad), 'desk')
        for missing in ('orphans', 'orphans_total'):
            bad = dict(data)
            del bad[missing]
            with self.assertRaises(ValueError):
                serve.validate_snapshot(bad, 'desk')


if __name__ == '__main__':
    unittest.main()
