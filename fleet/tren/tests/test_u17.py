"""U17: almacenes separados, dos lotes y lectura activa sin sincronizador.

Sólo host remoto. El checkout humano se clona una vez antes de los ticks;
no se sincroniza después ni comparte objetos/alternates con otro almacén.
"""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_activo as activo
import test_u16 as u16
from dominio import Admitido, Armado, Certificado, Congelado, Retenido, Terminal, datos
from observador import GitHubGit

REPO = activo.REPO


class ContextoLectura(u16.ContextoCI):
    def setUp(self):
        activo.ActivoTests.setUp(self)
        # Publicar ramas iniciales permite que el único clone conozca B y H.
        # --no-local evita hardlinks; ningún fetch del fixture durante los ticks.
        for pr, head in self.miembros:
            self.push(head, 'refs/heads/ticket-{}'.format(pr))
        self.humano = (self.raiz / 'admision').resolve()
        self.comando(self.raiz, 'clone', '--no-local', str(self.origen), str(self.humano))
        self.contexto.config['git'] = str(self.humano)
        self.config = self.raiz / 'config.json'
        self.config.write_text(json.dumps({'repos': {REPO: self.contexto.config}}))
        # Un checkout humano con rama, índice y cambios que debemos conservar.
        self.comando(self.humano, 'switch', '-c', 'trabajo-humano')
        (self.humano / 'comun').write_text('cambio en indice\n')
        self.comando(self.humano, 'add', 'comun')
        (self.humano / 'comun').write_text('cambio sin guardar\n')
        (self.humano / 'nota').write_text('no versionado\n')
        self.antes = self.snapshot()
        self.assertEqual(len({self.humano, self.origen.resolve(),
                              self.contexto.git.ruta.resolve()}), 3)
        self.assertFalse((self.humano / '.git/objects/info/alternates').exists())
        self.ahora = 100

    def snapshot(self):
        return {str(p.relative_to(self.humano)): p.read_bytes()
                for p in self.humano.rglob('*') if p.is_file()}

    def gh(self, argv, **kwargs):
        # CI verde para el C publicado, con la ref exacta; sólo simular GitHub.
        endpoint = argv[4]
        if '/check-suites?' in endpoint or '/check-runs?' in endpoint:
            head = endpoint.split('/')[4]
            ramas = self.comando(self.origen, 'for-each-ref', '--points-at', head,
                '--format=%(refname:short)', 'refs/heads/tren/').splitlines()
            valor = [{'check_suites': [{'id': n, 'head_sha': head, 'head_branch': rama}
                                       for n, rama in enumerate(ramas, 1)]}]
            if '/check-runs?' in endpoint:
                valor = [{'check_runs': [{'id': 1, 'name': 'gate', 'head_sha': head,
                    'check_suite': {'id': 1}, 'status': 'completed', 'conclusion': 'success'}]}]
            return subprocess.CompletedProcess(argv, 0, json.dumps(valor), '')
        return self.contexto.gh(argv, **kwargs)

    def tick_real(self):
        args = activo.cli.parser().parse_args(['tick', '--repo', REPO,
                                              '--config', str(self.config)])
        # Ejercitar la composición productiva: sin inyectar git ni observador.
        def observador(config, **kwargs):
            return GitHubGit(config, ejecutar=self.gh, **kwargs)
        with patch.object(activo.cli, 'GitHubGit', observador):
            codigo, resultado = activo.cli.ejecutar(args, ahora=self.ahora,
                hasito=self.contexto.hasito, escritor=self.contexto.escritor)
        self.ahora += 120
        self.assertEqual(codigo, 0, resultado)
        self.assertEqual(self.snapshot(), self.antes)
        return self.estado()

    def avanzar(self, condicion, limite=50):
        for _ in range(limite):
            estado = self.tick_real()
            self.assertIsNone(estado.pausa, datos(estado.pausa))
            if condicion(estado):
                return estado
        self.fail('no convergió: ' + str(datos(estado)))

    def sin_objeto_humano(self, sha):
        resultado = subprocess.run(['git', '-C', str(self.humano), 'cat-file', '-e', sha],
                                   capture_output=True, text=True, timeout=30)
        self.assertNotEqual(resultado.returncode, 0)

    def habilitar(self, pr):
        head = dict(self.miembros)[pr]
        (self.raiz / 'apto-{}.json'.format(pr)).write_text(json.dumps({
            'head': head, 'veredicto': 'APTO', 'hallazgos_abiertos': 0}))


