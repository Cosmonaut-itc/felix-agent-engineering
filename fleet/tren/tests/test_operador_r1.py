"""Ronda 1: procesos y Git reales en fixtures privadas del host de pruebas."""
from dataclasses import replace
import inspect
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
import test_operador as fixtures
from adaptadores.jale import Jale
from adaptadores.hasito import ResultadoInvalido
from dominio import Certificado, EsperandoCI, Promoviendo, Terminal
import git_ops
from ledger import Ledger


class OperadorR1Tests(unittest.TestCase):
    # Composición: discovery no vuelve a ejecutar todos los casos originales.
    def setUp(self):
        self.t = fixtures.OperadorTests()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def operador(self):
        # Permite injertar la misma regresión en el head revisado sin errores
        # de firma; allí el remoto explícito todavía no existía.
        opciones = ({'remoto': str(self.t.remoto_operador)} if 'remoto' in
                    inspect.signature(git_ops.CheckoutOperador).parameters else {})
        return git_ops.CheckoutOperador(self.t.operador, **opciones)

    def esperar_archivo(self, ruta):
        limite = time.monotonic() + 20
        while not ruta.exists() and time.monotonic() < limite:
            time.sleep(.02)
        self.assertTrue(ruta.exists(), str(ruta))

    def jale_vivo(self):
        t = self.t
        t.hasta_fase(Certificado)
        cert = t.estado().trenes[0].fase
        inicio, seguir, mezcla = (t.raiz / n for n in ('inicio', 'seguir', 'mezcla.log'))
        archivos = {p.name: p.read_text() for p in t.operador.iterdir() if p.is_file()}
        def version(nombre, padre):
            entrada = ('echo entrada=' + nombre + ' >> ' + shlex.quote(str(mezcla)) +
                '\ntouch ' + shlex.quote(str(inicio)) + '\nwhile [ ! -e ' +
                shlex.quote(str(seguir)) + ' ]; do sleep .02; done\n' +
                '. "$(dirname "$0")/modulo.sh"\nexec ' +
                shlex.join([sys.executable, str(t.productor), str(t.raiz)]) + ' "$@"\n')
            return t.commit(dict(archivos, **{'jale.sh': entrada, 'modulo.sh': 'echo modulo=' + nombre +
                            ' >> ' + shlex.quote(str(mezcla)) + '\n'}), (padre,))
        viejo = version('viejo', t.nuevo)
        nuevo = version('nuevo', viejo)
        t.push(viejo, 'refs/heads/main')
        registro = t.raiz / 'registro-vivo'
        # El padre real muere con os._exit: no hay finally que pueda sostener
        # el candado en su nombre. Coordinador y lanzador quedan vivos.
        codigo = '''import inspect, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from adaptadores.jale import Jale
from git_ops import CheckoutOperador
lanzador, registro, C, remoto, inicio, pid = sys.argv[2:]
a = Jale(lanzador, estado=Path(registro).parent / 'estado-jale', modos=['contract'])
kw = {'remoto': remoto} if 'remoto' in inspect.signature(CheckoutOperador).parameters else {}
a.operador = CheckoutOperador(Path(lanzador).parent, **kw)
def morir(p):
    Path(pid).write_text(str(p.pid))
    limite = time.monotonic() + 20
    while not Path(inicio).exists() and time.monotonic() < limite: time.sleep(.02)
    os._exit(77 if Path(inicio).exists() else 78)
a.esperar = morir
a.correr(Path(registro), C, 'contract', (), True)
'''
        with subprocess.Popen([sys.executable, '-I', '-B', '-c', codigo, str(activo.RAIZ),
                str(t.operador / 'jale.sh'), str(registro), cert.C, str(t.remoto_operador),
                str(inicio), str(t.raiz / 'coordinador.pid')], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE) as padre:
            salida, error = padre.communicate(timeout=30)
            self.assertEqual(padre.returncode, 77, (salida, error))
        self.assertFalse((registro / 'codigo.json').exists())
        os.kill(int((t.raiz / 'coordinador.pid').read_text()), 0)
        def terminar():
            seguir.touch()
            self.esperar_archivo(registro / 'codigo.json')
        self.addCleanup(terminar)
        t.push(nuevo, 'refs/heads/main')
        a = Jale(t.operador / 'jale.sh', estado=t.raiz / 'estado-jale', modos=['contract'])
        a.operador = self.operador()
        return a, registro, cert, seguir, mezcla, viejo, nuevo

    def test_r1_jale_reenganche_coordinador_vivo_no_mezcla_modulo(self):
        a, registro, cert, seguir, mezcla, viejo, _ = self.jale_vivo()
        a.esperar = lambda p: (seguir.touch(), p.wait(timeout=30))
        r = a.correr(registro, cert.C, 'contract', (), True)
        self.assertEqual(r['codigo'], 0)
        self.assertEqual(mezcla.read_text().splitlines(), ['entrada=viejo', 'modulo=viejo'])
        self.assertEqual(self.t.cabeza(), viejo)

    def test_r1_lector_huerfano_difiere_trabajo_nuevo_y_estado_lo_muestra(self):
        _, registro, cert, seguir, _, viejo, nuevo = self.jale_vivo()
        t = self.t
        # Crear otro efecto real de suite, con el lector huérfano aún vivo.
        # Volver al estado anterior a lanzar preserva el resto de la fixture.
        t.contexto.config.pop('pre_aterrizaje', None)
        from dominio import Efecto, datos, clave
        with t.ledger() as ledger:
            estado = ledger.estado(activo.REPO, operativo=True)
            tren = estado.trenes[0]
            # Una repetición llama al mismo runner con otra clave; usar iniciar
            # suite sobre el candidato certificado basta para esta frontera IO.
            from dominio import Candidato, EsperandoCI
            candidato = Candidato(cert.B, (), cert.C, cert.arbol)
            fase = EsperandoCI(candidato)
            estado = replace(estado, trenes=(replace(tren, fase=fase),))
            efecto = Efecto(clave('suite', tren.id), 'iniciar-suite', (tren.id,))
            ledger.db.execute("UPDATE outbox SET estado='pendiente' WHERE clave=?", (efecto.clave,))
            ledger.guardar_paso(estado, (efecto,), 8000)
        anteriores = t.lineas('versiones.log')
        t.contexto.hasito.estado = t.raiz / 'suite-nueva'
        observar = t.contexto.observador.observar
        with patch.object(t.contexto.observador, 'observar',
                side_effect=lambda e, a: replace(observar(e, a), base=cert.B)):
            t.contexto.tick(8001)
        self.assertEqual(t.cabeza(), viejo)
        self.assertEqual(t.lineas('versiones.log'), anteriores)
        self.assertIsNone(t.estado().pausa)
        _, visible = t.cli('estado')
        self.assertTrue(any(r['resultado'] and
            r['resultado'].get('error', {}).get('codigo') == 'checkout-operador-lector-vivo'
            for r in visible['outbox']))
        seguir.touch()
        self.esperar_archivo(registro / 'codigo.json')
        # El código durable precede por instantes al cierre del descriptor.
        limite = time.monotonic() + 10
        while True:
            with patch.object(t.contexto.observador, 'observar',
                    side_effect=lambda e, a: replace(observar(e, a), base=cert.B)):
                t.contexto.tick(8002)
            if t.cabeza() == nuevo:
                break
            if time.monotonic() >= limite:
                self.fail('no reintentó al terminar el lector')
            time.sleep(.02)
        self.assertEqual(t.lineas('versiones.log'), anteriores + ['nuevo'])

    def test_r1_hasito_y_gancho_heredan_lector_tras_muerte_del_tick(self):
        t = self.t
        for tipo in ('hasito', 'gancho'):
            with self.subTest(tipo=tipo):
                inicio, seguir, fin = (t.raiz / (tipo + '-' + n) for n in ('inicio', 'seguir', 'fin'))
                def version(nombre, padre):
                    entrada = ('touch ' + shlex.quote(str(inicio)) + '\nwhile [ ! -e ' +
                        shlex.quote(str(seguir)) + ' ]; do sleep .02; done\n' +
                        '. "$(dirname "$0")/modulo.sh"\n')
                    modulo = 'echo ' + nombre + ' > ' + shlex.quote(str(fin)) + '\n'
                    return t.commit({'entrada.sh': entrada, 'modulo.sh': modulo}, (padre,))
                anterior = t.comando(t.remoto_operador, 'rev-parse', 'main')
                viejo = version('viejo', anterior)
                nuevo = version('nuevo', viejo)
                t.push(viejo, 'refs/heads/main')
                self.operador().avanzar()
                codigo = '''import inspect, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from adaptadores.hasito import Hasito
from git_ops import CheckoutOperador, GitTren
from preaterrizaje import Previo
from ledger import Ledger
raiz, remoto, tipo = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
kw = {'remoto': remoto} if 'remoto' in inspect.signature(CheckoutOperador).parameters else {}
if tipo == 'hasito':
    a = Hasito(raiz / 'entrada.sh', GitTren(remoto))
    a.operador = CheckoutOperador(raiz, **kw)
    a.correr(['bash', str(raiz / 'entrada.sh')], raiz.parent / 'hasito-huerfano')
else:
    p = Previo(Ledger(), GitTren(remoto), {}, lambda *a: None)
    p.llamar({'comando': ['/bin/bash', str(raiz / 'entrada.sh')], 'plazo_s': 60},
             {'id': 'lector'}, 'verificar', str(raiz))
'''
                padre = subprocess.Popen([sys.executable, '-I', '-B', '-c', codigo,
                    str(activo.RAIZ), str(t.operador), str(t.remoto_operador), tipo],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    self.esperar_archivo(inicio)
                    padre.kill()
                    padre.communicate(timeout=10)
                    t.push(nuevo, 'refs/heads/main')
                    try:
                        self.operador().avanzar()
                        bloqueo = None
                    except git_ops.CheckoutOperadorPendiente as exc:
                        bloqueo = str(exc)
                    cabeza = t.cabeza()
                finally:
                    seguir.touch()
                    if padre.poll() is None:
                        padre.kill()
                    padre.communicate(timeout=10)
                    self.esperar_archivo(fin)
                self.assertEqual(bloqueo, 'checkout-operador-lector-vivo')
                self.assertEqual(cabeza, viejo)
                self.assertEqual(fin.read_text().strip(), 'viejo')
                # El lector terminó: el siguiente intento sí puede avanzar.
                limite = time.monotonic() + 10
                while True:
                    try:
                        self.operador().avanzar()
                        break
                    except git_ops.CheckoutOperadorPendiente:
                        if time.monotonic() >= limite: raise
                        time.sleep(.02)
                self.assertEqual(t.cabeza(), nuevo)

    def test_r1_origin_ajeno_no_cambia_procedencia_del_tick(self):
        t = self.t
        ajeno = t.raiz / 'ajeno.git'
        t.comando(t.raiz, 'init', '--bare', '--initial-branch=main', str(ajeno))
        no_publicado = t.version('ajeno', t.nuevo)
        t.comando(t.fuente, 'push', str(ajeno), no_publicado + ':refs/heads/main')
        t.comando(t.operador, 'remote', 'set-url', 'origin', str(ajeno))
        t.hasta_fase(Certificado)
        self.assertEqual(t.cabeza(), t.nuevo)
        self.assertEqual(t.lineas('versiones.log'), ['nuevo'])

    def symlink(self, nombre, externo):
        t = self.t
        # Preservar los demás scripts para validar ambos lanzadores y gancho.
        entradas = t.comando(t.fuente, 'ls-tree', t.nuevo).splitlines()
        blob = t.comando(t.fuente, 'hash-object', '-w', '--stdin', input=str(externo))
        entradas = [e for e in entradas if e.split('\t')[1] != nombre]
        entradas.append('120000 blob ' + blob + '\t' + nombre)
        arbol = t.comando(t.fuente, 'mktree', input='\n'.join(entradas) + '\n')
        commit = t.comando(t.fuente, '-c', 'user.name=Fixture', '-c',
            'user.email=fixture@example.invalid', 'commit-tree', arbol, '-p', t.nuevo,
            input='symlink\n')
        t.push(commit, 'refs/heads/main')

    def test_r1_symlink_gancho_cambia_despues_de_validar_no_invoca(self):
        t = self.t
        t.contexto.config['pre_aterrizaje'] = dict(
            comando=[sys.executable, str(t.operador / 'gancho.py')], plazo_s=5)
        git_ops.validar_checkout_operador(t.contexto.config)
        externo = t.raiz / 'externo.py'
        externo.write_text('from pathlib import Path\nPath(' + repr(str(t.raiz / 'invocado')) +
                           ').touch()\n')
        self.symlink('gancho.py', externo)
        t.hasta_pausa()
        self.assertFalse((t.raiz / 'invocado').exists())
        self.assertEqual(t.estado().pausa.detalle, 'checkout-operador:script-fuera-del-checkout')

    def test_r1_symlink_lanzador_cambia_despues_de_validar_no_invoca(self):
        t = self.t
        t.hasta_fase(EsperandoCI)
        git_ops.validar_checkout_operador(dict(t.contexto.config,
            lanzador_hasito=str(t.operador / 'hasito.sh')))
        externo = t.raiz / 'externo.sh'
        externo.write_text('touch ' + shlex.quote(str(t.raiz / 'invocado')) + '\n')
        self.symlink('hasito.sh', externo)
        t.hasta_pausa()
        self.assertFalse((t.raiz / 'invocado').exists())
        self.assertEqual(t.estado().pausa.detalle, 'checkout-operador:script-fuera-del-checkout')

    def preparar_pausa_gancho(self):
        t = self.t
        fallo = t.raiz / 'cancelar-falla'
        fallo.touch()
        codigo = ('import json,sys\nfrom pathlib import Path\ns=json.load(sys.stdin)\n'
            + 'fallo=Path(' + repr(str(fallo)) + ').exists()\n'
            + 'sys.exit(1) if s["accion"]=="cancelar" and fallo else None\n'
            + 'print(json.dumps(dict(version=1,id=s["id"],resultado="pausar" '
            + 'if s["accion"]=="verificar" else "cancelado")))\n')
        entradas = t.comando(t.fuente, 'ls-tree', t.nuevo).splitlines()
        blob = t.comando(t.fuente, 'hash-object', '-w', '--stdin', input=codigo)
        entradas = [e for e in entradas if e.split('\t')[1] != 'gancho.py']
        entradas.append('100644 blob ' + blob + '\tgancho.py')
        arbol = t.comando(t.fuente, 'mktree', input='\n'.join(entradas) + '\n')
        tercero = t.comando(t.fuente, '-c', 'user.name=Fixture', '-c',
            'user.email=fixture@example.invalid', 'commit-tree', arbol, '-p', t.nuevo,
            input='gancho pausante\n')
        t.push(tercero, 'refs/heads/main')
        t.contexto.config['pre_aterrizaje'] = dict(
            comando=[sys.executable, str(t.operador / 'gancho.py')], plazo_s=5)
        t.hasta_pausa()
        self.assertEqual(t.estado().pausa.detalle, 'pre-aterrizaje:gancho-pausar')
        gancho = t.operador / 'gancho.py'
        original = gancho.read_text()
        gancho.write_text(original + '# local\n')
        return fallo, gancho, original

    def test_r1_pausa_original_conservada_y_reanudar_repone_promover(self):
        t = self.t
        fallo, gancho, original = self.preparar_pausa_gancho()
        t.contexto.tick(9000)
        self.assertEqual(t.estado().pausa.detalle, 'pre-aterrizaje:gancho-pausar')
        _, visible = t.cli('estado')
        self.assertIn('checkout-operador:cambios-sin-commitear', json.dumps(visible))
        gancho.write_text(original)
        fallo.rename(t.raiz / 'cancelacion-reparada')
        t.juicio('reanudar')
        _, r = t.contexto.tick(9100)
        self.assertIn('promover', [e['tipo'] for e in r['efectos']])
        self.assertIsNone(t.estado().pausa)

    def muerte_despues_del_primer_commit_pausado(self, condicion):
        t = self.t
        t.ruta_contexto.write_text(json.dumps(t.contexto.config))
        # Inyección de fallo IO en limpiar, no del reductor ni del ledger.
        # La misma frontera mata tras el primer COMMIT del estado pausado:
        # en el head revisado es el COMMIT prematuro, con el arreglo es el
        # COMMIT único que incluye efectos y ACK.
        codigo = '''from test_activo import Contexto, REPO
from ledger import Ledger
from dominio import Certificado, Promoviendo
from preaterrizaje import Previo
from git_ops import CheckoutOperadorBloqueado
import os
original = Ledger.guardar_paso
def frontera(nombre, efecto):
    if nombre == 'paso-guardado': os._exit(77)
def limpiar(self, estado, observacion=None):
    if CONDICION:
        raise CheckoutOperadorBloqueado('cambios-sin-commitear')
    return True
def guardar(self, estado, efectos, ahora, consumido=None):
    original(self, estado, efectos, ahora, consumido)
    if estado.pausa and estado.pausa.detalle.startswith('checkout-operador:'):
        frontera('paso-guardado', None)
Previo.limpiar = limpiar
Ledger.guardar_paso = guardar
c = Contexto(sys.argv[3])
c.tick(9200, frontera)
'''.replace('CONDICION', condicion)
        with activo.ActivoTests.hijo(t, codigo) as hijo:
            salida, error = hijo.communicate(timeout=30)
            self.assertEqual(hijo.returncode, 77, (salida, error))

    def test_r1_muerte_al_guardar_pausa_no_pierde_promover_ni_juicio(self):
        t = self.t
        fallo, gancho, original = self.preparar_pausa_gancho()
        gancho.write_text(original)
        t.juicio('reanudar')
        self.muerte_despues_del_primer_commit_pausado(
            'estado.trenes and isinstance(estado.trenes[0].fase, Promoviendo) '
            'and estado.trenes[0].fase.intentos == 2')
        with t.ledger() as ledger:
            decision = ledger.db.execute('SELECT estado FROM decisions ORDER BY rowid DESC').fetchone()[0]
            self.assertEqual(decision, 'aplicada')
            efectos = [json.loads(r['payload']) for r in ledger.outbox(activo.REPO)]
            self.assertTrue(any(e['tipo'] == 'promover' and e['datos'][-1] == '2'
                                for e in efectos), efectos)

    def test_r1_muerte_al_guardar_pausa_marca_resultado_consumido(self):
        t = self.t
        t.hasta_fase(EsperandoCI)
        t.contexto.tick(8000)
        with t.ledger() as ledger:
            resultados = [r for r in ledger.outbox(activo.REPO) if r['estado'] == 'resultado']
            self.assertEqual(len(resultados), 1)
            clave_resultado = resultados[0]['clave']
        self.muerte_despues_del_primer_commit_pausado(
            'estado.trenes and isinstance(estado.trenes[0].fase, Certificado)')
        self.assertIsInstance(t.estado().trenes[0].fase, Certificado)
        with t.ledger() as ledger:
            self.assertEqual(ledger.db.execute('SELECT estado FROM outbox WHERE clave=?',
                (clave_resultado,)).fetchone()[0], 'completo')
            ultimo = json.loads(ledger.db.execute("SELECT payload FROM events WHERE tipo='operativo' "
                "ORDER BY seq DESC LIMIT 1").fetchone()[0])
            self.assertEqual(ultimo['consumido'], clave_resultado)


if __name__ == '__main__':
    unittest.main()
