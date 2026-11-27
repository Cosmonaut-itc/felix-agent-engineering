"""SEN-7032: Git real, lanzadores de prueba y tick activo."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_activo as activo
from test_jale import FALLO, PRODUCTOR, esperar_resultado
import test_juicio as juicio_fixtures
from adaptadores import seleccionar
from adaptadores.jale import EN_CURSO
from dominio import (PLAZO_CI, Certificado, Congelado, Corriendo, Diagnosticando, Efecto, EsperandoCI,
                     Motivo, Pausa, Promoviendo, Reproduccion, ResultadoCI, Retenido, Terminal,
                     clave, desde_datos, recibo_ci)
from operador import FalloSnapshot, Snapshots, lanzamiento_activo


class SnapshotTests(unittest.TestCase):
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    hasta_fase = activo.ActivoTests.hasta_fase
    lineas = activo.ActivoTests.lineas
    cli = juicio_fixtures.JuicioTests.cli
    juicio = juicio_fixtures.JuicioTests.juicio
    hasta_pausa = juicio_fixtures.JuicioTests.hasta_pausa
    hijo = activo.ActivoTests.hijo

    def setUp(self):
        activo.ActivoTests.setUp(self)
        self.operador = self.raiz / 'operador'
        self.productor = self.raiz / 'productor.py'
        self.productor.write_text(PRODUCTOR)
        self.plan([{}])
        self.viejo = self.version('viejo', self.B)
        self.push(self.viejo, 'refs/heads/main')
        self.comando(self.raiz, 'clone', str(self.origen), str(self.operador))
        self.nuevo = self.version('nuevo', self.viejo)
        self.push(self.nuevo, 'refs/heads/main')
        self.config = self.contexto.config
        self.config.update(checkout_operador=str(self.operador), adaptador='jale',
                           lanzador=str(self.operador / 'jale.sh'), modos=['contract'])
        self.contexto.hasito = seleccionar(self.config, self.contexto.git)

    def plan(self, filas):
        (self.raiz / 'plan.json').write_text(json.dumps(filas))

    def version(self, nombre, padre, extra='', enlace=None):
        salida = shlex.quote(str(self.raiz / 'versiones.log'))
        lanzamiento = shlex.join([sys.executable, str(self.productor), str(self.raiz)])
        gancho = '''import json, sys
from pathlib import Path
s = json.load(sys.stdin)
with Path(LOG).open('a') as f:
    f.write(s['accion'] + ' ' + VERSION + '\\n')
print(json.dumps(dict(version=1, id=s['id'], resultado=
    'esperar' if s['accion'] == 'verificar' else 'cancelado')))
'''.replace('LOG', repr(str(self.raiz / 'gancho.log'))).replace('VERSION', repr(nombre))
        guion = ('#!/bin/bash\nset -eu\n' + extra + '\n'
                 'printf "%s %s\\n" ' + shlex.quote(nombre) + ' '
                 '"$(cat "$(dirname "$0")/modulo")" >> ' + salida +
                 '\nexec ' + lanzamiento + ' "$@"\n')
        archivos = {'comun': 'base\n', 'jale.sh': guion, 'modulo': nombre,
                    'gancho.py': gancho}
        if enlace is None:
            return self.commit(archivos, (padre,), nombre)
        ruta, destino = enlace
        for n, contenido in archivos.items():
            blob = self.comando(self.fuente, 'hash-object', '-w', '--stdin',
                                input=str(destino) if n == ruta else contenido)
            archivos[n] = ('120000' if n == ruta else '100644') + f' blob {blob}\t{n}'
        arbol = self.comando(self.fuente, 'mktree', input='\n'.join(archivos.values()) + '\n')
        return self.comando(self.fuente, '-c', 'user.name=Fixture', '-c',
            'user.email=fixture@example.invalid', 'commit-tree', arbol, '-p', padre,
            input=nombre + '\n')

    def gancho(self, ejecutable=False):
        ruta = str(self.operador / 'gancho.py')
        self.config['pre_aterrizaje'] = {
            'comando': [ruta] if ejecutable else [sys.executable, ruta], 'plazo_s': 5}

    def solicitudes(self):
        return sorted(self.contexto.hasito.estado.rglob('solicitud.json'))

    def argv(self, registro=None):
        return json.loads((registro or self.solicitudes()[0]).read_text())

    def cabeza(self, ruta):
        return self.comando(ruta, 'rev-parse', 'HEAD')

    def esperar(self, ruta):
        limite = time.monotonic() + 10
        while not ruta.exists() and time.monotonic() < limite:
            time.sleep(.025)
        self.assertTrue(ruta.exists(), ruta)

    def comprobar_snapshot(self, argv, commit):
        ruta = Path(argv[1]).parent
        self.assertNotEqual(ruta, self.operador)
        self.assertEqual(self.cabeza(ruta), commit)
        self.assertEqual(self.cabeza(self.operador), self.viejo)
        return ruta

    def test_checkout_atras_y_sin_tocar(self):
        (self.operador / 'sin-versionar').write_text('local')
        antes = self.comando(self.operador, 'status', '--porcelain')
        self.hasta_fase(Certificado)
        self.comprobar_snapshot(self.argv(), self.nuevo)
        self.assertEqual(self.lineas('versiones.log'), ['nuevo nuevo'])
        self.assertEqual(self.comando(self.operador, 'status', '--porcelain'), antes)

    def test_c_recien_armado_existe_en_el_repo_del_lanzador(self):
        # Como el export de Boat: el lanzador lee C de su propio repositorio.
        exige = self.version('exige-c', self.nuevo,
                             'git -C "$(dirname "$0")" cat-file -e "$4^{commit}" || exit 97')
        self.push(exige, 'refs/heads/main')
        estado = self.hasta_fase(Certificado)
        C = estado.trenes[0].fase.C
        self.comprobar_snapshot(self.argv(), exige)
        self.assertEqual(self.lineas('versiones.log'), ['exige-c exige-c'])
        self.assertEqual(self.comando(self.operador, 'cat-file', '-t', C), 'commit')
        self.assertEqual(self.comando(self.operador, 'rev-parse', 'refs/pao-tren/operador/' + C), C)
        self.assertEqual(self.cabeza(self.operador), self.viejo)

    def test_c_sin_transferir_pausa_sin_lanzar_y_reanuda(self):
        exige = self.version('exige-c', self.nuevo,
                             'git -C "$(dirname "$0")" cat-file -e "$4^{commit}" || exit 97')
        self.push(exige, 'refs/heads/main')
        original = Snapshots.comando_git
        bloqueados = []

        def comando_git(snapshots, ruta, *args, verificar=True):
            # Falla sólo la transferencia de C, no la del main del snapshot.
            if args and args[0] == 'fetch-pack' and not args[-1].endswith(exige):
                bloqueados.append(args[-1])
                raise FalloSnapshot('git-del-operador')
            return original(snapshots, ruta, *args, verificar=verificar)

        with patch.object(Snapshots, 'comando_git', comando_git):
            pausa = self.hasta_pausa().pausa
        self.assertEqual(pausa.detalle, 'checkout-operador:sha-no-disponible')
        self.assertTrue(bloqueados)
        self.assertEqual(len(self.solicitudes()), 1)
        self.assertFalse(self.lineas('versiones.log'))
        self.juicio('reanudar')
        C = self.hasta_fase(Certificado).trenes[0].fase.C
        self.assertEqual(self.lineas('versiones.log'), ['exige-c exige-c'])
        self.assertEqual(self.comando(self.operador, 'cat-file', '-t', C), 'commit')

    def pausar_por_c_sin_transferir(self, bloquear):
        """Pausa por una transferencia fallida de C; devuelve el main que avanza."""
        tercero = self.version('tercero', self.nuevo)
        original = Snapshots.comando_git

        def comando_git(snapshots, ruta, *args, verificar=True):
            if bloquear.activo and args and args[0] == 'fetch-pack' and not (
                    args[-1].endswith(self.nuevo) or args[-1].endswith(tercero)):
                raise FalloSnapshot('git-del-operador')
            return original(snapshots, ruta, *args, verificar=verificar)

        bloquear.parche = patch.object(Snapshots, 'comando_git', comando_git)
        bloquear.parche.start()
        self.addCleanup(bloquear.parche.stop)
        self.assertEqual(self.hasta_pausa().pausa.detalle, 'checkout-operador:sha-no-disponible')
        self.assertFalse(self.lineas('lanzamientos.jsonl'))
        self.assertFalse(list(self.contexto.hasito.estado.rglob('iniciado.json')))
        self.push(tercero, 'refs/heads/main')
        return tercero

    def assert_rearma_sin_lanzar(self, tercero):
        self.juicio('reanudar')
        self.contexto.tick(9000)
        self.contexto.tick(9120)
        estado = self.estado()
        self.assertEqual(estado.trenes[0].fase, Terminal('REARMAR'))
        self.assertIsNone(estado.pausa)
        self.assertFalse(self.lineas('lanzamientos.jsonl'))
        self.contexto.tick(9240)
        self.assertEqual(self.estado().trenes[-1].fase, Congelado(tercero))

    def test_primer_lanzamiento_fallido_con_transferencia_restaurada_rearma(self):
        # disponer(C) falla antes de Popen: ningún coordinador, ningún iniciado.json.
        bloquear = type('Bloqueo', (), {'activo': True})
        tercero = self.pausar_por_c_sin_transferir(bloquear)
        bloquear.activo = False
        self.assert_rearma_sin_lanzar(tercero)

    def test_primer_lanzamiento_fallido_con_fallo_persistente_rearma(self):
        bloquear = type('Bloqueo', (), {'activo': True})
        tercero = self.pausar_por_c_sin_transferir(bloquear)
        self.assert_rearma_sin_lanzar(tercero)

    def test_dos_modos_con_contract_vivo_y_e2e_fallido_no_rearma(self):
        self.config['modos'] = ['contract', 'e2e']
        self.contexto.hasito = seleccionar(self.config, self.contexto.git)
        tercero = self.version('tercero', self.nuevo)
        original = Snapshots.disponer
        llamadas = []

        def disponer(snapshots, commit):
            llamadas.append(commit)
            if len(llamadas) == 2:
                raise FalloSnapshot('sha-no-disponible')
            return original(snapshots, commit)

        with patch.object(Snapshots, 'disponer', disponer):
            self.assertEqual(self.hasta_pausa().pausa.detalle,
                             'checkout-operador:sha-no-disponible')
        self.esperar(next(self.contexto.hasito.estado.rglob('contract')) / '0' / 'iniciado.json')
        self.assertFalse(list(self.contexto.hasito.estado.rglob('e2e/0/iniciado.json')))
        self.push(tercero, 'refs/heads/main')
        self.juicio('reanudar')
        for n in range(15):
            self.contexto.tick(9000 + n * 120)
            fase = self.estado().trenes[0].fase
            self.assertNotEqual(fase, Terminal('REARMAR'))
            if isinstance(fase, Certificado):
                break
        self.assertIsInstance(fase, Certificado)

    def suite_lenta(self, *filas):
        self.plan([{'esperar': True, 'plazo': 8, **fila} for fila in filas] or
                  [{'esperar': True, 'plazo': 8}])
        self.addCleanup((self.raiz / 'seguir').touch)
        self.hasta_fase(EsperandoCI)

    def filas_suite(self, tipo='iniciar-suite'):
        with self.ledger() as ledger:
            return [(r, desde_datos(json.loads(r['payload']))) for r in ledger.outbox(activo.REPO)
                    if json.loads(r['payload'])['tipo'] == tipo]

    def eventos_suite(self, tipo):
        with self.ledger() as ledger:
            return [json.loads(payload) for payload, in ledger.db.execute(
                'SELECT payload FROM events WHERE repo=? AND tipo=?', (activo.REPO, tipo))]

    def coordinador_vivo(self, marca='listo', registro=None):
        self.esperar(self.raiz / marca)
        registro = registro or self.solicitudes()[0].parent
        self.assertTrue(lanzamiento_activo(registro / 'candado'))
        self.assertFalse((registro / 'codigo.json').exists())
        return registro

    def test_suite_libre_candado_y_outbox(self):
        self.suite_lenta()
        self.contexto.tick(2000)
        registro = self.coordinador_vivo()
        [fila, _] = self.filas_suite()[0]
        self.assertEqual((fila['estado'], fila['resultado']), ('ejecutando', None))
        self.assertFalse(self.eventos_suite('efecto-fallido'))
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        codigo, salida = self.cli('pausa', '--motivo', 'prueba')
        self.assertEqual(codigo, 0, salida)
        self.contexto.tick(2100)
        self.assertIsNotNone(self.estado().pausa)
        self.coordinador_vivo(registro=registro)

    def test_suite_libre_un_coordinador(self):
        self.suite_lenta()
        with patch('adaptadores.jale.subprocess.Popen', wraps=subprocess.Popen) as popen:
            self.contexto.tick(2000)
            self.coordinador_vivo()
            self.contexto.tick(2100)
        coordinadores = [llamada.args[0] for llamada in popen.call_args_list
                         if any(str(a).endswith('/ejecucion_jale.py') for a in llamada.args[0])]
        self.assertEqual(len(coordinadores), 1)
        self.assertEqual(len(self.lineas('invocaciones.jsonl')), 1)
        self.assertEqual(self.filas_suite()[0][0]['estado'], 'ejecutando')
        self.assertEqual(len([e for e in self.eventos_suite('efecto-iniciado')
                              if e['clave'] == self.filas_suite()[0][0]['clave']]), 1)

    def test_suite_iniciada_no_vence_el_plazo_de_ci(self):
        self.suite_lenta()
        self.contexto.tick(2000)
        registro = self.coordinador_vivo()
        tren = self.estado().trenes[0]
        (self.raiz / 'fallo-ci').touch()
        self.contexto.tick(tren.fase.desde + PLAZO_CI + 1)
        self.assertIsNone(self.estado().pausa)
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        self.coordinador_vivo(registro=registro)

    @patch('preaterrizaje.Previo.llamar', return_value='seguir')
    def test_reanudar_ci_con_suite_iniciada_exige_el_verde_vigente(self, _gancho):
        self.gancho()
        self.suite_lenta()
        self.contexto.tick(2000)
        registro = self.coordinador_vivo()
        anterior = self.estado()
        tren = anterior.trenes[0]
        rama = 'tren/' + tren.id + '-ci-1'
        ci = self.contexto.observador.ci
        ci_vigente = 'pendiente'

        def leer_ci(repo, sha, ref=None, **kwargs):
            if sha == tren.fase.candidato.C:
                return ResultadoCI(ci_vigente if ref == rama else 'pendiente')
            return ci(repo, sha, ref, **kwargs)

        lector = patch.object(self.contexto.observador, 'ci', side_effect=leer_ci)
        lector.start()
        self.addCleanup(lector.stop)
        # Una pausa escrita antes de exceptuar del plazo a la suite iniciada.
        ahora = tren.fase.desde + PLAZO_CI + 1
        with self.ledger() as ledger:
            ledger.guardar_paso(replace(anterior, pausa=Pausa(
                'infraestructura', detalle='ci-sin-resultado')), (), ahora)
        self.juicio('reanudar')
        self.contexto.tick(ahora + 1)
        nuevo = self.estado().trenes[0]
        self.assertEqual(nuevo.fase.solicitud, 1)
        self.assertEqual(self.remoto('refs/heads/' + rama), nuevo.fase.candidato.C)
        vigente = recibo_ci(nuevo)
        (self.raiz / 'seguir').touch()
        self.esperar(registro / 'codigo.json')
        for n in range(2, 7):
            self.contexto.tick(ahora + n)
        pushes = sum(p.endswith(':refs/heads/main') for p in self.lineas('pushes.log'))
        self.assertEqual(pushes, 0, f'pushes a main: {pushes}')
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        fila, efecto = self.filas_suite()[0]
        self.assertEqual(fila['estado'], 'resultado')
        resultado = json.loads(fila['resultado'])
        self.assertNotIn('ci_candidato', resultado['observacion'])
        self.assertNotIn('ci_recibo', resultado['observacion'])

        # También se puede recuperar una fila que ya recibió el verde fabricado.
        resultado['observacion'].update(ci_candidato='verde', ci_recibo=vigente)
        with self.ledger() as ledger:
            ledger.registrar_efecto(activo.REPO, efecto, ahora + 7, resultado, completo=True)
        self.contexto.tick(ahora + 8)
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        self.assertEqual(self.remoto(), nuevo.fase.candidato.B)

        ci_vigente = 'verde'
        self.contexto.tick(ahora + 9)
        cert = self.estado().trenes[0].fase
        self.assertIsInstance(cert, Certificado)
        self.assertEqual(cert.ci, vigente)
        self.assertFalse(self.filas_suite())

    def test_ci_nuevo_vence_con_resultado_de_suite_anterior_retenido(self):
        self.suite_lenta()
        self.contexto.tick(2000)
        registro = self.coordinador_vivo()
        estado = self.estado()
        tren = estado.trenes[0]
        ahora = tren.fase.desde + PLAZO_CI + 1
        with self.ledger() as ledger:
            ledger.guardar_paso(replace(estado, pausa=Pausa(
                'infraestructura', detalle='ci-sin-resultado')), (), ahora)
        self.juicio('reanudar')
        self.contexto.tick(ahora + 1)
        fase = self.estado().trenes[0].fase
        self.assertEqual((fase.solicitud, fase.desde), (1, ahora + 1))
        self.assertEqual(self.remoto('refs/heads/tren/' + tren.id + '-ci-1'), fase.candidato.C)

        ci = self.contexto.observador.ci

        def leer_ci(repo, sha, ref=None, **kwargs):
            return ResultadoCI('pendiente') if sha == fase.candidato.C else ci(repo, sha, ref, **kwargs)

        (self.raiz / 'seguir').touch()
        self.esperar(registro / 'codigo.json')
        with patch.object(self.contexto.observador, 'ci', side_effect=leer_ci):
            self.contexto.tick(fase.desde + PLAZO_CI - 1)
            self.assertIsNone(self.estado().pausa)
            fila, _ = self.filas_suite()[0]
            self.assertEqual(fila['estado'], 'resultado')
            self.contexto.tick(fase.desde + PLAZO_CI)
        estado = self.estado()
        self.assertEqual(estado.pausa, Pausa('infraestructura', detalle='ci-sin-resultado'))
        self.assertEqual(estado.trenes[0].fase, fase)
        self.assertEqual(self.filas_suite()[0][0], fila)
        self.assertFalse(any(p.endswith(':refs/heads/main') for p in self.lineas('pushes.log')))

    def lector_ci_vigente(self, tren):
        rama = 'tren/' + tren.id + '-ci-1'
        ci = self.contexto.observador.ci
        lectura = {'valor': 'pendiente'}

        def leer_ci(repo, sha, ref=None, **kwargs):
            if sha == tren.fase.candidato.C:
                return ResultadoCI(lectura['valor'] if ref == rama else 'pendiente')
            return ci(repo, sha, ref, **kwargs)

        lector = patch.object(self.contexto.observador, 'ci', side_effect=leer_ci)
        lector.start()
        self.addCleanup(lector.stop)
        return rama, lectura

    def pushes_a_main(self):
        return sum(p.endswith(':refs/heads/main') for p in self.lineas('pushes.log'))

    def test_suite_emitida_antes_de_reanudar_arranca_con_el_ci_vigente(self):
        self.suite_lenta()

        class Muerte(Exception):
            pass

        def morir(punto, efecto):
            if punto == 'paso-guardado':
                raise Muerte()

        # El paso emite iniciar-suite con el verde de la solicitud 0 y el tick
        # muere antes de iniciarla.
        with self.assertRaises(Muerte):
            self.contexto.tick(2000, morir)
        self.assertEqual(self.filas_suite()[0][0]['estado'], 'pendiente')
        self.assertFalse(self.solicitudes())
        tren = self.estado().trenes[0]
        rama, lectura = self.lector_ci_vigente(tren)
        ahora = tren.fase.desde + PLAZO_CI
        self.contexto.tick(ahora)
        self.assertEqual(self.estado().pausa, Pausa('infraestructura', detalle='ci-sin-resultado'))
        self.juicio('reanudar')
        self.contexto.tick(ahora + 1)
        fase = self.estado().trenes[0].fase
        self.assertEqual((fase.solicitud, fase.desde), (1, ahora + 1))
        self.assertEqual(self.remoto('refs/heads/' + rama), fase.candidato.C)
        lectura['valor'] = 'verde'
        self.contexto.tick(ahora + 2)
        registro = self.coordinador_vivo()
        lectura['valor'] = 'pendiente'
        self.contexto.tick(fase.desde + PLAZO_CI)
        estado = self.estado()
        self.assertIsNone(estado.pausa)
        self.assertEqual(estado.trenes[0].fase, fase)
        self.coordinador_vivo(registro=registro)
        fila, _ = self.filas_suite()[0]
        self.assertEqual((fila['estado'], fila['resultado']), ('ejecutando', None))
        self.assertFalse(self.eventos_suite('efecto-fallido'))
        self.assertEqual(len(self.lineas('invocaciones.jsonl')), 1)
        self.assertEqual(self.pushes_a_main(), 0)

    def test_fila_fallida_reiniciada_tras_el_verde_vigente_conserva_su_solicitud(self):
        self.suite_lenta()
        self.contexto.tick(2000)
        registro = self.coordinador_vivo()
        anterior = self.estado()
        tren = anterior.trenes[0]
        _, lectura = self.lector_ci_vigente(tren)
        ahora = tren.fase.desde + PLAZO_CI + 1
        with self.ledger() as ledger:
            ledger.guardar_paso(replace(anterior, pausa=Pausa(
                'infraestructura', detalle='ci-sin-resultado')), (), ahora)
        self.juicio('reanudar')
        self.contexto.tick(ahora + 1)
        nuevo = self.estado().trenes[0]
        self.assertEqual(nuevo.fase.solicitud, 1)
        lectura['valor'] = 'verde'
        self.contexto.tick(ahora + 2)
        # Una versión anterior deja la fila en fallido con la corrida viva. El
        # tick la reinicia ya con el verde vigente leído.
        _, efecto = self.filas_suite()[0]
        with self.ledger() as ledger:
            ledger.registrar_efecto(activo.REPO, efecto, ahora + 2, {'error': 'ejecucion-incompleta'})
        self.contexto.tick(ahora + 3)
        fila, _ = self.filas_suite()[0]
        self.assertEqual((fila['estado'], fila['resultado']), ('ejecutando', None))
        (self.raiz / 'seguir').touch()
        self.esperar(registro / 'codigo.json')
        lectura['valor'] = 'pendiente'
        self.contexto.tick(ahora + 4)
        self.assertIsInstance(self.estado().trenes[0].fase, EsperandoCI)
        fila, _ = self.filas_suite()[0]
        self.assertEqual(fila['estado'], 'resultado')
        self.assertNotIn('ci_candidato', json.loads(fila['resultado'])['observacion'])
        lectura['valor'] = 'verde'
        self.contexto.tick(ahora + 5)
        cert = self.estado().trenes[0].fase
        self.assertIsInstance(cert, Certificado)
        self.assertEqual(cert.ci, recibo_ci(nuevo))
        self.assertEqual(len(self.lineas('invocaciones.jsonl')), 1)
        self.assertEqual(self.pushes_a_main(), 0)

    def test_resultado_de_suite_sin_tren_deja_consumir_el_siguiente(self):
        self.suite_lenta()
        huerfana = Efecto(clave('suite', 'ausente'), 'iniciar-suite', ('ausente',))
        with self.ledger() as ledger:
            ledger.guardar_paso(self.estado(), (huerfana,), 1900)
            ledger.registrar_efecto(activo.REPO, huerfana, 1900, {'observacion': {}}, completo=True)
        self.contexto.tick(2000)
        registro = self.coordinador_vivo()
        (self.raiz / 'seguir').touch()
        self.esperar(registro / 'codigo.json')
        self.contexto.tick(2100)
        self.assertIsInstance(self.estado().trenes[0].fase, Certificado)
        self.assertEqual([efecto for _, efecto in self.filas_suite()], [huerfana])

    def test_fallo_de_arranque_espera_al_otro_modo(self):
        self.config['modos'] = ['contract', 'e2e']
        self.contexto.hasito = seleccionar(self.config, self.contexto.git)
        self.suite_lenta()
        popen = subprocess.Popen

        def falla_e2e(argv, *args, **kwargs):
            if (len(argv) == 5 and str(argv[2]).endswith('/ejecucion_jale.py') and
                    Path(argv[-1]).parent.name == 'e2e'):
                raise OSError('fallo de arranque simulado')
            return popen(argv, *args, **kwargs)

        with patch('adaptadores.jale.subprocess.Popen', side_effect=falla_e2e):
            self.contexto.tick(2000)
            registro = next(r.parent for r in self.solicitudes() if r.parents[1].name == 'contract')
            self.coordinador_vivo(registro=registro)
            self.contexto.tick(2100)
            fila, _ = self.filas_suite()[0]
            self.assertEqual((fila['estado'], fila['resultado']), ('ejecutando', None))
            self.assertFalse(self.eventos_suite('efecto-fallido'))
            (self.raiz / 'seguir').touch()
            self.esperar(registro / 'codigo.json')
            self.contexto.tick(2200)
        fila, _ = self.filas_suite()[0]
        self.assertEqual(fila['estado'], 'fallido')
        self.assertEqual(json.loads(fila['resultado'])['error']['codigo'], 'lanzador-no-disponible')
        self.assertEqual(len(self.eventos_suite('efecto-fallido')), 1)

    def test_suite_libre_recoge_verde_y_rojo(self):
        for estado, fase in (('pasa', Certificado), ('falla', Diagnosticando)):
            with self.subTest(estado=estado):
                if estado == 'falla':
                    self.setUp()
                self.suite_lenta({'estados': [estado]})
                self.contexto.tick(2000)
                registro = self.coordinador_vivo()
                (self.raiz / 'seguir').touch()
                self.esperar(registro / 'codigo.json')
                self.contexto.tick(2100)
                self.assertIsInstance(self.estado().trenes[0].fase, fase)
                self.assertFalse(self.filas_suite())

    def test_suite_libre_aborta_y_descarta(self):
        self.suite_lenta()
        self.contexto.tick(2000)
        registro = self.coordinador_vivo()
        tren = self.estado().trenes[0]
        (self.raiz / 'pr-cerrado-1').touch()
        self.contexto.tick(2100)
        self.assertEqual(self.estado().pausa.motivo, 'head-cambiado-en-tren')
        self.juicio('abortar')
        self.contexto.tick(2200)
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        esperado = {'tren': tren.id, 'modo': 'contract', 'terminada': False,
                    'cancelacion_pedida': True, 'cancelacion_confirmada': True,
                    'clave': json.loads((registro / 'iniciado.json').read_text())['clave']}
        self.assertEqual(self.eventos_suite('suite-sin-leer'), [esperado])
        self.assertEqual(self.eventos_suite('suite-prelanzada-sin-leer'), [])
        self.assertTrue((registro / 'cancelar.json').exists())
        with self.ledger() as ledger:
            clave_efecto = clave('suite', tren.id)
            fila = ledger.db.execute('SELECT estado FROM outbox WHERE clave=?',
                                     (clave_efecto,)).fetchone()
            self.assertEqual(fila[0], 'cancelado')
        self.esperar(registro / 'codigo.json')
        self.contexto.tick(2300)
        self.assertEqual(self.eventos_suite('suite-sin-leer'), [esperado])
        self.assertFalse((registro.parent / 'completo.json').exists())
        self.assertFalse(self.filas_suite())

    def lanzar_repeticion_lenta(self, paralelas):
        self.config.update(costo_suite_sana_s=100, repeticiones_paralelas=paralelas)
        self.contexto.hasito = seleccionar(self.config, self.contexto.git)
        lentas = {'esperar': True, 'esperar_intento': True, 'plazo': 8, 'estados': ['falla']}
        self.plan([{'estados': ['falla']}, lentas])
        for n in ('1', '2'):
            self.addCleanup((self.raiz / ('seguir-' + n)).touch)
        self.hasta_fase(Diagnosticando)
        [fila, efecto] = self.filas_suite('repetir')[0]
        self.assertEqual(fila['estado'], 'ejecutando')
        raiz = self.contexto.hasito.estado / clave('repetir', *efecto.datos[:2])
        primero, segundo = (raiz / n / '0' for n in ('1', '2'))
        self.coordinador_vivo(primero / 'listo', primero)
        return primero, segundo

    def verificar_repeticion_lenta(self, paralelas):
        primero, segundo = self.lanzar_repeticion_lenta(paralelas)
        if paralelas > 1:
            self.coordinador_vivo(segundo / 'listo', segundo)
        else:
            self.assertFalse((segundo / 'solicitud.json').exists())
        self.assertFalse((primero.parent / 'completo.json').exists())
        (self.raiz / 'seguir-1').touch()
        self.esperar(primero / 'codigo.json')
        self.contexto.tick(1800)
        self.coordinador_vivo(segundo / 'listo', segundo)
        self.assertTrue((primero.parent / 'completo.json').exists())
        self.assertFalse((segundo.parent / 'completo.json').exists())
        self.assertEqual(self.filas_suite('repetir')[0][0]['estado'], 'ejecutando')
        invocaciones = [Path(json.loads(linea)['log']).parent
                        for linea in self.lineas('invocaciones.jsonl')]
        self.assertCountEqual([r for r in invocaciones if r.parents[1] == primero.parents[1]],
                              [primero, segundo])
        (self.raiz / 'seguir-2').touch()
        self.esperar(segundo / 'codigo.json')
        self.contexto.tick(1900)
        self.assertTrue((segundo.parent / 'completo.json').exists())
        self.assertEqual(len(self.estado().trenes[0].fase.evidencias), 1)

    def test_suite_libre_repeticion_en_serie(self):
        self.verificar_repeticion_lenta(1)

    def test_suite_libre_repeticion_en_paralelo(self):
        self.verificar_repeticion_lenta(2)

    def repeticion_serie_con_segundo_vivo(self):
        primero, segundo = self.lanzar_repeticion_lenta(1)
        (self.raiz / 'seguir-1').touch()
        self.esperar(primero / 'codigo.json')
        self.contexto.tick(1800)
        self.coordinador_vivo(segundo / 'listo', segundo)
        return primero, segundo

    def test_repeticion_serie_con_cache_invalida_y_segundo_vivo(self):
        primero, segundo = self.repeticion_serie_con_segundo_vivo()
        (primero.parent / 'completo.json').write_text('"inválido"')
        self.contexto.tick(1900)
        self.coordinador_vivo(segundo / 'listo', segundo)
        fila, _ = self.filas_suite('repetir')[0]
        self.assertEqual((fila['estado'], fila['resultado']), ('ejecutando', None))
        self.assertFalse(self.eventos_suite('efecto-fallido'))

    def test_repeticion_serie_con_recibo_incompleto_y_segundo_vivo(self):
        primero, segundo = self.repeticion_serie_con_segundo_vivo()
        (primero / 'codigo.json').write_text('75')
        (primero / 'corrida.log').write_text('')
        self.contexto.tick(1900)
        self.coordinador_vivo(segundo / 'listo', segundo)
        fila, _ = self.filas_suite('repetir')[0]
        self.assertEqual((fila['estado'], fila['resultado']), ('ejecutando', None))
        self.assertFalse(self.eventos_suite('efecto-fallido'))

    def test_repeticion_paralela_retenida_con_segundo_vivo(self):
        self.config.update(costo_suite_sana_s=100, repeticiones_paralelas=2)
        self.contexto.hasito = seleccionar(self.config, self.contexto.git)
        lenta = {'esperar': True, 'esperar_intento': True, 'plazo': 12, 'estados': ['falla']}
        self.plan([{'estados': ['falla']}, lenta])
        self.addCleanup((self.raiz / 'seguir-2').touch)
        popen = subprocess.Popen

        def falla_primero(argv, *args, **kwargs):
            if (len(argv) == 5 and str(argv[2]).endswith('/ejecucion_jale.py') and
                    Path(argv[-1]).parent.name == '1'):
                raise OSError('fallo de arranque simulado')
            return popen(argv, *args, **kwargs)

        with patch('adaptadores.jale.subprocess.Popen', side_effect=falla_primero):
            self.hasta_fase(Diagnosticando)
        fila, efecto = self.filas_suite('repetir')[0]
        self.assertEqual((fila['estado'], fila['resultado']), ('ejecutando', None))
        raiz = self.contexto.hasito.estado / clave('repetir', *efecto.datos[:2])
        segundo = raiz / '2' / '0'
        self.coordinador_vivo(segundo / 'listo', segundo)
        self.assertFalse((raiz / '1' / '0' / 'iniciado.json').exists())
        (self.raiz / 'ci-pendiente').touch()

        self.contexto.tick(1800)
        tren = self.estado().trenes[0]
        self.assertTrue(all(e.admision == Retenido(Motivo.CI)
                            for e in self.estado().entradas if e.tren == tren.id))
        self.coordinador_vivo(segundo / 'listo', segundo)
        fila, _ = self.filas_suite('repetir')[0]
        self.assertEqual((fila['estado'], fila['resultado']), ('ejecutando', None))
        self.assertFalse(self.eventos_suite('efecto-fallido'))

    def test_aborto_en_diagnostico_avisa_por_repeticiones_adelantadas(self):
        self.config.update(costo_suite_sana_s=100, repeticiones_paralelas=2)
        self.contexto.hasito = seleccionar(self.config, self.contexto.git)
        lentas = {'esperar': True, 'esperar_intento': True, 'plazo': 12, 'estados': ['falla']}
        self.plan([{'estados': ['falla']}, lentas])
        for n in ('1', '2'):
            self.addCleanup((self.raiz / ('seguir-' + n)).touch)
        estado = self.hasta_fase(Diagnosticando)
        tren = estado.trenes[0]
        _, efecto = self.filas_suite('repetir')[0]
        raiz_confirmada = self.contexto.hasito.estado / clave('repetir', *efecto.datos[:2])
        confirmadas, adelantos = [], []
        for solicitud in self.solicitudes():
            registro = solicitud.parent
            if registro.parent.name not in ('1', '2'):
                continue
            self.coordinador_vivo(registro / 'listo', registro)
            iniciado = json.loads((registro / 'iniciado.json').read_text())
            aviso = {'tren': tren.id, 'modo': 'contract', 'clave': iniciado['clave'],
                     'terminada': False, 'cancelacion_pedida': True,
                     'cancelacion_confirmada': True}
            if registro.parents[1] == raiz_confirmada:
                confirmadas.append(aviso)
            else:
                adelantos.append(aviso)
        self.assertEqual((len(confirmadas), len(adelantos)), (2, 2))
        (self.raiz / 'pr-cerrado-1').touch()
        self.contexto.tick(1800)
        self.juicio('abortar')
        self.contexto.tick(1900)
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        self.assertCountEqual(self.eventos_suite('suite-sin-leer'), confirmadas)
        self.assertCountEqual(self.eventos_suite('suite-prelanzada-sin-leer'), adelantos)

    def test_suite_libre_muerte_despues_de_lanzar(self):
        self.suite_lenta()
        self.ruta_contexto.write_text(json.dumps(self.config))
        codigo = """import signal
