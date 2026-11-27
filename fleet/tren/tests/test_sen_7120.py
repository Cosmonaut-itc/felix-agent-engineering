"""Atribución paralela con entregas, repositorios y recibos sintéticos."""
from dataclasses import replace
from itertools import combinations, count
import json
import os
import subprocess
import time
import unittest
from unittest.mock import Mock, patch

import test_activo as activo
import test_jale as jale
import test_sen_7280_prelanzar as prelanzar
from test_hasito import B, C, CANDIDATO
from test_tren import Aislado, estado, observacion, vista
from adaptadores import seleccionar
from adaptadores.hasito import Hasito, ResultadoInvalido
from adaptadores.jale import EN_CURSO, Jale
from atribucion import planificar
from dominio import (Candidato, ConflictoCadena, Diagnosticando, Evidencia, Incompleto,
                     Intento, RechazoLanzador, Repeticion, Reproduccion, Terminal, Tren,
                     UnidadRepetida, avanzar_suite, clave, desde_datos, efecto_diagnostico)
from efectos import ejecutar
from git_ops import FalloGit
from operador import FalloSnapshot, lanzamiento_activo
from operativo import resultado_observacion

FALLO = jale.FALLO
OTRO = replace(FALLO, unidad=jale.OTRA)
PRS = (1, 2, 3, 4)
LOTE = Candidato(B, tuple((pr, 'a' * 40, str(pr) * 40) for pr in PRS), C, 'd' * 40)
TREN = 'lote-sintetico'


def evidencia(solicitud, falla=False, duracion=4):
    f = solicitud.fallo
    unidad = UnidadRepetida(f.check, f.unidad, f.parte, f.check + ':' + f.unidad, tuple(
        Intento(n, 'falla' if falla else 'pasa', f.firma if falla else '', duracion)
        for n in (1, 2)))
    sha = C if solicitud.objetivo == 'C' else B
    return Evidencia(solicitud, Repeticion(sha, C, B, 'e' * 64, (unidad,)))


def plan(historial=(), presupuesto=500, *, fallos=(FALLO,), paralelo=False):
    return planificar(fallos, PRS, historial, presupuesto, en_paralelo=paralelo)


def confirmado():
    c, b = plan().tanda
    return evidencia(c, falla=True), evidencia(b)


def diagnostico(historial, pedido, **kwargs):
    inicial = estado(4)
    fase = Diagnosticando(LOTE, (FALLO,), 'suite.json', evidencias=historial,
                          pendiente=pedido, **kwargs)
    tren = Tren(TREN, tuple(e.entrega.recibo for e in inicial.entradas), fase)
    return replace(inicial, trenes=(tren,)), tren


