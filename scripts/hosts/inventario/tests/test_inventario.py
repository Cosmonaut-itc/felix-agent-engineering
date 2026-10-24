import contextlib
import io
import json
import os
import subprocess
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import inventario as inv


class InventarioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'runs').mkdir()
        (self.root / 'work').mkdir()
        self.now = 1800000000
        self.procesos = {}

    def vieja(self, path, dias):
        epoch = self.now - dias * 86400
        os.utime(path, (epoch, epoch))
        return epoch

    def copia(self, name='copia', dias=8):
        p = self.root / 'work' / name
        p.mkdir()
        self.vieja(p, dias)
        return p

    def corrida(self, name='20261001-120000-abcdef', dias=31, pid=None,
                started=True, fin=False, mirror='copia'):
        p = self.root / 'runs' / name
        p.mkdir()
        if mirror is not None:
            (p / 'meta').write_text('src=/src\nhost=mini\nmirror=remote-run/work/'
                                    + mirror + '\nsub=.\ncmd=true\n')
        if pid is not None:
            (p / 'pid').write_text(str(pid))
        if started:
            (p / 'started').write_text(str(self.now - dias * 86400))
        if fin:
            (p / 'exit').write_text('0')
        self.vieja(p, dias)
        return p

    def inventario(self, **kwargs):
        with patch.object(inv, 'consultar_inicio', side_effect=lambda pid: self.procesos[pid]):
            return inv.inventariar(self.root, ahora=self.now, **kwargs)

    def test_clases_corridas_y_frontera(self):
        for dias, borrable in [(29.99, False), (30, False), (30.01, True)]:
            with self.subTest(dias=dias):
                p = self.corrida(name=str(dias), dias=dias, fin=True)
                item = next(x for x in self.inventario()['corridas'] if x['ruta'] == str(p))
                self.assertEqual((item['clase'], item['borrable']), ('terminal', borrable))
        self.corrida(name='viva', pid=12)
        self.procesos[12] = self.now - 31 * 86400 + 120
        self.corrida(name='dudosa', pid=13, started=False)
        clases = {Path(x['ruta']).name: x['clase'] for x in self.inventario()['corridas']}
        self.assertEqual(clases['viva'], 'viva')
        self.assertEqual(clases['dudosa'], 'dudosa')

    def test_copias_frontera_y_ocultas(self):
        for name, dias in [('corta', 6.99), ('justa', 7), ('.oculta', 8)]:
            self.copia(name, dias)
        items = {Path(x['ruta']).name: x for x in self.inventario()['copias']}
        self.assertEqual(items['corta']['clase'], 'reciente')
        self.assertFalse(items['corta']['borrable'])
        for name in ('justa', '.oculta'):
            self.assertEqual(items[name]['clase'], 'inactiva')
            self.assertTrue(items[name]['borrable'])

    def test_sen_3284(self):
        p = self.copia()
        (p / 'phasefailed').write_text('fallo cliente')
        self.vieja(p, 40)
        self.corrida(pid=12)
        self.procesos[12] = self.now - 31 * 86400
        self.assertEqual(self.inventario()['copias'][0]['clase'], 'en-uso')
        (self.root / 'runs/20261001-120000-abcdef/started').unlink()
        item = self.inventario()['copias'][0]
        self.assertEqual(item['clase'], 'protegida-dudosa')
        self.assertFalse(item['borrable'])

    def test_reuso_pid_y_stop(self):
        self.corrida(name='reuso', pid=12)
        self.procesos[12] = self.now
        self.corrida(name='stop', pid=13)
        self.procesos[13] = None
        self.assertTrue(all(x['clase'] == 'terminal' for x in self.inventario()['corridas']))

    def test_preparaciones(self):
        for dias, borrable in [(29.99, False), (30, False), (31, True)]:
            self.corrida(name=str(dias), dias=dias, started=False, mirror=None)
        for x in self.inventario()['corridas']:
            self.assertEqual(x['clase'], 'preparacion-abortada')
            self.assertEqual(x['borrable'], Path(x['ruta']).name == '31')

    def test_lock_huerfano_y_reciente(self):
        p = self.copia()
        r = self.corrida(fin=True)
        lock = p / '.remote-run.lock'
        lock.write_text(r.name)
        for dias, clase, borrable in [(6.99, 'reciente', False), (7, 'lock-huerfano', True)]:
            self.vieja(lock, dias)
            self.vieja(p, dias)
            x = self.inventario()['copias'][0]
            self.assertEqual((x['clase'], x['borrable']), (clase, borrable))
            self.assertIn('huérfano', x['motivo'])

    def test_lock_vivo_dudoso_o_inexistente(self):
        p = self.copia()
        r = self.corrida(pid=12, mirror='otra')
        lock = p / '.remote-run.lock'
        lock.write_text(r.name)
        self.procesos[12] = self.now - 31 * 86400
        self.assertEqual(self.inventario()['copias'][0]['clase'], 'en-uso')
        (r / 'started').unlink()
        self.assertEqual(self.inventario()['copias'][0]['clase'], 'protegida-dudosa')
        lock.write_text('inexistente')
        self.assertEqual(self.inventario()['copias'][0]['clase'], 'protegida-dudosa')

    def test_fallos_meta_symlinks_y_ps(self):
        self.copia()
        (self.root / 'work/enlace').symlink_to('/no-existe')
        r = self.corrida(pid=12)
        (r / 'meta').write_text('mirror=../../escape\n')
        data = self.inventario()
        self.assertEqual(data['corridas'][0]['clase'], 'dudosa')
        self.assertTrue(all(x['clase'] == 'protegida-dudosa' for x in data['copias']))
        (r / 'meta').write_text('mirror=remote-run/work/copia\n')
        with patch.object(inv, 'consultar_inicio', side_effect=OSError('ps falló')):
            self.assertEqual(inv.inventariar(self.root, ahora=self.now)['corridas'][0]['clase'], 'dudosa')

    def test_started_solo_y_datos_invalidos(self):
        self.corrida(name='solo')
        r = self.corrida(name='nan', pid=12)
        (r / 'started').write_text('nan')
        self.assertTrue(all(x['clase'] == 'dudosa' for x in self.inventario()['corridas']))

    def test_ilegible_y_symlink_corrida(self):
        self.copia()
        (self.root / 'runs/enlace').symlink_to('/no-existe')
        with patch.object(inv, 'listar', side_effect=PermissionError('sin permiso')):
            data = self.inventario()
        self.assertFalse(any(x['borrable'] for x in data['copias'] + data['corridas']))
        self.assertEqual(self.inventario()['corridas'][0]['clase'], 'dudosa')

    def test_ultimo_uso_y_tamanos_sin_seguir_enlaces(self):
        p = self.copia()
        (p / 'dato').write_bytes(b'x' * 8192)
        (p / 'enlace').symlink_to('/no-existe')
        self.vieja(p, 8)
        with patch.object(inv, 'tamano', wraps=inv.tamano) as size:
            self.assertIsNone(self.inventario()['copias'][0]['tamanoBytes'])
            size.assert_not_called()
            self.assertGreater(self.inventario(tamanos=True)['copias'][0]['tamanoBytes'], 0)
        self.corrida(dias=1, fin=True)
        self.assertEqual(self.inventario()['copias'][0]['clase'], 'reciente')

    def test_json_y_solo_lectura(self):
        self.copia()
        self.corrida(fin=True)
        def foto():
            return {str(p): (p.lstat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                    for p in [self.root] + list(self.root.rglob('*'))}
        antes = foto()
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            self.assertEqual(inv.main(['--raiz', str(self.root)]), 0)
        data = json.loads(salida.getvalue())
        self.assertEqual(data['schema'], 'remote-run-inventario@1')
        self.assertEqual(data['reglas']['copiaDias'], 7)
        for x in data['copias'] + data['corridas']:
            self.assertEqual(set(x), {'ruta', 'clase', 'borrable', 'motivo', 'ultimoUso',
                                      'corridasAsociadas', 'tamanoBytes'})
        self.assertEqual(antes, foto())

    def test_ps_epoch_entorno_y_zona_padre(self):
        anterior = os.environ.get('TZ')
        try:
            for zona in ('UTC0', 'America/New_York', 'Asia/Tokyo'):
                with self.subTest(zona=zona), patch.dict(os.environ, TZ=zona):
                    time.tzset()
                    with patch.object(inv.subprocess, 'run') as run:
                        run.return_value = subprocess.CompletedProcess([], 0, 'Fri Oct 23 12:00:00 2026\n', '')
                        self.assertEqual(inv.consultar_inicio(12), 1792756800)
                        self.assertEqual(run.call_args.args[0], ['ps', '-o', 'lstart=', '-p', '12'])
                        self.assertEqual(run.call_args.kwargs['env']['TZ'], 'UTC0')
                        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')
                        self.assertEqual(run.call_args.kwargs['timeout'], 5)
        finally:
            if anterior is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = anterior
            time.tzset()

    def test_ps_dos_identidades_hora_repetida(self):
        self.copia()
        r = self.corrida(pid=12)
        for salida, epoch in [('Sun Nov  1 05:30:00 2026', 1793511000),
                              ('Sun Nov  1 06:30:00 2026', 1793514600)]:
            with self.subTest(epoch=epoch), patch.object(inv.subprocess, 'run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, salida, '')
                (r / 'started').write_text(str(epoch))
                self.assertEqual(inv.consultar_inicio(12), epoch)
                data = inv.inventariar(self.root, ahora=epoch + 40 * inv.DIA)
                self.assertEqual((data['corridas'][0]['clase'], data['corridas'][0]['borrable']), ('viva', False))
                self.assertEqual((data['copias'][0]['clase'], data['copias'][0]['borrable']), ('en-uso', False))

    def test_ps_ausencia_errores_y_timeout(self):
        self.corrida(pid=12)
        with patch.object(inv.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')):
            self.assertIsNone(inv.consultar_inicio(12))
            item = inv.inventariar(self.root, ahora=self.now)['corridas'][0]
            self.assertEqual((item['clase'], item['borrable']), ('terminal', True))
        for resultado in (subprocess.CompletedProcess([], 1, '', 'permiso denegado'),
                          subprocess.CompletedProcess([], 0, 'salida inválida', ''),
                          subprocess.TimeoutExpired(['ps'], 5)):
            with self.subTest(resultado=resultado), patch.object(inv.subprocess, 'run') as run:
                if isinstance(resultado, Exception):
                    run.side_effect = resultado
                else:
                    run.return_value = resultado
                item = inv.inventariar(self.root, ahora=self.now)['corridas'][0]
                self.assertEqual((item['clase'], item['borrable']), ('dudosa', False))