class RegresionesTests(ContextoLectura):
    def test_dos_lotes_sucesivos_sin_sincronizar_checkout(self):
        for pr in (2, 3):
            (self.raiz / 'apto-{}.json'.format(pr)).write_text('{}')
        self.tick_real()
        self.ahora += 3600
        primero = self.avanzar(lambda e: e.trenes and e.trenes[0].fase == Terminal('ATERRIZADO'))
        C1 = self.remoto()
        self.assertNotEqual(C1, self.B)
        self.sin_objeto_humano(C1)
        self.assertEqual(primero.trenes[0].recibos, (primero.entradas[0].entrega.recibo,))
        for pr in (2, 3):
            self.habilitar(pr)
        observado = self.tick_real()
        self.assertTrue(all(isinstance(e.admision, Admitido) for e in observado.entradas[1:]),
                        datos(observado.entradas))
        self.ahora += 3600
        armado = self.avanzar(lambda e: len(e.trenes) == 2 and isinstance(e.trenes[1].fase, Armado))
        self.assertEqual(armado.trenes[1].fase.candidato.B, C1)
        final = self.avanzar(lambda e: len(e.trenes) == 2 and e.trenes[1].fase == Terminal('ATERRIZADO'))
        self.assertEqual([t.fase for t in final.trenes], [Terminal('ATERRIZADO')] * 2)
        self.assertEqual(len(self.lineas('trabajos.log')), 2)
        self.comando(self.origen, 'merge-base', '--is-ancestor', C1, self.remoto())
        self.sin_objeto_humano(self.remoto())
        with self.ledger() as ledger:
            self.assertEqual(ledger.outbox(REPO), [])

    def test_main_externo_pausa_reanudar_y_aterrizar_sin_sincronizador(self):
        self.avanzar(lambda e: e.trenes and isinstance(e.trenes[0].fase, Certificado))
        # Crear fuera del checkout de admisión y fuera del bare del tren.
        ajeno = self.commit({'comun': 'base\n', 'externo': 'cambio\n'}, (self.B,))
        self.push(ajeno, 'refs/heads/main')
        self.sin_objeto_humano(ajeno)
        for _ in range(10):
            if self.tick_real().pausa:
                break
        self.assertEqual(self.estado().pausa.motivo, 'escritor-externo')
        self.juicio('reanudar')
        self.tick_real()
        rearmado = self.tick_real()
        self.assertTrue(all(isinstance(e.admision, Admitido) for e in rearmado.entradas),
                        datos(rearmado.entradas))
        self.assertEqual(rearmado.trenes[-1].fase, Congelado(ajeno))
        self.avanzar(lambda e: e.trenes[-1].fase == Terminal('ATERRIZADO'))
        self.comando(self.origen, 'merge-base', '--is-ancestor', ajeno, self.remoto())
        self.sin_objeto_humano(ajeno)