class PlanTests(unittest.TestCase):
    def test_confirmaciones_priorizadas_y_sin_duplicados(self):
        p = plan(fallos=(OTRO, FALLO, FALLO))
        self.assertEqual(p.siguiente, plan().siguiente)
        self.assertEqual([(s.fallo, s.objetivo) for s in p.tanda],
                         [(f, o) for f in (FALLO, OTRO) for o in ('C', 'B')])
        self.assertEqual(len({s.id for s in p.tanda}), 4)
        caro = replace(OTRO, duracion_s=5)
        self.assertEqual(plan(fallos=(FALLO, caro), presupuesto=8).tanda, p.tanda[:2])
        c, b = p.tanda[2:]
        posterior = (evidencia(c), evidencia(b))
        self.assertEqual(plan(posterior, fallos=(FALLO, OTRO)).tanda, p.tanda[:2])

    def test_subconjuntos_individuales_en_orden(self):
        p = plan(confirmado(), paralelo=True)
        self.assertEqual(p.siguiente, p.tanda[0])
        self.assertEqual([s.prs for s in p.tanda], [(pr,) for pr in PRS])

    def test_veredicto_con_evidencia_en_cualquier_orden(self):
        historial = confirmado()
        serie = historial
        while (p := plan(serie)).siguiente is not None:
            serie += (evidencia(p.siguiente, falla=p.siguiente.prs == (2,)),)
        self.assertTrue(p.terminado)
        self.assertEqual(p.causas[0].minimos, ((2,),))
        tanda = tuple(evidencia(s, falla=s.prs == (2,)) for s in plan(historial).tanda)
        for orden in (tanda, tanda[::-1]):
            self.assertEqual(plan(historial + orden, paralelo=True), p)

    def test_combinaciones_esperan_individuales_y_excluyen_minimos(self):
        historial = confirmado()
        simples = plan(historial).tanda
        antes = historial + tuple(evidencia(s) for s in simples[:-1])
        self.assertEqual(plan(antes).tanda, simples[-1:])
        self.assertTrue(plan(antes + (evidencia(simples[-1], falla=True),)).terminado)
        historial = antes + (evidencia(simples[-1]),)
        pares = plan(historial).tanda
        self.assertEqual([s.prs for s in pares], list(combinations(PRS, 2)))
        historial += tuple(evidencia(s, falla=s.prs == (1, 2)) for s in pares)
        self.assertEqual([s.prs for s in plan(historial).tanda], [(1, 3, 4), (2, 3, 4)])

    def test_presupuesto_y_reanudacion_suman_por_tanda(self):
        historial = confirmado()
        p = plan(historial, 16)
        self.assertEqual(p.incierto, 'presupuesto-agotado:' + FALLO.unidad)
        p = plan(historial, 16, paralelo=True)
        s = p.siguiente
        self.assertEqual(s.prs, (1,))
        self.assertEqual(len(p.tanda), 4)
        inicial, tren = diagnostico(historial, s, presupuesto_s=24, esperar_hasta=100,
                                    subconjunto=LOTE, reanudar='reproduccion')
        serie, _ = avanzar_suite(inicial, tren, observacion(4))
        self.assertEqual(serie.pausa.detalle, 'presupuesto-agotado-en-reanudacion')
        paralelo, efectos = avanzar_suite(inicial, tren,
                                           observacion(4, repeticiones_en_paralelo=True))
        self.assertIsNone(paralelo.pausa)
        self.assertEqual(efectos[0].tipo, 'repetir')
        self.assertEqual(paralelo.trenes[0].fase.costo_infra_s, s.costo_s)


CONTROLES = '''
f = dict(trabajo['plan'])
registro = Path(log).parent
intento = registro.parent.name

def esperar(rutas):
    limite = time.monotonic() + 5
    while not all(r.exists() for r in rutas):
        if time.monotonic() >= limite:
            sys.exit(2)
        time.sleep(.01)

if filtros:
    (registro / 'marca').touch()
    if (raiz / 'barrera-intentos').exists() and registro.parents[2].name != 'serie':
        otro = '2' if intento == '1' else '1'
        esperar([registro.parents[1] / otro / registro.name / 'marca'])
    if (raiz / 'en-serie').exists() and intento == '2':
        if not (registro.parents[1] / '1' / registro.name / 'codigo.json').exists():
            sys.exit(2)
    if (raiz / 'sin-recibo').exists() and intento == '1' and trabajo['conexiones'] == 1:
        f.update(codigo=1, sin_linea=True)
    if (raiz / 'primero-75').exists() and intento == '1' and registro.name == '0':
        f.update(codigo=75, sin_linea=True)
    if (raiz / 'primero-malformado').exists() and intento == '1':
        f.update(malformado=True)

producto = raiz / 'producto.json'
if producto.exists():
    import subprocess
    origen, culpable, subconjuntos = json.loads(producto.read_text())
    contiene = subprocess.run(['git', '--git-dir', origen, 'merge-base', '--is-ancestor',
                               culpable, sha]).returncode == 0
    f['estados'] = ['falla' if contiene else 'pasa']
    if filtros and sha in subconjuntos:
        (raiz / ('inicio-' + sha + '-' + intento)).touch()
        if (raiz / 'barrera-tanda').exists():
            esperar([raiz / ('inicio-' + s + '-' + n) for s in subconjuntos for n in ('1', '2')])
'''


