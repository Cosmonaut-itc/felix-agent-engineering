"""U18 R3: obligaciones observadas en el transporte, independientes del diario."""
from itertools import product
import unittest
from unittest.mock import patch

from test_u18_r2 import ContextoR2
from dominio import Aterrizado, Incierto, Terminal
from git_ops import FalloGit
from preaterrizaje import invocar


class ContextoR3(ContextoR2):
    def instrumentar(self):
        self.fallos = (False, False, False)  # push, lectura posterior, cancelar
        self.deudas = set()
        self.intentos = 0
        self.eventos = []
        self.push_en_tick = False
        original_git = self.contexto.git.git
        original_reconciliar = self.contexto.git.reconciliar

        def transporte(config, solicitud, accion):
            self.identidad = solicitud['id']
            self.C = solicitud['C']
            if accion == 'cancelar':
                # La solicitud ya debe ser durable antes de gastar IO.
                self.assertTrue(self.diario()[0][0]['cancelacion_requerida'])
                self.assertEqual(self.remoto(), self.B)
                if self.fallos[2]:
                    (self.raiz / 'cancelacion-falla').write_text('fallar')
                elif (self.raiz / 'cancelacion-falla').exists():
                    (self.raiz / 'cancelacion-falla').rename(self.raiz / 'recuperada')
            resultado = invocar(config, solicitud, accion)
            self.eventos.append((accion, resultado))
            if resultado == 'cancelado':
                self.deudas.discard(solicitud['id'])
            return resultado

        def git(*args, **kwargs):
            if args[0] == 'push' and args[-1].endswith(':refs/heads/main'):
                # Oráculo externo: no consulta la marca que estamos probando.
                self.assertFalse(self.deudas, 'push con cancelación sin confirmar')
                self.intentos += 1
                self.push_en_tick = True
                self.eventos.append(('push', self.fallos[0]))
                if self.fallos[0]:
                    self.deudas.add(self.identidad)
                    raise FalloGit('transporte-push')
            return original_git(*args, **kwargs)

        def reconciliar(B, C, arbol, heads):
            if self.push_en_tick and self.fallos[1]:
                self.deudas.add(self.identidad)
                return Incierto(B, C, 'lectura-posterior-incierta')
            lectura = original_reconciliar(B, C, arbol, heads)
            if isinstance(lectura, Aterrizado):
                self.deudas.discard(self.identidad)
            return lectura

        self.contexto.git.git = git
        self.contexto.git.reconciliar = reconciliar
        return patch('preaterrizaje.invocar', side_effect=transporte)

    def tick_comprobado(self, fallos):
        self.fallos = fallos
        self.push_en_tick = False
        inicio = len(self.eventos)
        self.tick()
        self.assertIsNone(self.estado().pausa, 'la limpieza no debe pausar')
        visibles = self.cli('estado')[1]['cancelaciones_pre_aterrizaje']
        self.assertEqual({r['id'] for r in visibles}, self.deudas,
                         'estado perdió una obligación del transporte')
        pendientes = {r['solicitud']['id'] for r, p in self.diario()
                      if p and r.get('cancelacion_requerida')}
        self.assertEqual(pendientes, self.deudas, 'diario perdió una obligación')
        cancelaciones = [e for e in self.eventos[inicio:] if e[0] == 'cancelar']
        self.assertLessEqual(len(cancelaciones), 1)

    def recuperar(self):
        for _ in range(6):
            self.tick_comprobado((False, False, False))
            if self.estado().trenes[0].fase == Terminal('ATERRIZADO'):
                break
        self.assertEqual(self.estado().trenes[0].fase, Terminal('ATERRIZADO'))
        self.assertFalse(self.deudas)
        self.assertEqual(self.remoto(), self.C)
        self.assertEqual(self.pushes_main(), 1)
        self.sin_decisiones()


class RegresionTests(ContextoR3):
    def test_dos_pushes_fallidos_conservan_segunda_obligacion_hasta_tick_siguiente(self):
        self.preparar()
        self.modo('seguir')
        with self.instrumentar():
            self.tick_comprobado((True, False, True))
            self.assertEqual(self.intentos, 1)
            self.tick_comprobado((True, False, False))
            self.assertEqual(self.intentos, 2)
            self.assertEqual(self.remoto(), self.B)
            self.assertEqual(self.pushes_main(), 0)
            self.assertEqual(len(self.deudas), 1)
            self.assertEqual(self.eventos, [('verificar', 'seguir'), ('push', True),
                ('cancelar', 'error'), ('cancelar', 'cancelado'),
                ('verificar', 'seguir'), ('push', True)])
            self.recuperar()
        self.assertEqual([s['accion'] for s in self.llamadas()],
            ['verificar', 'cancelar', 'cancelar', 'verificar', 'cancelar', 'verificar'])
        self.assertEqual(self.intentos, 3)


class InvarianteTests(unittest.TestCase):
    def test_combinaciones_por_tick_con_origen_y_gancho_reales(self):
        opciones = list(product((False, True), repeat=3))
        cantidad = 0
        # 8 x 8: todos los pares de fallos de dos ticks consecutivos,
        # seguidos de dos ticks sanos. Incluye U18-sol-4 y CAS sin ACK legible.
        for primero, segundo in product(opciones, repeat=2):
            with self.subTest(primero=primero, segundo=segundo):
                c = ContextoR3()
                c.setUp()
                try:
                    c.preparar()
                    c.modo('seguir')
                    with c.instrumentar():
                        for fallos in (primero, segundo, (False, False, False),
                                       (False, False, False)):
                            c.tick_comprobado(fallos)
                        # Drena los ACK/avisos restantes sin decisiones humanas;
                        # cada tick adicional comprueba el mismo invariante.
                        c.recuperar()
                finally:
                    c.doCleanups()
                    cantidad += 1
        self.assertEqual(cantidad, 64)
        print('U18 R3 invariante: 64 combinaciones, cuatro ticks por combinación '
              'y recuperación comprobada por tick', flush=True)
