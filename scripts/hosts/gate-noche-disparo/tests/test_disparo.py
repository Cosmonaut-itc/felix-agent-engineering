import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest

BASE = Path(__file__).parents[1]
SCRIPT = BASE / 'disparar.sh'


class DisparoTest(unittest.TestCase):
    def preparar(self, codigos, estado='active'):
        temporal = tempfile.TemporaryDirectory()
        raiz = Path(temporal.name)
        contador = raiz / 'contador'
        llamadas = raiz / 'llamadas'
        gh = raiz / 'gh'
        gh.write_text(
            '#!/bin/sh\n'
            f"printf '%s\\n' \"$*\" >> '{llamadas}'\n"
            f"if [ \"$1\" = api ]; then echo '{estado}'; exit {0 if estado != 'error' else 1}; fi\n"
            f"n=$(wc -l < '{llamadas}')\n"
            f"case $n in " + ' '.join(f'{i}) exit {codigo};;' for i, codigo in enumerate(codigos, 1)) + ' *) exit 1;; esac\n'
        )
        gh.chmod(0o755)
        env = os.environ.copy()
        env.update({'GATE_NOCHE_GH': str(gh), 'GATE_NOCHE_ESPERA': '0'})
        return temporal, env, llamadas

    def ejecutar(self, codigos, args=(), estado='active'):
        temporal, env, llamadas = self.preparar(codigos, estado)
        try:
            resultado = subprocess.run(['/bin/bash', str(SCRIPT), *args], env=env,
                                       text=True, capture_output=True, check=False)
            lineas = llamadas.read_text().splitlines() if llamadas.exists() else []
            return resultado, lineas
        finally:
            temporal.cleanup()

    def test_exito_al_primer_intento(self):
        resultado, llamadas = self.ejecutar([0])
        self.assertEqual(resultado.returncode, 0)
        self.assertEqual(llamadas, ['workflow run gate-noche.yml --repo SEN-HASO/Hasito --ref main'])

    def test_dos_fallos_y_exito(self):
        resultado, llamadas = self.ejecutar([1, 1, 0])
        self.assertEqual(resultado.returncode, 0)
        self.assertEqual(len(llamadas), 3)

    def test_tres_fallos(self):
        resultado, llamadas = self.ejecutar([1, 1, 1])
        self.assertEqual(resultado.returncode, 1)
        self.assertEqual(len(llamadas), 3)

    def test_en_seco_activo_no_despacha(self):
        resultado, llamadas = self.ejecutar([], ['--en-seco'])
        self.assertEqual(resultado.returncode, 0)
        self.assertEqual(llamadas, ['api repos/SEN-HASO/Hasito/actions/workflows/gate-noche.yml --jq .state'])

    def test_en_seco_estado_inactivo_o_error(self):
        for estado in ('disabled', 'error'):
            with self.subTest(estado=estado):
                resultado, _ = self.ejecutar([], ['--en-seco'], estado)
                self.assertEqual(resultado.returncode, 1)

    def test_argumento_desconocido(self):
        resultado, llamadas = self.ejecutar([0], ['--otro'])
        self.assertEqual(resultado.returncode, 2)
        self.assertEqual(llamadas, [])

    def test_plist_calendario_y_rutas(self):
        with (BASE / 'com.cosmonaut.gate-noche-disparo.plist').open('rb') as archivo:
            datos = plistlib.load(archivo)
        self.assertEqual(datos['StartCalendarInterval']['Hour'], 1)
        self.assertEqual(datos['StartCalendarInterval']['Minute'], 35)
        self.assertNotIn('RunAtLoad', datos)
        self.assertIn('__HOME__', datos['ProgramArguments'][1])
        self.assertIn('__HOME__', datos['StandardOutPath'])
        self.assertIn('__HOME__', datos['StandardErrorPath'])


if __name__ == '__main__':
    unittest.main()
