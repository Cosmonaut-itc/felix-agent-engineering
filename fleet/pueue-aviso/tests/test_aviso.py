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
GIB = 1024**3
MINIMO_DISCO = 25 * GIB
DISCO_BAJO = {'total': 500 * GIB, 'free': 24 * GIB}
DISCO_UMBRAL = {'total': 500 * GIB, 'free': MINIMO_DISCO}
DISCO_SANO = {'total': 500 * GIB, 'free': MINIMO_DISCO + 1}
SIN_DISCO = object()


def control(razones):
    return dict(schema='rack-host-result@1', status='ok', pause_reasons=razones)


def tarea(grupo, estado='Queued'):
    return dict(group=grupo, state=estado)


def foto(host, momento, control_host, tareas, fresh=True, errores=(), abiertos=()):
    # Por omisión Pueue tiene pausado todo grupo con alguna razón de pausa.
    razones = control_host.get('pause_reasons', {}) if isinstance(control_host, dict) else {}
    grupos = [dict(name=g, status='Paused' if motivos and g not in abiertos else 'Running', slots=1)
              for g, motivos in razones.items()]
    return dict(id=host, fresh=fresh, snapshot=dict(schema='rack-host@1', host=host, taken=momento,
                control=control_host, groups=grupos, tasks=tareas, errors=list(errores)))


def rack(*hosts):
    return dict(schema='rack@1', hosts=list(hosts))


class AvisoTests(unittest.TestCase):
    def setUp(self):
        self.estado = {}

    def pasar(self, segundos, razones=None, tareas=(), disponible=True, abiertos=()):
        ahora = INICIO + segundos
        desk = foto('desk', ahora, control(razones or {}), list(tareas), fresh=disponible, abiertos=abiertos)
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

    def test_grupo_abierto_con_razon_guardada_no_avisa_y_cierra_el_episodio(self):
        # pausar guarda la razón antes de la RPC; si la RPC falla, el grupo sigue Running.
        razones, tareas = {'hasito': ['manual']}, [tarea('hasito')]
        self.assertEqual(self.pasar(0, razones, tareas), [])
        self.assertEqual(self.pasar(5, razones, tareas, abiertos=('hasito',)), [])
        self.assertEqual(self.estado['desk']['grupos'], {})
        self.assertEqual(self.pasar(UMBRAL + 6, razones, tareas, abiertos=('hasito',)), [])

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
        self.assertEqual(lecturas, ({'desk': {'default': (('ambiguous', 'startup'), 1)}, 'mini': {}}, None))
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
            dict(id='desk', fresh=True, snapshot=dict(schema='rack-host@1', host='desk', taken=INICIO,
                 control=control({}), tasks=[], errors=[])),
        )
        for desk in casos:
            with self.subTest(desk=desk):
                lectura = aviso.observar(rack(desk, mini), INICIO)
                self.assertEqual(lectura, ({'desk': None, 'mini': {'suites': (('manual',), 1)}}, None))

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
            casos = (
                ({'rack_url': URL}, 1800, MINIMO_DISCO),
                ({'rack_url': URL, 'minutos': 8, 'disco_minimo_bytes': 30 * GIB}, 480, 30 * GIB),
            )
            for datos, umbral, minimo in casos:
                config.write_text(json.dumps(datos), encoding='utf-8')
                self.assertEqual(aviso.main(), 0)
                pasada.assert_called_with(Path(tmp) / '.local/state/pao/aviso-fila-cerrada', URL, umbral, minimo)


