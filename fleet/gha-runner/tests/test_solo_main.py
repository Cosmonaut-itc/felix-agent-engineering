from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'solo-main.sh'
REF = 'SEN-HASO/Hasito/.github/workflows/gate-noche.yml@refs/heads/main'


class SoloMain(unittest.TestCase):
    # Al rechazar, el hook termina al primer ancestro llamado Runner.Worker. En CI estas
    # pruebas corren dentro de uno real, así que cada rechazo corre bajo uno de mentira
    # o bajo más procesos intermedios de los que el hook recorre.
    @classmethod
    def setUpClass(cls):
        cls.carpeta = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.carpeta.cleanup)
        cls.worker = Path(cls.carpeta.name) / 'Runner.Worker'
        shutil.copy2(shutil.which('bash'), cls.worker)

    def entorno(self, cambios):
        entorno = dict(os.environ, GITHUB_REPOSITORY='SEN-HASO/Hasito',
                       GITHUB_WORKFLOW_REF=REF, GITHUB_EVENT_NAME='schedule')
        for variable, valor in cambios.items():
            if valor is None:
                entorno.pop(variable)
            else:
                entorno[variable] = valor
        return entorno

    def como_worker(self, cambios):
        # El runner lanza el hook desde Runner.Worker y después sigue con los pasos del trabajo.
        return subprocess.run([str(self.worker), '-c', f'bash "{SCRIPT}"; echo paso siguiente'],
                              env=self.entorno(cambios), text=True, capture_output=True)

    def test_acepta_eventos_autorizados_y_el_trabajo_sigue(self):
        for evento in ('schedule', 'workflow_dispatch'):
            with self.subTest(evento=evento):
                resultado = self.como_worker({'GITHUB_EVENT_NAME': evento})
                self.assertEqual(resultado.returncode, 0, resultado.stderr)
                self.assertEqual(resultado.stdout, 'paso siguiente\n')
                self.assertEqual(resultado.stderr, '')

    def test_rechaza_nombra_el_valor_y_detiene_al_worker(self):
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
        with ThreadPoolExecutor(len(casos)) as hilos:
            resultados = list(hilos.map(lambda caso: self.como_worker({caso[0]: caso[1]}), casos))
        for (variable, valor), resultado in zip(casos, resultados):
            with self.subTest(variable=variable, valor=valor):
                self.assertEqual(resultado.returncode, -signal.SIGKILL)
                self.assertEqual(resultado.stdout, '')
                mostrado = '<ausente>' if valor is None else valor
                self.assertEqual(resultado.stderr,
                                 f'gha-runner: {variable} no aceptado: {mostrado}\n')

    def test_sin_worker_entre_sus_ancestros_avisa_y_sale_con_1(self):
        # El hook recorre 6 ancestros; con 8 intermedios nunca llega a los de esta prueba.
        orden = f'bash "{SCRIPT}"'
        for _ in range(8):
            orden = f'bash -c {shlex.quote(orden + "; exit $?")}'
        resultado = subprocess.run(['bash', '-c', orden + '; exit $?'],
                                   env=self.entorno({'GITHUB_EVENT_NAME': 'push'}),
                                   text=True, capture_output=True)
        self.assertEqual(resultado.returncode, 1)
        self.assertEqual(resultado.stdout, '')
        rechazo, aviso = resultado.stderr.splitlines()
        self.assertEqual(rechazo, 'gha-runner: GITHUB_EVENT_NAME no aceptado: push')
        self.assertIn('no se encontró el proceso del trabajo', aviso)


if __name__ == '__main__':
    unittest.main()
