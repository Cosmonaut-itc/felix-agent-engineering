import os
from pathlib import Path
import subprocess
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'solo-main.sh'
REF = 'SEN-HASO/Hasito/.github/workflows/gate-noche.yml@refs/heads/main'


class SoloMain(unittest.TestCase):
    def correr(self, cambios):
        entorno = dict(os.environ, GITHUB_REPOSITORY='SEN-HASO/Hasito',
                       GITHUB_WORKFLOW_REF=REF, GITHUB_EVENT_NAME='schedule')
        for variable, valor in cambios.items():
            if valor is None:
                entorno.pop(variable)
            else:
                entorno[variable] = valor
        return subprocess.run(['bash', str(SCRIPT)], env=entorno, text=True, capture_output=True)

    def test_acepta_eventos_autorizados_sin_salida(self):
        for evento in ('schedule', 'workflow_dispatch'):
            with self.subTest(evento=evento):
                resultado = self.correr({'GITHUB_EVENT_NAME': evento})
                self.assertEqual(resultado.returncode, 0)
                self.assertEqual(resultado.stdout + resultado.stderr, '')

    def test_rechaza_y_nombra_el_valor(self):
        casos = [
            ('GITHUB_WORKFLOW_REF', REF.replace('/main', '/otra')),
            ('GITHUB_EVENT_NAME', 'push'),
            ('GITHUB_EVENT_NAME', 'pull_request'),
            ('GITHUB_WORKFLOW_REF', REF.replace('gate-noche.yml', 'otro.yml')),
            ('GITHUB_REPOSITORY', 'SEN-HASO/Jale'),
        ]
        casos += [(variable, valor) for variable in
                  ('GITHUB_REPOSITORY', 'GITHUB_WORKFLOW_REF', 'GITHUB_EVENT_NAME')
                  for valor in (None, '')]
        for variable, valor in casos:
            with self.subTest(variable=variable, valor=valor):
                resultado = self.correr({variable: valor})
                self.assertEqual(resultado.returncode, 1)
                self.assertEqual(resultado.stdout, '')
                self.assertEqual(len(resultado.stderr.splitlines()), 1)
                self.assertIn(variable, resultado.stderr)
                self.assertIn('<ausente>' if valor is None else valor, resultado.stderr)


if __name__ == '__main__':
    unittest.main()