class DiscoTests(unittest.TestCase):
    def setUp(self):
        temporal = TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.directorio = Path(temporal.name)
        self.ruta = self.directorio / 'estado.json'

    def pasar(self, segundos, disco=DISCO_BAJO, *, host='mini', fresh=True, errores=(),
              fallo_entrega=False):
        ahora = INICIO + segundos
        fotos = {nombre: foto(nombre, ahora, control({}), []) for nombre in ('desk', 'mini')}
        fotos[host]['fresh'] = fresh
        fotos[host]['snapshot']['errors'] = list(errores)
        if disco is not SIN_DISCO:
            fotos[host]['snapshot']['disk'] = disco
        with patch.object(aviso, 'leer_rack', return_value=rack(*fotos.values())), \
                patch.object(aviso.time, 'time', return_value=ahora), \
                patch.object(aviso, 'notificar') as enviar, patch('sys.stderr'):
            if fallo_entrega:
                enviar.side_effect = subprocess.TimeoutExpired('osascript', 10)
            self.assertEqual(aviso.pasada(self.directorio, URL, UMBRAL), int(fallo_entrega))
        return [llamada.args[0].mensaje for llamada in enviar.call_args_list]

    def test_disco_bajo_avisa_en_la_primera_pasada(self):
        self.assertEqual(self.pasar(0, DISCO_UMBRAL), [])
        self.assertEqual(self.pasar(1, DISCO_SANO), [])
        self.assertEqual(self.pasar(2), ['mini · disco bajo · 24.00 GiB libres · umbral 25.00 GiB'])

    def test_disco_bajo_repite_al_cumplir_el_intervalo(self):
        self.assertEqual(len(self.pasar(0)), 1)
        self.assertEqual(self.pasar(1), [])
        self.assertEqual(self.pasar(UMBRAL - 1), [])
        self.assertEqual(len(self.pasar(UMBRAL)), 1)
        self.assertEqual(self.pasar(UMBRAL + 1), [])
        self.assertEqual(len(self.pasar(2 * UMBRAL)), 1)

    def test_recuperacion_cierra_el_episodio_y_permite_otro_aviso_inmediato(self):
        self.assertEqual(len(self.pasar(0)), 1)
        self.assertEqual(self.pasar(1, DISCO_SANO), [])
        self.assertEqual(len(self.pasar(2)), 1)
        self.assertEqual(self.pasar(3, DISCO_UMBRAL), [])
        self.assertEqual(len(self.pasar(4)), 1)

    def test_lectura_de_disco_invalida_no_abre_ni_cierra_el_episodio(self):
        invalidas = (SIN_DISCO, None, [], {'total': 500 * GIB},
                     *({'total': 500 * GIB, 'free': libre} for libre in (True, '0', 0.0, -1)))
        for disco in invalidas:
            with self.subTest(disco=disco):
                self.assertEqual(self.pasar(0, disco), [])
                self.assertNotIn('disco_bajo', aviso.cargar_estado(self.ruta)['mini'])
        self.assertEqual(len(self.pasar(1)), 1)
        for disco in invalidas:
            with self.subTest(disco=disco):
                self.assertEqual(self.pasar(2, disco), [])
                self.assertEqual(self.pasar(3), [])
        self.assertEqual(self.pasar(UMBRAL + 1, None), [])
        self.assertEqual(len(self.pasar(UMBRAL + 2)), 1)

    def test_disco_del_desk_y_foto_no_fresca_de_la_mini_no_avisan(self):
        self.assertEqual(self.pasar(0, host='desk'), [])
        self.assertEqual(self.pasar(1, fresh=False), [])
        self.assertEqual(len(self.pasar(2)), 1)
        self.assertEqual(self.pasar(3, DISCO_SANO, fresh=False), [])
        self.assertEqual(self.pasar(4), [])

    def test_estado_anterior_se_conserva_sin_inventar_un_aviso(self):
        anterior = {
            'schema': 'pao-aviso-fila@1',
            'hosts': {
                'mini': {'sin_lectura': None, 'grupos': {}},
                'desk': {'sin_lectura': None, 'grupos': {}},
            },
        }
        self.ruta.write_text(json.dumps(anterior), encoding='utf-8')
        self.assertEqual(aviso.cargar_estado(self.ruta), anterior['hosts'])
        self.assertEqual(self.pasar(0, SIN_DISCO), [])
        self.assertEqual(aviso.cargar_estado(self.ruta), anterior['hosts'])

    def test_aviso_de_disco_fallido_se_reintenta_y_la_entrega_se_persiste(self):
        self.assertEqual(len(self.pasar(0, fallo_entrega=True)), 1)
        self.assertEqual(len(self.pasar(1)), 1)
        self.assertEqual(self.pasar(UMBRAL), [])
        self.assertEqual(len(self.pasar(UMBRAL + 1)), 1)

    def test_una_falla_en_la_cola_no_invalida_el_disco_de_una_foto_fresca(self):
        self.assertEqual(len(self.pasar(0, errores=[{'stage': 'pueue'}])), 1)

    def test_umbral_invalido_se_rechaza_como_minutos(self):
        with patch.object(aviso.Path, 'home', return_value=self.directorio), \
                patch.object(aviso, 'pasada') as pasada, patch('sys.stderr') as stderr:
            config = self.directorio / '.config/pao/aviso-fila-cerrada.json'
            config.parent.mkdir(parents=True)
            for clave in ('minutos', 'disco_minimo_bytes'):
                for valor in (0, -1, True, '1', 1.0, None):
                    with self.subTest(clave=clave, valor=valor):
                        config.write_text(json.dumps({'rack_url': URL, clave: valor}), encoding='utf-8')
                        self.assertEqual(aviso.main(), 2)
            pasada.assert_not_called()
            stderr.write.assert_any_call('Revisa ~/.config/pao/aviso-fila-cerrada.json.')
