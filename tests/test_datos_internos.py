import importlib.util
from pathlib import Path
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
                ("'/c/Users/Demo User/AppData/Local'", 'home de macOS'),
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
                '/Users/<usuario>/bin, <home>/bin y /Users/u/run.sh',
                '/c/Users/<usuario>/AppData y /c/Users/U/AppData',
                r'C:\Users\<usuario>\AppData y C:\Users\u\AppData',
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
                '/System/Volumes/Data/Users/DemoUser/bin',
                r'"\/Users\/DemoUser\/bin"'):
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
                ('/Volumes/Disco/Users/DemoUser', 'macOS'),
                ('/System/Volumes/Data/Users/DemoUser', 'macOS'),
                ('file://localhost/Users/DemoUser', 'macOS'),
                (r'\\host.example\c$\Users\DemoUser', 'Windows'),
                (r'"C:\Users\Nombre Con Espacio"', 'Windows'),
                (r'"C:\Users\Public Lab"', 'Windows'),
                (r'local\Users\DemoUser', 'macOS'),
                (r'prefix//uSeRs/\DemoUser/bin', 'macOS'),
                (r'clang -IC:\//Users\\DemoUser', 'Windows'),
                (r'\\host.example/c$\Users/DemoUser', 'Windows'),
                (r'"file://localhost/Users/Shared Fixture"', 'macOS'),
                ('$HOME/Users/demo/bin', 'macOS'),
                ('/tmp/Users/demo/bin', 'macOS')):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), ['home de ' + platform])

    def test_allows_public_profiles_and_prose_after_synthetic_profiles(self):
        for text in (
                '/Users/Shared/work',
                r'C:\Users\Public\Documents',
                r'El perfil C:\Users\u usa un marcador',
                'C:/Users/u y /c/Users/<usuario>',
                '/c/Users/u y otra cosa',
                r'C:\Users\u '):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), [])

    def test_profile_boundaries(self):
        self.assertEqual(kinds(r'"C:\Users\u example\bin"'), ['home de Windows'])
        self.assertEqual(kinds('/Users/SharedFixture/x'), ['home de macOS'])
        self.assertEqual(kinds(r'C:\Users\PublicFixture\x'), ['home de Windows'])
        self.assertEqual(kinds(r'/Users/DemoUser/x C:\Users\DemoUser\x'),
                         ['home de macOS', 'home de Windows'])
        for text in ('HOME=~demo/bin', r'//host.example/c$/Users/U/bin'):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), [])


if __name__ == '__main__':
    unittest.main()