class ProductorParalelo(prelanzar.Productor):
    def preparar_productor(self):
        super().preparar_productor()
        productor = self.raiz / 'productor.py'
        productor.write_text(jale.PRODUCTOR.replace("f = trabajo['plan']", CONTROLES))


class JaleParaleloTests(ProductorParalelo, Aislado):
    def setUp(self):
        super().setUp()
        self.preparar_productor()
        self.adaptador = self.jale(4)
        ruta = self.raiz / 'origen.json'
        ruta.write_text(json.dumps([dict(sha=C, modo='contract', unidades=[
            {'unidad': fallo.unidad} for fallo in (FALLO, OTRO)])]))
        self.origen = str(ruta)
        self.pedidos = tuple((s, C if s.objetivo == 'C' else B)
                             for s in plan(fallos=(FALLO, OTRO)).tanda)

    def jale(self, paralelas, nombre='jale'):
        return Jale(self.lanzador, modos=('contract',), estado=self.raiz / nombre,
                    repeticiones_paralelas=paralelas)

    def adelantar(self, pedidos=None, adaptador=None):
        (adaptador or self.adaptador).adelantar_repeticiones(
            CANDIDATO, self.origen, self.pedidos if pedidos is None else pedidos, TREN)

    def repetir(self, pedido=None, adaptador=None, **kwargs):
        s, commit = pedido or self.pedidos[0]
        return jale.esperar_resultado((adaptador or self.adaptador).repetir,
            CANDIDATO, self.origen, s, commit, TREN, **kwargs)

    def registros(self, pedido):
        raiz = self.adaptador.estado / clave('repetir', TREN, pedido[0].id)
        return [raiz / n / '0' for n in ('1', '2')]

    def test_intentos_simultaneos_conservan_resultado(self):
        (self.raiz / 'barrera-intentos').touch()
        paralelo = self.repetir()
        serie = self.repetir(adaptador=self.jale(1, 'serie'))
        self.assertIsInstance(paralelo, Repeticion)
        self.assertEqual(paralelo, replace(serie,
            unidades=tuple(replace(u, intentos=tuple(
                replace(i, ensayo=i.ensayo.replace('/serie/', '/jale/')) for i in u.intentos))
                for u in serie.unidades),
            comando=tuple(arg.replace('/serie/', '/jale/') for arg in serie.comando)))

    def test_adelanta_y_vuelve_con_corridas_vivas(self):
        self.plan([{'esperar': True}])
        pedidos = self.pedidos[1:]
        self.adelantar(pedidos)
        for pedido in pedidos:
            for registro in self.registros(pedido):
                self.assertTrue(lanzamiento_activo(registro / 'candado'))
                self.assertFalse((registro / 'codigo.json').exists())
        (self.raiz / 'seguir').touch()
        for pedido in pedidos:
            self.assertIsInstance(self.repetir(pedido), Repeticion)
        self.assertEqual(len(self.invocaciones()), 6)

    def test_adelanto_distingue_vivo_terminado_y_sin_lanzar(self):
        self.plan([{'esperar': True}])
        pedido, otro = self.pedidos[:2]
        self.adelantar((pedido,))
        self.assertEqual(self.adaptador.adelanto(CANDIDATO, TREN, pedido[0]), 'vivo')
        self.assertEqual(self.adaptador.adelanto(CANDIDATO, TREN, otro[0]), '')
        (self.raiz / 'seguir').touch()
        for registro in self.registros(pedido):
            self.esperar_archivo(registro / 'codigo.json')
        limite = time.monotonic() + 5
        while (any(lanzamiento_activo(r / 'candado') for r in self.registros(pedido))
               and time.monotonic() < limite):
            time.sleep(.01)
        self.assertEqual(self.adaptador.adelanto(CANDIDATO, TREN, pedido[0]), 'terminado')

    def test_adelanto_de_base_cuenta_los_intentos_servidos_por_la_cache(self):
        base = self.pedidos[1]
        self.assertEqual(base[0].objetivo, 'B')
        self.plan([{}, {'codigo': 75, 'sin_linea': True}])
        anterior = jale.esperar_resultado(self.jale(1).repetir, CANDIDATO, self.origen, base[0], base[1], 'lote-anterior')
        self.assertIsInstance(anterior, Incompleto)
        self.assertEqual(self.adaptador.adelanto(CANDIDATO, TREN, base[0]), '')
        self.plan([{}])
        self.adelantar((base,))
        primero, segundo = self.registros(base)
        self.esperar_archivo(segundo / 'codigo.json')
        limite = time.monotonic() + 5
        while lanzamiento_activo(segundo / 'candado') and time.monotonic() < limite:
            time.sleep(.01)
        self.assertFalse((primero / 'codigo.json').exists())
        self.assertEqual(self.adaptador.adelanto(CANDIDATO, TREN, base[0]), 'terminado')
        self.assertEqual(self.adaptador.adelanto(CANDIDATO, TREN, self.pedidos[0][0]), '')

    def test_fallo_al_lanzar_el_segundo_intento_conserva_el_75_del_primero(self):
        (self.raiz / 'primero-75').touch()
        popen = subprocess.Popen
        llamadas = count(1)

        def sin_segundo(*args, **kwargs):
            if next(llamadas) == 2:
                raise OSError('sin procesos')
            return popen(*args, **kwargs)

        with patch('adaptadores.jale.subprocess.Popen', side_effect=sin_segundo):
            self.assertIsInstance(self.repetir(), Incompleto)
        with patch('adaptadores.jale.subprocess.Popen', side_effect=OSError('sin procesos')):
            with self.assertRaisesRegex(ResultadoInvalido, 'lanzador-no-disponible'):
                self.repetir(self.pedidos[1])

    def test_fallo_al_lanzar_el_primer_intento_espera_al_segundo(self):
        self.plan([{'esperar': True}])
        pedido, commit = self.pedidos[0]
        primero, segundo = self.registros(self.pedidos[0])
        popen = subprocess.Popen

        def sin_primero(argv, *args, **kwargs):
            if argv[-1] == str(primero):
                raise OSError('sin procesos')
            return popen(argv, *args, **kwargs)

        with patch('adaptadores.jale.subprocess.Popen', side_effect=sin_primero):
            self.assertIs(self.adaptador.repetir(
                CANDIDATO, self.origen, pedido, commit, TREN), EN_CURSO)
            self.esperar_archivo(self.raiz / 'listo')
            self.assertTrue(lanzamiento_activo(segundo / 'candado'))
            self.assertIs(self.adaptador.repetir(
                CANDIDATO, self.origen, pedido, commit, TREN), EN_CURSO)
            (self.raiz / 'seguir').touch()
            self.esperar_archivo(segundo / 'codigo.json')
            with self.assertRaisesRegex(ResultadoInvalido, 'lanzador-no-disponible'):
                self.adaptador.repetir(CANDIDATO, self.origen, pedido, commit, TREN)
        self.assertIsInstance(self.repetir(), Repeticion)
        self.assertCountEqual([i['log'] for i in self.invocaciones()],
                              [str(r / 'corrida.log') for r in (primero, segundo)])

    def test_otra_instancia_recupera_coordinadores_vivos_y_terminados(self):
        self.plan([{'esperar': True}])
        pedidos = self.pedidos[:2]
        self.adelantar(pedidos)
        otro = self.jale(4)
        self.adelantar(pedidos, otro)
        (self.raiz / 'seguir').touch()
        for pedido in pedidos:
            for registro in self.registros(pedido):
                self.esperar_archivo(registro / 'codigo.json')
        self.adelantar(pedidos, otro)
        for pedido in pedidos:
            self.assertIsInstance(self.repetir(pedido, otro), Repeticion)
        self.assertEqual(len(self.invocaciones()), 4)

    def test_sin_permiso_no_espera_ni_lee_registros_especulativos(self):
        self.plan([{'esperar': True}])
        self.adelantar(self.pedidos[:1])
        with patch('adaptadores.jale.leer_salida') as leer, \
             patch('adaptadores.jale.subprocess.Popen') as popen:
            self.assertIsNone(self.repetir(permitir_lanzar=False))
            leer.assert_not_called()
            popen.assert_not_called()
        (self.raiz / 'seguir').touch()
        confirmada = self.repetir()
        self.assertIsInstance(confirmada, Repeticion)
        self.assertEqual(self.repetir(permitir_lanzar=False), confirmada)
        self.assertEqual(len(self.invocaciones()), 2)

    def test_tope_de_reproducciones(self):
        pedidos = tuple((replace(self.pedidos[0][0], id='pedido-' + str(n)), C)
                        for n in range(6))
        self.adaptador = self.jale(3)
        self.adelantar(pedidos)
        for pedido in pedidos[:2]:
            self.assertIsInstance(self.repetir(pedido), Repeticion)
        self.assertTrue(all(not r.exists() for p in pedidos[2:] for r in self.registros(p)))
        self.assertEqual(len(self.invocaciones()), 4)

    def test_config_por_omision_desactiva_y_acepta_solo_enteros_del_rango(self):
        config = dict(adaptador='jale', lanzador=str(self.lanzador))
        self.assertEqual(seleccionar(config, None).repeticiones_paralelas, 1)
        self.assertEqual(seleccionar(dict(config, repeticiones_paralelas=8),
                                     None).repeticiones_paralelas, 8)
        for valor in (False, True, '4', 2.0, None, 0, 9):
            with self.subTest(valor=valor), self.assertRaises(ValueError):
                seleccionar(dict(config, repeticiones_paralelas=valor), None)
        self.adaptador = self.jale(1)
        self.adelantar()
        self.assertFalse(self.adaptador.estado.exists())
        (self.raiz / 'en-serie').touch()
        self.assertIsInstance(self.repetir(), Repeticion)
        self.assertEqual(len(self.invocaciones()), 2)

    def test_reenganche_de_adelantada_sin_recibo_conserva_registro_y_clave(self):
        (self.raiz / 'sin-recibo').touch()
        self.adelantar(self.pedidos[:1])
        r = self.repetir()
        registro = self.registros(self.pedidos[0])[0]
        self.assertEqual(r, RechazoLanzador(str(registro / 'coordinador.log'), 'salida-sin-recibo'))
        self.assertIsInstance(self.repetir(reenganche=(1, r.evidencia)), Repeticion)
        self.assertTrue((registro / 'codigo-reenganche-1.json').exists())
        self.assertEqual(len(self.lanzamientos()), 2)
        self.assertEqual(len(self.invocaciones()), 3)

    def test_rechazo_del_primer_intento_conserva_el_recibo_del_segundo(self):
        (self.raiz / 'sin-recibo').touch()
        self.adelantar(self.pedidos[:1])
        r = self.repetir()
        primero, segundo = self.registros(self.pedidos[0])
        self.assertEqual(r, RechazoLanzador(str(primero / 'coordinador.log'), 'salida-sin-recibo'))
        self.assertTrue((segundo.parent / 'completo.json').exists())
        (self.raiz / 'sin-recibo').unlink()
        self.assertIsInstance(self.repetir(intento=1), Repeticion)
        relanzados = [json.loads(solicitud.read_text())[3] for solicitud in
                      sorted(primero.parents[1].glob('*/1/solicitud.json'))]
        self.assertEqual(relanzados, [str(primero.parent / '1' / 'corrida.log')])

    def test_rechazo_del_primero_prevalece_si_el_segundo_no_deja_salida(self):
        rechazo = RechazoLanzador('coordinador.log', 'salida-sin-recibo')

        def recoger(salidas):
            def siguiente(registro, proceso, *args):
                proceso.wait()
                salida = salidas.pop(0)
                if isinstance(salida, Exception):
                    raise salida
                return salida
            return siguiente

        with patch.object(self.adaptador, 'recoger_corrida', side_effect=recoger(
                [rechazo, ResultadoInvalido('corrida-interrumpida')])):
            self.assertEqual(self.repetir(), rechazo)
        serie = self.jale(1, 'serie')
        with patch.object(serie, 'recoger_corrida', side_effect=recoger([rechazo])):
            self.assertEqual(self.repetir(adaptador=serie), rechazo)
        raiz = serie.estado / clave('repetir', TREN, self.pedidos[0][0].id)
        self.assertFalse((raiz / '2').exists())

    def test_75_del_primer_intento_conserva_el_recibo_del_segundo(self):
        (self.raiz / 'primero-75').touch()
        r = self.repetir()
        primero, segundo = self.registros(self.pedidos[0])
        self.assertEqual(r, Incompleto(75, str(primero.parents[1])))
        self.assertTrue((segundo.parent / 'completo.json').exists())
        self.assertIsInstance(self.repetir(intento=1, reanudar=r.reanudar), Repeticion)
        relanzados = [json.loads(solicitud.read_text())[3] for solicitud in
                      sorted(primero.parents[1].glob('*/1/solicitud.json'))]
        self.assertEqual(relanzados, [str(primero.parent / '1' / 'corrida.log')])
        serie = self.jale(1, 'serie')
        self.assertIsInstance(self.repetir(adaptador=serie), Incompleto)
        raiz = serie.estado / clave('repetir', TREN, self.pedidos[0][0].id)
        self.assertFalse((raiz / '2').exists())

    def test_recibo_invalido_del_primer_intento_conserva_el_recibo_del_segundo(self):
        (self.raiz / 'primero-malformado').touch()
        self.adelantar(self.pedidos[:1])
        resultado = self.repetir()
        primero, segundo = self.registros(self.pedidos[0])
        self.assertEqual(resultado, RechazoLanzador(str(primero / 'corrida.log'), 'recibo-malformado'))
        self.assertTrue((segundo.parent / 'completo.json').exists())
        serie = self.jale(1, 'serie')
        resultado = self.repetir(adaptador=serie)
        raiz = serie.estado / clave('repetir', TREN, self.pedidos[0][0].id)
        self.assertEqual(resultado, RechazoLanzador(str(raiz / '1' / '0' / 'corrida.log'),
                                                    'recibo-malformado'))
        self.assertFalse((raiz / '2').exists())

    def test_adelantada_con_75_pide_la_primera_reanudacion(self):
        (self.raiz / 'primero-75').touch()
        self.adelantar(self.pedidos[:1])
        r = self.repetir()
        self.assertIsInstance(r, Incompleto)
        inicial, tren = diagnostico((), self.pedidos[0][0], presupuesto_s=100)
        nuevo, efectos = avanzar_suite(inicial, tren,
            observacion(4, tren_id=TREN, reproduccion_id=tren.fase.pendiente.id, repeticion=r))
        self.assertEqual(efectos, ())
        self.assertEqual(nuevo.trenes[0].fase.esperar_hasta, 400)
        self.assertIsInstance(self.repetir(intento=1, reanudar=r.reanudar), Repeticion)


