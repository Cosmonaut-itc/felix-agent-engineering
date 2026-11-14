from __future__ import annotations

from contextlib import redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from unittest.mock import patch

from test_vigilante import Fixture, ROOT, SOURCE

spec = importlib.util.spec_from_file_location('instalar_u11', SOURCE / 'instalar.py')
i = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = i
spec.loader.exec_module(i)


class InstalarTests(Fixture):
    def deploy(self, accion='instalar', host='desk', modo='informe', seco=False):
        argv = [accion, '--host', host]
        if modo is not None:
            argv.extend(['--modo', modo])
        if seco:
            argv.append('--seco')
        # Entrar por la función del instalador mantiene la RED en NotImplementedError.
        # El ssh falso ejecuta la carga completa con el mismo intérprete y -I.
        with patch.dict(os.environ, FAKE_HOST=host):
            return i.main(argv)

    def arbol(self):
        return {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*')
                if p.is_file() and p.name != 'calls'}

    def rutas(self, host):
        return i.destinos(host, self.home)

    def test_seco_no_escribe_y_muestra_censo(self):
        for host in ('mini', 'desk'):
            antes = self.arbol()
            with patch.object(i.subprocess, 'run', wraps=subprocess.run) as transporte:
                self.assertEqual(self.deploy(host=host, seco=True), 0)
            llamada = transporte.call_args
            self.assertEqual(llamada.args[0], ['ssh', '-o', 'BatchMode=yes', '-o', 'ControlPath=none',
                                              host, '/usr/bin/python3', '-I', '-'])
            # Inspeccionar además la salida con un segundo transporte falso privado.
            resultado = subprocess.run(llamada.args[0], input=llamada.kwargs['input'], text=True,
                                       capture_output=True)
            self.assertEqual(resultado.returncode, 0, resultado.stderr)
            self.assertIn('censo:', resultado.stdout)
            self.assertIn('"seco": true', resultado.stdout)
            self.assertEqual(self.arbol(), antes)
        self.assertFalse(any(c[1] in ('bootstrap', 'bootout', 'enable', 'disable', 'stop')
                             for c in self.calls() if c[0] in ('systemctl', 'launchctl')))

    def test_instala_renderiza_respalda_y_es_idempotente(self):
        for host in ('mini', 'desk'):
            self.assertEqual(self.deploy(host=host), 0)
            rutas = self.rutas(host)
            self.assertTrue(all(p.exists() for p in rutas.values()))
            anterior = {p: p.read_bytes() for p in rutas.values()}
            previos = {p: set(p.parent.glob(p.name + '.bak-*')) for p in rutas.values()}
            self.assertEqual(self.deploy(host=host, modo='cortar'), 0)
            for p, datos in anterior.items():
                respaldos = list(set(p.parent.glob(p.name + '.bak-*')) - previos[p])
                self.assertEqual(len(respaldos), 1)
                self.assertEqual(respaldos[0].read_bytes(), datos)
            for nombre, p in rutas.items():
                if nombre == 'vigilante.py':
                    continue
                texto = p.read_text()
                self.assertNotIn('@HOME@', texto)
                self.assertNotIn('@MODO@', texto)
                if nombre.endswith('.plist'):
                    unidad = plistlib.loads(p.read_bytes())
                    self.assertEqual(unidad['StartInterval'], 60)
                    self.assertNotIn('KeepAlive', unidad)
                    self.assertEqual(unidad['ProgramArguments'][-1], 'cortar')
                elif nombre.endswith('.service'):
                    self.assertIn('--modo cortar', texto)
                    self.assertIn(str(self.home), texto)
                    self.assertIn('TimeoutStartSec=300', texto)
                    self.assertNotIn('Requires=', texto)
            antes = self.arbol()
            self.assertEqual(self.deploy(host=host, modo='cortar'), 0)
            self.assertEqual(self.arbol(), antes)
        gestores = [c for c in self.calls() if c[0] in ('launchctl', 'systemctl')]
        self.assertTrue(any('bootstrap' in c for c in gestores))
        self.assertTrue(any('enable' in c and i.TIMER in c for c in gestores))
        self.assertFalse(any(any('guard' in a for a in c) for c in gestores))
        self.assertFalse(any(c[0] == 'pueue' and c[1] != 'status' for c in self.calls()))

    def test_sin_modo_sale_con_2(self):
        antes = self.arbol()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            i.main(['instalar', '--host', 'desk'])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.arbol(), antes)

    def test_retirar_renombra_y_conserva_estado(self):
        for host in ('mini', 'desk'):
            self.assertEqual(self.deploy(host=host), 0)
            self.h.estado.joinpath('vigilante.log').write_text('bitácora anterior\n')
            pulso = self.h.estado.joinpath('pulso.json').read_bytes()
            previos = {p: set(p.parent.glob(p.name + '.retirado-*')) for p in self.rutas(host).values()}
            self.assertEqual(self.deploy('retirar', host, modo=None), 0)
            for p in self.rutas(host).values():
                self.assertFalse(p.exists())
                self.assertEqual(len(set(p.parent.glob(p.name + '.retirado-*')) - previos[p]), 1)
            self.assertEqual(self.h.estado.joinpath('pulso.json').read_bytes(), pulso)
            self.assertEqual(self.h.estado.joinpath('vigilante.log').read_text(), 'bitácora anterior\n')
            self.assertTrue(self.h.estado.joinpath('pasada.lock').exists())
            antes = self.arbol()
            self.assertEqual(self.deploy('retirar', host, modo=None), 0)
            self.assertEqual(self.arbol(), antes)

    def test_volver_restaura_juego_mas_reciente(self):
        for host in ('mini', 'desk'):
            self.assertEqual(self.deploy(host=host, modo='informe'), 0)
            self.assertEqual(self.deploy(host=host, modo='cortar'), 0)
            esperado = {p: p.read_bytes() for p in self.rutas(host).values()}
            self.assertEqual(self.deploy(host=host, modo='informe'), 0)
            reemplazado = {p: p.read_bytes() for p in esperado}
            self.assertEqual(self.deploy('volver', host, modo=None), 0)
            self.assertEqual({p: p.read_bytes() for p in esperado}, esperado)
            for p, datos in reemplazado.items():
                if datos != esperado[p]:
                    copias = list(p.parent.glob(p.name + '.antes-volver-*'))
                    self.assertEqual([c.read_bytes() for c in copias], [datos])

    def test_previa_falla_no_escribe(self):
        for fallo in ('status-falla', 'json-invalido', 'gestor-falla'):
            ruta = self.home / fallo
            ruta.touch()
            antes = self.arbol()
            for host in ('mini', 'desk'):
                self.assertNotEqual(self.deploy(host=host), 0)
                self.assertEqual(self.arbol(), antes)
            ruta.unlink()
        self.assertFalse(any('bootstrap' in c or 'enable' in c for c in self.calls()))

    def test_ilegible_impide_instalar(self):
        mala = self.tarea()
        del mala['group']
        self.escribir([mala])
        antes = self.arbol()
        self.assertNotEqual(self.deploy(), 0)
        self.assertEqual(self.arbol(), antes)

    def test_volver_rechaza_juego_incompleto(self):
        rutas = self.rutas('desk')
        codigo = rutas['vigilante.py']
        codigo.parent.mkdir(parents=True)
        codigo.with_name(codigo.name + '.bak-20990101-000000').write_text('incompleto')
        antes = self.arbol()
        self.assertNotEqual(self.deploy('volver', modo=None), 0)
        self.assertEqual(self.arbol(), antes)
