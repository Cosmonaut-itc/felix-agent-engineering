import contextlib
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import borrar_copias as borrar
import inventario as inv


NOMBRES = (
    '.comprador-felix-mode-83cc1dfe',
    '.comprador-humo-099-f3371349',
    '.comprador-humo-103-680cb5db',
    '.comprador-humo-int-e5fab47b',
)


class BorrarCopiasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)
        (self.raiz / 'runs').mkdir()
        (self.raiz / 'work').mkdir()
        self.ahora = time.time()
        for nombre in NOMBRES + ('vieja-no-autorizada', 'reciente'):
            p = self.raiz / 'work' / nombre
            p.mkdir()
            (p / 'dato').write_bytes(b'x' * 8192)
            if nombre != 'reciente':
                self.envejecer(p)

    def envejecer(self, ruta):
        os.utime(ruta, (self.ahora - 40 * inv.DIA,) * 2)

    def corrida(self, nombre='terminal', mirror=NOMBRES[0], clase='terminal'):
        ruta = self.raiz / 'runs' / nombre
        ruta.mkdir()
        if mirror is not None:
            (ruta / 'meta').write_text('mirror=remote-run/work/' + mirror + '\n')
        (ruta / 'started').write_text(str(self.ahora - 40 * inv.DIA))
        if clase == 'terminal':
            (ruta / 'exit').write_text('0')
        elif clase == 'viva':
            (ruta / 'pid').write_text('123')
        return ruta

    def llamar(self, ejecutar=False):
        salida = io.StringIO()
        argumentos = ['--raiz', str(self.raiz)]
        if ejecutar:
            argumentos.append('--ejecutar')
        with contextlib.redirect_stdout(salida):
            codigo = borrar.main(argumentos)
        return codigo, json.loads(salida.getvalue())

    def intactas(self, nombres=NOMBRES):
        for nombre in nombres:
            self.assertEqual((self.raiz / 'work' / nombre / 'dato').read_bytes(), b'x' * 8192)

    def test_lista_literal_y_seco_informa_sin_modificar(self):
        self.assertEqual(borrar.COPIAS_AUTORIZADAS, NOMBRES)
        antes = {str(p): (p.lstat().st_mtime_ns, p.lstat().st_size)
                 for p in self.raiz.rglob('*')}
        with patch.object(borrar.shutil, 'rmtree') as rmtree:
            rmtree.avoids_symlink_attacks = True
            codigo, datos = self.llamar()
            rmtree.assert_not_called()
        self.assertEqual(codigo, 0)
        self.assertEqual(datos['modo'], 'seco')
        self.assertEqual(datos['resultado'], 'validado-en-seco')
        self.assertEqual([x['nombre'] for x in datos['copias']], list(NOMBRES))
        self.assertEqual(antes, {str(p): (p.lstat().st_mtime_ns, p.lstat().st_size)
                                 for p in self.raiz.rglob('*')})

    def test_ejecutar_solo_cuatro_y_no_sigue_symlinks_internos(self):
        destino = self.raiz / 'work/vieja-no-autorizada'
        (self.raiz / 'work' / NOMBRES[0] / 'enlace').symlink_to(destino)
        self.envejecer(self.raiz / 'work' / NOMBRES[0])
        cwd = Path.cwd()
        codigo, datos = self.llamar(ejecutar=True)
        self.assertEqual(codigo, 0)
        self.assertEqual(Path.cwd(), cwd)
        self.assertEqual(datos['resultado'], 'borrado-completo')
        self.assertEqual(datos['listadoPosterior']['nombres'], ['reciente', 'vieja-no-autorizada'])
        self.assertTrue(all(not x['existe'] for x in datos['listadoPosterior']['comprobaciones']))
        self.assertTrue(all(x['resultado']['estado'] == 'borrada' for x in datos['copias']))
        self.intactas(('vieja-no-autorizada', 'reciente'))

    def test_no_borrable_reciente_lock_viva_dudosa_todo_o_nada(self):
        p = self.raiz / 'work' / NOMBRES[0]
        for caso in ('reciente', 'lock', 'viva', 'dudosa'):
            with self.subTest(caso=caso):
                if caso == 'reciente':
                    os.utime(p, None)
                elif caso == 'lock':
                    r = self.corrida()
                    (p / '.remote-run.lock').write_text(r.name)
                    self.envejecer(p / '.remote-run.lock')
                    self.envejecer(p)
                    self.assertTrue(inv.inventariar(self.raiz)['copias'][0]['borrable'])
                else:
                    r = self.corrida(nombre=caso, clase=caso)
                with patch.object(inv, 'consultar_inicio', return_value=self.ahora - 40 * inv.DIA):
                    codigo, datos = self.llamar(ejecutar=True)
                self.assertNotEqual(codigo, 0)
                self.assertFalse(datos['copias'][0]['validacion']['valida'])
                self.assertTrue(datos['copias'][0]['validacion']['motivos'])
                self.intactas()
                if caso == 'lock':
                    (p / '.remote-run.lock').unlink()
                if caso != 'reciente':
                    for archivo in r.iterdir():
                        archivo.unlink()
                    r.rmdir()
                self.envejecer(p)

    def test_sen_3284_phasefailed_no_acredita_terminacion(self):
        p = self.raiz / 'work' / NOMBRES[0]
        (p / 'phasefailed').write_text('falló el cliente')
        self.envejecer(p)
        self.corrida(clase='viva')
        with patch.object(inv, 'consultar_inicio', return_value=self.ahora - 40 * inv.DIA):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        self.assertEqual(datos['copias'][0]['validacion']['item']['clase'], 'en-uso')
        self.intactas()

    def test_symlink_y_ausencia_rechazados(self):
        p = self.raiz / 'work' / NOMBRES[0]
        p.rename(self.raiz / 'destino')
        for enlace in (False, True):
            with self.subTest(enlace=enlace):
                if enlace:
                    p.symlink_to(self.raiz / 'destino')
                codigo, datos = self.llamar(ejecutar=True)
                self.assertNotEqual(codigo, 0)
                self.assertFalse(datos['copias'][0]['validacion']['valida'])
                self.assertEqual((self.raiz / 'destino/dato').read_bytes(), b'x' * 8192)
                self.intactas(NOMBRES[1:])

    def test_work_symlink_no_permite_borrar_fuera(self):
        (self.raiz / 'work').rename(self.raiz / 'fuera')
        (self.raiz / 'work').symlink_to(self.raiz / 'fuera')
        codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        self.assertEqual(datos['resultado'], 'rechazado')
        self.intactas()

    def test_bloqueo_global_dueno_desconocido_y_runs_ilegible(self):
        self.corrida(mirror=None, clase='dudosa')
        codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        self.assertTrue(all('dueño desconocido' in x['validacion']['item']['motivo'] for x in datos['copias']))
        self.intactas()
        listar = inv.listar
        def lectura(ruta):
            if ruta == self.raiz / 'runs':
                raise PermissionError('runs ilegible')
            return listar(ruta)
        with patch.object(inv, 'listar', side_effect=lectura):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        self.assertTrue(all(not x['validacion']['valida'] for x in datos['copias']))
        self.intactas()

    def test_lock_nuevo_segunda_reverificacion_detiene_restantes(self):
        inventariar = borrar.inventariar
        llamadas = []
        def observar(*args, **kwargs):
            llamadas.append(kwargs)
            if len(llamadas) == 3:
                p = self.raiz / 'work' / NOMBRES[1]
                (p / '.remote-run.lock').write_text('aparece-entre-validaciones')
            return inventariar(*args, **kwargs)
        with patch.object(borrar, 'inventariar', side_effect=observar):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        self.assertEqual(len(llamadas), 3)
        self.assertTrue(all(x == {'tamanos': True} for x in llamadas))
        self.assertFalse((self.raiz / 'work' / NOMBRES[0]).exists())
        self.intactas(NOMBRES[1:])
        self.assertFalse(datos['copias'][1]['reverificacion']['valida'])
        self.assertEqual(datos['copias'][2]['resultado']['estado'], 'no-procesada')
        self.assertEqual(datos['resultado'], 'detenido')

    def test_rmtree_inseguro_aborta_incluso_en_seco(self):
        with patch.object(borrar.shutil.rmtree, 'avoids_symlink_attacks', False):
            for ejecutar in (False, True):
                codigo, datos = self.llamar(ejecutar=ejecutar)
                self.assertNotEqual(codigo, 0)
                self.assertIn('avoids_symlink_attacks', datos['error'])
                self.intactas()

    def test_du_json_listado_y_horas_utc(self):
        codigo, datos = self.llamar()
        self.assertEqual(codigo, 0)
        self.assertEqual(len(datos['listadoPosterior']['nombres']), 6)
        self.assertEqual(datos['inventario']['totales']['copias']['cantidad'], 6)
        for x in datos['copias']:
            self.assertTrue(x['validacion']['valida'])
            self.assertTrue(x['reverificacion']['valida'])
            self.assertIsNotNone(x['validacion']['item']['tamanoBytes'])
            self.assertEqual(x['du']['codigo'], 0)
            self.assertIn(x['nombre'], x['du']['stdout'])
            self.assertGreater(int(x['du']['stdout'].split()[0]), 0)
            self.assertEqual(x['du']['tamanoBytes'], x['validacion']['item']['tamanoBytes'])
        def horas(valor):
            if isinstance(valor, dict):
                for clave, hijo in valor.items():
                    if clave == 'horaUTC':
                        self.assertEqual(datetime.fromisoformat(hijo).utcoffset().total_seconds(), 0)
                    else:
                        horas(hijo)
            elif isinstance(valor, list):
                for hijo in valor:
                    horas(hijo)
        horas(datos)

    def test_du_argumentos_sin_shell_y_timeout(self):
        with patch.object(borrar, 'medir_du', wraps=borrar.medir_du) as medir:
            codigo, _ = self.llamar()
        self.assertEqual(codigo, 0)
        self.assertEqual(medir.call_count, 4)
        with patch.object(borrar.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '8\truta\n', '')) as run:
            borrar.medir_du(self.raiz / 'work' / NOMBRES[0], 8192)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/du', '-sk', str(self.raiz / 'work' / NOMBRES[0])])
        self.assertFalse(run.call_args.kwargs.get('shell', False))
        self.assertGreater(run.call_args.kwargs['timeout'], 0)

    def test_fallo_du_no_borra_nada(self):
        for efecto in (subprocess.TimeoutExpired(['du'], 30), OSError('du no disponible')):
            with self.subTest(efecto=efecto), patch.object(borrar.subprocess, 'run', side_effect=efecto):
                codigo, datos = self.llamar(ejecutar=True)
            self.assertNotEqual(codigo, 0)
            self.assertEqual(datos['resultado'], 'rechazado')
            self.intactas()

    def test_error_rmtree_se_informa_y_no_continua(self):
        with patch.object(borrar.shutil, 'rmtree', side_effect=OSError('borrado falló')) as rmtree:
            rmtree.avoids_symlink_attacks = True
            codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        self.assertEqual(datos['resultado'], 'detenido')
        self.assertEqual(datos['copias'][0]['resultado']['estado'], 'error')
        self.assertEqual(rmtree.call_count, 1)
        self.intactas()