class EfectoTests(unittest.TestCase):
    def setUp(self):
        historial = confirmado()
        self.s, *self.demas = plan(historial).tanda
        self.estado, self.tren = diagnostico(historial, self.s, presupuesto_s=500,
            subconjunto=Candidato(B, LOTE.eslabones[:1], '1' * 40, LOTE.arbol))
        self.efecto = efecto_diagnostico(self.tren, self.tren.fase)
        self.obs = observacion(4)
        self.adaptador = Mock(spec=Jale)
        self.adaptador.repeticiones_paralelas = 4
        self.adaptador.adelanto.return_value = ''
        self.adaptador.repetir.return_value = evidencia(self.s).resultado
        self.git = Mock()
        self.git.armar.side_effect = (ConflictoCadena(2, ('conflicto',)), *(
            Candidato(B, LOTE.eslabones[pr - 1:pr], str(pr) * 40, LOTE.arbol) for pr in (3, 4)))

    def ejecutar(self, adaptador=None, obs=None):
        return ejecutar(self.efecto, self.estado, obs or self.obs, self.git,
                        adaptador if adaptador is not None else self.adaptador)

    def test_adelanta_commits_armados_y_omite_conflicto(self):
        r = self.ejecutar()
        self.assertTrue(r.completo)
        self.assertEqual(r.observacion.repeticion, self.adaptador.repetir.return_value)
        self.assertEqual([c.args for c in self.git.armar.call_args_list],
                         [(B, ((pr, 'a' * 40),)) for pr in (2, 3, 4)])
        self.adaptador.adelantar_repeticiones.assert_called_once_with(
            LOTE, 'suite.json', tuple((s, str(s.prs[0]) * 40) for s in self.demas[1:]), TREN)
        anterior = resultado_observacion(self.efecto, r.observacion)
        self.assertNotIn('repeticiones_en_paralelo', anterior)
        lectura = replace(self.obs, **{k: desde_datos(v) for k, v in anterior.items()})
        self.assertEqual(lectura, r.observacion)

    def test_arma_solo_los_subconjuntos_que_caben_en_el_tope(self):
        self.adaptador.repeticiones_paralelas = 2
        self.ejecutar()
        self.assertEqual([c.args for c in self.git.armar.call_args_list],
                         [(B, ((pr, 'a' * 40),)) for pr in (2, 3)])
        self.adaptador.adelantar_repeticiones.assert_called_once_with(
            LOTE, 'suite.json', ((self.demas[1], '3' * 40),), TREN)

    def test_adelantadas_vivas_ocupan_lugar_y_las_terminadas_no_se_repiten(self):
        self.adaptador.repeticiones_paralelas = 3
        self.git.armar.side_effect = None
        self.git.armar.return_value = Candidato(B, LOTE.eslabones[3:], '4' * 40, LOTE.arbol)
        self.adaptador.adelanto.side_effect = ('vivo', 'terminado', '')
        self.ejecutar()
        self.assertEqual([c.args for c in self.adaptador.adelanto.call_args_list],
                         [(LOTE, TREN, s) for s in self.demas])
        self.git.armar.assert_called_once_with(B, ((4, 'a' * 40),))
        self.adaptador.adelantar_repeticiones.assert_called_once_with(
            LOTE, 'suite.json', ((self.demas[2], '4' * 40),), TREN)
        self.adaptador.adelanto.side_effect = ('vivo', 'vivo', '')
        self.ejecutar()
        self.git.armar.assert_called_once()
        self.assertEqual(self.adaptador.adelantar_repeticiones.call_args.args[2], ())

    def test_fallos_de_adelanto_avisan_y_conservan_efecto_serial(self):
        hasito = Hasito('/sintetico', self.git)
        with patch.object(hasito, 'repetir', return_value=self.adaptador.repetir.return_value):
            esperado = self.ejecutar(hasito)
        for error, motivo in (
                (ResultadoInvalido('recibo-no-corresponde'), 'recibo-no-corresponde'),
                (FalloSnapshot('snapshot-no-verificable'), 'snapshot-no-verificable'),
                (FalloGit('/privado/git'), 'FalloGit'),
                (OSError('/privado/lanzador'), 'OSError'),
                (subprocess.CalledProcessError(2, ['/privado/lanzador'], stderr='oculto'),
                 'CalledProcessError')):
            with self.subTest(clase=type(error).__name__):
                self.git.armar.side_effect = (ConflictoCadena(2, ('conflicto',)),) * 3
                self.adaptador.adelantar_repeticiones.side_effect = error
                r = self.ejecutar()
                self.assertEqual(replace(r, aviso=None), esperado)
                self.assertEqual(r.aviso, dict(tren=TREN, motivo=motivo,
                                              clase=type(error).__name__))

    def test_espera_y_miembro_retenido_no_adelantan(self):
        self.adaptador.repetir.return_value = None
        retenida = replace(self.obs, prs=(vista(apto=False),) + self.obs.prs[1:])
        self.assertFalse(self.ejecutar(obs=retenida).completo)
        self.assertFalse(self.adaptador.repetir.call_args.kwargs['permitir_lanzar'])
        esperando = replace(self.tren, fase=replace(self.tren.fase, esperar_hasta=500))
        self.estado = replace(self.estado, trenes=(esperando,))
        with self.assertRaisesRegex(ValueError, 'repetición obsoleta'):
            self.ejecutar()
        self.adaptador.repetir.assert_called_once()
        self.adaptador.adelantar_repeticiones.assert_not_called()
        self.git.armar.assert_not_called()