from adaptadores import seleccionar
from test_activo import Contexto
c = Contexto(sys.argv[3])
c.hasito = seleccionar(c.config, c.git)
def morir(punto, efecto):
    if punto == 'efecto-ejecutado' and efecto.tipo == 'iniciar-suite':
        (c.raiz / 'tick-en-frontera').touch()
        signal.pause()
c.tick(2000, morir)
"""
        with self.hijo(codigo) as hijo:
            try:
                self.esperar(self.raiz / 'tick-en-frontera')
                registro = self.coordinador_vivo()
                self.assertIsNone(hijo.poll())
            finally:
                if hijo.poll() is None:
                    hijo.kill()
                salida, error = hijo.communicate(timeout=5)
            self.assertEqual(hijo.returncode, -signal.SIGKILL, (salida, error))
        self.contexto.tick(2100)
        self.coordinador_vivo(registro=registro)
        self.assertEqual(self.filas_suite()[0][0]['estado'], 'ejecutando')
        (self.raiz / 'seguir').touch()
        self.esperar(registro / 'codigo.json')
        self.contexto.tick(2200)
        self.assertIsInstance(self.estado().trenes[0].fase, Certificado)
        self.assertEqual(len(self.lineas('invocaciones.jsonl')), 1)

    def test_suite_libre_recupera_fila_fallida(self):
        self.suite_lenta()
        self.contexto.tick(2000)
        registro = self.coordinador_vivo()
        _, efecto = self.filas_suite()[0]
        with self.ledger() as ledger:
            ledger.registrar_efecto(activo.REPO, efecto, 2000, {'error': 'ejecucion-incompleta'})
        self.contexto.tick(2100)
        fila, _ = self.filas_suite()[0]
        self.assertEqual((fila['estado'], fila['resultado']), ('ejecutando', None))
        self.assertEqual(len(self.eventos_suite('efecto-fallido')), 1)
        (self.raiz / 'seguir').touch()
        self.esperar(registro / 'codigo.json')
        self.contexto.tick(2200)
        self.assertIsInstance(self.estado().trenes[0].fase, Certificado)
        self.assertEqual(len(self.lineas('invocaciones.jsonl')), 1)

    def test_origin_ajeno_e_insteadof_sobre_la_ruta_local(self):
        ajeno = self.raiz / 'ajeno.git'
        self.comando(self.raiz, 'init', '--bare', str(ajeno))
        self.comando(self.operador, 'remote', 'set-url', 'origin', str(ajeno))
        self.comando(self.operador, 'config',
                     'url.' + str(ajeno) + '.insteadOf', str(self.contexto.git.ruta.resolve()))
        self.hasta_fase(Certificado)
        self.comprobar_snapshot(self.argv(), self.nuevo)
        self.assertEqual(self.lineas('versiones.log'), ['nuevo nuevo'])

    def test_symlink_lanzador_sale_del_snapshot(self):
        self.comprobar_symlink('jale.sh')

    def test_symlink_argumento_gancho_sale_del_snapshot(self):
        self.gancho()
        self.comprobar_symlink('gancho.py')

    def test_symlink_ejecutable_gancho_sale_del_snapshot(self):
        self.gancho(ejecutable=True)
        self.comprobar_symlink('gancho.py')

    def comprobar_symlink(self, archivo):
        externo = self.raiz / 'externo'
        externo.write_text('#!/bin/bash\nexit 0\n')
        externo.chmod(0o755)
        malo = self.version('malo', self.nuevo, enlace=(archivo, externo))
        self.push(malo, 'refs/heads/main')
        pausa = self.hasta_pausa().pausa
        self.assertEqual(pausa.detalle, 'checkout-operador:ruta-fuera-del-snapshot')
        self.assertFalse(self.lineas('versiones.log'))
        self.assertFalse(self.lineas('gancho.log'))

    def test_objeto_inexistente_pausa_y_reanuda(self):
        self.hasta_fase(EsperandoCI)
        lectura = self.contexto.git.leer_ref
        with patch.object(self.contexto.git, 'leer_ref',
                          side_effect=lambda ref: 'f' * 40 if ref == 'refs/heads/main' else lectura(ref)):
            pausa = self.hasta_pausa().pausa
        self.assertEqual(pausa.detalle, 'checkout-operador:sha-no-disponible')
        self.assertFalse(self.solicitudes())
        with self.ledger() as ledger:
            eventos = ledger.db.execute("SELECT tipo, payload FROM events WHERE repo=? "
                "ORDER BY seq DESC LIMIT 2", (activo.REPO,)).fetchall()
            self.assertEqual([r[0] for r in eventos], ['operativo', 'efecto-fallido'])
            self.assertEqual(json.loads(eventos[0][1])['estado']['pausa']['detalle'], pausa.detalle)
            self.assertEqual(json.loads(eventos[1][1])['resultado']['error']['codigo'],
                             'checkout-operador')
        self.juicio('reanudar')
        self.hasta_fase(Certificado)
        self.comprobar_snapshot(self.argv(), self.nuevo)

    def test_pausa_manual_persiste(self):
        self.hasta_fase(EsperandoCI)
        self.assertEqual(self.cli('pausa', '--motivo', 'mantenimiento', ahora=890)[0], 0)
        with patch.object(self.contexto.git, 'leer_ref', return_value='f' * 40):
            self.contexto.tick(900)
            self.contexto.tick(910)
        self.assertEqual(replace(self.estado().pausa, hasta=None), Pausa('manual', detalle='mantenimiento'))
        self.assertFalse(self.solicitudes())
        self.assertEqual(self.cli('pausa', '--levantar', '--motivo', 'listo', ahora=920)[0], 0)
        self.hasta_fase(Certificado)
        self.comprobar_snapshot(self.argv(), self.nuevo)

    def test_gancho_cancela_con_argv_verificado(self):
        self.gancho()
        self.hasta_fase(Promoviendo)
        for n in range(3):
            if self.lineas('gancho.log'):
                break
            self.contexto.tick(3000 + n)
        self.assertEqual(self.lineas('gancho.log'), ['verificar nuevo'])
        con_otra_base = self.version('tercero', self.nuevo)
        self.push(con_otra_base, 'refs/heads/main')
        self.contexto.tick(4000)
        self.assertIn('cancelar nuevo', self.lineas('gancho.log'))
        self.assertFalse(any('tercero' in l for l in self.lineas('gancho.log')))

    def test_cancelacion_con_snapshot_sucio_conserva_pausa_existente(self):
        self.gancho()
        self.hasta_fase(Promoviendo)
        for n in range(3):
            if self.lineas('gancho.log'):
                break
            self.contexto.tick(3000 + n)
        self.assertEqual(self.lineas('gancho.log'), ['verificar nuevo'])
        arbol = self.comprobar_snapshot(self.argv(), self.nuevo)
        (arbol / 'modulo').write_text('tocado')
        self.assertEqual(self.cli('pausa', '--motivo', 'mantenimiento')[0], 0)
        self.contexto.tick(4000)
        self.assertEqual(replace(self.estado().pausa, hasta=None), Pausa('manual', detalle='mantenimiento'))
        self.assertEqual(self.lineas('gancho.log'), ['verificar nuevo'])
        codigo, estado = self.cli('estado')
        self.assertEqual(codigo, 0)
        self.assertEqual(estado['cancelaciones_pre_aterrizaje'][0]['ultimo_error'],
                         'checkout-operador:snapshot-no-verificable')
        self.assertEqual((arbol / 'modulo').read_text(), 'tocado')

    def ruta_snapshot(self):
        with self.ledger() as ledger:
            gestor = Snapshots(ledger, activo.REPO, self.contexto.git, self.config)
            return gestor.raiz / clave(str(self.operador)) / self.nuevo

    def comprobar_ocupante(self, arbol):
        (arbol / 'ajeno').write_text('conservar')
        # La versión anterior llega a Certificado y borra el archivo.
        for n in range(8):
            self.contexto.tick(100 + n * 120)
            estado = self.estado()
            if estado.pausa or estado.trenes and isinstance(estado.trenes[0].fase, Certificado):
                break
        self.assertTrue((arbol / 'ajeno').is_file(), 'el tren borró el ocupante ajeno')
        self.assertEqual((arbol / 'ajeno').read_text(), 'conservar')
        self.assertEqual(estado.pausa, Pausa('infraestructura', detalle='checkout-operador:ruta-ocupada'))
        self.assertFalse(self.solicitudes())
        self.assertFalse(self.lineas('versiones.log'))

    def test_worktree_ajeno_sin_reserva_sobrevive_y_pausa(self):
        arbol = self.ruta_snapshot()
        arbol.parent.mkdir(parents=True)
        self.comando(self.operador, 'worktree', 'add', '--detach', str(arbol), self.viejo)
        self.comprobar_ocupante(arbol)
        self.assertEqual(self.cabeza(arbol), self.viejo)

    def test_directorio_ajeno_sin_reserva_sobrevive_y_pausa(self):
        arbol = self.ruta_snapshot()
        arbol.mkdir(parents=True)
        self.comprobar_ocupante(arbol)

    def test_snapshot_reservado_interrumpido_se_recrea(self):
        arbol = self.ruta_snapshot()
        git = Snapshots.comando_git

        def interrumpir(gestor, ruta, *args, **opciones):
            if args[:2] == ('worktree', 'add'):
                # Otra conexión ve la reserva antes de que Git registre la ruta.
                with self.ledger() as ledger:
                    tabla = ledger.db.execute("SELECT 1 FROM sqlite_master "
                                              "WHERE name='snapshots_reservados'").fetchone()
                    self.assertIsNotNone(tabla, 'falta la reserva durable del snapshot')
                    reserva = ledger.db.execute("SELECT 1 FROM snapshots_reservados WHERE ruta=?",
                                                (str(arbol),)).fetchone()
                    self.assertIsNotNone(reserva)
                git(gestor, ruta, *args[:2], '--no-checkout', *args[2:], **opciones)
                git(gestor, ruta, 'worktree', 'lock', '--reason', 'initializing', str(arbol))
                (arbol / 'modulo').write_text('checkout interrumpido')
                raise FalloSnapshot('git-del-operador')
            return git(gestor, ruta, *args, **opciones)

        with patch.object(Snapshots, 'comando_git', interrumpir):
            self.assertEqual(self.hasta_pausa().pausa.detalle, 'checkout-operador:git-del-operador')
        self.assertEqual((arbol / 'modulo').read_text(), 'checkout interrumpido')
        self.assertFalse(self.solicitudes())
        self.juicio('reanudar')
        self.hasta_fase(Certificado)
        self.assertEqual(self.comprobar_snapshot(self.argv(), self.nuevo), arbol)
        self.assertEqual((arbol / 'modulo').read_text(), 'nuevo')
        with self.ledger() as ledger:
            self.assertIsNone(ledger.db.execute("SELECT 1 FROM snapshots_reservados WHERE ruta=?",
                                                (str(arbol),)).fetchone())

    def test_reenganche_tras_muerte_conserva_snapshot_y_argv(self):
        self.plan([{'esperar': True}])
        self.hasta_fase(EsperandoCI)
        self.ruta_contexto.write_text(json.dumps(self.config))
        codigo = """import os
