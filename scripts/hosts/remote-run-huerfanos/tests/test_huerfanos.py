import importlib.util
from pathlib import Path
import signal
import sys
import unittest

spec = importlib.util.spec_from_file_location('huerfanos', Path(__file__).parents[1] / 'huerfanos.py')
h = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = h
spec.loader.exec_module(h)
ID = '20261111-123456-abc123'
VIEJA = 'sh -c cd remote-run/runs/' + ID + ' || exit 3; while [ ! -f exit ]; do sleep .2; done; cat log; exit $(cat exit)'
NUEVA = '/bin/zsh -c cd remote-run/runs/' + ID + ' || exit 3\nif [ -f pueue.py ]; then\n  python3 pueue.py wait\nfi'


class HuerfanosTest(unittest.TestCase):
    def procesos(self, args=VIEJA, edad='02:00:00', padre=20):
        return h.parsear_instantanea(f'10 {padre} {edad} {args}\n20 30 02:00:01 /bin/bash /usr/local/bin/remote-run wait {ID}\n30 40 02:00:02 remote-run wait {ID}\n40 1 7-01:02:03 ssh mini\n')

    def ejecutar(self, procesos=None, existe=lambda i: False, dry=False, vivo=lambda p: False):
        senales, esperas, lineas = [], [], []
        def enviar(pid, senal):
            senales.append((pid, senal))
            return 'enviada'
        total = h.ejecutar(self.procesos() if procesos is None else procesos,
                           existe, enviar, vivo, esperas.append, lambda: 'fecha', lineas.append, dry)
        return total, senales, esperas, lineas

    def test_vieja_y_cadena(self):
        total, senales, esperas, _ = self.ejecutar()
        self.assertEqual(total, 3)
        self.assertEqual(senales, [(pid, signal.SIGTERM) for pid in (10, 20, 30)])
        self.assertEqual(esperas, [5])

    def test_nueva_ppid_uno(self):
        procesos = h.parsear_instantanea('10 1 11:00 ' + NUEVA)
        self.assertIn('\nif [ -f pueue.py ]', procesos[0].args)
        self.assertEqual(self.ejecutar(procesos)[1], [(10, signal.SIGTERM)])

    def test_directorio_existente(self):
        self.assertEqual(self.ejecutar(existe=lambda i: True)[1], [])

    def test_joven_y_limite(self):
        for edad in ('09:59', '10:00'):
            self.assertEqual(self.ejecutar(self.procesos(edad=edad))[1], [])

    def test_ancestro_ajeno(self):
        procesos = [h.Proceso(10, 40, 700, VIEJA), h.Proceso(40, 1, 900, 'ssh mini')]
        self.assertEqual(self.ejecutar(procesos)[1], [(10, signal.SIGTERM)])

    def test_reaparicion(self):
        respuestas = iter([False, True, True, True])
        self.assertEqual(self.ejecutar(existe=lambda i: next(respuestas))[1], [])

    def test_sobrevive_term(self):
        _, senales, _, _ = self.ejecutar(vivo=lambda p: True)
        self.assertEqual(senales, [(p, signal.SIGTERM) for p in (10, 20, 30)] +
                         [(p, signal.SIGKILL) for p in (10, 20, 30)])

    def test_dry_run(self):
        total, senales, esperas, lineas = self.ejecutar(dry=True)
        self.assertEqual((total, senales, esperas), (3, [], []))
        self.assertIn('simulado', lineas[0])

    def test_reaparece_durante_espera_term(self):
        existe = [False]
        senales = []
        def enviar(pid, senal):
            senales.append(senal)
            return 'enviada'
        h.ejecutar([h.Proceso(10, 1, 700, VIEJA)], lambda i: existe[0],
                   enviar, lambda pid: True, lambda s: existe.__setitem__(0, True),
                   lambda: 'fecha', lambda s: None)
        self.assertEqual(senales, [signal.SIGTERM])

    def test_no_shell(self):
        self.assertEqual(self.ejecutar([h.Proceso(10, 1, 700, 'echo ' + VIEJA)])[1], [])

    def test_edad_dias(self):
        self.assertEqual(h.edad_segundos('7-01:02:03'), 608523)


if __name__ == '__main__':
    unittest.main()
