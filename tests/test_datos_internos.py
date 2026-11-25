import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

_spec = importlib.util.spec_from_file_location('datos_internos', Path(__file__).with_name('datos_internos.py'))
datos = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(datos)


def kinds(text):
    return [kind for _, kind in datos.findings(text)]


class DatosInternosTests(unittest.TestCase):
    def test_rejects_concrete_access_data(self):
        for text, kind in [
                ('http://mini.tail0a1b2c.ts.net:8788/api/rack.json', 'FQDN de tailnet'),
                ('`casa.ts.net`', 'FQDN de tailnet'),
                ('from="100.64.1.2",restrict', 'IP de tailnet'),
                ('ve 100.127.255.254 como origen.', 'IP de tailnet'),
                ("('100.101.0.3', 8787)", 'IP de tailnet'),
                ('/usr/bin/python3 -I /home/ana/.local/bin/x', 'home de usuario'),
                ('`HOME=/home/ana`', 'home de usuario'),
                ("'bash /home/ana/run.sh'", 'home de usuario'),
                ('HOME=/Users/demo.user/bin', 'home de macOS'),
                ('</Users/DemoUser>', 'home de macOS'),
                ('file:///Users/DemoUser/Library', 'home de macOS'),
                ("'/c/Users/Demo User/AppData/Local'", 'home de Windows'),
                ('C:/Users/DemoUser/AppData/Local', 'home de Windows'),
                (r'C:\Users\Demo-User\AppData\Local', 'home de Windows'),
                (r'clang -IC:\Users\DemoUser\include', 'home de Windows'),
                ('"AllowedIPs": ["100.64.1.2/32"]', 'IP de tailnet'),
                ('HTTP://MINI.TAIL0A1B2C.TS.NET:8788', 'FQDN de tailnet'),
                ('wsl -d Ubuntu -u ana -- ~/.local/bin/x', 'cuenta de WSL'),
                ('wsl.exe --user ana --exec true', 'cuenta de WSL')]:
            with self.subTest(text=text):
                self.assertEqual(kinds(text), [kind])

    def test_allows_markers_ranges_and_synthetic_values(self):
        for text in [
                'http://<host>.<tailnet>.ts.net:8788/api/rack.json',
                'un nombre .ts.net de Tailscale',
                'mini.example.ts.net:8787',
                'from="<ip-tailnet-mini>",restrict',
                'bind en 100.64.0.0/10 o 127.0.0.1',
                "('100.100.0.1', 8787) y 100.100.0.255",
                '100.63.255.255 y 100.128.0.0 quedan fuera',
                'versión 1.100.64.2.3',
                '/home/<usuario>/.local/bin/x y /home/u/run.sh',
                '/Users/<usuario>/bin, <home>/bin y /Users/|',
                '/c/Users/<usuario>/AppData',
                r'C:\Users\<usuario>\AppData',
                'state/project/home/fixtures',
                "re.compile(r'/Users/|/home/|https?://')",
                '~/.local/bin/pueue y $HOME/home/x',
                '100.100.0.0/24 y 100.101.0.0/16',
                'MINI.EXAMPLE.TS.NET',
                'wsl -d Ubuntu -u <usuario> -- /usr/bin/true',
                'wsl -u root -- true y wsl -d Ubuntu -- sort -u archivo']:
            with self.subTest(text=text):
                self.assertEqual(kinds(text), [])

    def test_reports_line_numbers_without_values(self):
        found = datos.findings('primera\nhttp://a.tail0.ts.net\n/home/ana 100.64.0.9\n')
        self.assertEqual(found, [(2, 'FQDN de tailnet'), (3, 'IP de tailnet'), (3, 'home de usuario')])

    def test_rejects_alternate_mac_homes(self):
        for text in (
                '/users/DemoUser/bin',
                'file://localhost/Users/DemoUser/bin',
                'clang -I/Users/DemoUser/include',
                'rsync host.example:22:/Users/DemoUser/bin',
                '/System/Volumes/Data/Users/DemoUser/bin'):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), ['home de macOS'])

    def test_rejects_windows_admin_shares(self):
        for text in (r'\\host.example\c$\Users\DemoUser\bin',
                     '//host.example/c$/Users/DemoUser/bin'):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), ['home de Windows'])

    def test_profiles_follow_the_segment_and_its_immediate_prefix(self):
        for text, platform in (
                ('clang -IC:/Users/DemoUser/include', 'Windows'),
                ('-IC:/Users/Shared/include', 'Windows'),
                ('-IC://Users/Shared/include', 'Windows'),
                ('/Volumes/Disco/Users/DemoUser', 'macOS'),
                ('/System/Volumes/Data/Users/DemoUser', 'macOS'),
                ('file://localhost/Users/DemoUser', 'macOS'),
                (r'\\host.example\c$\Users\DemoUser', 'Windows'),
                (r'"C:\Users\Nombre Con Espacio"', 'Windows'),
                (r'"C:\Users\Public Lab"', 'Windows'),
                (r'local\Users\DemoUser', 'Windows'),
                (r'prefix//uSeRs/\DemoUser/bin', 'macOS'),
                (r'clang -IC:\//Users\\DemoUser', 'Windows'),
                (r'"file://localhost/Users/Shared Fixture"', 'macOS'),
                ('$HOME/Users/demo/bin', 'macOS'),
                ('/tmp/Users/demo/bin', 'macOS')):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), ['home de ' + platform])

    def test_rejects_profiles_regardless_of_name(self):
        for text, platform in (
                ('/Users/Shared/x', 'macOS'),
                (r'C:\Users\Public\x', 'Windows'),
                ('/Users/u/x', 'macOS'),
                (r'El perfil C:\Users\u usa un marcador', 'Windows'),
                ('C:/Users/u y /c/Users/<usuario>', 'Windows'),
                ('/c/Users/u y otra cosa', 'Windows'),
                (r'C:\Users\u ', 'Windows'),
                (r'\\?\C:\Users\Public', 'Windows'),
                ('file:///C:/Users/Public/x', 'Windows')):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), ['home de ' + platform])

    def test_profile_boundaries(self):
        self.assertEqual(kinds(r'"C:\Users\u example\bin"'), ['home de Windows'])
        self.assertEqual(kinds('/Users/SharedFixture/x'), ['home de macOS'])
        self.assertEqual(kinds(r'C:\Users\PublicFixture\x'), ['home de Windows'])
        self.assertEqual(kinds(r'/Users/DemoUser/x C:\Users\DemoUser\x'),
                         ['home de macOS', 'home de Windows'])
        self.assertEqual(kinds(r'\\host.example/c$\Users/DemoUser'),
                         ['home de macOS', 'home de Windows'])
        self.assertEqual(kinds(r'//host.example/c$/Users/U/bin'), ['home de Windows'])
        for text in ('HOME=~demo/bin', 'Users list', 'Users/DemoUser', '/Users/<usuario>/x',
                     "r'/Users/|/home/'", '/Users/', '/SuperUsers/DemoUser'):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), [])

    def test_profiles_with_punctuation_at_start_of_name(self):
        for text, platform in (('/Users/-demo/x', 'macOS'),
                               (r'C:\Users\.demo\x', 'Windows'),
                               ("/Users/'demo/x", 'macOS')):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), ['home de ' + platform])

    def test_profiles_in_paths_from_the_reviews(self):
        for text, platform in (
                ('/c/Users/DemoUser/bin', 'Windows'),
                ('/c/Users/Shared/bin', 'Windows'),
                ('/c/Users/Public/bin', 'Windows'),
                ('/mnt/c/Users/Shared/bin', 'Windows'),
                ('/mnt/C/Users/pUbLiC/bin', 'Windows'),
                ('clang -isystemC://Users/Shared/include', 'Windows'),
                ('clang -isystemC://Users/Public/include', 'Windows'),
                ('prefix//Users/Shared/bin', 'macOS'),
                ('https://Users/Shared/bin', 'macOS'),
                ('/Volumes/Disco/Users/Shared/bin', 'macOS'),
                ('/System/Volumes/Data/Users/Shared/bin', 'macOS'),
                ('file:///Users/Shared/bin', 'macOS'),
                ('file://localhost/Users/Shared/bin', 'macOS'),
                ('file:///C:/Users/DemoUser/AppData', 'Windows'),
                (r'\\host.example\c$\Users\Public\bin', 'Windows'),
                ('/Users/Shared./bin', 'macOS'),
                ('/Users/Shared%2E/bin', 'macOS'),
                ('/Users/Shared+Lab/bin', 'macOS'),
                (r'"C:\Users\Public Lab"', 'Windows'),
                (r'cp C:/Users/Public\ Lab/AppData .', 'Windows'),
                (r'"/Users/Shared"', 'macOS'),
                ('/Users/sHaReD', 'macOS'),
                (r'C:\Users\Public Lab', 'Windows'),
                ('cp "/c/Users/Public"Lab/AppData .', 'Windows'),
                ("cp '/Users/Shared'Lab/Library .", 'macOS'),
                (r'cp /c/Users/Public\&Lab/AppData .', 'Windows')):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), ['home de ' + platform])

    def test_old_marker_does_not_exempt_profiles(self):
        text = 'C:/Users/Demo %2FUsers%2FDemo # datos-internos: sintético'
        self.assertEqual(kinds(text), ['home de macOS', 'home de Windows'])

    def test_profiles_in_raw_percent_encoded_and_json_forms(self):
        for text, expected in (
                ('file:///C:%5CUsers%5CDemoUser%5CAppData', ['Windows']),
                ('file:///C:/Users/Public%20Lab/AppData', ['Windows']),
                ('C:%5cUsers%2fPublic%20Lab', ['Windows']),
                ('C:%5cUsers%2fPublic%20Lab%5CAppData', ['Windows']),
                ('C:%5cUsers%2fDemoUser%5CAppData', ['Windows']),
                ('C:%5cUsers%2fPublic%5CAppData', ['Windows']),
                ('%2fUsers%2FDemoUser/bin', ['macOS']),
                ('%255cUsers%255CDemoUser/bin', ['Windows']),
                ('%2525252FUsers%2525252FDemoUser/bin', ['macOS']),
                ('copy%20/c/Users/DemoUser/bin', ['macOS', 'Windows']),
                (r'{"path":"C:\\Users\\Public Lab\\AppData"}', ['Windows']),
                (r'{"path":"\/Users\/Shared Lab\/Library"}', ['macOS', 'Windows']),
                (r'"\/Users\/DemoUser\/bin"', ['macOS', 'Windows']),
                (r'{"path":"C:\\Users\\Public\u0020Lab\\AppData"}', ['Windows']),
                (r'{"command":"echo \"C:\\Users\\Public Lab\""}', ['Windows']),
                (r'{"command":"echo \"/Users/Shared Lab/Library\""}', ['macOS']),
                (r'{"path":"\u002fUsers\u002fDemoUser/Library"}', ['macOS']),
                (r'{"path":"\u002fUsers\u002fDemoUser\q"}', ['macOS']),
                (r'{"path":"C:\u005cUsers\u005cPublic\u0020Lab\u005cAppData"}', ['Windows']),
                (r'{"path":"\u002fUsers\u002fShared Lab/Library"}', ['macOS']),
                (r'{"path":"C:\u005cUsers%5CDemoUser\u005cAppData"}', ['Windows']),
                (r'{"p":"\u00252FUsers%2FDemo%2Fx"}', ['macOS']),
                (r'{"path":"\u00252FUsers%2FDemoUser%2FLibrary"}', ['macOS']),
                (r'"\u0022\u005cu002fUsers\u005cu002fDemoUser/Library\u0022"', ['macOS']),
                (r'"\\u002fUsers\\u002fDemoUser"', ['macOS']),
                (r'/Users/\ud801\udc00/bin', ['macOS']),
                (r'\ud801 /Users/Demo/bin', ['macOS']),
                ('%ZZ %FF /Users/Demo/bin', ['macOS']),
                ('%FF %2FUsers%2FDemo%2Fx', ['macOS']),
                (r'HOME=/Users/DemoUser %2FUsers%2fDemoUser "C:\\Users\\DemoUser"',
                 ['macOS', 'Windows'])):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), ['home de ' + platform for platform in expected])

    def test_stops_after_four_decode_passes(self):
        self.assertEqual(kinds('%252525252FUsers%252525252FDemoUser/bin'), [])


class DatosInternosCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'repo'
        self.fleet = self.root / 'fleet'
        self.fleet.mkdir(parents=True)
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1')
        self.git('init', '-q')

    def git(self, *args):
        subprocess.run(['git', *args], cwd=self.root, env=self.env, check=True, capture_output=True)

    def assert_cli(self, expected):
        self.git('add', 'fleet')
        rule = Path(datos.__file__).resolve()
        result = subprocess.run([sys.executable, '-I', '-B', str(rule)], cwd=self.root,
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, int(bool(expected)))
        self.assertEqual(result.stdout.splitlines(), expected)
        if expected:
            self.assertIn(f'{len(expected)} dato(s) interno(s)', result.stderr)
            self.assertNotIn('Demo', result.stdout + result.stderr)
        else:
            self.assertEqual(result.stderr, '')

    def test_reads_utf16_profiles(self):
        text = 'cabecera\nC:\\Users\\Demo\\x\n'
        for encoding, bom in (('utf-16-le', b'\xff\xfe'), ('utf-16-le', b''),
                              ('utf-16-be', b'\xfe\xff'), ('utf-16-be', b'')):
            with self.subTest(encoding=encoding, bom=bom):
                (self.fleet / 'fixture').write_bytes(bom + text.encode(encoding))
                self.assert_cli(['fleet/fixture:2: home de Windows'])

    def test_reads_nul_separated_paths(self):
        (self.fleet / 'fixture').write_bytes(b'a\0/Users/Demo/x\0')
        self.assert_cli(['fleet/fixture:2: home de macOS'])

    def test_reports_profile_link_text_with_existing_or_missing_target(self):
        (self.fleet / 'absolute-link').symlink_to('/Users/Demo/x')
        target = self.root.parent / 'Users' / 'Demo' / 'x'
        target.parent.mkdir(parents=True)
        (self.fleet / 'temp-link').symlink_to(target)
        for exists in (False, True):
            with self.subTest(target_exists=exists):
                if exists:
                    target.write_text('contenido limpio', encoding='utf-8')
                self.assert_cli(['fleet/absolute-link:1: home de macOS',
                                 'fleet/temp-link:1: home de macOS'])

    def test_does_not_follow_links_to_files_or_directories(self):
        target = self.root.parent / 'untracked'
        target.write_text('/Users/Demo/x', encoding='utf-8')
        (self.fleet / 'file-link').symlink_to(target)
        (self.fleet / 'directory-link').symlink_to(target.parent)
        self.assert_cli([])


if __name__ == '__main__':
    unittest.main()