class TickTests(ProductorParalelo, unittest.TestCase):
    comando = activo.ActivoTests.comando
    push = activo.ActivoTests.push
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    cli = prelanzar.TickActivoTests.cli
    tick = prelanzar.TickActivoTests.tick

    def commit(self, *args, **kwargs):
        fecha = '2025-01-02T03:04:05Z'
        with patch.dict(os.environ, GIT_AUTHOR_DATE=fecha, GIT_COMMITTER_DATE=fecha):
            return activo.ActivoTests.commit(self, *args, **kwargs)

    def cuarto_miembro(self):
        head = self.commit({'comun': 'base\n', 'pr-4': 'ticket\n'}, padres=(self.B,))
        self.push(head, 'refs/pull/4/head')
        self.miembros.append((4, head))

    def diagnosticar(self, paralelas):
        activo.ActivoTests.setUp(self, self.cuarto_miembro)
        self.preparar_productor()
        self.reloj = count(1000, 120)
        self.privada = self.raiz / 'config.json'
        config = dict(self.contexto.config, adaptador='jale', modos=['contract'],
                      costo_suite_sana_s=1000, lanzador=str(self.lanzador),
                      repeticiones_paralelas=paralelas)
        self.contexto.config.update(config)
        self.privada.write_text(json.dumps({'repos': {activo.REPO: config}}))
        subconjuntos = [self.contexto.git.armar(self.B, (m,)).C for m in self.miembros]
        (self.raiz / 'producto.json').write_text(json.dumps(
            [str(self.origen), self.miembros[1][1], subconjuntos]))
        if paralelas > 1:
            (self.raiz / 'barrera-tanda').touch()
        vistos = []

        def frontera(punto, efecto):
            if punto != 'efecto-ejecutado' or efecto.tipo != 'repetir':
                return
            fase = self.estado().trenes[0].fase
            if fase.pendiente.prs and not vistos:
                esperados = subconjuntos if paralelas > 1 else [fase.subconjunto.C]
                for sha in esperados:
                    self.esperar_archivo(self.raiz / ('inicio-' + sha + '-1'))
                vistos.append({sha for sha in subconjuntos
                              if (self.raiz / ('inicio-' + sha + '-1')).exists()})

        for _ in range(40):
            self.tick(frontera=frontera)
            final = self.estado()
            self.assertIsNone(final.pausa, final.pausa)
            if final.trenes and final.trenes[0].fase == Terminal('DEVUELTO'):
                break
        else:
            self.fail('el lote no llegó a su veredicto')
        self.assertEqual([e.entrega.pr for e in final.entradas if e.terminal], [2])
        self.assertEqual(len(vistos[0]), 4 if paralelas > 1 else 1)
        with self.ledger() as ledger:
            efectos = [desde_datos(json.loads(payload.replace(str(self.raiz), '/sintetico')))
                       for payload, in ledger.db.execute('SELECT payload FROM effects')
                       if json.loads(payload)['tipo'] == 'devolver']
        self.assertTrue(any(e.tipo == 'devolver' for e in efectos))
        return len(self.invocaciones()), efectos

    def test_tick_rojo_conserva_devolucion_y_lanza_toda_la_tanda(self):
        serie = self.diagnosticar(1)
        paralelo = self.diagnosticar(4)
        self.assertEqual(serie[0], 13)
        self.assertEqual(paralelo, serie)
