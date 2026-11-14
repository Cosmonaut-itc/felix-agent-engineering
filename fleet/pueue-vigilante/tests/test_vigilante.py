from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('PAO_VIGILANTE_SOURCE', str(ROOT)))
spec = importlib.util.spec_from_file_location('vigilante_u11', SOURCE / 'vigilante.py')
v = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = v
spec.loader.exec_module(v)


def fecha(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class Fixture(unittest.TestCase):
    def setUp(self):
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.home = Path(temporal.name)
        self.bin = self.home / '.local/bin'
        self.bin.mkdir(parents=True)
        fake = (ROOT / 'tests/fake_pueue.py').read_text()
        for nombre in ('pueue', 'ssh', 'launchctl', 'systemctl'):
            ruta = self.bin / nombre
            ruta.write_text('#!' + sys.executable + ' -I\n' + fake.split('\n', 1)[1])
            ruta.chmod(0o755)
        entorno = patch.dict(os.environ, HOME=str(self.home), FAKE_ROOT=str(self.home),
                             PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        entorno.start()
        self.addCleanup(entorno.stop)
        self.ahora = time.time()
        self.h = v.Host('desk', self.bin / 'pueue', self.home / '.local/share/pueue/task_logs',
                        self.home / '.local/state/pao/pueue-vigilante', self.home)
        self.h.task_logs.mkdir(parents=True)
        self.h.estado.mkdir(parents=True)
        self.sembrar_pulso()
        self.escribir([self.tarea()])

    def tarea(self, ident=7, minutos=46, **campos):
        return dict(id=ident, created_at=fecha(self.ahora - 60000), group='jale', label=None,
                    command='bash /home/felix/sen-56xx-dbgen/run.sh', path='/home/felix/sen-56xx-dbgen',
                    status={'Running': {'start': fecha(self.ahora - minutos * 60)}}, **campos)

    def escribir(self, tareas):
        self.cola = {'tasks': {str(t['id']): t for t in tareas}}
        (self.home / 'pueue.json').write_text(json.dumps(self.cola))

    def sembrar_pulso(self, ultima=None, reanudado=None):
        (self.h.estado / 'pulso.json').write_text(json.dumps(dict(
            ultima=self.ahora if ultima is None else ultima,
            reanudado_en=self.ahora - 60000 if reanudado is None else reanudado)))

    def log(self, minutos=0, ident=7):
        ruta = self.h.task_logs / f'{ident}.log'
        ruta.write_text('salida\n')
        os.utime(ruta, (self.ahora - minutos * 60,) * 2)
        return ruta

    def pasar(self, modo=v.Modo.CORTAR):
        return v.pasada(self.h, modo, ahora=lambda: self.ahora, gracia_s=0.2)

    def calls(self):
        ruta = self.home / 'calls'
        return [json.loads(linea) for linea in ruta.read_text().splitlines()] if ruta.exists() else []

    def kills(self):
        return [c[1:] for c in self.calls() if c[:2] == ['pueue', 'kill']]

    def filas(self, evento=None):
        ruta = self.h.estado / 'vigilante.log'
        filas = [json.loads(s) for s in ruta.read_text().splitlines()] if ruta.exists() else []
        return [f for f in filas if evento is None or f['evento'] == evento]


class VigilanteTests(Fixture):
    def test_corta_manual_vencida_por_tope(self):
        t = v.Tarea(7, 'huella', 'jale', 'Running', self.ahora - 46 * 60, None, 'bash run.sh', '/tmp')
        self.assertIsInstance(v.decidir(t, v.Limites(2700, 900, 'grupo', None),
                                       self.ahora, 0, self.ahora), v.Cortar)
        self.log()
        self.assertEqual(self.pasar(), 0)
        self.assertEqual(self.kills(), [['kill', '-s', 'SIGTERM', '7']])
        self.assertEqual(self.filas('corte')[0]['motivo'], 'tope')

    def test_corta_manual_por_silencio(self):
        self.escribir([self.tarea(minutos=20)])
        self.log(16)
        self.assertEqual(self.pasar(), 0)
        self.assertEqual(self.filas('corte')[0]['motivo'], 'silencio')

    def test_nunca_toca_lanzadores(self):
        for modo in v.Modo:
            tareas = [self.tarea(i, 600) for i in (1, 2, 3)]
            tareas[0]['label'] = 'apotheca-remote-run'
            tareas[1]['path'] = str(self.home / 'remote-run/runs/20261114-123456-abcdef')
            tareas[2]['command'] = f'exec bash {self.home}/.local/state/jale/pueue/job-Ab123456/task.sh'
            self.escribir(tareas)
            for i in (1, 2, 3):
                self.log(500, i)
            self.assertEqual(self.pasar(modo), 0)
        self.assertTrue(all(c == ['pueue', 'status', '--json'] for c in self.calls()))
        self.assertEqual(self.filas('corte'), [])

    def test_dudosa_no_se_toca_y_se_informa(self):
        tareas = [self.tarea(i, 600) for i in range(1, 6)]
        tareas[0]['path'] = str(self.home / 'remote-run/runs/otra')
        tareas[1]['command'] = 'python3 pueue.py exec abc'
        tareas[2]['command'] = 'exec bash /otro/job-Ab123456/task.sh'
        tareas[3]['label'] = 'apotheca-remote-run-2'
        tareas[4]['command'] = 'bash /otro/remote-run/runs/otra/run.sh'
        self.escribir(tareas)
        self.assertEqual(self.pasar(), 0)
        self.assertEqual(self.kills(), [])
        self.assertEqual(self.filas('pasada')[0]['clases']['dudosa'], 5)

    def test_informe_no_actua(self):
        log = self.log()
        antes = log.read_bytes()
        self.assertEqual(self.pasar(v.Modo.INFORME), 0)
        self.assertEqual(self.calls(), [['pueue', 'status', '--json']])
        self.assertEqual(self.filas('cortaria')[0]['motivo'], 'tope')
        self.assertEqual(log.read_bytes(), antes)

    def test_relectura_con_otra_huella_no_corta(self):
        otra = {'tasks': {'7': self.tarea(minutos=47)}}
        (self.home / 'secuencia.json').write_text(json.dumps([self.cola, otra]))
        self.assertEqual(self.pasar(), 0)
        self.assertEqual(self.kills(), [])
        self.assertEqual(len(self.filas('cambio')), 1)

    def test_ignora_sigterm_y_se_fuerza(self):
        (self.home / 'obstinada-7').touch()
        self.log()
        self.assertEqual(self.pasar(), 0)
        self.assertEqual(self.kills(), [['kill', '-s', 'SIGTERM', '7'], ['kill', '7']])
        self.assertEqual(self.filas('fin_corte')[0]['desenlace'], 'forzada')

    def test_persiste_se_reintenta(self):
        (self.home / 'inmortal-7').touch()
        reloj = [0.0]
        def dormir(segundos):
            reloj[0] += segundos
        with patch.object(v.time, 'monotonic', side_effect=lambda: reloj[0]), patch.object(v.time, 'sleep', dormir):
            self.assertEqual(self.pasar(), 0)
            self.assertEqual(self.pasar(), 0)
        self.assertEqual(len(self.kills()), 4)
        self.assertEqual(len(self.filas('persiste')), 2)
        self.assertEqual([f['desenlace'] for f in self.filas('fin_corte')], ['persiste'] * 2)
        self.assertFalse((self.h.task_logs / '7.log').exists())

    def test_pasadas_cruzadas(self):
        with (self.h.estado / 'pasada.lock').open('a') as candado:
            fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.pasar(), 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(self.filas('omitida')), 1)

    def test_pueue_falla_no_actua(self):
        for fallo in ('status-falla', 'json-invalido'):
            ruta = self.home / fallo
            ruta.touch()
            self.assertEqual(self.pasar(), 1)
            ruta.unlink()
        self.assertEqual(self.kills(), [])
        self.assertEqual(len(self.filas('error')), 2)

    def test_tarea_ilegible_no_tumba_la_pasada(self):
        mala = self.tarea(8)
        del mala['group']
        self.escribir([self.tarea(), mala])
        self.assertEqual(self.pasar(), 0)
        self.assertEqual(self.kills(), [['kill', '-s', 'SIGTERM', '7']])
        self.assertEqual(self.filas('ilegible')[0]['clave'], '8')

    def test_sin_start_o_futuro_es_indeterminada(self):
        for inicio, motivo in ((None, 'sin-inicio'), ('mala', 'sin-inicio'),
                                (fecha(self.ahora + 61), 'inicio-futuro')):
            t = self.tarea()
            t['status']['Running']['start'] = inicio
            self.escribir([t])
            self.assertEqual(self.pasar(), 0)
            self.assertEqual(self.filas('indeterminada')[-1]['motivo'], motivo)
        self.assertEqual(self.kills(), [])

    def test_sin_log_solo_tope(self):
        self.escribir([self.tarea(minutos=20)])
        self.assertEqual(self.pasar(), 0)
        self.assertEqual(self.kills(), [])
        self.escribir([self.tarea()])
        self.assertEqual(self.pasar(), 0)
        self.assertEqual(self.filas('corte')[0]['motivo'], 'tope')

    def test_hueco_abre_ventana(self):
        self.escribir([self.tarea(minutos=60)])
        self.log(60)
        self.sembrar_pulso(ultima=self.ahora - 3 * 3600)
        self.assertEqual(self.pasar(), 0)
        self.assertEqual(self.kills(), [])
        inicio = self.ahora
        for minuto in range(1, 16):
            self.ahora = inicio + minuto * 60
            self.assertEqual(self.pasar(), 0)
        self.assertEqual(len(self.filas('hueco')), 1)
        self.assertEqual(self.filas('corte')[0]['motivo'], 'silencio')

    def test_pulso_corrupto_es_hueco(self):
        for contenido in ('{', '{}', 'null', '{"ultima":NaN,"reanudado_en":0}'):
            (self.h.estado / 'pulso.json').write_text(contenido)
            self.assertEqual(self.pasar(), 0)
        self.assertEqual(len(self.filas('hueco')), 4)
        self.assertEqual(self.kills(), [])

    def test_etiqueta_escape(self):
        t = v.Tarea(7, 'h', 'jale', 'Running', self.ahora - 46 * 60, None, 'bash x', '/tmp')
        lim = v.limites('jale', 'tope=90')
        self.assertIsInstance(v.decidir(t, lim, self.ahora, 0, self.ahora), v.Seguir)
        self.assertIsInstance(v.decidir(replace(t, inicio=self.ahora - 91 * 60), lim,
                                       self.ahora, 0, self.ahora), v.Cortar)
        for etiqueta in ('tope=200', 'tope=0', 'tope=01', 'tope=2 tope=3', 'tope=abc', 'tope=2,'):
            with self.subTest(etiqueta=etiqueta):
                lim = v.limites('jale', etiqueta)
                self.assertEqual(lim.tope_s, 45 * 60)
                self.assertIsNotNone(lim.aviso)
        self.assertEqual(v.limites('rapidas', 'tope=30').tope_s, 600)
        self.assertEqual(v.limites('jale', 'tope=1').tope_s, 60)
        self.assertEqual(v.limites('jale', 'silencio=40').silencio_s, 2400)
        self.assertEqual(v.limites('desconocido', None).tope_s, 2700)
        self.assertEqual(v.limites('pesadas', None).tope_s, 9000)

    def test_solo_running(self):
        tareas = [self.tarea(i) for i in range(1, 6)]
        for t, estado in zip(tareas, ('Paused', 'Queued', 'Stashed', 'Locked', 'Done')):
            t['status'] = {estado: {'start': fecha(self.ahora - 60000)}}
        self.escribir(tareas)
        self.assertEqual(self.pasar(), 0)
        self.assertEqual(self.kills(), [])

    def test_fechas(self):
        base = datetime(2026, 11, 14, 12, tzinfo=timezone.utc).timestamp()
        for fraccion, esperado in (('1', .1), ('123', .123), ('123456', .123456), ('123456789', .123456)):
            for hora in ('12:00:00.{}Z', '06:00:00.{}-06:00'):
                self.assertAlmostEqual(v.epoch('2026-11-14T' + hora.format(fraccion)), base + esperado, places=5)
        self.assertIsNone(v.epoch('mala'))
        self.assertIsNone(v.epoch(None))

    def test_marca_tras_el_fin(self):
        ruta = self.log()
        self.assertEqual(self.pasar(), 0)
        lineas = ruta.read_text().splitlines()
        self.assertTrue(lineas[-1].startswith('PAO_VIGILANTE_CORTE motivo=tope'))
        self.assertIn('última salida del hijo', lineas)
        self.assertIn('Done', json.loads((self.home / 'pueue.json').read_text())['tasks']['7']['status'])

    def test_bitacora_rota_a_1_mib(self):
        ruta = self.h.estado / 'vigilante.log'
        ruta.write_bytes(b'x' * (1024 * 1024))
        v.bitacora(self.h, 'prueba')
        self.assertEqual(ruta.with_name('vigilante.log.1').stat().st_size, 1024 * 1024)
        self.assertEqual(self.filas()[0]['evento'], 'prueba')

    def test_censo_es_lectura(self):
        antes = {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        censo = json.loads(v.censo(self.h, self.home))
        self.assertEqual(censo['clases'], {'manual': 1, 'lanzador': 0, 'dudosa': 0})
        self.assertEqual(censo['reglas'], {'ninguna': 1})
        self.assertEqual(self.calls(), [['pueue', 'status', '--json']])
        despues = {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob('*')
                   if p.is_file() and p.name != 'calls'}
        self.assertEqual(antes, despues)

    def test_no_fuerza_si_cambia_clase_durante_gracia(self):
        manual = self.cola
        bodega = json.loads(json.dumps(manual))
        bodega['tasks']['7']['label'] = 'apotheca-remote-run'
        (self.home / 'obstinada-7').touch()
        (self.home / 'secuencia.json').write_text(json.dumps([manual, manual, manual, bodega]))
        self.assertEqual(self.pasar(), 0)
        self.assertEqual(self.kills(), [['kill', '-s', 'SIGTERM', '7']])
        self.assertEqual(self.filas('fin_corte')[0]['desenlace'], 'cambio')
        self.assertFalse((self.h.task_logs / '7.log').exists())

    def test_no_expone_comando_ni_entorno_en_bitacora(self):
        t = self.tarea()
        t['command'] = 'TOKEN=secreto bash run.sh --password oculto'
        t['envs'] = {'OTRO': 'muy-secreto'}
        self.escribir([t])
        self.assertEqual(self.pasar(v.Modo.INFORME), 0)
        texto = (self.h.estado / 'vigilante.log').read_text()
        self.assertNotIn('secreto', texto)
        self.assertNotIn('oculto', texto)
        self.assertEqual(self.filas('cortaria')[0]['programa'], 'bash')

    def test_host_y_retroceso_de_reloj(self):
        mini = v.host('mini', Path('/opt/homebrew/bin/pueue'), self.home)
        self.assertEqual(mini.task_logs, self.home / 'Library/Application Support/pueue/task_logs')
        self.sembrar_pulso(ultima=self.ahora + 100)
        self.assertEqual(self.pasar(), 0)
        self.assertEqual(len(self.filas('hueco')), 1)
        self.assertEqual(self.kills(), [])
