"""SEN-7032: checkouts reales y fronteras falsas; sólo en host de pruebas."""
import fcntl
import json
import os
from pathlib import Path
import selectors
import shlex
import subprocess
import sys
import unittest

import test_activo as activo
import test_juicio as juicio_fixtures
from test_jale import PRODUCTOR
from test_hasito import F, suite_fixture
from adaptadores import seleccionar
from adaptadores.hasito import Hasito
from dominio import Candidato, Certificado, EsperandoCI, Promoviendo, Terminal, clave
from observador import configuracion
import git_ops


class OperadorTests(unittest.TestCase):
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

    def setUp(self):
        activo.ActivoTests.setUp(self)
        self.operador = self.raiz / 'operador'
        self.remoto_operador = self.raiz / 'operador-origin.git'
        self.comando(self.raiz, 'init', '--bare', '--initial-branch=main', str(self.remoto_operador))
        self.productor = self.raiz / 'productor.py'
        self.productor.write_text(PRODUCTOR)
        (self.raiz / 'plan.json').write_text(json.dumps([{}]))
        productor_hasito = self.raiz / 'productor-hasito.py'
        productor_hasito.write_text('''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_hasito import suite_fixture, repeticion_fixture, F
from dominio import Candidato
args = sys.argv[2:]
raiz = Path(args[args.index('--evidencia') + 1])
raiz.mkdir(parents=True, exist_ok=True)
rep = '--repetir' in args
commit = args[args.index('--sha' if rep else '--suite') + 1]
if rep:
    origen = Path(args[args.index('--repetir') + 1])
    fuente = json.loads(origen.read_text())
    candidato = Candidato(fuente['base'], (), fuente['sha'], 'd' * 40)
    r = repeticion_fixture(origen, commit, candidato, F, ('pasa', 'pasa'))
else:
    candidato = Candidato(args[args.index('--base') + 1], (), commit, 'd' * 40)
    r = suite_fixture(candidato, 0, ())
ruta = raiz / ('repeticion.json' if rep else 'suite.json')
ruta.write_text(json.dumps(r))
print(('REPETICION' if rep else 'SUITE') + ' veredicto=0 ' +
      ('repeticion=' if rep else 'resultado=') + str(ruta))
''')
        self.productor_hasito = productor_hasito
        self.viejo = self.version('viejo')
        self.comando(self.fuente, 'push', str(self.remoto_operador), self.viejo + ':refs/heads/main')
        self.comando(self.raiz, 'clone', str(self.remoto_operador), str(self.operador))
        self.nuevo = self.version('nuevo', self.viejo)
        self.comando(self.fuente, 'push', str(self.remoto_operador), self.nuevo + ':refs/heads/main')
        self.contexto.config['checkout_operador'] = str(self.operador)
        self.contexto.hasito = Hasito(self.operador / 'hasito.sh', self.contexto.git)

    def version(self, nombre, padre=None):
        salida = shlex.quote(str(self.raiz / 'versiones.log'))
        lanzadores = {}
        for adaptador, productor, prefijo in (
                ('hasito', self.productor_hasito, [str(activo.RAIZ / 'tests')]),
                ('jale', self.productor, [str(self.raiz)])):
            lanzadores[adaptador + '.sh'] = ('#!/bin/bash\necho ' + nombre + ' >> ' + salida +
                '\nexec ' + shlex.join([sys.executable, str(productor), *prefijo]) + ' "$@"\n')
        lanzadores['gancho.py'] = ("import json, sys\nfrom pathlib import Path\n"
            + 'Path(' + repr(str(self.raiz / 'gancho-version')) + ').write_text(' + repr(nombre) + ')\n'
            + "s=json.load(sys.stdin)\n"
            + 'Path(' + repr(str(self.raiz / 'gancho-accion')) + ").write_text(s['accion'])\n"
            + 'esperar=Path(' + repr(str(self.raiz / 'gancho-esperar')) + ').exists()\n'
            + "print(json.dumps(dict(version=1,id=s['id'],resultado="
            + "('esperar' if esperar else 'seguir') if s['accion']=='verificar' else 'cancelado')))\n")
        lanzadores['vivo.sh'] = ('echo listo\nread -r continuar\n' + '# relleno\n' * 20000 +
                                  'echo ' + nombre + '\n')
        if padre:
            lanzadores['agregado'] = 'nuevo\n'
        return self.commit(lanzadores, (padre,) if padre else (), nombre)

    def cabeza(self):
        return self.comando(self.operador, 'rev-parse', 'HEAD')

    def avanzar(self):
        clase = getattr(git_ops, 'CheckoutOperador', None)
        self.assertIsNotNone(clase, 'falta avance del checkout del operador')
        return clase(self.operador).avanzar()

    def lanzar(self):
        self.hasta_fase(Certificado)
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.lineas('versiones.log'), ['nuevo'])
        self.assertEqual(self.cabeza(), self.nuevo)

    def test_atras_avanza_antes_de_lanzar_hasito(self):
        self.lanzar()

    def test_atras_avanza_antes_de_lanzar_jale(self):
        self.contexto.config.update(adaptador='jale', lanzador=str(self.operador / 'jale.sh'),
                                   modos=['contract'])
        self.contexto.hasito = seleccionar(self.contexto.config, self.contexto.git)
        self.lanzar()

    def test_al_dia_no_reemplaza_archivos_ni_mueve_head(self):
        self.assertTrue(self.avanzar())
        ruta = self.operador / 'hasito.sh'
        antes = ruta.stat()
        self.assertFalse(self.avanzar())
        despues = ruta.stat()
        self.assertEqual((antes.st_ino, antes.st_mtime_ns), (despues.st_ino, despues.st_mtime_ns))
        self.assertEqual(self.cabeza(), self.nuevo)

    def bloqueo(self, codigo, reparar):
        antes = self.cabeza()
        self.hasta_fase(EsperandoCI)
        self.hasta_pausa()
        pausa = self.estado().pausa
        self.assertEqual((pausa.motivo, pausa.detalle),
                         ('infraestructura', 'checkout-operador:' + codigo))
        self.assertEqual(self.cabeza(), antes)
        self.assertEqual(self.lineas('versiones.log'), [])
        _, visible = self.cli('estado', '--juicio')
        self.assertEqual(visible['estado']['pausa']['detalle'], pausa.detalle)
        self.assertEqual({a['decision'] for a in visible['juicio']['admitidas']}, {'reanudar', 'abortar'})
        reparar()
        self.juicio('reanudar')
        self.contexto.tick(10000)
        self.lanzar()

    def test_sucio_pausa_y_reanudar_avanza(self):
        ruta = self.operador / 'hasito.sh'
        original = ruta.read_text()
        ruta.write_text(original + '# local\n')
        self.bloqueo('cambios-sin-commitear', lambda: ruta.write_text(original))

    def test_otra_rama_pausa_y_reanudar_avanza(self):
        self.comando(self.operador, 'switch', '-c', 'otra')
        self.bloqueo('rama-distinta-de-main', lambda: self.comando(self.operador, 'switch', 'main'))

    def test_divergente_pausa_y_reanudar_avanza(self):
        self.comando(self.operador, 'config', 'user.name', 'Fixture')
        self.comando(self.operador, 'config', 'user.email', 'fixture@example.invalid')
        self.comando(self.operador, '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'local')
        # Conserva el commit local antes de restaurar la rama de la fixture.
        self.comando(self.operador, 'branch', 'conservar-local')
        self.bloqueo('historia-divergente', lambda: self.comando(self.operador, 'reset', '--hard', self.viejo))

    def test_sin_seguimiento_no_se_pisa_y_reanudar_avanza(self):
        ruta = self.operador / 'agregado'
        ruta.write_text('propio\n')
        self.bloqueo('sin-seguimiento-en-el-avance', lambda: ruta.rename(self.raiz / 'agregado-conservado'))
        self.assertEqual((self.raiz / 'agregado-conservado').read_text(), 'propio\n')

    def test_desacoplado_ancestro_avanza_sin_cambiar_rama(self):
        self.comando(self.operador, 'switch', '--detach', self.viejo)
        self.assertTrue(self.avanzar())
        self.assertEqual(self.cabeza(), self.nuevo)
        self.assertEqual(self.comando(self.operador, 'rev-parse', '--abbrev-ref', 'HEAD'), 'HEAD')

    def test_desacoplado_no_ancestro_pausa(self):
        ajeno = self.commit({'ajeno': 'local\n'})
        self.comando(self.fuente, 'push', str(self.remoto_operador), ajeno + ':refs/heads/ajeno')
        self.comando(self.operador, 'fetch', 'origin', 'ajeno')
        self.comando(self.operador, 'switch', '--detach', ajeno)
        self.bloqueo('head-desacoplado-no-ancestro',
                     lambda: self.comando(self.operador, 'switch', 'main'))

    def test_fallo_fetch_no_pausa_reintenta_tick_siguiente(self):
        self.hasta_fase(EsperandoCI)
        self.comando(self.operador, 'remote', 'set-url', 'origin', str(self.raiz / 'inexistente.git'))
        self.contexto.tick(5000)
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.lineas('versiones.log'), [])
        _, visible = self.cli('estado')
        self.assertTrue(any(r['resultado'] and isinstance(r['resultado'].get('error'), dict) and
                            r['resultado']['error'].get('codigo') ==
                            'checkout-operador-fetch-pendiente' for r in visible['outbox']))
        self.assertEqual(self.cabeza(), self.viejo)
        self.comando(self.operador, 'remote', 'set-url', 'origin', str(self.remoto_operador))
        self.contexto.tick(5001)
        self.lanzar()

    def test_gancho_corre_con_checkout_avanzado(self):
        self.hasta_fase(Certificado)
        # La suite ya usó la versión nueva. Dejar el operador de nuevo atrás
        # para acreditar la frontera independiente de pre-aterrizaje.
        self.comando(self.operador, 'reset', '--hard', self.viejo)
        self.contexto.config['pre_aterrizaje'] = dict(
            comando=[sys.executable, str(self.operador / 'gancho.py')], plazo_s=5)
        self.hasta_fase(Promoviendo)
        self.assertEqual((self.raiz / 'gancho-version').read_text(), 'nuevo')
        self.assertEqual(self.cabeza(), self.nuevo)

    def test_bash_vivo_conserva_version_original_e_inodo(self):
        ruta = self.operador / 'vivo.sh'
        with ruta.open('rb') as abierto:
            proceso = subprocess.Popen(['bash', str(ruta)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(lambda: proceso.poll() is None and proceso.kill())
            with selectors.DefaultSelector() as selector:
                selector.register(proceso.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(10), 'bash no llegó a la barrera')
            self.assertEqual(proceso.stdout.readline().strip(), 'listo')
            self.assertTrue(self.avanzar())
            self.assertNotEqual(os.fstat(abierto.fileno()).st_ino, ruta.stat().st_ino)
            self.assertIn(b'echo viejo', abierto.read())
            salida, error = proceso.communicate('seguir\n', timeout=10)
            self.assertEqual(proceso.returncode, 0, error)
            self.assertEqual(salida.strip(), 'viejo')
            self.assertIn('echo nuevo', ruta.read_text())

    def test_config_rechaza_lanzador_y_gancho_fuera_al_cargar(self):
        config = dict(self.contexto.config, lanzador_hasito=str(self.operador / 'hasito.sh'))
        ruta = self.raiz / 'privada.json'
        (self.operador / 'enlace.sh').symlink_to(self.raiz / 'fuera.sh')
        for invalido in (
                dict(config, lanzador_hasito=str(self.raiz / 'fuera.sh')),
                dict(config, lanzador=str(self.raiz / 'fuera.sh')),
                dict(config, lanzador_hasito=str(self.operador / 'enlace.sh')),
                dict(config, pre_aterrizaje=dict(comando=[sys.executable, str(self.raiz / 'fuera.py')], plazo_s=5)),
                dict(config, checkout_operador='relativo')):
            with self.subTest(config=invalido):
                ruta.write_text(json.dumps({'repos': {activo.REPO: invalido}}))
                with self.assertRaises(ValueError):
                    configuracion(ruta, activo.REPO)

    def test_config_acepta_script_interno_e_interprete_externo(self):
        ruta = self.raiz / 'privada.json'
        config = dict(self.contexto.config, lanzador_hasito=str(self.operador / 'hasito.sh'),
            pre_aterrizaje=dict(comando=[sys.executable, str(self.operador / 'gancho.py'),
                                        '--private-config', '/private/config.json'], plazo_s=5))
        ruta.write_text(json.dumps({'repos': {activo.REPO: config}}))
        self.assertEqual(configuracion(ruta, activo.REPO), config)

    def test_sin_clave_conserva_lanzamiento_anterior(self):
        self.contexto.config.pop('checkout_operador')
        (self.operador / 'hasito.sh').write_text((self.operador / 'hasito.sh').read_text() + '# local\n')
        self.hasta_fase(Certificado)
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.lineas('versiones.log'), ['viejo'])
        self.assertEqual(self.cabeza(), self.viejo)

    def test_abortar_conserva_checkout_y_no_lanza(self):
        (self.operador / 'hasito.sh').write_text('cambio propio\n')
        self.hasta_pausa()
        self.assertEqual(self.estado().pausa.motivo, 'infraestructura')
        self.juicio('abortar')
        self.contexto.tick(10000)
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        self.assertEqual(self.cabeza(), self.viejo)
        self.assertEqual(self.lineas('versiones.log'), [])


    def test_candado_comun_serializa_otro_proceso(self):
        comun = Path(self.comando(self.operador, 'rev-parse', '--absolute-git-dir'))
        with (comun / 'pao-tren-operador.lock').open('a') as candado:
            fcntl.flock(candado, fcntl.LOCK_EX)
            guion = ("import sys; sys.path.insert(0, sys.argv[1]); "
                     "from git_ops import CheckoutOperador; "
                     "CheckoutOperador(sys.argv[2]).avanzar()")
            proceso = subprocess.Popen([sys.executable, '-I', '-c', guion,
                str(activo.RAIZ), str(self.operador)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(lambda: proceso.poll() is None and proceso.kill())
            with self.assertRaises(subprocess.TimeoutExpired):
                proceso.communicate(timeout=.3)
            self.assertEqual(self.cabeza(), self.viejo)
            fcntl.flock(candado, fcntl.LOCK_UN)
            salida, error = proceso.communicate(timeout=10)
            self.assertEqual(proceso.returncode, 0, (salida, error))
            self.assertEqual(self.cabeza(), self.nuevo)

    def test_reanudar_y_repetir_hasito_avanzan_antes_del_runner(self):
        self.assertTrue(self.avanzar())
        self.hasta_fase(Certificado)
        certificado = self.estado().trenes[0].fase
        candidato = Candidato(certificado.B, (), certificado.C, certificado.arbol)
        adaptador = self.contexto.hasito
        from dominio import Reproduccion
        origen = self.raiz / 'origen-suite.json'
        origen.write_text(json.dumps(suite_fixture(candidato, 1, (F,))))
        solicitud = Reproduccion('reproduccion', F, 'C', (1,), 10)
        # Construir el comando de reanudación válido sin haber corrido antes.
        raiz = adaptador.estado / clave('suite', 'reanudar')
        argv = adaptador.argv_suite(candidato, self.contexto.git.worktree(candidato.C),
                                   raiz / 'evidencia', 'suite-reanudar')
        self.comando(self.operador, 'reset', '--hard', self.viejo)
        adaptador.suite(candidato, 'reanudar', 1, shlex.join([*argv, '--reanudar']))
        self.assertEqual(self.cabeza(), self.nuevo)
        self.comando(self.operador, 'reset', '--hard', self.viejo)
        adaptador.repetir(candidato, str(origen), solicitud, candidato.C, 'repetir')
        self.assertEqual(self.cabeza(), self.nuevo)
        self.assertEqual(self.lineas('versiones.log'), ['nuevo', 'nuevo', 'nuevo'])

    def test_reanudar_y_repetir_jale_avanzan(self):
        self.contexto.config.update(adaptador='jale', lanzador=str(self.operador / 'jale.sh'),
                                   modos=['contract'])
        self.contexto.hasito = seleccionar(self.contexto.config, self.contexto.git)
        self.hasta_fase(Certificado)
        adaptador = self.contexto.hasito
        certificado = self.estado().trenes[0].fase
        candidato = Candidato(certificado.B, (), certificado.C, certificado.arbol)
        self.comando(self.operador, 'reset', '--hard', self.viejo)
        raiz, _ = adaptador.suite(candidato, 'reanudar-nuevo', intento=1)
        self.assertEqual(self.cabeza(), self.nuevo)
        self.comando(self.operador, 'reset', '--hard', self.viejo)
        from dominio import Reproduccion
        from test_jale import FALLO
        solicitud = Reproduccion('rep-jale', FALLO, 'C', (1,), 10)
        adaptador.repetir(candidato, raiz, solicitud, candidato.C, 'rep-jale')
        self.assertEqual(self.cabeza(), self.nuevo)
        self.assertEqual(self.lineas('versiones.log'), ['nuevo'] * 4)


    def test_cancelacion_avanza_checkout_original_antes_del_gancho(self):
        self.hasta_fase(Certificado)
        self.contexto.config['pre_aterrizaje'] = dict(
            comando=[sys.executable, str(self.operador / 'gancho.py')], plazo_s=5)
        (self.raiz / 'gancho-esperar').write_text('esperar')
        self.hasta_fase(Promoviendo)
        self.assertEqual((self.raiz / 'gancho-accion').read_text(), 'verificar')
        self.comando(self.operador, 'reset', '--hard', self.viejo)
        self.contexto.config.pop('pre_aterrizaje')
        self.contexto.tick(5000)
        self.assertEqual(self.cabeza(), self.nuevo)
        self.assertEqual((self.raiz / 'gancho-version').read_text(), 'nuevo')
        self.assertEqual((self.raiz / 'gancho-accion').read_text(), 'cancelar')


    def test_ignorado_que_se_pisaria_pausa_sin_perder_archivo(self):
        (self.operador / '.git/info/exclude').write_text('agregado\n')
        ruta = self.operador / 'agregado'
        ruta.write_text('ignorado propio\n')
        self.bloqueo('sin-seguimiento-en-el-avance',
                     lambda: ruta.rename(self.raiz / 'ignorado-conservado'))
        self.assertEqual((self.raiz / 'ignorado-conservado').read_text(), 'ignorado propio\n')

    def test_archivo_sin_seguimiento_ajeno_al_avance_se_conserva(self):
        ruta = self.operador / 'local'
        ruta.write_text('conservar\n')
        self.lanzar()
        self.assertEqual(ruta.read_text(), 'conservar\n')

    def test_cancelacion_sucia_pausa_y_reanudar_cancela(self):
        self.hasta_fase(Certificado)
        self.contexto.config['pre_aterrizaje'] = dict(
            comando=[sys.executable, str(self.operador / 'gancho.py')], plazo_s=5)
        (self.raiz / 'gancho-esperar').write_text('esperar')
        self.hasta_fase(Promoviendo)
        self.comando(self.operador, 'reset', '--hard', self.viejo)
        ruta = self.operador / 'hasito.sh'
        original = ruta.read_text()
        ruta.write_text(original + '# local\n')
        self.contexto.config.pop('pre_aterrizaje')
        self.contexto.tick(5000)
        self.assertIsNotNone(self.estado().pausa)
        self.assertEqual(self.estado().pausa.detalle, 'checkout-operador:cambios-sin-commitear')
        self.assertEqual((self.raiz / 'gancho-accion').read_text(), 'verificar')
        self.assertEqual(self.cabeza(), self.viejo)
        ruta.write_text(original)
        self.juicio('reanudar')
        self.contexto.tick(5001)
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.cabeza(), self.nuevo)
        self.assertEqual((self.raiz / 'gancho-accion').read_text(), 'cancelar')
