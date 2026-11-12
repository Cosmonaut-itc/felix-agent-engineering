"""Dobles de ssh, systemd y /proc: ninguna prueba conecta al desk."""
import contextlib
import io
import importlib.util
import json
import os
import pwd
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MESSAGE = ("deskpolicy retirado el 2026-11-12: el desk toma turno con Pueue. "
           "Actualiza tu rama a origin/main y usa el lanzador del repositorio.\n")
spec = importlib.util.spec_from_file_location("retirement_remote", ROOT / "remote.py")
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)

SSH = r'''import json, os, pathlib, pwd, subprocess, sys, types
assert sys.argv[1:] == ['desk', '/usr/bin/python3', '-I', '-']
root = pathlib.Path(os.environ['FAKE_DESK'])
program = sys.stdin.read()
with (root / 'ssh.log').open('a') as log:
    log.write('ssh desk\n')
pwd.getpwuid = lambda uid: types.SimpleNamespace(pw_dir=str(root / 'home'))
original_run = subprocess.run
state_path = root / 'systemd.json'
state = json.loads(state_path.read_text())
def run(argv, **kwargs):
    if argv[:2] == ['sudo', '-n'] and os.environ.get('FAIL_SUDO'):
        raise subprocess.CalledProcessError(1, argv, stderr='sudo: a password is required')
    if argv == ['sudo', '-n', 'true']:
        return subprocess.CompletedProcess(argv, 0, '', '')
    if 'systemctl' not in argv:
        return original_run(argv, **kwargs)
    with (root / 'commands.jsonl').open('a') as log:
        log.write(json.dumps(argv) + '\n')
    args = argv[argv.index('systemctl') + 1:]
    scope = 'user' if args[0] == '--user' else 'system'
    if scope == 'user': args = args[1:]
    command, *tail = args
    units = state[scope]
    status, output = 0, ''
    if command in ('list-unit-files', 'list-units'):
        output = ''.join(name + ' enabled\n' for name in units)
    else:
        name = tail[0]
        unit = units[name]
        if command == 'cat': output = unit['content']
        elif command == 'show':
            output = unit.get('links', '') if '--property=Triggers,TriggeredBy' in tail else unit.get('properties', '')
        elif command == 'stop': unit['active'] = 'inactive'
        elif command == 'disable': unit['enabled'] = 'disabled'
        elif command == 'is-active': output = unit['active']
        elif command == 'is-enabled': output = unit['enabled']
        else: raise AssertionError(argv)
    if os.environ.get('FAIL_STOP') and command == 'stop':
        raise subprocess.CalledProcessError(1, argv)
    state_path.write_text(json.dumps(state))
    return subprocess.CompletedProcess(argv, status, output, '')
subprocess.run = run
program = program.replace('\nsys.exit(retire(', '\nprocesses = lambda: {}\nopen_admissions = lambda: []\nsys.exit(retire(')
exec(compile(program, '<ssh desk doble>', 'exec'))
'''


