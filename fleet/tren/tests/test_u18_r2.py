"""U18 R2: recuperación por tick sin decisiones, con CLI y origen git reales."""
from test_u18_r1 import ContextoR1
from dominio import Incierto, Terminal
from git_ops import FalloGit


class ContextoR2(ContextoR1):
    def sin_decisiones(self):
        self.assertEqual(self.cli('estado', '--juicio')[1]['juicio']['decisiones'], [])

    def lectura_transitoria(self, gancho=True):
        if not gancho:
            self.config.pop('pre_aterrizaje')
        self.preparar()
        self.modo('seguir')
        original = self.contexto.git.reconciliar
        self.incierto = True
        def reconciliar(B, C, arbol, heads):
            if self.incierto and self.remoto() != self.B:
                return Incierto(B, C, 'lectura-remota-fallida')
            return original(B, C, arbol, heads)
        self.contexto.git.reconciliar = reconciliar
        self.hasta(lambda: self.pushes_main() == 1)
        for _ in range(2):
            self.tick()
            self.assertIsNone(self.estado().pausa)
            self.assertEqual(self.pushes_main(), 1)
            self.assertFalse(self.llamadas('cancelar'))
            self.assertEqual([p['payload']['state'] for p in self.posts('/statuses/')],
                             ['pending'] * 3)
            if gancho:
                registro, pendiente = self.diario()[0]
                self.assertEqual(pendiente, 1)
                self.assertTrue(registro['cancelacion_requerida'])
                self.assertEqual(len(self.cli('estado')[1]['cancelaciones_pre_aterrizaje']), 1)
        self.incierto = False
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'), limite=6)
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.pushes_main(), 1)
        self.assertFalse(self.llamadas('cancelar'))
        self.assertEqual([p['payload']['state'] for p in self.posts('/statuses/')],
                         ['pending'] * 3 + ['success'] * 3)
        self.assertEqual(len(self.llamadas('verificar')), int(gancho))
        if gancho:
            self.assertEqual(self.diario()[0][1], 0)
        self.sin_decisiones()


class RegresionesTests(ContextoR2):
    def test_lectura_incierta_tras_push_recupera_exito_sin_cancelar_ni_decision(self):
        self.lectura_transitoria()

    def test_push_fallido_y_cancelar_fallido_una_vez_reverifica_y_aterriza_solo(self):
        self.preparar()
        self.modo('seguir')
        # El proceso falso falla exactamente la primera cancelación.
        self.guion.write_text(self.guion.read_text().replace(
            '        sys.exit(2)',
            "        (r / 'cancelacion-falla').rename(r / 'cancelacion-recuperada')\n"
            '        sys.exit(2)'))
        (self.raiz / 'cancelacion-falla').write_text('una vez')
        original = self.contexto.git.git
        intentos = []
        def git(*args, **kwargs):
            if args[0] == 'push' and args[-1].endswith(':refs/heads/main'):
                intentos.append(args[-1])
                if len(intentos) == 1:
                    raise FalloGit('transporte-push')
            return original(*args, **kwargs)
        self.contexto.git.git = git
        self.hasta(lambda: len(self.llamadas('cancelar')) == 1)
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(self.pushes_main(), 0)
        self.assertEqual(self.diario()[0][1], 1)
        self.assertTrue(self.diario()[0][0]['cancelacion_requerida'])
        self.assertEqual(len(self.llamadas('verificar')), 1)
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'), limite=6)
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.pushes_main(), 1)
        self.assertEqual(len(intentos), 2)
        self.assertEqual(len(self.llamadas('cancelar')), 2)
        self.assertEqual(len(self.llamadas('verificar')), 2)
        self.assertEqual(len({s['id'] for s in self.llamadas()}), 1)
        self.assertEqual(self.diario()[0][1], 0)
        self.assertEqual([p['payload']['state'] for p in self.posts('/statuses/')],
                         ['pending'] * 3 + ['success'] * 3)
        self.sin_decisiones()


class ControlTests(ContextoR2):
    def test_misma_lectura_incierta_sin_gancho_recupera_exito_solo(self):
        self.lectura_transitoria(gancho=False)


class LimitesTests(ContextoR2):
    def test_lectura_incierta_despues_del_gancho_retiene_y_recupera_sin_pausa(self):
        self.preparar()
        self.modo('seguir')
        original = self.contexto.git.reconciliar
        incierto = [False]
        def reconciliar(B, C, arbol, heads):
            if incierto[0]:
                return Incierto(B, C, 'lectura-remota-fallida')
            return original(B, C, arbol, heads)
        self.contexto.git.reconciliar = reconciliar
        def frontera(nombre, efecto):
            if nombre == 'antes-pre-aterrizaje':
                incierto[0] = True
        self.tick(frontera)
        for _ in range(2):
            self.tick()
            self.assertIsNone(self.estado().pausa)
            self.assertEqual(self.pushes_main(), 0)
            self.assertEqual(self.remoto(), self.B)
            self.assertEqual(self.diario()[0][1], 1)
            self.assertFalse(self.llamadas('cancelar'))
            self.assertEqual(len(self.llamadas('verificar')), 1)
        incierto[0] = False
        self.hasta(lambda: self.estado().trenes[0].fase == Terminal('ATERRIZADO'))
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(self.pushes_main(), 1)
        self.assertEqual(len(self.llamadas('cancelar')), 1)
        self.assertEqual(len(self.llamadas('verificar')), 2)
        self.sin_decisiones()
