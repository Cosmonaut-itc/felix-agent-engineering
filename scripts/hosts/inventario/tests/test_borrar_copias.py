import contextlib
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
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
        argumentos = ['--ejecutar'] if ejecutar else ['--raiz', str(self.raiz)]
        with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(borrar, 'raiz_predeterminada', return_value=self.raiz), \
                patch.object(borrar, 'leer_host', return_value=borrar.HOST_MINI):
            codigo = borrar.main(argumentos)
        return codigo, json.loads(salida.getvalue())

    def intactas(self, nombres=NOMBRES):
        for nombre in nombres:
            self.assertEqual((self.raiz / 'work' / nombre / 'dato').read_bytes(), b'x' * 8192)

    def cuarentena_intacta(self, datos, indice=0, contenido=b'x' * 8192):
        copia = datos['copias'][indice]
        self.assertEqual(copia['resultado']['estado'], 'error-parcial')
        self.assertIn('horaUTC', copia['resultado'])
        self.assertTrue(copia['resultado']['error'])
        self.assertEqual((Path(copia['cuarentena']) / 'dato').read_bytes(), contenido)
        self.assertFalse((self.raiz / 'work' / copia['nombre']).exists())
        self.intactas(NOMBRES[indice + 1:])

    def test_lista_literal_y_seco_informa_sin_modificar(self):
        self.assertEqual(borrar.COPIAS_AUTORIZADAS, NOMBRES)
        antes = {str(p): (p.lstat().st_mtime_ns, p.lstat().st_size)
                 for p in self.raiz.rglob('*')}
        with patch.object(borrar, 'cuarentenar', wraps=borrar.cuarentenar) as apartar:
            codigo, datos = self.llamar()
            apartar.assert_not_called()
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
        validar = borrar.validar
        def lectura(raiz, nombre, *args, **kwargs):
            paso = validar(raiz, nombre, *args, **kwargs)
            if nombre == NOMBRES[1] and not (raiz / 'work' / NOMBRES[0]).exists():
                (raiz / 'work' / nombre / '.remote-run.lock').write_text('nuevo')
            return paso
        with patch.object(borrar, 'validar', side_effect=lectura):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        self.assertFalse((self.raiz / 'work' / NOMBRES[0]).exists())
        self.intactas(NOMBRES[2:])
        self.cuarentena_intacta(datos, indice=1)
        self.assertEqual(datos['copias'][2]['resultado']['estado'], 'no-procesada')
        self.assertEqual(datos['resultado'], 'detenido')

    def test_capacidades_fd_obligatorias_ambos_modos(self):
        for atributo, funcion in [('supports_dir_fd', borrar.os.rename),
                                  ('supports_fd', borrar.os.scandir)]:
            with patch.object(borrar.os, atributo, getattr(os, atributo) - {funcion}):
                for ejecutar in (False, True):
                    codigo, datos = self.llamar(ejecutar=ejecutar)
                    self.assertNotEqual(codigo, 0)
                    self.assertIn('descriptor', datos['error'])
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

    def test_sustitucion_entre_reverificacion_y_cuarentena(self):
        apartar = borrar.cuarentenar
        p = self.raiz / 'work' / NOMBRES[0]
        a = self.raiz / 'work/A-apartada'
        cambiado = []
        def sustituir(*args, **kwargs):
            if cambiado:
                return apartar(*args, **kwargs)
            cambiado.append(True)
            p.rename(a)
            p.mkdir()
            (p / 'dato').write_bytes(b'B')
            self.envejecer(p)
            return apartar(*args, **kwargs)
        with patch.object(borrar, 'cuarentenar', side_effect=sustituir):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        self.assertTrue((a / 'dato').is_file())
        self.assertFalse(p.exists())
        self.assertEqual((a / 'dato').read_bytes(), b'x' * 8192)
        self.cuarentena_intacta(datos, contenido=b'B')
        self.assertIn('identidad', datos['copias'][0]['resultado']['error'])
        self.intactas(NOMBRES[1:])

    def test_lock_despues_reverificacion(self):
        apartar = borrar.cuarentenar
        def nuevo_lock(*args, **kwargs):
            (self.raiz / 'work' / NOMBRES[0] / '.remote-run.lock').write_text('nuevo')
            return apartar(*args, **kwargs)
        with patch.object(borrar, 'cuarentenar', side_effect=nuevo_lock):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        self.cuarentena_intacta(datos)
        self.assertFalse(datos['copias'][0]['uso']['lock']['libre'])

    def test_lsof_proceso_real_cwd(self):
        self.assertTrue(Path('/usr/sbin/lsof').is_file())
        subdir = self.raiz / 'work' / NOMBRES[0] / 'profundo' / 'hijo'
        subdir.mkdir(parents=True)
        self.envejecer(self.raiz / 'work' / NOMBRES[0])
        proceso = subprocess.Popen(['/bin/sleep', '60'], cwd=subdir)
        try:
            codigo, datos = self.llamar(ejecutar=True)
            self.assertNotEqual(codigo, 0)
            uso = datos['copias'][0]['usoPrevio']['lsof']
            self.assertIn(str(proceso.pid), uso['stdout'])
            self.assertFalse(uso['libre'])
            self.assertEqual(uso['argumentos'], ['/usr/sbin/lsof', '-a', '-p', '^' + str(os.getpid()),
                                                '-nP', '+D', str(self.raiz / 'work' / NOMBRES[0])])
            self.assertEqual(uso['timeout'], 120)
            self.assertEqual(datos['copias'][0]['resultado']['estado'], 'rechazada')
            self.intactas()
        finally:
            proceso.terminate()
            proceso.wait(timeout=5)

    def test_ps_proceso_real_nombre(self):
        proceso = subprocess.Popen(['/bin/sh', '-c', 'read espera', str(self.raiz / 'work' / NOMBRES[0])],
                                   stdin=subprocess.PIPE)
        try:
            codigo, datos = self.llamar(ejecutar=True)
            self.assertNotEqual(codigo, 0)
            uso = datos['copias'][0]['usoPrevio']['ps']
            self.assertTrue(any(str(proceso.pid) in x for x in uso['coincidencias']))
            self.assertFalse(uso['libre'])
            self.assertEqual(datos['copias'][0]['resultado']['estado'], 'rechazada')
            self.intactas()
        finally:
            proceso.terminate()
            proceso.communicate(timeout=5)

    def test_lsof_error_y_timeout_cuentan_como_uso(self):
        run = borrar.subprocess.run
        for efecto in (subprocess.CompletedProcess([], 1, '', 'lsof: status error'),
                       subprocess.CompletedProcess([], 2, '', 'error'),
                       subprocess.TimeoutExpired(['lsof'], 120)):
            def ejecutar(args, **kwargs):
                if args[0] == '/usr/sbin/lsof':
                    if isinstance(efecto, Exception):
                        raise efecto
                    return efecto
                return run(args, **kwargs)
            with patch.object(borrar.subprocess, 'run', side_effect=ejecutar):
                codigo, datos = self.llamar(ejecutar=True)
            self.assertNotEqual(codigo, 0)
            self.assertFalse(datos['copias'][0]['usoPrevio']['lsof']['libre'])
            self.intactas()

    def test_work_sustituido_despues_abrir_fd(self):
        comprobar = borrar.comprobar_work
        cambiado = []
        def sustituir(*args, **kwargs):
            if cambiado:
                return comprobar(*args, **kwargs)
            cambiado.append(True)
            (self.raiz / 'work').rename(self.raiz / 'work-apartado')
            (self.raiz / 'work').mkdir()
            (self.raiz / 'work/nuevo').write_text('intacto')
            return comprobar(*args, **kwargs)
        with patch.object(borrar, 'comprobar_work', side_effect=sustituir), \
                patch.object(borrar, 'cuarentenar', wraps=borrar.cuarentenar) as apartar:
            codigo, datos = self.llamar(ejecutar=True)
            apartar.assert_not_called()
        self.assertNotEqual(codigo, 0)
        self.assertIn('work', datos['error'])
        self.assertEqual((self.raiz / 'work/nuevo').read_text(), 'intacto')
        for nombre in NOMBRES:
            self.assertEqual((self.raiz / 'work-apartado' / nombre / 'dato').read_bytes(), b'x' * 8192)
        self.assertFalse(list((self.raiz / 'work-apartado').glob('.borrando-*')))

    def test_cuarentena_no_pisa_copia_recreada(self):
        comprobar = borrar.comprobar_uso
        def recrear(*args, **kwargs):
            p = self.raiz / 'work' / NOMBRES[0]
            p.mkdir()
            (p / 'nuevo').write_text('productor')
            (Path(args[1]) / '.remote-run.lock').write_text('uso')
            return comprobar(*args, **kwargs)
        with patch.object(borrar, 'comprobar_uso', side_effect=recrear):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        copia = datos['copias'][0]
        self.assertEqual(copia['resultado']['estado'], 'error-parcial')
        self.assertEqual((Path(copia['cuarentena']) / 'dato').read_bytes(), b'x' * 8192)
        self.assertEqual((self.raiz / 'work' / NOMBRES[0] / 'nuevo').read_text(), 'productor')
        self.intactas(NOMBRES[1:])

    def test_error_parcial_detiene_y_mide_cuarentena(self):
        p = self.raiz / 'work' / NOMBRES[0]
        (p / 'segundo').write_text('restante')
        self.envejecer(p)
        vaciar = borrar.vaciar
        unlink = borrar.os.unlink
        llamadas = []
        def fallar(nombre, **kwargs):
            llamadas.append(nombre)
            if len(llamadas) == 2:
                raise OSError('fallo después de borrar')
            return unlink(nombre, **kwargs)
        def parcial(*args, **kwargs):
            with patch.object(borrar.os, 'unlink', side_effect=fallar):
                return vaciar(*args, **kwargs)
        with patch.object(borrar, 'vaciar', side_effect=parcial):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        copia = datos['copias'][0]
        self.assertEqual(copia['resultado']['estado'], 'error-parcial')
        self.assertEqual(copia['duPosterior']['codigo'], 0)
        self.assertEqual(len(list(Path(copia['cuarentena']).iterdir())), 1)
        self.assertIn(Path(copia['cuarentena']).name, datos['listadoPosterior']['nombres'])
        self.intactas(NOMBRES[1:])

    def test_du_codigo_y_salida_invalidos(self):
        run = borrar.subprocess.run
        for salida in (subprocess.CompletedProcess([], 2, '', 'error'),
                       subprocess.CompletedProcess([], 0, 'inválido', '')):
            def ejecutar(args, **kwargs):
                return salida if args[0] == '/usr/bin/du' else run(args, **kwargs)
            with self.subTest(salida=salida), patch.object(borrar.subprocess, 'run', side_effect=ejecutar):
                codigo, datos = self.llamar(ejecutar=True)
            self.assertNotEqual(codigo, 0)
            self.assertEqual(datos['resultado'], 'rechazado')
            self.intactas()

    def cli_rechazada(self, argumentos, host=None):
        with patch.object(borrar, 'raiz_predeterminada', return_value=self.raiz), \
                patch.object(borrar, 'leer_host', return_value=host or borrar.HOST_MINI), \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                borrar.main(argumentos)
        self.assertEqual(exc.exception.code, 2)
        self.intactas()

    def test_cli_sin_abreviaturas(self):
        self.cli_rechazada(['--e'])

    def test_cli_raiz_explicita_prohibida(self):
        self.cli_rechazada(['--raiz', str(self.raiz), '--ejecutar'])

    def test_cli_host_distinto(self):
        self.cli_rechazada(['--ejecutar'], host='otro-host')

    def test_lectura_host_argumentos_timeout_y_error(self):
        with patch.object(borrar.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'Felixs-Mac-mini\n', '')) as run:
            self.assertEqual(borrar.leer_host(), 'Felixs-Mac-mini')
        self.assertEqual(run.call_args.args[0], ['/usr/sbin/scutil', '--get', 'LocalHostName'])
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')
        self.assertGreater(run.call_args.kwargs['timeout'], 0)
        with patch.object(borrar, 'leer_host', side_effect=OSError('sin host')), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                borrar.main(['--ejecutar'])
        self.assertEqual(exc.exception.code, 2)
        self.intactas()

    def test_cli_fresca_no_crea_bytecode(self):
        codigo = self.raiz / 'codigo'
        codigo.mkdir()
        for nombre in ('borrar_copias.py', 'inventario.py'):
            shutil.copyfile(Path(borrar.__file__).parent / nombre, codigo / nombre)
        # El Python de Apple guarda el bytecode bajo ~/Library/Caches/com.apple.python, no junto al código,
        # y el arranque del intérprete escribe el suyo antes de la primera línea del script.
        def entorno(prefijo):
            valores = dict(os.environ, PYTHONPYCACHEPREFIX=str(prefijo))
            valores.pop('PYTHONDONTWRITEBYTECODE', None)
            return valores

        def archivos(prefijo):
            return {str(p.relative_to(prefijo)) for p in prefijo.rglob('*') if p.is_file()} if prefijo.exists() else set()

        base, prefijo = self.raiz / 'pycache-arranque', self.raiz / 'pycache-script'
        arranque = subprocess.run([sys.executable, '-c', 'pass'], cwd=self.raiz, env=entorno(base),
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(arranque.returncode, 0, arranque.stderr)
        salida = subprocess.run([sys.executable, str(codigo / 'borrar_copias.py'), '--raiz', str(self.raiz)],
                                cwd=self.raiz, env=entorno(prefijo), capture_output=True, text=True, timeout=180)
        self.assertEqual(salida.returncode, 0, salida.stderr + salida.stdout)
        cache = subprocess.run([sys.executable, '-B', '-c',
                                'import importlib.util, sys; print(importlib.util.cache_from_source(sys.argv[1]))',
                                os.path.realpath(codigo / 'inventario.py')],
                               env=entorno(prefijo), capture_output=True, text=True, timeout=60)
        self.assertEqual(cache.returncode, 0, cache.stderr)
        self.assertTrue(cache.stdout.strip().startswith(str(prefijo)), cache.stdout)
        self.assertFalse(Path(cache.stdout.strip()).exists())
        self.assertEqual(archivos(prefijo) - archivos(base), set())
        self.assertFalse((codigo / '__pycache__').exists())

    def test_interrupcion_segunda_sin_json_con_rastro(self):
        vaciar, procesar = borrar.vaciar, borrar.procesar
        llamadas, informes = [], []
        def capturar(raiz, ejecutar, informe, recursos):
            informes.append(informe)
            return procesar(raiz, ejecutar, informe, recursos)
        def interrumpir(*args, **kwargs):
            llamadas.append(args)
            if len(llamadas) == 2:
                raise KeyboardInterrupt()
            return vaciar(*args, **kwargs)
        salida, progreso = io.StringIO(), io.StringIO()
        with patch.object(borrar, 'raiz_predeterminada', return_value=self.raiz), \
                patch.object(borrar, 'leer_host', return_value=borrar.HOST_MINI), \
                patch.object(borrar, 'procesar', side_effect=capturar), \
                patch.object(borrar, 'vaciar', side_effect=interrumpir), \
                contextlib.redirect_stdout(salida), contextlib.redirect_stderr(progreso):
            with self.assertRaises(KeyboardInterrupt):
                borrar.main(['--ejecutar'])
        self.assertEqual(salida.getvalue(), '')
        datos = informes[0]
        self.assertEqual(datos['copias'][0]['resultado']['estado'], 'borrada')
        self.assertFalse((self.raiz / 'work' / NOMBRES[0]).exists())
        copia = datos['copias'][1]
        self.assertEqual((Path(copia['cuarentena']) / 'dato').read_bytes(), b'x' * 8192)
        self.assertIn(copia['cuarentena'], progreso.getvalue())
        self.intactas(NOMBRES[2:])
        codigo, siguiente = self.llamar()
        self.assertEqual(codigo, 1)
        self.assertEqual(siguiente['cuarentenasPrevias'][0]['ruta'], copia['cuarentena'])

    def test_excepcion_no_prevista_se_propaga_sin_json(self):
        salida = io.StringIO()
        with patch.object(borrar, 'raiz_predeterminada', return_value=self.raiz), \
                patch.object(borrar, 'leer_host', return_value=borrar.HOST_MINI), \
                patch.object(borrar, 'comprobar_uso', side_effect=RuntimeError('no prevista')), \
                contextlib.redirect_stdout(salida), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'no prevista'):
                borrar.main(['--ejecutar'])
        self.assertEqual(salida.getvalue(), '')
        cuarentena = next((self.raiz / 'work').glob('.borrando-*'))
        self.assertEqual((cuarentena / 'dato').read_bytes(), b'x' * 8192)
        self.assertFalse((self.raiz / 'work' / NOMBRES[0]).exists())
        self.intactas(NOMBRES[1:])

    def test_cuarentena_preexistente_rechaza_ambos_modos(self):
        p = self.raiz / 'work' / ('.borrando-' + NOMBRES[0] + '-anterior')
        p.mkdir()
        (p / 'dato').write_text('no recuperar')
        for ejecutar in (False, True):
            codigo, datos = self.llamar(ejecutar=ejecutar)
            self.assertNotEqual(codigo, 0)
            self.assertEqual(datos['resultado'], 'rechazado')
            self.assertEqual(datos['cuarentenasPrevias'][0]['ruta'], str(p))
            self.assertEqual(datos['cuarentenasPrevias'][0]['identidad'], [p.stat().st_dev, p.stat().st_ino])
            self.intactas()
        self.assertEqual((p / 'dato').read_text(), 'no recuperar')

    def test_recorrido_rechaza_otro_dispositivo(self):
        p = self.raiz / 'work' / NOMBRES[0]
        (p / 'montado').mkdir()
        (p / 'montado/externo').write_text('otro dispositivo')
        self.envejecer(p)
        vaciar = borrar.vaciar
        fstat = borrar.os.fstat
        def dispositivo(fd):
            info = fstat(fd)
            if info.st_ino == (p.parent / Path(self.cuarentena).name / 'montado').stat().st_ino:
                valores = list(info)
                valores[2] = info.st_dev + 1
                return os.stat_result(valores)
            return info
        def recorrer(fd, dev):
            self.cuarentena = next((self.raiz / 'work').glob('.borrando-*'))
            with patch.object(borrar.os, 'fstat', side_effect=dispositivo):
                return vaciar(fd, dev)
        with patch.object(borrar, 'vaciar', side_effect=recorrer):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        self.assertEqual(datos['copias'][0]['resultado']['estado'], 'error-parcial')
        self.assertEqual((self.cuarentena / 'montado/externo').read_text(), 'otro dispositivo')
        self.intactas(NOMBRES[1:])

    def test_listado_acepta_recreada(self):
        vaciar = borrar.vaciar
        def recrear(*args, **kwargs):
            p = self.raiz / 'work' / NOMBRES[0]
            if not p.exists():
                p.mkdir()
                (p / 'nuevo').write_text('productor')
            return vaciar(*args, **kwargs)
        with patch.object(borrar, 'vaciar', side_effect=recrear):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertEqual(codigo, 0)
        self.assertEqual(datos['listadoPosterior']['comprobaciones'][0]['estado'], 'recreada-por-productor')
        self.assertEqual((self.raiz / 'work' / NOMBRES[0] / 'nuevo').read_text(), 'productor')

    def test_inventario_nuevo_tras_cuarentena_conserva_proteccion(self):
        comprobar = borrar.comprobar_uso
        for mirror in (NOMBRES[0], None):
            def corrida_nueva(*args, **kwargs):
                r = self.corrida(nombre='nueva-' + str(mirror), mirror=mirror, clase='dudosa')
                try:
                    return comprobar(*args, **kwargs)
                finally:
                    shutil.rmtree(r)
            with self.subTest(mirror=mirror), patch.object(borrar, 'comprobar_uso', side_effect=corrida_nueva):
                codigo, datos = self.llamar(ejecutar=True)
            self.assertNotEqual(codigo, 0)
            uso = datos['copias'][0]['uso']['inventario']
            self.assertFalse(uso['libre'])
            self.assertTrue(uso['bloqueoGlobal'] if mirror is None else uso['corridas'])
            self.cuarentena_intacta(datos)
            Path(datos['copias'][0]['cuarentena']).rename(self.raiz / 'work' / NOMBRES[0])
            self.intactas()

    def test_identidad_distinta_en_reverificacion(self):
        observar = borrar.observar
        llamadas = []
        p = self.raiz / 'work' / NOMBRES[0]
        def cambiar(*args, **kwargs):
            llamadas.append(True)
            if len(llamadas) == 2:
                p.rename(self.raiz / 'anterior')
                p.mkdir()
                (p / 'dato').write_text('sustituta')
                self.envejecer(p)
            return observar(*args, **kwargs)
        with patch.object(borrar, 'observar', side_effect=cambiar), \
                patch.object(borrar, 'cuarentenar', wraps=borrar.cuarentenar) as apartar:
            codigo, datos = self.llamar(ejecutar=True)
            apartar.assert_not_called()
        self.assertNotEqual(codigo, 0)
        self.assertIn('identidad distinta', ' '.join(datos['copias'][0]['reverificacion']['motivos']))
        self.assertEqual((self.raiz / 'anterior/dato').read_bytes(), b'x' * 8192)
        self.assertEqual((p / 'dato').read_text(), 'sustituta')
        self.intactas(NOMBRES[1:])

    def test_lsof_identidad_ruta_antes_y_despues(self):
        ruta = self.raiz / 'work' / NOMBRES[0]
        fd = os.open(str(ruta), borrar.FLAGS_DIRECTORIO)
        self.addCleanup(os.close, fd)
        run = borrar.subprocess.run
        def sustituir(args, **kwargs):
            ruta.rename(self.raiz / 'apartada')
            ruta.mkdir()
            return subprocess.CompletedProcess([], 1, '', '')
        with patch.object(borrar.subprocess, 'run', side_effect=sustituir):
            uso = borrar.consultar_lsof(ruta, fd)
        self.assertFalse(uso['libre'])
        self.assertIn('identidad', uso['error'])
        with patch.object(borrar.subprocess, 'run', wraps=run) as consulta:
            uso = borrar.consultar_lsof(ruta, fd)
            consulta.assert_not_called()
        self.assertFalse(uso['libre'])
        self.assertEqual((self.raiz / 'apartada/dato').read_bytes(), b'x' * 8192)

    def test_fallos_uso_posterior_conservan_cuarentena(self):
        for consulta in ('consultar_lsof', 'consultar_ps', 'inventariar'):
            with self.subTest(consulta=consulta):
                comprobar = borrar.comprobar_uso
                def fallar(*args, **kwargs):
                    with patch.object(borrar, consulta, side_effect=OSError('lectura fallida')):
                        return comprobar(*args, **kwargs)
                with patch.object(borrar, 'comprobar_uso', side_effect=fallar):
                    codigo, datos = self.llamar(ejecutar=True)
                self.assertNotEqual(codigo, 0)
                self.cuarentena_intacta(datos)
                Path(datos['copias'][0]['cuarentena']).rename(self.raiz / 'work' / NOMBRES[0])

    def test_uso_previo_rechaza_sin_rename(self):
        for consulta in ('consultar_lsof', 'consultar_ps'):
            with patch.object(borrar, consulta, return_value={'libre': False}), \
                    patch.object(borrar, 'cuarentenar', wraps=borrar.cuarentenar) as apartar:
                codigo, datos = self.llamar(ejecutar=True)
                apartar.assert_not_called()
            self.assertNotEqual(codigo, 0)
            self.assertFalse(datos['copias'][0]['usoPrevio'][consulta[10:]]['libre'])
            self.intactas()

    def test_cuarentena_destino_existente_no_se_reemplaza(self):
        work = self.raiz / 'work'
        destino = '.borrando-colision'
        (work / destino).mkdir()
        fd = os.open(str(work), borrar.FLAGS_DIRECTORIO)
        try:
            info = (work / destino).stat()
            with self.assertRaisesRegex(ValueError, 'ya existe'):
                borrar.cuarentenar(fd, NOMBRES[0], destino)
            self.assertEqual(borrar.identidad((work / destino).stat()), borrar.identidad(info))
            self.intactas()
        finally:
            os.close(fd)

    def test_rename_fallido_sin_reconciliacion_conserva_original(self):
        def fallar(*args, **kwargs):
            raise OSError('rename rechazado')
        with patch.object(borrar.os, 'supports_dir_fd', borrar.os.supports_dir_fd | {fallar}), \
                patch.object(borrar.os, 'rename', new=fallar):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertEqual(codigo, 1)
        copia = datos['copias'][0]
        self.assertNotIn('reconciliacion', copia)
        self.assertEqual(copia['resultado']['estado'], 'error-parcial')
        self.assertIn('rename rechazado', copia['resultado']['error'])
        self.assertFalse(Path(copia['cuarentena']).exists())
        self.intactas()

    def uso_posterior_ocupado(self, consulta):
        comprobar = borrar.comprobar_uso
        def ocupado(*args, **kwargs):
            self.assertTrue(Path(args[1]).is_dir())
            self.assertFalse((self.raiz / 'work' / NOMBRES[0]).exists())
            with patch.object(borrar, consulta, return_value={'libre': False}) as lectura:
                paso = comprobar(*args, **kwargs)
                lectura.assert_called_once()
                return paso
        with patch.object(borrar, 'comprobar_uso', side_effect=ocupado):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertEqual(codigo, 1)
        self.assertTrue(datos['copias'][0]['usoPrevio']['libre'])
        self.assertFalse(datos['copias'][0]['uso'][consulta[10:]]['libre'])
        self.cuarentena_intacta(datos)
        self.assertEqual(datos['resultado'], 'detenido')
        self.assertTrue(all(c['resultado']['estado'] == 'no-procesada' for c in datos['copias'][1:]))

    def test_lsof_ocupado_solo_despues_rename(self):
        self.uso_posterior_ocupado('consultar_lsof')

    def test_ps_ocupado_solo_despues_rename(self):
        self.uso_posterior_ocupado('consultar_ps')

    def borde_rmdir_sin_reconciliacion(self, consumado):
        rmdir = borrar.os.rmdir
        rutas = []
        def fallar(nombre, *args, **kwargs):
            if str(nombre).startswith('.borrando-'):
                rutas.append(self.raiz / 'work' / nombre)
                if consumado:
                    rmdir(nombre, *args, **kwargs)
                raise OSError('borde de rmdir')
            return rmdir(nombre, *args, **kwargs)
        with patch.object(borrar.os, 'supports_dir_fd', borrar.os.supports_dir_fd | {fallar}), \
                patch.object(borrar.os, 'rmdir', new=fallar):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertEqual(codigo, 1)
        self.assertEqual(len(rutas), 1)
        copia = datos['copias'][0]
        self.assertEqual(copia['resultado']['estado'], 'error-parcial')
        self.assertIn('borde de rmdir', copia['resultado']['error'])
        self.assertNotIn('reconciliacion', copia)
        self.assertEqual(Path(copia['cuarentena']), rutas[0])
        self.assertEqual(rutas[0].exists(), not consumado)
        self.assertEqual(rutas[0].name in datos['listadoPosterior']['nombres'], not consumado)
        if not consumado:
            self.assertEqual(list(rutas[0].iterdir()), [])
        self.assertFalse((self.raiz / 'work' / NOMBRES[0]).exists())
        self.intactas(NOMBRES[1:])
        self.assertEqual(datos['resultado'], 'detenido')
        self.assertTrue(all(c['resultado']['estado'] == 'no-procesada' for c in datos['copias'][1:]))

    def test_rmdir_consumado_con_error_sin_nlink_queda_error_parcial(self):
        self.borde_rmdir_sin_reconciliacion(consumado=True)

    def test_rmdir_aun_enlazado_con_error_sin_nlink_queda_error_parcial(self):
        self.borde_rmdir_sin_reconciliacion(consumado=False)

    def test_listado_rechaza_solo_nombre_cuarentena_con_otra_identidad(self):
        listado = borrar.listado_posterior
        def reaparecer(raiz, copias, work_fd=None, identidades=None):
            ruta = Path(copias[0]['cuarentena'])
            ruta.mkdir()
            (ruta / 'nuevo').write_text('otra identidad')
            self.assertNotEqual(borrar.identidad(ruta.stat()), copias[0]['validacion']['identidades']['copia'])
            return listado(raiz, copias, work_fd, identidades)
        with patch.object(borrar, 'listado_posterior', side_effect=reaparecer):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertEqual(codigo, 1)
        self.assertEqual(datos['resultado'], 'detenido')
        self.assertIn('nombre de cuarentena', datos['listadoPosterior']['error'])
        self.assertTrue(all(c['resultado']['estado'] == 'borrada' for c in datos['copias']))
        self.assertEqual((Path(datos['copias'][0]['cuarentena']) / 'nuevo').read_text(), 'otra identidad')

    def estado_incompleto_impide_exito(self, ejecutar):
        listado = borrar.listado_posterior
        def incompleto(raiz, copias, *args, **kwargs):
            paso = listado(raiz, copias, *args, **kwargs)
            copias[0]['resultado']['estado'] = 'error-parcial'
            return paso
        with patch.object(borrar, 'listado_posterior', side_effect=incompleto):
            codigo, datos = self.llamar(ejecutar=ejecutar)
        self.assertEqual(codigo, 1)
        self.assertEqual(datos['resultado'], 'detenido')
        self.assertNotIn('error', datos['listadoPosterior'])

    def test_ejecutar_exige_cuatro_estados_borrada(self):
        self.estado_incompleto_impide_exito(ejecutar=True)

    def test_seco_exige_cuatro_estados_conservada(self):
        self.estado_incompleto_impide_exito(ejecutar=False)
        self.intactas()

    def test_error_lectura_listado_no_degrada_borradas(self):
        listado = borrar.listado_posterior
        def fallar(*args, **kwargs):
            with patch.object(borrar.os, 'scandir', side_effect=OSError('EIO al listar')):
                return listado(*args, **kwargs)
        with patch.object(borrar, 'listado_posterior', side_effect=fallar):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertEqual(codigo, 1)
        self.assertEqual(datos['resultado'], 'detenido')
        self.assertIn('EIO al listar', datos['listadoPosterior']['error'])
        self.assertTrue(all(c['resultado']['estado'] == 'borrada' for c in datos['copias']))

    def test_descriptores_borrados_abiertos_hasta_listado(self):
        abrir = borrar.os.open
        listado = borrar.listado_posterior
        descriptores = []
        def abrir_copia(nombre, *args, **kwargs):
            fd = abrir(nombre, *args, **kwargs)
            if str(nombre).startswith('.borrando-'):
                descriptores.append((fd, borrar.identidad(os.fstat(fd))))
            return fd
        def comprobar(*args, **kwargs):
            self.assertEqual(len(descriptores), 4)
            for fd, ident in descriptores:
                try:
                    actual = borrar.identidad(os.fstat(fd))
                except OSError as exc:
                    self.fail('descriptor cerrado antes del listado: ' + str(exc))
                self.assertEqual(actual, ident)
            return listado(*args, **kwargs)
        with patch.object(borrar.os, 'supports_dir_fd', borrar.os.supports_dir_fd | {abrir_copia}), \
                patch.object(borrar.os, 'open', new=abrir_copia), \
                patch.object(borrar, 'listado_posterior', side_effect=comprobar):
            codigo, _ = self.llamar(ejecutar=True)
        self.assertEqual(codigo, 0)
        for fd, _ in descriptores:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_listado_rechaza_identidad_borrada_persistente(self):
        listado = borrar.listado_posterior
        scandir = borrar.os.scandir
        def inyectar(raiz, copias, work_fd=None, identidades=None):
            p = raiz / 'work' / 'identidad-persistente'
            p.mkdir()
            ident = copias[0]['validacion']['identidades']['copia']
            class Entrada:
                name = p.name
                def stat(self, **kwargs):
                    valores = list(p.stat())
                    valores[1], valores[2] = ident[1], ident[0]
                    return os.stat_result(valores)
            class Listado:
                def __enter__(self):
                    with scandir(work_fd) as entradas:
                        return iter([e for e in entradas if e.name != p.name] + [Entrada()])
                def __exit__(self, *args):
                    pass
            with patch.object(borrar.os, 'scandir', return_value=Listado()):
                return listado(raiz, copias, work_fd, identidades)
        with patch.object(borrar, 'listado_posterior', side_effect=inyectar):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        self.assertIn('persiste una identidad', datos['listadoPosterior']['error'])

    def test_vaciar_lista_antes_de_unlink(self):
        p = self.raiz / 'work' / NOMBRES[0]
        fd = os.open(str(p), borrar.FLAGS_DIRECTORIO)
        scandir, unlink = borrar.os.scandir, borrar.os.unlink
        agotado = []
        def entradas(descriptor):
            with scandir(descriptor) as items:
                yield from items
            agotado.append(True)
        def borrar_entrada(*args, **kwargs):
            self.assertTrue(agotado, 'se empezó a borrar antes de terminar el listado')
            return unlink(*args, **kwargs)
        try:
            with patch.object(borrar.os, 'scandir', side_effect=entradas), \
                    patch.object(borrar.os, 'unlink', side_effect=borrar_entrada):
                borrar.vaciar(fd, os.fstat(fd).st_dev)
            self.assertEqual(list(p.iterdir()), [])
        finally:
            os.close(fd)

    def test_raiz_predeterminada_ignora_home(self):
        with patch.dict(os.environ, HOME=str(self.raiz / 'otro-home')):
            import pwd
            esperada = Path(pwd.getpwuid(os.getuid()).pw_dir) / 'remote-run'
            self.assertEqual(borrar.raiz_predeterminada(), esperada)

    def test_inventario_clasificacion_real_cuarentena(self):
        p = self.raiz / 'work' / ('.borrando-' + NOMBRES[0] + '-fixture')
        (self.raiz / 'work' / NOMBRES[0]).rename(p)
        # Se fija mtime para cubrir ambas edades sin depender de la semántica de rename del FS.
        os.utime(p, None)
        reciente = next(x for x in inv.inventariar(self.raiz)['copias'] if x['ruta'] == str(p))
        self.assertEqual((reciente['clase'], reciente['borrable']), ('reciente', False))
        self.envejecer(p)
        vieja = next(x for x in inv.inventariar(self.raiz)['copias'] if x['ruta'] == str(p))
        self.assertEqual((vieja['clase'], vieja['borrable']), ('inactiva', True))