class ObjetosTests(ContextoLectura):
    def test_fetch_fallido_retiene_sin_feedback_rojo_y_reintenta(self):
        original = subprocess.run
        for objetivo in (self.B, 'refs/pull/2/head'):
            with self.subTest(objetivo=objetivo):
                intentos = []
                def transporte(argv, **kwargs):
                    if argv[0] == 'git' and 'fetch' in argv and argv[-1] == objetivo:
                        intentos.append(argv)
                        raise subprocess.TimeoutExpired(argv, 1, stderr='dato privado')
                    return original(argv, **kwargs)
                with patch('subprocess.run', transporte):
                    estado = self.tick_real()
                self.assertTrue(intentos)
                entrada = estado.entradas[0 if objetivo == self.B else 1]
                self.assertEqual(entrada.admision, Retenido('observacion-incompleta'))
                self.assertEqual(estado.trenes, ())
                self.assertIsNone(estado.pausa)
                self.assertEqual(self.posts(), [])
                self.assertEqual(self.lineas('trabajos.log'), [])
        self.avanzar(lambda e: e.trenes and e.trenes[0].fase == Terminal('ATERRIZADO'))
        self.assertFalse(any(p['payload'].get('state') == 'failure' for p in self.posts()))

    def test_head_solo_en_ref_pr_y_carrera_de_fetch_retienen(self):
        pr, viejo = self.miembros[0]
        nuevo = self.commit({'comun': 'base\n', 'nuevo': 'ticket\n'}, (viejo,))
        self.push(nuevo, 'refs/pull/1/head')
        self.sin_objeto_humano(nuevo)
        # API antigua frente a ref nueva: jamás admitir usando otro H.
        estado = self.tick_real()
        self.assertEqual(estado.entradas[0].admision, Retenido('observacion-incompleta'))
        self.assertEqual(estado.trenes, ())
        # La nueva entrega ve un head accesible sólo por refs/pull, como un fork.
        self.contexto.config['miembros'][0][1] = nuevo
        self.miembros[0] = (pr, nuevo)
        self.habilitar(pr)
        dirigida = self.raiz / 'dirigida-1.json'
        dirigida.write_text(json.dumps({'head': nuevo, 'resultado': 'limpio'}))
        from dominio import Entrega
        with self.ledger() as ledger:
            ledger.entregar(Entrega(REPO, pr, nuevo, str(self.raiz / 'apto-1.json'),
                                    str(dirigida)), self.ahora)
        estado = self.tick_real()
        self.assertIsInstance(estado.entradas[-1].admision, Admitido)
        self.sin_objeto_humano(nuevo)


class SombraTests(u16.SombraTests):
    def test_eventos_de_sombra_no_cambian(self):
        with patch('git_ops.GitTren.traer', side_effect=AssertionError('fetch en sombra')):
            super().test_eventos_de_sombra_no_cambian()
            self.inscritos.write_text(REPO + ' sombra\n')
            head = self.commit({'comun': 'base\n', 'pr-4': 'ticket\n'}, (self.B,))
            self.push(head, 'refs/pull/4/head')
            self.contexto.config['miembros'].append(['4', head])
            apto, dirigida = self.raiz / 'apto-4.json', self.raiz / 'dirigida-4.json'
            apto.write_text(json.dumps({'head': head, 'veredicto': 'APTO', 'hallazgos_abiertos': 0}))
            dirigida.write_text(json.dumps({'head': head, 'resultado': 'limpio'}))
            def gh(argv, **kwargs):
                if argv[0] == 'git' and 'fetch' in argv:
                    raise AssertionError('fetch en checkout de sombra')
                return self.contexto.gh(argv, **kwargs)
            lector = GitHubGit(self.contexto.config, ejecutar=gh, sombra=True)
            args = activo.cli.parser().parse_args(['entregar', '--repo', REPO, '--pr', '4',
                '--head', head, '--apto', str(apto), '--dirigida', str(dirigida)])
            self.assertEqual(activo.cli.ejecutar(args, observador=lector, ahora=7500)[0], 0)
            args = activo.cli.parser().parse_args(['tick', '--repo', REPO, '--seco'])
            self.assertEqual(activo.cli.ejecutar(args, observador=lector, ahora=7600)[0], 0)
        with self.ledger() as ledger:
            eventos = [(r['tipo'], r['instante'], json.loads(r['payload'])) for r in ledger.db.execute(
                "SELECT tipo, instante, payload FROM events WHERE tipo IN "
                "('sombra', 'sombra-doble-camino', 'admision-entrega') ORDER BY seq")]
        self.assertEqual({t for t, _, _ in eventos},
                         {'sombra', 'sombra-doble-camino', 'admision-entrega'})
        destino = os.environ.get('PAO_TREN_U17_SOMBRA')
        if destino:
            Path(destino).write_text(json.dumps(eventos, sort_keys=True).replace(str(self.raiz), '<raiz>'))


if __name__ == '__main__':
    unittest.main()
