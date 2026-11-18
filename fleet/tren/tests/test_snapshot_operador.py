"""SEN-7032: Git real, lanzadores de prueba y tick activo."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_activo as activo
from test_jale import FALLO, PRODUCTOR
import test_juicio as juicio_fixtures
from adaptadores import seleccionar
from dominio import (Certificado, Corriendo, EsperandoCI, Pausa, Promoviendo,
                     Reproduccion, Terminal, clave)


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
        self.assertEqual(self.cli('pausa', '--motivo', 'mantenimiento')[0], 0)
        with patch.object(self.contexto.git, 'leer_ref', return_value='f' * 40):
            self.contexto.tick(900)
            self.contexto.tick(910)
        self.assertEqual(self.estado().pausa, Pausa('manual', detalle='mantenimiento'))
        self.assertFalse(self.solicitudes())
        self.assertEqual(self.cli('pausa', '--levantar', '--motivo', 'listo')[0], 0)
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
        self.assertEqual(self.estado().pausa, Pausa('manual', detalle='mantenimiento'))
        self.assertEqual(self.lineas('gancho.log'), ['verificar nuevo'])
        codigo, estado = self.cli('estado')
        self.assertEqual(codigo, 0)
        self.assertEqual(estado['cancelaciones_pre_aterrizaje'][0]['ultimo_error'],
                         'checkout-operador:snapshot-no-verificable')
        self.assertEqual((arbol / 'modulo').read_text(), 'tocado')

    def test_snapshot_sin_marca_se_recrea(self):
        try:
            from operador import Snapshots
        except ImportError:
            self.fail('falta el registro de snapshots')
        with self.ledger() as ledger:
            gestor = Snapshots(ledger, activo.REPO, self.contexto.git, self.config)
            anterior = gestor.raiz / clave(str(self.operador)) / self.nuevo
        anterior.parent.mkdir(parents=True)
        self.comando(self.operador, 'worktree', 'add', '--detach', str(anterior), self.viejo)
        (anterior / 'rastro').touch()
        self.hasta_fase(Certificado)
        self.assertEqual(self.comprobar_snapshot(self.argv(), self.nuevo), anterior)
        self.assertFalse((anterior / 'rastro').exists())

    def test_reenganche_tras_muerte_conserva_snapshot_y_argv(self):
        self.plan([{'esperar': True}])
        self.hasta_fase(EsperandoCI)
        self.ruta_contexto.write_text(json.dumps(self.config))
        codigo = """import os
from adaptadores import seleccionar
from test_activo import Contexto
c = Contexto(sys.argv[3])
c.hasito = seleccionar(c.config, c.git)
c.hasito.esperar = lambda proceso: os._exit(77)
c.tick(2000)
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

    def test_recupera_argv_legacy_de_jale(self):
        ruta = self.config.pop('checkout_operador')
        self.hasta_fase(EsperandoCI)
        self.contexto.tick(2000)
        anterior = self.solicitudes()[0]
        self.assertEqual(Path(self.argv(anterior)[1]).parent, self.operador)
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
        try:
            from operador import Snapshots
        except ImportError:
            self.fail('no hay snapshots por SHA')
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
            procesos = []
            adaptador.esperar = procesos.append
            raiz = adaptador.estado
            try:
                # El coordinador queda vivo mientras se prepara otra generación.
                from adaptadores.hasito import ResultadoInvalido
                with self.assertRaisesRegex(ResultadoInvalido, 'corrida-interrumpida'):
                    adaptador.suite(tren.fase.candidato, tren.id)
                self.esperar(self.raiz / 'listo')
                registro = self.solicitudes()[0]
                viejo = self.comprobar_snapshot(self.argv(registro), anterior)
                siguiente = self.version('tercero', anterior)
                self.push(siguiente, 'refs/heads/main')
                otro = Snapshots(ledger, activo.REPO, self.contexto.git, self.config)
                adaptador.operador = otro
                del adaptador.esperar
                nuevo = adaptador.suite(tren.fase.candidato, 'otro')[0]
                gestor.limpiar(estado, [])
                self.assertTrue(viejo.exists())
                self.assertEqual(salida.read_text().splitlines(), ['tercero tercero'])
            finally:
                (self.raiz / 'seguir').touch()
                for proceso in procesos:
                    self.assertEqual(proceso.wait(timeout=15), 0)
            self.esperar(registro.with_name('codigo.json'))
            self.assertEqual(salida.read_text().splitlines(), ['tercero tercero', 'esperando esperando'])
            candidato = tren.fase.candidato
            solicitud = Reproduccion('rep', FALLO, 'C')
            # El recibo de origen contiene la unidad aunque el fixture haya pasado.
            ultimo = self.version('cuarto', siguiente)
            self.push(ultimo, 'refs/heads/main')
            adaptador.operador = Snapshots(ledger, activo.REPO, self.contexto.git, self.config)
            adaptador.repetir(candidato, nuevo, solicitud, candidato.C, tren.id)
            peticiones = list((raiz / clave('repetir', tren.id, 'rep')).rglob('solicitud.json'))
            self.assertEqual(len(peticiones), 2)
            for r in peticiones:
                self.comprobar_snapshot(self.argv(r), ultimo)
            gestor.limpiar(estado, [{'snapshot': str(viejo)}])
            self.assertTrue(viejo.exists())
            cerrado = replace(estado, trenes=(replace(tren, fase=Terminal('ABORTADO')),))
            gestor.limpiar(cerrado, [{'snapshot': str(viejo)}])
            self.assertTrue(viejo.exists())
            gestor.limpiar(cerrado, [])
            self.assertFalse(viejo.exists())
            registrados = self.comando(self.operador, 'worktree', 'list', '--porcelain')
            self.assertIn(ultimo, registrados)
            self.assertNotIn(anterior, registrados)

    def test_sin_checkout_operador(self):
        self.config.pop('checkout_operador')
        self.hasta_fase(Certificado)
        self.assertEqual(Path(self.argv()[1]).parent, self.operador)
        self.assertEqual(self.lineas('versiones.log'), ['viejo viejo'])
