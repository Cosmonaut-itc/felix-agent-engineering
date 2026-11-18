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
        def version(nombre, padre):
            entrada = ('echo entrada=' + nombre + ' >> ' + shlex.quote(str(mezcla)) +
                '\ntouch ' + shlex.quote(str(inicio)) + '\nwhile [ ! -e ' +
                shlex.quote(str(seguir)) + ' ]; do sleep .02; done\n' +
                '. "$(dirname "$0")/modulo.sh"\nexec ' +
                shlex.join([sys.executable, str(t.productor), str(t.raiz)]) + ' "$@"\n')
            return t.commit({'jale.sh': entrada, 'modulo.sh': 'echo modulo=' + nombre +
                            ' >> ' + shlex.quote(str(mezcla)) + '\n'}, (padre,))
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
        _, _, cert, _, _, viejo, _ = self.jale_vivo()
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
        r = t.contexto.tick(9100)
        self.assertIn('promover', [e['tipo'] for e in r['efectos']])
        self.assertIsNone(t.estado().pausa)

    def test_r1_muerte_al_guardar_pausa_no_pierde_efectos_ni_ack(self):
        t = self.t
        fallo, gancho, original = self.preparar_pausa_gancho()
        # La cancelación inicial no altera la pausa anterior. Abortar crea
        # Terminal y feedback; limpiar encuentra el checkout sucio después.
        t.juicio('abortar')
        t.ruta_contexto.write_text(json.dumps(t.contexto.config))
        codigo = '''from test_activo import Contexto, REPO
from ledger import Ledger
from dominio import Terminal
import os
original = Ledger.guardar_paso
def frontera(nombre, efecto):
    if nombre == 'paso-guardado': os._exit(77)
def guardar(self, estado, efectos, ahora, consumido=None):
    original(self, estado, efectos, ahora, consumido)
    if estado.trenes and isinstance(estado.trenes[0].fase, Terminal):
        frontera('paso-guardado', None)
Ledger.guardar_paso = guardar
c = Contexto(sys.argv[3])
c.tick(9200, frontera)
'''
        with activo.ActivoTests.hijo(t, codigo) as hijo:
            salida, error = hijo.communicate(timeout=30)
            self.assertEqual(hijo.returncode, 77, (salida, error))
        with t.ledger() as ledger:
            self.assertEqual(t.estado().trenes[0].fase, Terminal('ABORTADO'))
            decision = ledger.db.execute('SELECT estado FROM decisions ORDER BY rowid DESC').fetchone()[0]
            self.assertEqual(decision, 'aplicada')
            efectos = [json.loads(r['payload'])['tipo'] for r in ledger.outbox(activo.REPO)]
            self.assertTrue(any(e.startswith('avisar-') for e in efectos), efectos)
        gancho.write_text(original)
        fallo.rename(t.raiz / 'cancelacion-reparada')
        t.contexto.tick(9300)


if __name__ == '__main__':
    unittest.main()
