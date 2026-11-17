import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('guardia', Path(__file__).parents[1] / 'guardia.py')
g = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = g
spec.loader.exec_module(g)


class GuardiaTest(unittest.TestCase):
    def decidir(self, mem=1024, cpu=95, estado=g.Estado(42), pid=42, ahora=10000):
        return g.decidir(g.Muestra(pid, mem, cpu), estado, g.Umbrales(), ahora)

    def test_footprint(self):
        self.assertEqual(self.decidir(mem=8 * 1024 ** 3)[0], 'reiniciar')

    def test_menos_de_dieciocho(self):
        accion, estado, _ = self.decidir(estado=g.Estado(42, 16))
        self.assertEqual((accion, estado.consecutivas), ('observar', 17))

    def test_dieciocho(self):
        self.assertEqual(self.decidir(estado=g.Estado(42, 17))[0], 'reiniciar')

    def test_pid_nuevo(self):
        accion, estado, _ = self.decidir(estado=g.Estado(41, 17))
        self.assertEqual((accion, estado.consecutivas, estado.pid), ('observar', 1, 42))

    def test_cpu_baja(self):
        self.assertEqual(self.decidir(cpu=89, estado=g.Estado(42, 17))[1].consecutivas, 0)

    def test_enfriamiento(self):
        accion, _, motivo = self.decidir(mem=61 * 1024 ** 3, estado=g.Estado(42, 17, 9999))
        self.assertEqual((accion, motivo), ('observar', 'enfriamiento'))
        self.assertEqual(self.decidir(mem=8 * 1024 ** 3, estado=g.Estado(42, 0, 6400))[0], 'reiniciar')

    def test_unidades(self):
        for texto, esperado in [('61G', 61 * 1024 ** 3), ('3504K', 3504 * 1024),
                                ('4902M+', 4902 * 1024 ** 2), ('3.5M-', 3670016)]:
            self.assertEqual(g.memoria_bytes(texto), esperado)

    def test_segunda_muestra(self):
        texto = 'Processes: 400\nPID MEM %CPU\n42 3504K 0.2\nProcesses: 401\nPID MEM %CPU\n42 61G 99.1\n'
        self.assertEqual(g.parsear_top(texto, 42), g.Muestra(42, 61 * 1024 ** 3, 99.1))

    def test_top_incompleto(self):
        with self.assertRaises(ValueError):
            g.parsear_top('42 3504K 0.2', 42)

    def test_estado_atomico(self):
        with tempfile.TemporaryDirectory() as temporal:
            ruta = Path(temporal) / 'estado.json'
            g.guardar_estado(ruta, g.Estado(42, 3, 100))
            self.assertEqual(ruta.read_text(), '{"pid": 42, "consecutivas": 3, "ultimo_reinicio": 100}')
            self.assertEqual(len(list(ruta.parent.iterdir())), 1)

    def test_dry_run_no_mata_ni_escribe(self):
        with tempfile.TemporaryDirectory() as temporal:
            ruta = Path(temporal) / 'estado.json'
            comandos = []
            def comando(args):
                comandos.append(args[0])
                return '42 61G 95\n42 61G 95\n'
            self.assertEqual(g.pasar(ruta, g.Umbrales(), True, comando,
                                     lambda: 42, lambda: 10000, lambda s: None, lambda s: None), 0)
            self.assertEqual(comandos, ['top'])
            self.assertFalse(ruta.exists())

    def test_lectura_fallida_no_actua(self):
        with tempfile.TemporaryDirectory() as temporal:
            ruta = Path(temporal) / 'estado.json'
            comandos = []
            def comando(args):
                comandos.append(args[0])
                return 'sin muestras'
            self.assertEqual(g.pasar(ruta, g.Umbrales(), False, comando,
                                     lambda: 42, lambda: 10000, lambda s: None, lambda s: None), 1)
            self.assertEqual(comandos, ['top'])
            self.assertFalse(ruta.exists())

    def test_reinicio_confirma_pid_y_estado(self):
        with tempfile.TemporaryDirectory() as temporal:
            ruta = Path(temporal) / 'estado.json'
            pids = iter([42, 43])
            comandos, lineas = [], []
            def comando(args):
                comandos.append(args[0])
                return '42 61G 95\n42 61G 95\n'
            self.assertEqual(g.pasar(ruta, g.Umbrales(), False, comando,
                                     lambda: next(pids), lambda: 10000, lambda s: None, lineas.append), 0)
            self.assertEqual(comandos, ['top', 'killall'])
            self.assertIn('"pid": 43', ruta.read_text())
            self.assertIn('pid_nuevo=43', lineas[0])

    def test_reinicio_sin_pid_nuevo_conserva_enfriamiento(self):
        with tempfile.TemporaryDirectory() as temporal:
            ruta = Path(temporal) / 'estado.json'
            lineas = []
            self.assertEqual(g.pasar(ruta, g.Umbrales(), False,
                                     lambda args: '42 61G 95\n42 61G 95\n',
                                     lambda: 42, lambda: 10000, lambda s: None, lineas.append), 1)
            self.assertIn('"ultimo_reinicio": 10000', ruta.read_text())
            self.assertIn('no se confirmó pid nuevo', lineas[0])


if __name__ == '__main__':
    unittest.main()
