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
                patch.object(borrar, 'RAIZ_DEFAULT', str(self.raiz)), \
                patch.object(borrar, 'leer_host', return_value=borrar.HOST_MINI):
            codigo = borrar.main(argumentos)
        return codigo, json.loads(salida.getvalue())

    def intactas(self, nombres=NOMBRES):
        for nombre in nombres:
            self.assertEqual((self.raiz / 'work' / nombre / 'dato').read_bytes(), b'x' * 8192)

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
        self.intactas(NOMBRES[1:])
        self.assertEqual(datos['copias'][1]['resultado']['estado'], 'devuelta')
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
        self.assertTrue((p / 'dato').is_file())
        self.assertEqual((a / 'dato').read_bytes(), b'x' * 8192)
        self.assertEqual((p / 'dato').read_bytes(), b'B')
        self.assertEqual(datos['copias'][0]['resultado']['estado'], 'devuelta')
        self.assertIn('identidad', datos['copias'][0]['resultado']['error'])
        self.assertIn('horaUTC', datos['copias'][0]['devolucion'])
        self.intactas(NOMBRES[1:])

    def test_lock_despues_reverificacion(self):
        apartar = borrar.cuarentenar
        def nuevo_lock(*args, **kwargs):
            (self.raiz / 'work' / NOMBRES[0] / '.remote-run.lock').write_text('nuevo')
            return apartar(*args, **kwargs)
        with patch.object(borrar, 'cuarentenar', side_effect=nuevo_lock):
            codigo, datos = self.llamar(ejecutar=True)
        self.assertNotEqual(codigo, 0)
        self.assertEqual(datos['copias'][0]['resultado']['estado'], 'devuelta')
        self.assertFalse(datos['copias'][0]['uso']['lock']['libre'])
        self.intactas()

    def test_lsof_proceso_real_cwd(self):
        self.assertTrue(Path('/usr/sbin/lsof').is_file())
        proceso = subprocess.Popen(['/bin/sleep', '60'], cwd=self.raiz / 'work' / NOMBRES[0])
        try:
            codigo, datos = self.llamar(ejecutar=True)
            self.assertNotEqual(codigo, 0)
            uso = datos['copias'][0]['uso']['lsof']
            self.assertIn(str(proceso.pid), uso['stdout'])
            self.assertFalse(uso['libre'])
            self.assertEqual(uso['argumentos'], ['/usr/sbin/lsof', '-a', '-p', '^' + str(os.getpid()),
                                                '-nP', '+D', datos['copias'][0]['cuarentena']])
            self.assertEqual(uso['timeout'], 120)
            self.assertEqual(datos['copias'][0]['resultado']['estado'], 'devuelta')
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
            uso = datos['copias'][0]['uso']['ps']
            self.assertTrue(any(str(proceso.pid) in x for x in uso['coincidencias']))
            self.assertFalse(uso['libre'])
            self.assertEqual(datos['copias'][0]['resultado']['estado'], 'devuelta')
            self.intactas()
        finally:
            proceso.terminate()
            proceso.communicate(timeout=5)

    def test_lsof_error_y_timeout_cuentan_como_uso(self):
        run = borrar.subprocess.run
        for efecto in (subprocess.CompletedProcess([], 2, '', 'error'),
                       subprocess.TimeoutExpired(['lsof'], 120)):
            def ejecutar(args, **kwargs):
                if args[0] == '/usr/sbin/lsof':
                    if isinstance(efecto, Exception):
                        raise efecto
                    return efecto
                return run(args, **kwargs)
            with self.subTest(efecto=efecto), patch.object(borrar.subprocess, 'run', side_effect=ejecutar):
                codigo, datos = self.llamar(ejecutar=True)
            self.assertNotEqual(codigo, 0)
            self.assertFalse(datos['copias'][0]['uso']['lsof']['libre'])
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

    def test_devolucion_no_pisa_copia_recreada(self):
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
        self.assertEqual(copia['resultado']['estado'], 'en-cuarentena')
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
        with patch.object(borrar, 'RAIZ_DEFAULT', str(self.raiz)), \
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
        # El Python de Apple guarda el bytecode bajo ~/Library/Caches/com.apple.python, no junto al código.
        prefijo = self.raiz / 'pycache'
        entorno = dict(os.environ, PYTHONPYCACHEPREFIX=str(prefijo))
        entorno.pop('PYTHONDONTWRITEBYTECODE', None)
        salida = subprocess.run([sys.executable, str(codigo / 'borrar_copias.py'), '--raiz', str(self.raiz)],
                                cwd=self.raiz, env=entorno, capture_output=True, text=True, timeout=180)
        self.assertEqual(salida.returncode, 0, salida.stderr + salida.stdout)
        self.assertFalse(prefijo.exists())
        self.assertFalse((codigo / '__pycache__').exists())

    def test_interrupcion_segunda_con_informe_y_rastro(self):
        vaciar = borrar.vaciar
        llamadas = []
        def interrumpir(*args, **kwargs):
            llamadas.append(args)
            if len(llamadas) == 2:
                raise KeyboardInterrupt()
            return vaciar(*args, **kwargs)
        salida, progreso = io.StringIO(), io.StringIO()
        with patch.object(borrar, 'RAIZ_DEFAULT', str(self.raiz)), \
                patch.object(borrar, 'leer_host', return_value=borrar.HOST_MINI), \
                patch.object(borrar, 'vaciar', side_effect=interrumpir), \
                contextlib.redirect_stdout(salida), contextlib.redirect_stderr(progreso):
            with self.assertRaises(KeyboardInterrupt):
                borrar.main(['--ejecutar'])
        datos = json.loads(salida.getvalue())
        self.assertEqual(datos['resultado'], 'interrumpido')
        self.assertEqual(datos['copias'][0]['resultado']['estado'], 'borrada')
        copia = datos['copias'][1]
        self.assertTrue(Path(copia['cuarentena']).is_dir())
        self.assertIn(copia['cuarentena'], progreso.getvalue())
        self.assertIn('listadoPosterior', datos)
        self.intactas(NOMBRES[2:])

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

    def test_listado_acepta_recreada_y_detecta_identidad_restante(self):
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
            self.assertEqual(datos['copias'][0]['resultado']['estado'], 'devuelta')
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
