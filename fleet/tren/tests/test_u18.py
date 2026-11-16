"""U18: ticks y procesos reales, origen local y gancho ejecutable falso. Sólo remoto."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_activo as activo
import test_juicio
import test_u17
from dominio import Certificado, Promoviendo, Terminal, datos
from observador import GitHubGit

REPO = activo.REPO
AHORA = 2000000000


def utc(valor):
    return datetime.fromtimestamp(valor, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ContextoU18(unittest.TestCase):
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    lineas = activo.ActivoTests.lineas
    hijo = activo.ActivoTests.hijo
    juicio = test_juicio.JuicioTests.juicio
    cli = test_juicio.JuicioTests.cli
    posts = test_juicio.JuicioTests.posts

    def setUp(self):
        activo.ActivoTests.setUp(self)
        self.ahora = AHORA
        self.guion = self.raiz / "gancho.py"
        self.guion.write_text('''import json, pathlib, subprocess, sys, time
r = pathlib.Path(__file__).resolve().parent
s = json.load(sys.stdin)
with (r / 'gancho.log').open('a') as f:
    f.write(json.dumps(s, sort_keys=True) + '\\n')
modo = (r / 'modo').read_text()
if s['accion'] == 'cancelar':
    if (r / 'cancelacion-falla').exists():
        sys.exit(2)
    resultado = 'cancelado'
else:
    if modo == 'timeout':
        time.sleep(5)
    if modo == 'malformado':
        print('dato privado que nunca sale del proceso')
        sys.exit(0)
    if modo == 'error':
        sys.exit(4)
    if modo == 'id-distinto':
        s['id'] = 'otra-identidad'
        modo = 'seguir'
    if modo == 'version-desconocida':
        print(json.dumps({'version': 2, 'id': s['id'], 'resultado': 'seguir'}))
        sys.exit(0)
    if modo == 'duplicado':
        print('{"version":1,"id":' + json.dumps(s['id']) + ',"resultado":"pausar","resultado":"seguir"}')
        sys.exit(0)
    if modo == 'cambiar-base':
        valor = json.loads((r / 'cambio.json').read_text())
        subprocess.run(['git', '-C', valor['origen'], 'update-ref', 'refs/heads/main', valor['sha']], check=True)
        modo = 'seguir'
    if modo == 'vencer':
        (r / 'reloj').write_text(str(2000000200))
        modo = 'seguir'
    resultado = modo
print(json.dumps({'version': 1, 'id': s['id'], 'resultado': resultado}))
''')
        self.config = self.contexto.config
        self.config['pre_aterrizaje'] = {'comando': [sys.executable, '-I', str(self.guion)],
                                        'plazo_s': 1}
        self.modo('esperar')

    def modo(self, valor):
        (self.raiz / 'modo').write_text(valor)

    def llamadas(self, accion=None):
        llamadas = [json.loads(s) for s in self.lineas('gancho.log')]
        return [s for s in llamadas if accion is None or s['accion'] == accion]

    def reloj(self):
        ruta = self.raiz / 'reloj'
        return int(ruta.read_text()) if ruta.exists() else self.ahora

    def tick(self, frontera=None):
        # Se usa el CLI activo y su flock, con el observador y origen reales.
        # El reloj controla la hora al final de la suite y después del gancho.
        with patch('time.time', side_effect=self.reloj):
            codigo, salida = self.contexto.tick(5000, frontera)
        self.assertEqual(codigo, 0, salida)
        self.sin_rojo()
        return salida

    def hasta(self, condicion, limite=15):
        for _ in range(limite):
            self.tick()
            if condicion():
                return self.estado()
        self.fail('no convergio: ' + json.dumps(datos(self.estado())))

    def preparar(self):
        self.hasta(lambda: bool(self.estado().trenes) and
                   isinstance(self.estado().trenes[0].fase, Certificado))
        self.assertFalse(self.llamadas())
        self.assertEqual(len(self.lineas('trabajos.log')), 1)
        self.assertEqual(self.remoto(), self.B)

    def sin_rojo(self):
        self.assertFalse(any(p['payload'].get('state') == 'failure' for p in self.posts()))
        self.assertFalse(self.posts('/comments'))

    def individual(self, inicio, fin):
        pr, head = self.miembros[0]
        # Custodia del resto sigue presente pero sus vistas quedan retenidas.
        self.contexto.observador.vista_original = self.contexto.observador.vista
        original = self.contexto.observador.vista
        def vista(entrega):
            v = original(entrega)
            return replace(v, ci='pendiente') if entrega.pr != pr else v
        self.contexto.observador.vista = vista
        self.config['heads_autorizados'] = [{'head': head, 'ventana':
            {'inicio': utc(inicio), 'fin': utc(fin)}}]


class RegresionesTests(ContextoU18):
    def test_esperar_no_aterriza_ni_agota_intentos_y_reconsulta(self):
        self.preparar()
        self.tick()
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(len(self.llamadas('verificar')), 1)
        primera = self.llamadas()[0]
        self.assertEqual((primera['repo'], primera['B']), (REPO, self.B))
        self.assertEqual(primera['prs'], [{'pr': p, 'head': h} for p, h in self.miembros])
        self.assertNotEqual(primera['C'], self.B)
        almacen = Path(primera['almacen_git'])
        self.assertEqual(almacen, self.contexto.git.ruta.resolve())
        self.assertEqual(self.comando(almacen, 'rev-parse', primera['C']), primera['C'])
        for _ in range(4):
            self.tick()
            self.assertIsNone(self.estado().pausa)
            self.assertEqual(self.estado().trenes[0].fase.intentos, 1)
            self.assertEqual(self.remoto(), self.B)
        self.assertEqual(len(self.llamadas('verificar')), 5)
        self.assertFalse(self.llamadas('cancelar'))
        self.modo('seguir')
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))
        self.assertEqual(self.remoto(), primera['C'])
        self.assertFalse(self.llamadas('cancelar'))

    def test_pausar_timeout_error_y_salida_malformada(self):
        self.preparar()
        for modo in ('pausar', 'timeout', 'malformado', 'desconocida', 'error',
                     'id-distinto', 'version-desconocida', 'duplicado'):
            with self.subTest(modo=modo):
                cancelaciones = len(self.llamadas('cancelar'))
                self.modo(modo)
                self.hasta(lambda: self.estado().pausa is not None)
                self.assertEqual(self.remoto(), self.B)
                self.assertEqual(self.estado().pausa.motivo, 'infraestructura')
                expediente = self.cli('estado', '--juicio')[1]['juicio']
                self.assertEqual({a['decision'] for a in expediente['admitidas']}, {'reanudar', 'abortar'})
                self.assertEqual(len(self.llamadas('cancelar')), cancelaciones + 1)
                self.assertNotIn('dato privado', json.dumps(expediente))
                self.juicio('reanudar')
                self.modo('esperar')
                self.tick()
                self.assertIsNone(self.estado().pausa)
        self.modo('seguir')
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))

    def test_base_cambia_durante_gancho_no_push_y_cancelacion(self):
        self.preparar()
        ajeno = self.commit({'comun': 'base\n', 'ajeno': 'externo\n'}, (self.B,))
        self.push(ajeno, 'refs/heads/externo')
        (self.raiz / 'cambio.json').write_text(json.dumps({'origen': str(self.origen), 'sha': ajeno}))
        self.modo('cambiar-base')
        self.tick()
        self.assertEqual(self.remoto(), ajeno)
        self.assertEqual(len(self.llamadas('verificar')), 1)
        self.assertEqual(len(self.llamadas('cancelar')), 1)
        self.tick()
        self.assertEqual(self.estado().pausa.motivo, 'escritor-externo')
        self.assertFalse(any(p.endswith(':refs/heads/main') for p in self.lineas('pushes.log')))

    def test_ventana_antes_dentro_y_vencida_sin_gancho(self):
        self.config.pop('pre_aterrizaje')
        self.individual(AHORA + 100, AHORA + 200)
        self.preparar()
        self.tick()
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(len(self.estado().trenes[0].recibos), 1)
        self.assertIsNone(self.estado().pausa)
        self.ahora = AHORA + 100
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))
        self.assertFalse(self.llamadas())

    def test_ventana_vencida_y_que_vence_en_el_gancho(self):
        self.individual(AHORA - 100, AHORA + 100)
        self.preparar()
        self.modo('vencer')
        self.hasta(lambda: self.estado().pausa is not None)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.estado().pausa.detalle, 'pre-aterrizaje:ventana-vencida')
        self.assertEqual(len(self.llamadas('cancelar')), 1)
        self.juicio('reanudar')
        self.modo('seguir')
        self.tick()
        self.hasta(lambda: self.estado().pausa is not None)
        self.assertEqual(len(self.llamadas('verificar')), 1)
        self.assertEqual(self.remoto(), self.B)
        self.config['heads_autorizados'][0]['ventana']['fin'] = utc(AHORA + 300)
        self.juicio('reanudar')
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))

    def test_muerte_tras_gancho_revalida_sin_usar_seguir_guardado(self):
        self.preparar()
        self.modo('seguir')
        self.ruta_contexto.write_text(json.dumps(self.config))
        proceso = self.hijo("from test_activo import Contexto\n"
            "from unittest.mock import patch\n"
            "import os\nc = Contexto(sys.argv[3])\n"
            "def frontera(nombre, efecto):\n"
            "    if nombre == 'pre-aterrizaje-verificado': os._exit(77)\n"
            "with patch('time.time', return_value=" + str(self.ahora) + "):\n"
            "    c.tick(5000, frontera)\n")
        out, err = proceso.communicate(timeout=30)
        self.assertEqual(proceso.returncode, 77, out + err)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(len(self.llamadas('verificar')), 1)
        self.modo('esperar')
        self.tick()
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(len(self.llamadas('verificar')), 2)
        self.assertEqual(self.llamadas()[0]['id'], self.llamadas()[1]['id'])
        self.modo('seguir')
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))


class CicloTests(ContextoU18):
    def test_cancelar_al_pausar_y_abortar_sin_rojo(self):
        self.preparar()
        self.tick()
        self.cli('pausa', '--motivo', 'pausa de prueba')
        self.tick()
        self.assertEqual(len(self.llamadas('cancelar')), 1)
        self.cli('pausa', '--levantar', '--motivo', 'reanudar prueba')
        self.tick()
        self.modo('pausar')
        self.hasta(lambda: self.estado().pausa is not None)
        self.juicio('abortar')
        self.tick()
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ABORTADO'))
        self.assertEqual(self.remoto(), self.B)

    def test_cancelacion_fallida_retiene_diario_y_reanuda_con_verificacion(self):
        self.preparar()
        (self.raiz / 'cancelacion-falla').write_text('fallar')
        self.modo('pausar')
        self.hasta(lambda: self.estado().pausa is not None)
        self.assertEqual(self.remoto(), self.B)
        with self.ledger() as l:
            self.assertEqual(l.db.execute('SELECT pendiente FROM pre_aterrizajes').fetchone()[0], 1)
        self.tick()
        expediente = self.cli('estado', '--juicio')[1]['juicio']
        self.assertEqual({a['decision'] for a in expediente['admitidas']}, {'reanudar', 'abortar'})
        (self.raiz / 'cancelacion-falla').rename(self.raiz / 'cancelacion-recuperada')
        self.juicio('reanudar')
        self.modo('seguir')
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))
        with self.ledger() as l:
            self.assertEqual(l.db.execute('SELECT pendiente FROM pre_aterrizajes').fetchone()[0], 0)

    def test_muerte_despues_push_no_cancela_ni_repite_gancho(self):
        self.preparar()
        self.modo('seguir')
        self.ruta_contexto.write_text(json.dumps(self.config))
        proceso = self.hijo("from test_activo import Contexto\n"
                            "Contexto(sys.argv[3], 'durante-promover').tick(5000)\n")
        out, err = proceso.communicate(timeout=30)
        self.assertEqual(proceso.returncode, 77, out + err)
        self.assertNotEqual(self.remoto(), self.B)
        self.modo('pausar')
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))
        self.assertEqual(len(self.llamadas('verificar')), 1)
        self.assertFalse(self.llamadas('cancelar'))

    def test_cas_rechaza_cambio_tras_verificacion(self):
        self.preparar()
        self.modo('seguir')
        ajeno = self.commit({'comun': 'base\n', 'ajeno': 'externo\n'}, (self.B,))
        self.push(ajeno, 'refs/heads/externo')
        def frontera(nombre, efecto):
            if nombre == 'pre-aterrizaje-verificado':
                self.push(ajeno, 'refs/heads/main')
        self.tick(frontera)
        self.assertEqual(self.remoto(), ajeno)
        self.assertEqual(len(self.llamadas('cancelar')), 1)
        self.tick()
        self.assertEqual(self.estado().pausa.motivo, 'escritor-externo')

    def test_ventana_vence_mientras_espera_cancela_antes_de_pausar(self):
        self.individual(AHORA - 100, AHORA + 100)
        self.preparar()
        self.tick()
        self.assertEqual(len(self.llamadas('verificar')), 1)
        self.ahora = AHORA + 100
        self.hasta(lambda: self.estado().pausa is not None)
        self.assertEqual(self.estado().pausa.detalle, 'pre-aterrizaje:ventana-vencida')
        self.assertEqual(len(self.llamadas('cancelar')), 1)
        self.assertEqual(len(self.llamadas('verificar')), 1)
        self.assertEqual(self.remoto(), self.B)

    def test_perder_admision_cancela_y_revalida_al_recuperarla(self):
        self.preparar()
        self.tick()
        (self.raiz / 'ci-pendiente').write_text('pendiente')
        self.tick()
        self.assertEqual(len(self.llamadas('cancelar')), 1)
        self.assertEqual(self.remoto(), self.B)
        self.assertIsNone(self.estado().pausa)
        (self.raiz / 'ci-pendiente').rename(self.raiz / 'ci-recuperado')
        self.modo('seguir')
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))
        self.assertEqual(len(self.llamadas('verificar')), 2)

    def test_configuracion_nueva_cancela_con_comando_anterior(self):
        self.preparar()
        self.tick()
        self.config.pop('pre_aterrizaje')
        self.modo('seguir')
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))
        self.assertEqual(len(self.llamadas('cancelar')), 1)
        self.assertEqual(len(self.llamadas('verificar')), 1)


class ConfigTests(unittest.TestCase):
    def test_ventanas_y_comando_rechazan_ambiguedad(self):
        from preaterrizaje import autorizaciones, gancho
        head = 'a' * 40
        self.assertEqual(autorizaciones({'heads_autorizados': [head]}), {head: None})
        for v in ({'inicio': '2026-01-01T00:00:00+01:00', 'fin': '2026-01-02T00:00:00Z'},
                  {'inicio': '2026-01-01T00:00:00Z', 'fin': '2026-01-01T00:00:00Z'},
                  {'inicio': '2026-02-30T00:00:00Z', 'fin': '2026-03-02T00:00:00Z'}):
            with self.assertRaises(ValueError):
                autorizaciones({'heads_autorizados': [{'head': head, 'ventana': v}]})
        with self.assertRaises(ValueError):
            autorizaciones({'heads_autorizados': [head, head]})
        for c in ({'comando': ['relative'], 'plazo_s': 1},
                  {'comando': ['/absolute'], 'plazo_s': float('nan')},
                  {'comando': ['/absolute'], 'plazo_s': True}):
            with self.assertRaises(ValueError):
                gancho({'pre_aterrizaje': c})


class SombraTests(test_u17.SombraTests):
    def setUp(self):
        super().setUp()
        marca = self.raiz / 'gancho-prohibido'
        self.contexto.config['pre_aterrizaje'] = {
            'comando': [sys.executable, '-c',
                        'from pathlib import Path; Path(' + repr(str(marca)) + ').touch()'],
            'plazo_s': 1}
        # La sombra conserva la interpretación histórica de la lista de SHA:
        # ni siquiera una autorización con ventana puede modificar sus eventos.
        self.contexto.config['sensibles']['gobierno'] = ['src/*']
        self.contexto.config['heads_autorizados'] = [{'head': self.siguientes[0][1],
            'ventana': {'inicio': '2026-01-01T00:00:00Z', 'fin': '2026-01-02T00:00:00Z'}}]
        self.privada.write_text(json.dumps({'repos': {REPO: self.contexto.config}}))
        self.addCleanup(lambda: self.assertFalse(marca.exists()))