from adaptadores import seleccionar
from test_activo import Contexto
c = Contexto(sys.argv[3])
c.hasito = seleccionar(c.config, c.git)
def morir(punto, efecto):
    if punto == 'efecto-ejecutado' and efecto.tipo == 'iniciar-suite':
        os._exit(77)
c.tick(2000, morir)
"""
        with self.hijo(codigo) as hijo:
            salida, error = hijo.communicate(timeout=30)
            self.assertEqual(hijo.returncode, 77, (salida, error))
        self.esperar(self.raiz / 'listo')
        registro = self.solicitudes()[0]
        anterior = registro.read_bytes()
        self.comprobar_snapshot(self.argv(registro), self.nuevo)
        siguiente = self.version('tercero', self.nuevo)
        self.push(siguiente, 'refs/heads/main')
        (self.raiz / 'seguir').touch()
        self.hasta_fase(Certificado)
        self.assertEqual(self.solicitudes(), [registro])
        self.assertEqual(registro.read_bytes(), anterior)
        self.assertEqual(self.lineas('versiones.log'), ['nuevo nuevo'])
        self.assertEqual(len(self.lineas('lanzamientos.jsonl')), 1)

    def test_main_avanza_antes_de_la_suite_rearma_sin_lanzar(self):
        self.hasta_fase(EsperandoCI)
        tercero = self.version('tercero', self.nuevo)
        self.push(tercero, 'refs/heads/main')
        self.contexto.tick(2000)
        estado = self.estado()
        self.assertEqual(estado.trenes[0].fase, Terminal('REARMAR'))
        self.assertIsNone(estado.pausa)
        self.assertFalse(self.solicitudes())
        self.contexto.tick(2120)
        self.assertEqual(self.estado().trenes[-1].fase, Congelado(tercero))

    def prelanzar(self):
        self.config['prelanzar'] = True
        self.contexto.hasito = seleccionar(self.config, self.contexto.git)

    def ramas_ci(self):
        return self.comando(self.origen, 'for-each-ref', '--format=%(refname)',
                            'refs/heads/tren/').split()

    def test_main_avanza_tras_morir_al_pedir_ci_no_publica_ni_lanza(self):
        self.prelanzar()
        self.ruta_contexto.write_text(json.dumps(self.config))
        codigo = """import os
