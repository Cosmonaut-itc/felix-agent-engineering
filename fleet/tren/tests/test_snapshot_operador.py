"""SEN-7032: Git real, lanzadores de prueba y tick activo."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_activo as activo
from test_jale import PRODUCTOR
import test_juicio as juicio_fixtures
from adaptadores import seleccionar
from dominio import Certificado, EsperandoCI, Pausa, Promoviendo, Terminal, clave


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
        self.assertTrue(pausa.detalle.startswith('checkout-operador:'), pausa)
        self.assertFalse(self.lineas('versiones.log'))
        self.assertFalse(self.lineas('gancho.log'))

    def test_objeto_inexistente_pausa_y_reanuda(self):
        self.hasta_fase(EsperandoCI)
        lectura = self.contexto.git.leer_ref
        with patch.object(self.contexto.git, 'leer_ref',
                          side_effect=lambda ref: 'f' * 40 if ref == 'refs/heads/main' else lectura(ref)):
            pausa = self.hasta_pausa().pausa
        self.assertTrue(pausa.detalle.startswith('checkout-operador:'), pausa)
        self.assertFalse(self.solicitudes())
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
            self.contexto.tick(3000 + n)
            if self.lineas('gancho.log'):
                break
        self.assertEqual(self.lineas('gancho.log'), ['verificar nuevo'])
        con_otra_base = self.version('tercero', self.nuevo)
        self.push(con_otra_base, 'refs/heads/main')
        self.contexto.tick(4000)
        self.assertIn('cancelar nuevo', self.lineas('gancho.log'))
        self.assertFalse(any('tercero' in l for l in self.lineas('gancho.log')))

    def test_sin_checkout_operador(self):
        self.config.pop('checkout_operador')
        self.hasta_fase(Certificado)
        self.assertEqual(Path(self.argv()[1]).parent, self.operador)
        self.assertEqual(self.lineas('versiones.log'), ['viejo viejo'])
