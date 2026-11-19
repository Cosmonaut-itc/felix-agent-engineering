import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


BASE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('aviso_fila', BASE / 'aviso.py')
aviso = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aviso)

INICIO = 100_000
UMBRAL = 30 * 60
URL = 'http://rack.example:8788/api/rack.json'


def control(razones):
    return dict(schema='rack-host-result@1', status='ok', pause_reasons=razones)


def tarea(grupo, estado='Queued'):
    return dict(group=grupo, state=estado)


def foto(host, momento, control_host, tareas, fresh=True, errores=()):
    return dict(id=host, fresh=fresh, snapshot=dict(schema='rack-host@1', host=host, taken=momento,
                control=control_host, tasks=tareas, errors=list(errores)))


def rack(*hosts):
    return dict(schema='rack@1', hosts=list(hosts))


class AvisoTests(unittest.TestCase):
    def setUp(self):
        self.estado = {}

    def pasar(self, segundos, razones=None, tareas=(), disponible=True):
        ahora = INICIO + segundos
        desk = foto('desk', ahora, control(razones or {}), list(tareas), fresh=disponible)
        mini = foto('mini', ahora, control({}), [])
        self.estado, avisos = aviso.decidir(self.estado, aviso.observar(rack(desk, mini), ahora), ahora, UMBRAL)
        for nota in avisos:
            host = self.estado[nota.host]
            episodio = host['sin_lectura'] if nota.grupo is None else host['grupos'][nota.grupo]
            episodio['avisado'] = True  # Simula una entrega confirmada por osascript.
        return avisos

    def test_manual_solo_despues_del_umbral(self):
        razones = {'hasito': ['manual', 'startup']}
        tareas = [tarea('hasito'), tarea('hasito'), tarea('hasito', 'Paused')]
        for segundos in (0, UMBRAL - 1, UMBRAL):
            self.assertEqual(self.pasar(segundos, razones, tareas), [])
        self.assertEqual(self.pasar(UMBRAL + 1, razones, tareas), [
            aviso.Aviso('desk', 'hasito', 'desk/hasito · manual, startup · 30 min · 2 en fila')])

    def test_ambiguous_solo_despues_del_umbral(self):
        razones = {'otros': ['ambiguous']}
        tareas = [tarea('otros')]
        self.assertEqual(self.pasar(0, razones, tareas), [])
        self.assertEqual(self.pasar(UMBRAL, razones, tareas), [])
        self.assertEqual(self.pasar(UMBRAL + 1, razones, tareas), [
            aviso.Aviso('desk', 'otros', 'desk/otros · ambiguous · 30 min · 1 en fila')])

    def test_sin_cola_o_solo_presion_no_avisa(self):
        razones = {'hasito': ['manual'], 'otros': ['pressure']}
        tareas = [tarea('hasito', 'Running'), tarea('otros')]
        self.assertEqual(self.pasar(0, razones, tareas), [])
        self.assertEqual(self.pasar(UMBRAL + 1, razones, tareas), [])
        self.assertEqual(self.estado['desk']['grupos'], {})

    def test_un_aviso_por_episodio_y_otro_al_volver_a_cerrar(self):
        razones, tareas = {'hasito': ['manual']}, [tarea('hasito')]
        self.assertEqual(self.pasar(0, razones, tareas), [])
        primero = self.pasar(UMBRAL + 1, razones, tareas)
        self.assertEqual(len(primero), 1)
        self.assertEqual(self.pasar(UMBRAL + 2, razones, tareas), [])
        self.assertEqual(self.pasar(UMBRAL + 3, {}, tareas), [])
        self.assertEqual(self.pasar(UMBRAL + 4, razones, tareas), [])
        self.assertEqual(self.pasar(2 * UMBRAL + 5, razones, tareas), primero)
        self.assertEqual(self.pasar(2 * UMBRAL + 6, razones, []), [])
        self.assertEqual(self.pasar(2 * UMBRAL + 7, razones, tareas), [])
        self.assertEqual(self.pasar(3 * UMBRAL + 8, razones, tareas), primero)

    def test_fallo_conserva_episodios_y_solo_avisa_una_vez_por_interrupcion(self):
        razones, tareas = {'hasito': ['manual']}, [tarea('hasito')]
        self.assertEqual(self.pasar(0, razones, tareas), [])
        self.assertEqual(self.pasar(5, {'jale': ['manual']}, [tarea('jale')], disponible=False), [])
        self.assertEqual(set(self.estado['desk']['grupos']), {'hasito'})
        self.assertEqual(self.pasar(UMBRAL + 5, disponible=False), [])
        sin_lectura = [aviso.Aviso('desk', None, 'No pude leer desk desde hace 30 min.')]
        self.assertEqual(self.pasar(UMBRAL + 6, disponible=False), sin_lectura)
        self.assertEqual(self.pasar(UMBRAL + 7, disponible=False), [])
        self.assertEqual(self.pasar(UMBRAL + 8, razones, tareas), [
            aviso.Aviso('desk', 'hasito', 'desk/hasito · manual · 30 min · 1 en fila')])
        self.assertEqual(self.pasar(UMBRAL + 9, disponible=False), [])
        self.assertEqual(self.pasar(2 * UMBRAL + 10, disponible=False), sin_lectura)
        self.assertEqual(self.pasar(2 * UMBRAL + 11, razones, tareas), [])

    def test_estado_ausente_o_corrupto_arranca_sin_avisos(self):
        with TemporaryDirectory() as tmp:
            ruta = Path(tmp) / 'estado.json'
            datos = (None, '{', '[]', json.dumps(dict(schema=aviso.ESQUEMA_ESTADO, hosts={
                'mini': {'sin_lectura': None, 'grupos': {}},
                'desk': {'sin_lectura': None, 'grupos': {'hasito': {'inicio': True, 'avisado': False}}}
            })))
            for crudo in datos:
                with self.subTest(crudo=crudo):
                    if crudo is not None:
                        ruta.write_text(crudo, encoding='utf-8')
                    self.estado = aviso.cargar_estado(ruta)
                    self.assertEqual(self.estado, {})
                    self.assertEqual(self.pasar(UMBRAL + 1, {'hasito': ['manual']}, [tarea('hasito')]), [])

    def test_proyeccion_usa_capabilities_reales_de_ambos_hosts(self):
        ruta = Path(__file__).with_name('fixtures') / 'capabilities.json'
        capturas = json.loads(ruta.read_text(encoding='utf-8'))
        # La tarea de default simula fila para la pausa que se observó en desk.
        # Las 13 tareas de rapidas reproducen el conteo observado en el rack de la Mini.
        desk = foto('desk', INICIO, capturas['desk'], [tarea('default')])
        mini = foto('mini', INICIO, capturas['mini'], [tarea('rapidas')] * 13)
        lecturas = aviso.observar(rack(desk, mini), INICIO)
        self.assertEqual(lecturas, {'desk': {'default': (('ambiguous', 'startup'), 1)}, 'mini': {}})
        estado, avisos = aviso.decidir({}, lecturas, INICIO, UMBRAL)
        self.assertEqual(avisos, [])
        _, avisos = aviso.decidir(estado, lecturas, INICIO + UMBRAL + 1, UMBRAL)
        self.assertEqual([(a.host, a.grupo) for a in avisos], [('desk', 'default')])

    def test_rack_antiguo_o_parcial_invalida_solo_al_host_afectado(self):
        mini = foto('mini', INICIO, control({'suites': ['manual']}), [tarea('suites')])
        casos = (
            foto('desk', INICIO - aviso.ANTIGUEDAD_MAXIMA - 1, control({}), []),
            foto('desk', INICIO, control({}), [], fresh=False),
            foto('desk', INICIO, control({}), [], errores=[{'stage': 'pueue'}]),
            foto('desk', INICIO, {'schema': 'rack-host-result@1', 'status': 'busy'}, []),
            foto('desk', INICIO, control({}), [{'group': 'hasito'}]),
        )
        for desk in casos:
            with self.subTest(desk=desk):
                lectura = aviso.observar(rack(desk, mini), INICIO)
                self.assertEqual(lectura, {'desk': None, 'mini': {'suites': (('manual',), 1)}})

    def test_entrega_fallida_reintenta_y_entrega_exitosa_persiste(self):
        with TemporaryDirectory() as tmp, patch.object(aviso, 'leer_rack') as leer, \
                patch.object(aviso.time, 'time') as reloj, patch.object(aviso, 'notificar') as enviar, \
                patch('sys.stderr'):
            directorio = Path(tmp)
            enviar.side_effect = [subprocess.TimeoutExpired('osascript', 10), None]
            for segundos, codigo in ((0, 0), (61, 1), (62, 0), (63, 0)):
                ahora = INICIO + segundos
                reloj.return_value = ahora
                leer.return_value = rack(foto('desk', ahora, control({'hasito': ['manual']}), [tarea('hasito')]),
                                          foto('mini', ahora, control({}), []))
                self.assertEqual(aviso.pasada(directorio, URL, 60), codigo)
            self.assertEqual(enviar.call_count, 2)
            self.assertEqual(enviar.call_args_list[0], enviar.call_args_list[1])
            self.assertTrue(aviso.cargar_estado(directorio / 'estado.json')['desk']['grupos']['hasito']['avisado'])

    def test_launchagent_y_umbral_configurable(self):
        ruta = BASE / 'launchagents/local.pao.aviso-fila-cerrada.plist'
        agente = plistlib.loads(ruta.read_bytes())
        self.assertEqual(agente['Label'], ruta.stem)
        self.assertEqual(agente['StartInterval'], 180)
        self.assertTrue(agente['RunAtLoad'])
        self.assertIn('/usr/bin/python3 -I "$HOME/.local/lib/pao-aviso-fila/aviso.py"', agente['ProgramArguments'][2])
        with TemporaryDirectory() as tmp, patch.object(aviso.Path, 'home', return_value=Path(tmp)), \
                patch.object(aviso, 'pasada', return_value=0) as pasada:
            config = Path(tmp) / '.config/pao/aviso-fila-cerrada.json'
            config.parent.mkdir(parents=True)
            for datos, umbral in (({'rack_url': URL}, 1800), ({'rack_url': URL, 'minutos': 8}, 480)):
                config.write_text(json.dumps(datos), encoding='utf-8')
                self.assertEqual(aviso.main(), 0)
                pasada.assert_called_with(Path(tmp) / '.local/state/pao/aviso-fila-cerrada', URL, umbral)
