"""Regresiones de la síntesis U2; también se ejecutan sobre la base elegida."""
from dataclasses import replace
import subprocess
import unittest

import test_aterrizaje as fixtures
from dominio import (Armado, Certificado, Congelado, Incierto, Motivo, Pausa,
                     Promoviendo, Rechazado, Retenido, Terminal, paso)
from efectos import ejecutar
from git_ops import ErrorGit, GitTren


def observacion_de(resultado):
    # Permite reproducir las regresiones con el contrato anterior del ejecutor.
    return getattr(resultado, "observacion", resultado)


class InjertoTests(fixtures.AterrizajeTests):
    def certificado(self):
        estado, candidato, obs = self.armar()
        cert = Certificado(candidato.C, candidato.arbol, "suite", "ci", candidato.B)
        return replace(estado, trenes=(replace(estado.trenes[0], fase=cert),)), obs

    def test_certificado_head_cambiado_caduca_sin_promover(self):
        estado, obs = self.certificado()
        vistas = (replace(obs.prs[0], head="f" * 40),) + obs.prs[1:]
        final, efectos = paso(estado, replace(obs, prs=vistas))
        self.assertEqual(final.pausa, Pausa("head-cambiado-en-tren"))
        self.assertEqual(final.entradas[0].admision, Rechazado(Motivo.HEAD))
        self.assertEqual([e.tipo for e in efectos], ["avisar-caducidad"])
        self.assertIsInstance(final.trenes[0].fase, Certificado)
        self.assertEqual(self.remoto(), self.B)

    def test_certificado_evidencia_no_vigente_retiene_sin_promover(self):
        estado, obs = self.certificado()
        for cambio, motivo in (({"apto": False}, Motivo.APTO),
                               ({"dirigida": False}, Motivo.DIRIGIDA),
                               ({"ci": "pendiente"}, Motivo.CI),
                               ({"completa": False}, Motivo.INCIERTO)):
            with self.subTest(cambio=cambio):
                vistas = (replace(obs.prs[0], **cambio),) + obs.prs[1:]
                final, efectos = paso(estado, replace(obs, prs=vistas))
                self.assertIsInstance(final.trenes[0].fase, Certificado)
                self.assertEqual(final.entradas[0].admision, Retenido(motivo))
                self.assertEqual(efectos, ())

    def test_candidato_alcanzable_y_fetch_sin_mantenimiento(self):
        llamadas = []
        def registrar(args, **kwargs):
            llamadas.append(args)
            return subprocess.run(args, **kwargs)
        self.git.ejecutar = registrar
        candidato = self.git.armar(self.B, self.miembros)
        refs = self.comando(self.git.ruta, "for-each-ref", "--format=%(objectname)").splitlines()
        self.assertIn(candidato.C, refs)
        self.assertEqual(self.comando(self.git.ruta, "rev-parse",
                         "refs/tren/candidatos/" + candidato.C), candidato.C)
        for args in llamadas:
            if "fetch" in args:
                self.assertIn("--no-auto-maintenance", args)

    def test_rearmar_misma_base_nueva_generacion_arma_y_aterriza(self):
        estado, candidato, obs = self.armar()
        primeros_recibos = estado.trenes[0].recibos
        fechas = tuple((e.recibida, e.admitida) for e in estado.entradas)
        claves = set()
        # Dos terminales sobre B: cada generación necesita claves nuevas.
        for generacion in range(2):
            cert = Certificado(candidato.C, candidato.arbol, "suite", "ci", self.B)
            estado = replace(estado, trenes=estado.trenes[:-1] +
                             (replace(estado.trenes[-1], fase=cert),))
            estado, efectos = paso(estado, obs)
            ajeno = self.commit({"comun": "base\n", "ajeno": str(generacion)}, (self.B,))
            self.push(ajeno, "refs/heads/main")
            llegada = observacion_de(ejecutar(efectos[0], estado, obs, self.git))
            estado, _ = paso(estado, llegada)
            self.assertEqual(estado.trenes[-1].fase, Terminal("REARMAR"))
            self.assertEqual(estado.pausa, Pausa("escritor-externo", main=ajeno))
            self.comando(self.origen, "update-ref", "refs/heads/main", self.B, ajeno)
            entrada = replace(estado, pausa=None)
            lectura = replace(obs, candidato=None, main=None, tren_id="", intento_promocion=0)
            rearmado, efectos = paso(entrada, lectura)
            self.assertEqual(paso(entrada, lectura), (rearmado, efectos))
            self.assertNotIn(rearmado.trenes[-1].id, [t.id for t in estado.trenes])
            self.assertNotIn(efectos[0].clave, claves)
            claves.add(efectos[0].clave)
            self.assertEqual(rearmado.trenes[-1].fase, Congelado(self.B))
            self.assertEqual(rearmado.trenes[-1].recibos, primeros_recibos)
            self.assertEqual(tuple((e.recibida, e.admitida) for e in rearmado.entradas), fechas)
            obs = observacion_de(ejecutar(efectos[0], rearmado, lectura, self.git))
            estado, _ = paso(rearmado, obs)
            self.assertIsInstance(estado.trenes[-1].fase, Armado)
            candidato = obs.candidato
        cert = Certificado(candidato.C, candidato.arbol, "suite", "ci", self.B)
        estado = replace(estado, trenes=estado.trenes[:-1] +
                         (replace(estado.trenes[-1], fase=cert),))
        estado, efectos = paso(estado, obs)
        final, _ = paso(estado, observacion_de(ejecutar(efectos[0], estado, obs, self.git)))
        self.assertEqual(final.trenes[-1].fase, Terminal("ATERRIZADO"))
        self.assertEqual(self.remoto(), candidato.C)

    def test_rama_ci_ocupada_pausa_ligada_al_tren(self):
        estado, candidato, obs = self.armar()
        esperando, efectos = paso(estado, obs)
        ref = "refs/heads/tren/" + esperando.trenes[0].id
        self.push(self.B, ref)
        llegada = observacion_de(ejecutar(efectos[0], esperando, obs, self.git))
        ajena = replace(llegada, tren_id="otro-tren")
        self.assertEqual(paso(esperando, ajena), (esperando, ()))
        final, efectos = paso(esperando, llegada)
        self.assertEqual(final.pausa, Pausa("rama-tren-ocupada"))
        self.assertEqual(efectos, ())
        self.assertEqual(self.remoto(ref), self.B)
        self.assertEqual(self.remoto(), self.B)

    def test_ejecucion_incierta_pendiente_recupera_y_aterriza(self):
        estado, candidato, obs = self.armar()
        estado, efecto = self.certificar(estado, candidato, obs)
        def sin_lectura(args, **kwargs):
            if "ls-remote" in args:
                raise subprocess.TimeoutExpired(args, 120)
            return subprocess.run(args, **kwargs)
        self.git.ejecutar = sin_lectura
        incierta = ejecutar(efecto, estado, obs, self.git)
        self.assertFalse(incierta.completo)
        self.assertIsInstance(incierta.observacion.main, Incierto)
        self.assertEqual(paso(estado, incierta.observacion), (estado, ()))
        self.assertEqual(self.remoto(), candidato.C)
        self.git.ejecutar = subprocess.run
        recuperada = ejecutar(efecto, estado, obs, self.git)
        self.assertTrue(recuperada.completo)
        final, efectos = paso(estado, recuperada.observacion)
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
        self.assert_status_aterrizaje(final, efectos)

    def test_bare_config_local_ajena_no_arma(self):
        self.git.preparar()
        self.comando(self.git.ruta, "config", "merge.default", "union")
        self.assertEqual(self.git.armar(self.B, self.miembros),
                         ErrorGit("config-local-no-permitida"))
        self.assertEqual(self.remoto(), self.B)

    def test_bare_info_attributes_no_arma(self):
        self.git.preparar()
        info = self.git.ruta / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("* merge=union\n")
        self.assertEqual(self.git.armar(self.B, self.miembros),
                         ErrorGit("atributos-locales-no-permitidos"))
        self.assertEqual(self.remoto(), self.B)


def load_tests(loader, tests, pattern):
    # Reusar las fixtures, sin duplicar los tests heredados en discovery.
    return unittest.TestSuite(InjertoTests(nombre) for nombre in sorted(InjertoTests.__dict__)
                              if nombre.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