class RetirementTests(unittest.TestCase):
    def test_any_command_is_terminal(self):
        for arguments in ([], ['--help'], ['estado'], ['pendiente'], ['admitir'],
                          ['publicar', '--desactivar-noche'], ['preflight'],
                          ['admitir', '--request-id', 'nuevo'],
                          ['cerrar'], ['traspasar'], ['reconciliar'],
                          ['ejecutar', '--', 'touch', 'NO-EJECUTAR'], ['desconocido']):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, '-I', str(ROOT / 'cli.py'), *arguments],
                                        capture_output=True, text=True)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (64, '', MESSAGE))

    def fixture(self, root):
        binary = root / 'bin'
        binary.mkdir()
        ssh = binary / 'ssh'
        ssh.write_text('#!' + sys.executable + '\n' + SSH)
        ssh.chmod(0o700)
        home = root / 'home'
        receipt = home / 'remote-run/runs/antigua/pao-admission.json'
        waiting = home / 'pao-wait/pendiente.json'
        for path in (receipt, waiting):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('registro histórico\n')
        units = {
            'user': {
                'pao-deskpolicy.timer': dict(content='Timer', active='active', enabled='enabled'),
                'night-writer.service': dict(content='ExecStart=/home/felix/.local/lib/pao-deskpolicy/v1/deskpolicy/cli.py publicar', active='active', enabled='enabled', links='TriggeredBy=night.timer'),
                'night.timer': dict(content='Unit=night-writer.service', active='active', enabled='enabled-runtime'),
                'transient-writer.service': dict(content='', active='active', enabled='static', properties='ExecStart=/old/deskpolicy/cli.py publicar'),
                'pueued.service': dict(content='ExecStart=pueued', active='active', enabled='enabled'),
            },
            'system': {
                'deskpolicy-guard.service': dict(content='guardia', active='active', enabled='enabled'),
            },
        }
        (root / 'systemd.json').write_text(json.dumps(units))
        return dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'], FAKE_DESK=str(root))

    def test_dry_run_never_uses_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = self.fixture(root)
            before = (root / 'systemd.json').read_bytes()
            for script in ('deploy-desk.sh', 'withdraw-desk.sh'):
                result = subprocess.run(['bash', str(ROOT / script), '--seco'], env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('# ssh desk /usr/bin/python3 -I -', result.stdout)
                self.assertIn('stop_units', result.stdout)
            self.assertFalse((root / 'ssh.log').exists())
            self.assertEqual((root / 'systemd.json').read_bytes(), before)

    def test_real_transport_is_idempotent_and_preserves_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = self.fixture(root)
            for script in ('deploy-desk.sh', 'withdraw-desk.sh'):
                result = subprocess.run(['bash', str(ROOT / script)], env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('CLI verificada (64)', result.stdout)
                for relative in ('.local/lib/pao-deskpolicy/v1/deskpolicy/cli.py', '.local/bin/pao-deskpolicy'):
                    self.assertEqual((root / 'home' / relative).read_bytes(), (ROOT / 'cli.py').read_bytes())
                state = json.loads((root / 'systemd.json').read_text())
                for scope, units in state.items():
                    for name, unit in units.items():
                        expected = ('active', 'enabled') if name == 'pueued.service' else ('inactive', 'disabled')
                        if name == 'transient-writer.service': expected = ('inactive', 'static')
                        self.assertEqual((unit['active'], unit['enabled']), expected)
                for relative in ('remote-run/runs/antigua/pao-admission.json', 'pao-wait/pendiente.json'):
                    self.assertEqual((root / 'home' / relative).read_text(), 'registro histórico\n')
            commands = [json.loads(line) for line in (root / 'commands.jsonl').read_text().splitlines()]
            self.assertIn(['sudo', '-n', 'systemctl', 'stop', 'deskpolicy-guard.service'], commands)
            self.assertIn(['systemctl', '--user', 'disable', 'night.timer', '--runtime'], commands)
            self.assertEqual(sum('disable' in command for command in commands), 4)

    def test_stop_failure_does_not_publish_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = dict(self.fixture(root), FAIL_STOP='1')
            result = subprocess.run(['bash', str(ROOT / 'deploy-desk.sh')], env=env,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / 'home/.local/bin/pao-deskpolicy').read_bytes(),
                             (ROOT / 'cli.py').read_bytes())

    def test_sudo_preflight_preserves_services_and_old_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = dict(self.fixture(root), FAIL_SUDO='1')
            path = root / 'home/.local/lib/pao-deskpolicy/v1/deskpolicy/cli.py'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'CLI vieja')
            before = (root / 'systemd.json').read_bytes()
            result = subprocess.run(['bash', str(ROOT / 'deploy-desk.sh')], env=env,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_bytes(), b'CLI vieja')
            self.assertEqual((root / 'systemd.json').read_bytes(), before)

    def inflight(self):
        cli = str(Path(pwd.getpwuid(os.getuid()).pw_dir) /
                  '.local/lib/pao-deskpolicy/v1/deskpolicy/cli.py')
        return {410: (1, '100', 'S', ('python3', '-I', cli, 'ejecutar', '--', 'bash', 'cmd.sh')),
                411: (410, '101', 'S', ('bash', 'cmd.sh')),
                412: (411, '102', 'S', ('node', 'vitest', 'run')),
                510: (1, '200', 'S', ('python3', '-I', cli, 'publicar')),
                610: (1, '300', 'S', ('less', cli)),
                611: (1, '301', 'S', ('python3', '-c', 'print("deskpolicy")'))}

    def test_inflight_and_unrelated_processes_never_signalled(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            for pid, (parent, start, state, argv) in self.inflight().items():
                entry = proc / str(pid)
                entry.mkdir()
                fields = [state, str(parent)] + ['0'] * 17 + [start] + ['0'] * 5
                (entry / 'stat').write_text(f'{pid} (python) ' + ' '.join(fields))
                (entry / 'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in argv) + b'\0')
            alive = remote.processes(proc)
        with patch.object(remote, 'processes', side_effect=lambda: dict(alive)), \
             patch.object(remote.os, 'kill', side_effect=lambda pid, sig: alive.pop(pid, None)) as kill, \
             patch.object(remote.time, 'sleep'):
            remote.stop_processes()
        self.assertEqual([c.args[0] for c in kill.call_args_list], [510])
        self.assertEqual(set(alive), {410, 411, 412, 610, 611})

    def test_retire_installs_rejection_then_refuses_while_inflight(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            events = []
            cli = str(home / '.local/lib/pao-deskpolicy/v1/deskpolicy/cli.py')
            alive = {410: (1, '100', 'S', ('python3', '-I', cli, 'ejecutar', '--', 'bash', 'cmd.sh')),
                     411: (410, '101', 'S', ('bash', 'cmd.sh')),
                     510: (1, '200', 'S', ('python3', '-I', cli, 'publicar'))}
            def scan():
                self.assertEqual((home / '.local/bin/pao-deskpolicy').read_bytes(),
                                 (ROOT / 'cli.py').read_bytes())
                events.append('scan')
                return dict(alive)
            with patch.object(remote, 'open_admissions', return_value=[]), \
                 patch.object(remote, 'processes', side_effect=scan), \
                 patch.object(remote.pwd, 'getpwuid', return_value=type('User', (), {'pw_dir': str(home)})()), \
                 patch.object(remote, 'policy_units', return_value=[]), \
                 patch.object(remote, 'stop_units') as stop, \
                 patch.object(remote.os, 'kill') as kill:
                self.assertEqual(remote.retire((ROOT / 'cli.py').read_bytes()), 2)
                stop.assert_not_called()
                kill.assert_not_called()
                self.assertEqual(events, ['scan'])
                # Segunda pasada: las corridas terminaron por sí solas.
                alive.pop(410)
                alive.pop(411)
                kill.side_effect = lambda pid, sig: alive.pop(pid, None)
                with patch.object(remote.time, 'sleep'):
                    self.assertEqual(remote.retire((ROOT / 'cli.py').read_bytes()), 0)
                self.assertEqual([c.args[0] for c in kill.call_args_list], [510])

    def test_second_destination_failure_preserves_first_cli_and_units(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            first = home / '.local/lib/pao-deskpolicy/v1/deskpolicy/cli.py'
            first.parent.mkdir(parents=True)
            first.write_bytes(b'CLI vieja')
            (home / 'bin-real').mkdir()
            (home / '.local/bin').symlink_to(home / 'bin-real', target_is_directory=True)
            with patch.object(remote.pwd, 'getpwuid', return_value=type('User', (), {'pw_dir': str(home)})()), \
                 patch.object(remote, 'policy_units', return_value=[]), \
                 patch.object(remote, 'stop_units') as stop:
                with self.assertRaisesRegex(RuntimeError, 'enlazado'):
                    remote.retire((ROOT / 'cli.py').read_bytes())
            self.assertEqual(first.read_bytes(), b'CLI vieja')
            self.assertEqual(list(first.parent.glob('.deskpolicy-retirado-*')), [])
            stop.assert_not_called()

    def test_wait_option_reaches_remote_and_rejects_negative_values(self):
        result = subprocess.run(['bash', str(ROOT / 'deploy-desk.sh'), '--seco', '--esperar', '3'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('wait=3', result.stdout)
        result = subprocess.run(['bash', str(ROOT / 'deploy-desk.sh'), '--seco', '--esperar', '-1'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--esperar debe ser no negativo', result.stderr)

    def test_wait_drains_or_times_out_without_stopping_writer(self):
        with patch.object(remote, 'processes', return_value={}), \
             patch.object(remote, 'open_admissions', side_effect=[[{'project': 'jale', 'kind': 'chain'}], []]), \
             patch.object(remote.time, 'monotonic', side_effect=[0, 0]), \
             patch.object(remote.time, 'sleep') as sleep:
            self.assertTrue(remote.drain(1))
            sleep.assert_called_once_with(0.2)
        with patch.object(remote, 'processes', return_value={}), \
             patch.object(remote, 'open_admissions', return_value=[{'project': 'jale', 'kind': 'chain'}]), \
             patch.object(remote.time, 'monotonic', side_effect=[0, 1]):
            self.assertFalse(remote.drain(1))

    def test_process_failure_leaves_rejection_installed(self):
        for error in (PermissionError('EPERM'), RuntimeError('writer vivo')):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                home = Path(directory).resolve()
                with patch.object(remote.pwd, 'getpwuid', return_value=type('User', (), {'pw_dir': str(home)})()), \
                     patch.object(remote, 'policy_units', return_value=[]), \
                     patch.object(remote, 'processes', return_value={}), \
                     patch.object(remote, 'open_admissions', return_value=[]), \
                     patch.object(remote, 'stop_processes', side_effect=error):
                    with self.assertRaises(type(error)):
                        remote.retire((ROOT / 'cli.py').read_bytes())
                for relative in ('.local/lib/pao-deskpolicy/v1/deskpolicy/cli.py', '.local/bin/pao-deskpolicy'):
                    self.assertEqual((home / relative).read_bytes(), (ROOT / 'cli.py').read_bytes())

    @contextlib.contextmanager
    def historical_engine(self, records, held=True, members=(410,)):
        """Dobla las bibliotecas que permanecen instaladas, nunca el desk."""
        engine = unittest.mock.Mock()
        engine.records.return_value = records
        engine.life_path.return_value = Path('/locks/life')
        engine.claim_sessions.side_effect = lambda claim: {410, 420}
        libraries = {
            'deskpolicy': types.ModuleType('deskpolicy'),
            'deskpolicy.engine': types.ModuleType('deskpolicy.engine'),
            'deskpolicy.store': types.ModuleType('deskpolicy.store'),
        }
        libraries['deskpolicy.engine'].DeskPolicy = unittest.mock.Mock(return_value=engine)
        store = libraries['deskpolicy.store']
        store.Runtime = unittest.mock.Mock()
        store.held = unittest.mock.Mock(return_value=held)
        store.session_members = unittest.mock.Mock(return_value=list(members))
        store.mutex = lambda root: contextlib.nullcontext()
        store.secure_dir = unittest.mock.Mock()
        with patch.dict(sys.modules, libraries):
            yield engine, store

    def test_claims_use_life_lock_and_all_owner_keeper_sessions(self):
        record = {'project': 'jale', 'kind': 'chain',
                  'claim': {'state': 'active', 'lane': 1}}
        closed = {'claim': {'state': 'closed'}}
        with tempfile.TemporaryDirectory() as directory:
            for held, members, expected in ((True, [], [record]), (False, [411], [record]),
                                             (False, [], [])):
                with self.subTest(held=held, members=members), \
                     self.historical_engine([record, closed, {'claim': None}], held, members) as (_, store):
                    self.assertEqual(remote.open_admissions(Path(directory)), expected)
                    if not held and not members:
                        self.assertEqual({c.args[0] for c in store.session_members.call_args_list}, {410, 420})
            with self.historical_engine([record], False, []) as (_, store):
                store.session_members.side_effect = PermissionError('censo ilegible')
                with self.assertRaises(PermissionError):
                    remote.open_admissions(Path(directory))
            with self.historical_engine([record], False, []) as (_, store):
                store.session_members.return_value = None
                with self.assertRaisesRegex(RuntimeError, 'inconcluso'):
                    remote.open_admissions(Path(directory))

    def test_admitir_loaded_before_cutover_blocks_without_published_claim(self):
        cli = str(Path(pwd.getpwuid(os.getuid()).pw_dir) /
                  '.local/lib/pao-deskpolicy/v1/deskpolicy/cli.py')
        alive = {410: (1, '100', 'S', ('python3', '-I', cli, 'admitir'))}
        with patch.object(remote, 'processes', return_value=alive), \
             patch.object(remote, 'open_admissions', return_value=[]), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertFalse(remote.drain(0))
        self.assertIn('1 admitir', stderr.getvalue())

    def test_close_rejection_from_historical_engine_never_becomes_success(self):
        cli_spec = importlib.util.spec_from_file_location('retired_cli', ROOT / 'cli.py')
        cli = importlib.util.module_from_spec(cli_spec)
        cli_spec.loader.exec_module(cli)
        with self.historical_engine([]) as (engine, _), \
             patch.object(cli.os, 'read', return_value=b'token-invalido'), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            engine.close.side_effect = ValueError('claim-token')
            self.assertEqual(cli.close_previous(['--request-id', 'j1', '--run-id', 'rj1',
                                                 '--pid', '410', '--claim-token-fd', '7']), 75)
        self.assertEqual(json.loads(stdout.getvalue())['detail'], 'claim-token')

    def test_jale_admitir_bash_blocks_retirement_then_closes_with_exit_7(self):
        record = {'project': 'jale', 'kind': 'chain',
                  'claim': {'state': 'active', 'lane': 1, 'script_exit_code': None}}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            real_scan = getattr(remote, 'open_admissions', None)
            cli = home / '.local/lib/pao-deskpolicy/v1/deskpolicy/cli.py'
            alive = {410: (1, '100', 'S', ('bash', '-s', 'lint', '0' * 40)),
                     411: (410, '101', 'S', ('bun', 'run', 'lint'))}
            with self.historical_engine([record]) as (engine, store), \
                 patch.object(remote.pwd, 'getpwuid', return_value=types.SimpleNamespace(pw_dir=str(home))), \
                 patch.object(remote, 'processes', return_value=alive), \
                 patch.object(remote, 'open_admissions', side_effect=lambda: real_scan(home), create=True), \
                 patch.object(remote, 'policy_units', side_effect=lambda scope: ['writer.service'] if scope == 'user' else []), \
                 patch.object(remote, 'stop_units') as stop, \
                 patch.object(remote, 'stop_processes') as kill, \
                 contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(remote.retire((ROOT / 'cli.py').read_bytes()), 2)
                self.assertIn('1 admisiones vivas (1 jale/chain)', stderr.getvalue())
                stop.assert_not_called()
                kill.assert_not_called()
                # Jale invoca cerrar después de terminar su Bash; el mismo CLI
                # instalado debe delegar al close antiguo con los fd y el token.
                module_spec = importlib.util.spec_from_file_location('installed_retired_cli', cli)
                installed = importlib.util.module_from_spec(module_spec)
                module_spec.loader.exec_module(installed)
                def close(*args, **kwargs):
                    self.assertEqual(args, ('j1', 'rj1', 410, 8, 9))
                    self.assertEqual(kwargs, dict(claim_token='a' * 64, script_exit_code=7, cause='completed'))
                    record['claim'].update(state='closed', script_exit_code=7)
                    return record
                engine.close.side_effect = close
                argv = [str(cli), 'cerrar', '--request-id', 'j1', '--run-id', 'rj1', '--pid', '410',
                        '--claim-token-fd', '7', '--script-exit-code', '7']
                with patch.object(sys, 'argv', argv), patch.object(installed.os, 'read', return_value=b'a' * 64), \
                     contextlib.redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(installed.main(), 0)
                self.assertEqual(json.loads(stdout.getvalue())['claim']['script_exit_code'], 7)
                # Segunda pasada: claim cerrada, sin tocar ni borrar registros.
                self.assertEqual(remote.retire((ROOT / 'cli.py').read_bytes()), 0)
                self.assertEqual(stop.call_count, 2)
                kill.assert_called_once()

    def test_writer_children_terminated_without_killing_reused_pid(self):
        before = {410: self.inflight()[510],
                  411: (410, '101', 'S', ('worker',)), 500: (1, '200', 'S', ('pueued',))}
        reused = {410: (1, '999', 'S', ('otro',)), 411: before[411], 500: before[500]}
        with patch.object(remote, 'processes', side_effect=[before, before, reused, reused]), \
             patch.object(remote.os, 'kill') as kill, patch.object(remote.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'vivos'):
                remote.stop_processes()
        self.assertEqual(kill.call_args_list,
                         [unittest.mock.call(410, signal.SIGTERM), unittest.mock.call(411, signal.SIGTERM),
                          unittest.mock.call(411, signal.SIGKILL)])

    def test_writer_gone_is_idempotent(self):
        with patch.object(remote, 'processes', return_value={}), patch.object(remote.os, 'kill') as kill:
            remote.stop_processes()
            remote.stop_processes()
            kill.assert_not_called()

    def test_process_identity_from_proc(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            process = proc / '410'
            process.mkdir()
            # El nombre puede contener paréntesis; starttime es el campo 22.
            fields = ['S', '123'] + ['0'] * 17 + ['987654'] + ['0'] * 5
            (process / 'stat').write_text('410 (writer (python)) ' + ' '.join(fields))
            (process / 'cmdline').write_bytes(b'python3\0/deskpolicy/cli.py\0publicar\0')
            (proc / '411').mkdir()  # Proceso que desapareció antes de leer stat.
            self.assertEqual(remote.processes(proc),
                             {410: (123, '987654', 'S', ('python3', '/deskpolicy/cli.py', 'publicar'))})

    def test_installer_rejects_linked_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'real').mkdir()
            (root / 'link').symlink_to(root / 'real', target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'enlazado'):
                remote.install(root / 'link/cli.py', b'retirado')
            self.assertFalse((root / 'real/cli.py').exists())


if __name__ == '__main__':
    unittest.main()