from adaptadores import seleccionar
from test_activo import Contexto
c = Contexto(sys.argv[3])
c.hasito = seleccionar(c.config, c.git)
def morir(nombre, efecto):
    if nombre == 'antes-ejecutar' and efecto.tipo == 'solicitar-ci':
        os._exit(77)
for n in range(15):
    c.tick(100 + n * 120, morir)
"""
        with self.hijo(codigo) as hijo:
            salida, error = hijo.communicate(timeout=30)
            self.assertEqual(hijo.returncode, 77, (salida, error))
        tercero = self.version('tercero', self.nuevo)
        self.push(tercero, 'refs/heads/main')
        self.contexto.tick(2000)
        estado = self.estado()
        self.assertEqual(estado.trenes[0].fase, Terminal('REARMAR'))
        self.assertIsNone(estado.pausa)
        self.assertEqual(self.ramas_ci(), [])
        self.assertFalse(self.solicitudes())
        self.contexto.tick(2120)
        self.assertEqual(self.estado().trenes[-1].fase, Congelado(tercero))

    def test_main_avanza_en_pausa_y_reanudar_no_pide_ci_ni_lanza(self):
        self.prelanzar()
        self.hasta_fase(EsperandoCI)
        pendiente = self.raiz / 'ci-pendiente'
        pendiente.touch()
        self.assertEqual(self.hasta_pausa().pausa.detalle, 'ci-sin-resultado')
        # Con el CI verde, sólo la B vieja impide publicar y prelanzar.
        pendiente.unlink()
        ramas, lanzamientos = self.ramas_ci(), self.lineas('lanzamientos.jsonl')
        tercero = self.version('tercero', self.nuevo)
        self.push(tercero, 'refs/heads/main')
        self.juicio('reanudar')
        self.contexto.tick(9000)
        self.contexto.tick(9120)
        estado = self.estado()
        self.assertEqual(estado.trenes[0].fase, Terminal('REARMAR'))
        self.assertIsNone(estado.pausa)
        self.assertEqual(self.ramas_ci(), ramas)
        self.assertEqual(self.lineas('lanzamientos.jsonl'), lanzamientos)
        self.contexto.tick(9240)
        self.assertEqual(self.estado().trenes[-1].fase, Congelado(tercero))

    def test_recupera_argv_legacy_de_jale(self):
        ruta = self.config.pop('checkout_operador')
        self.hasta_fase(EsperandoCI)
        self.contexto.tick(2000)
        anterior = self.solicitudes()[0]
        self.assertEqual(Path(self.argv(anterior)[1]).parent, self.operador)
        self.esperar(anterior.with_name('codigo.json'))
        self.config.update(checkout_operador=ruta, lanzador=str(self.operador / 'renombrado.sh'))
        self.contexto.hasito = seleccionar(self.config, self.contexto.git)
        # Instala el tick después de una muerte justo antes de publicar el ACK.
        with self.ledger() as ledger:
            fila = next(r for r in ledger.outbox(activo.REPO) if 'iniciar-suite' in r['payload'])
            ledger.db.execute("UPDATE outbox SET estado='ejecutando' WHERE clave=?", (fila['clave'],))
        self.contexto.tick(2100)
        self.assertIsInstance(self.estado().trenes[0].fase, Certificado)
        self.assertEqual(self.solicitudes(), [anterior])
        self.assertEqual(self.lineas('versiones.log'), ['viejo viejo'])

    def usar_hasito(self):
        guion = self.operador / 'jale.sh'
        self.config.update(adaptador='hasito', lanzador=str(guion))
        self.contexto.hasito = seleccionar(self.config, self.contexto.git)
        original = self.contexto.lanzador

        def lanzar(argv, **kwargs):
            with (self.raiz / 'hasito-argv.log').open('a') as log:
                log.write(json.dumps(argv) + '\n')
            return original(argv, **kwargs)
        self.contexto.hasito.runner = lanzar

    def test_hasito_reanuda_con_la_punta_nueva(self):
        self.usar_hasito()
        (self.raiz / 'suite-75').touch()
        self.hasta_fase(Corriendo)
        primera = json.loads(self.lineas('hasito-argv.log')[0])
        self.comprobar_snapshot(primera, self.nuevo)
        siguiente = self.version('tercero', self.nuevo)
        self.push(siguiente, 'refs/heads/main')
        self.hasta_fase(Certificado)
        _, ultimo = map(json.loads, self.lineas('hasito-argv.log'))
        self.comprobar_snapshot(ultimo, siguiente)
        self.assertIn('--reanudar', ultimo)

    def test_hasito_reanuda_75_tras_retirar_checkout_operador(self):
        self.usar_hasito()
        (self.raiz / 'suite-75').touch()
        self.hasta_fase(Corriendo)
        primera = json.loads(self.lineas('hasito-argv.log')[0])
        self.comprobar_snapshot(primera, self.nuevo)
        self.config.pop('checkout_operador')
        self.usar_hasito()
        self.hasta_fase(Certificado)
        _, ultimo = map(json.loads, self.lineas('hasito-argv.log'))
        self.assertEqual(ultimo[1], self.config['lanzador'])
        self.assertIn('--reanudar', ultimo)

    def test_snapshot_de_tren_terminal_sin_recibo_tiene_plazo(self):
        self.hasta_fase(EsperandoCI)
        estado = self.estado()
        tren = estado.trenes[0]
        with self.ledger() as ledger:
            gestor = Snapshots(ledger, activo.REPO, self.contexto.git, self.config)
            _, anterior = gestor.preparar(self.nuevo)
            siguiente = self.version('tercero', self.nuevo)
            self.push(siguiente, 'refs/heads/main')
            gestor.preparar(siguiente)
            # Referencia histórica sin ruta al recibo del lanzador.
            ledger.db.execute('INSERT INTO snapshots_usados VALUES (?, ?)', (str(anterior), tren.id))
            cerrado = replace(estado, trenes=(replace(tren, fase=Terminal('ABORTADO')),))
            cierre = time.time()
            plazo = 24 * 60 * 60
            ledger.guardar_paso(cerrado, (), cierre)
            ledger.guardar_paso(cerrado, (), cierre + plazo - 2)
            with patch.object(time, 'time', return_value=cierre + plazo - 1):
                gestor.limpiar(cerrado, [])
            self.assertTrue(anterior.exists(), 'la limpieza se adelantó al plazo del tren terminal')
            with patch.object(time, 'time', return_value=cierre + plazo):
                gestor.limpiar(cerrado, [])
            self.assertFalse(anterior.exists())

    def test_limpiar_no_poda_worktrees_ajenos(self):
        # Un worktree ajeno sin directorio puede ser la única referencia a commits locales.
        ajeno = self.raiz / 'ajeno-del-operador'
        self.comando(self.operador, 'worktree', 'add', '--detach', str(ajeno), 'HEAD')
        self.comando(ajeno, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit',
                     '--allow-empty', '-m', 'local')
        local = self.comando(ajeno, 'rev-parse', 'HEAD').strip()
        subprocess.run(['rm', '-rf', str(ajeno)], check=True)
        self.hasta_fase(EsperandoCI)
        estado = self.estado()
        with self.ledger() as ledger:
            gestor = Snapshots(ledger, activo.REPO, self.contexto.git, self.config)
            _, viejo = gestor.preparar(self.viejo)
            _, anterior = gestor.preparar(self.nuevo)
            siguiente = self.version('tercero', self.nuevo)
            self.push(siguiente, 'refs/heads/main')
            gestor.preparar(siguiente)
            # Un snapshot propio cuyo directorio desapareció sólo conserva su registro.
            subprocess.run(['rm', '-rf', str(viejo)], check=True)
            gestor.limpiar(estado, [])
            registrados = self.comando(self.operador, 'worktree', 'list', '--porcelain')
            self.assertIn('worktree ' + str(ajeno), registrados)
            self.assertIn('HEAD ' + local, registrados)
            self.assertNotIn('worktree ' + str(viejo), registrados)
            self.assertNotIn('worktree ' + str(anterior), registrados)
            self.assertFalse(ledger.db.execute(
                'SELECT 1 FROM snapshots_operador WHERE ruta IN (?, ?)',
                (str(viejo), str(anterior))).fetchone())

    def test_hasito_recupera_recibo_anterior(self):
        ruta = self.config.pop('checkout_operador')
        self.usar_hasito()
        self.hasta_fase(EsperandoCI)
        self.contexto.tick(2000)
        anterior = next(self.contexto.hasito.estado.rglob('recibo.json'))
        # El formato instalado previo sólo tenía recibo.json.
        solicitud = anterior.with_name('solicitud.json')
        if solicitud.exists():
            solicitud.unlink()
        self.config.update(checkout_operador=ruta, lanzador=str(self.operador / 'renombrado.sh'))
        self.contexto.hasito.lanzador = self.config['lanzador']
        with self.ledger() as ledger:
            fila = next(r for r in ledger.outbox(activo.REPO) if 'iniciar-suite' in r['payload'])
            ledger.db.execute("UPDATE outbox SET estado='ejecutando' WHERE clave=?", (fila['clave'],))
        self.contexto.tick(2100)
        self.assertIsInstance(self.estado().trenes[0].fase, Certificado)
        self.assertEqual(len(self.lineas('hasito-argv.log')), 1)

    def test_hasito_reanuda_75_con_el_argv_ya_guardado(self):
        self.usar_hasito()
        (self.raiz / 'suite-75').touch()
        self.hasta_fase(Corriendo)
        with patch.object(self.contexto.hasito, 'runner', side_effect=OSError):
            for n in range(4):
                self.contexto.tick(2000 + n * 600)
        peticion = next(r for r in self.solicitudes() if r.parent.name == 'intento-1')
        anterior = self.argv(peticion)
        self.comprobar_snapshot(anterior, self.nuevo)
        self.config.pop('checkout_operador')
        self.usar_hasito()
        self.hasta_fase(Certificado)
        _, ultimo = map(json.loads, self.lineas('hasito-argv.log'))
        self.assertEqual(ultimo, anterior)
        self.assertEqual(self.argv(peticion), anterior)

    def test_cambio_posterior_del_lanzador_rechaza_64_y_reanuda_desde_snapshot_nuevo(self):
        seguir = self.raiz / 'seguir'
        mas_nuevo = self.version('tercero', self.nuevo)
        # Publica el cambio al ejecutar, después de fijar y registrar S.
        origen = shlex.quote(str(self.origen))
        fuente = shlex.quote(str(self.fuente))
        senal = shlex.quote(str(seguir))
        comprobar = (
            f'if ! [ -f {senal} ]; then : > {senal}; '
            f'git -C {fuente} push -f {origen} {mas_nuevo}:refs/heads/main; fi\n'
            'cd "$(dirname "$0")"\n'
            'git fetch -q origin main\n'
            'if [ -n "$(git log -1 --format=%H HEAD..origin/main -- jale.sh)" ]; then exit 64; fi'
        )
        con_guardia = self.version('guardia', self.nuevo, extra=comprobar)
        self.push(con_guardia, 'refs/heads/main')
        self.assertEqual(self.hasta_pausa().pausa.detalle, 'lanzador-rechazo')
        primera = self.solicitudes()[0]
        self.comprobar_snapshot(self.argv(primera), con_guardia)
        self.juicio('reanudar')
        self.hasta_fase(Certificado)
        segunda = next(r for r in self.solicitudes() if r != primera)
        self.comprobar_snapshot(self.argv(segunda), mas_nuevo)

    def test_lectura_concurrente_repeticion_y_limpieza(self):
        salida = self.raiz / 'versiones.log'
        listo, seguir = (shlex.quote(str(self.raiz / s)) for s in ('listo', 'seguir'))
        pausa = (f'touch {listo}; fin=$((SECONDS + 15)); '
                 f'while ! [ -f {seguir} ] && [ "$SECONDS" -lt "$fin" ]; do sleep 0.025; done\n'
                 f'test -f {seguir}')
        anterior = self.version('esperando', self.nuevo, extra=pausa)
        self.push(anterior, 'refs/heads/main')
        self.hasta_fase(EsperandoCI)
        estado = self.estado()
        tren = estado.trenes[0]
        with self.ledger() as ledger:
            adaptador = self.contexto.hasito
            gestor = Snapshots(ledger, activo.REPO, self.contexto.git, self.config)
            adaptador.operador = gestor
            raiz = adaptador.estado
            try:
                # El coordinador queda vivo mientras se prepara otra generación.
                self.assertIs(adaptador.suite(tren.fase.candidato, tren.id), EN_CURSO)
                self.esperar(self.raiz / 'listo')
                registro = self.solicitudes()[0]
                viejo = self.comprobar_snapshot(self.argv(registro), anterior)
                siguiente = self.version('tercero', anterior)
                self.push(siguiente, 'refs/heads/main')
                otro = Snapshots(ledger, activo.REPO, self.contexto.git, self.config)
                adaptador.operador = otro
                nuevo = esperar_resultado(adaptador.suite, tren.fase.candidato, 'otro')[0]
                gestor.limpiar(estado, [])
                self.assertTrue(viejo.exists())
                cerrado = replace(estado, trenes=(replace(tren, fase=Terminal('ABORTADO')),))
                ledger.guardar_paso(cerrado, (), time.time())
                gestor.limpiar(cerrado, [])
                self.assertTrue(viejo.exists(), 'el lanzador de un tren terminal sigue leyendo el snapshot')
                self.assertEqual(salida.read_text().splitlines(), ['tercero tercero'])
            finally:
                (self.raiz / 'seguir').touch()
            self.esperar(registro.with_name('codigo.json'))
            self.assertEqual(salida.read_text().splitlines(), ['tercero tercero', 'esperando esperando'])
            candidato = tren.fase.candidato
            solicitud = Reproduccion('rep', FALLO, 'C')
            # El recibo de origen contiene la unidad aunque el fixture haya pasado.
            ultimo = self.version('cuarto', siguiente)
            self.push(ultimo, 'refs/heads/main')
            adaptador.operador = Snapshots(ledger, activo.REPO, self.contexto.git, self.config)
            esperar_resultado(adaptador.repetir, candidato, nuevo, solicitud, candidato.C, tren.id)
            peticiones = list((raiz / clave('repetir', tren.id, 'rep')).rglob('solicitud.json'))
            self.assertEqual(len(peticiones), 2)
            for r in peticiones:
                self.comprobar_snapshot(self.argv(r), ultimo)
            gestor.limpiar(estado, [{'snapshot': str(viejo)}])
            self.assertTrue(viejo.exists())
            gestor.limpiar(cerrado, [{'snapshot': str(viejo)}])
            self.assertTrue(viejo.exists())
            gestor.limpiar(cerrado, [])
            self.assertFalse(viejo.exists())
            registrados = self.comando(self.operador, 'worktree', 'list', '--porcelain')
            self.assertIn(ultimo, registrados)
            self.assertNotIn(anterior, registrados)

    def test_hasito_conserva_snapshot_si_otro_proceso_del_intento_sigue_vivo(self):
        self.usar_hasito()
        listo, seguir = (shlex.quote(str(self.raiz / s)) for s in ('listo', 'seguir'))
        pausa = (f'touch {listo}; fin=$((SECONDS + 15)); '
                 f'while ! [ -f {seguir} ] && [ "$SECONDS" -lt "$fin" ]; do sleep 0.025; done\n'
                 f'test -f {seguir}\n'
                 'cat "$(dirname "$0")/modulo"\nexit 0')
        anterior = self.version('esperando', self.nuevo, extra=pausa)
        self.push(anterior, 'refs/heads/main')
        self.hasta_fase(EsperandoCI)
        estado = self.estado()
        tren = estado.trenes[0]
        adaptador = self.contexto.hasito
        procesos = []

        def lanzar_y_salir(argv, *, check, capture_output, **opciones):
            procesos.append(subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             **opciones))
            raise OSError('el tick no recogió el resultado')

        from adaptadores.hasito import ResultadoInvalido
        with self.ledger() as ledger:
            gestor = Snapshots(ledger, activo.REPO, self.contexto.git, self.config)
            adaptador.operador = gestor
            try:
                with patch.object(adaptador, 'runner', lanzar_y_salir):
                    with self.assertRaisesRegex(ResultadoInvalido, 'lanzador-no-disponible'):
                        adaptador.suite(tren.fase.candidato, tren.id)
                self.esperar(self.raiz / 'listo')
                registro = self.solicitudes()[0]
                viejo = self.comprobar_snapshot(self.argv(registro), anterior)
                adaptador.suite(tren.fase.candidato, tren.id)
                self.assertTrue(registro.with_name('recibo.json').exists())
                siguiente = self.version('tercero', anterior)
                self.push(siguiente, 'refs/heads/main')
                gestor.preparar(siguiente)
                cerrado = replace(estado, trenes=(replace(tren, fase=Terminal('ABORTADO')),))
                ledger.guardar_paso(cerrado, (), time.time())
                gestor.limpiar(cerrado, [])
                self.assertTrue(viejo.exists(), 'un recibo no detiene otra ejecución viva del intento')
            finally:
                (self.raiz / 'seguir').touch()
                for proceso in procesos:
                    salida, error = proceso.communicate(timeout=15)
            self.assertEqual((salida, error), ('esperando', ''))
            gestor.limpiar(cerrado, [])
            self.assertFalse(viejo.exists())

    def test_sin_checkout_operador(self):
        self.config.pop('checkout_operador')
        self.hasta_fase(Certificado)
        self.assertEqual(Path(self.argv()[1]).parent, self.operador)
        self.assertEqual(self.lineas('versiones.log'), ['viejo viejo'])
