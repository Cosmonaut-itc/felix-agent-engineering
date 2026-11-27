"""Regresiones R1: readmisión entre intentos y resultados de IO retenidos."""
from dataclasses import replace
import subprocess
import unittest

import test_aterrizaje as fixtures
from dominio import (Armado, Congelado, EnBase, EsperandoCI, Incierto,
                     LIMITE_PROMOCION, Motivo, Pausa, Rechazado,
                     Retenido, Terminal, datos, desde_datos, efecto_promover, paso)
from efectos import ejecutar


class RevisionR1Tests(fixtures.AterrizajeTests):
    def primer_push_fallido(self):
        estado, candidato, obs = self.armar()
        estado, efecto = self.certificar(estado, candidato, obs)
        llamadas = []

        def sin_enviar(args, **kwargs):
            llamadas.append(args)
            if "push" in args:
                return subprocess.CompletedProcess(args, 1,
                    f"!\t{args[-1]}\t[rejected] (stale info)\n", "")
            return subprocess.run(args, **kwargs)

        self.git.ejecutar = sin_enviar
        resultado = ejecutar(efecto, estado, obs, self.git)
        self.git.ejecutar = subprocess.run
        self.assertTrue(any("push" in args for args in llamadas))
        self.assertTrue(resultado.completo)
        self.assertEqual(resultado.observacion.main, EnBase(self.B, candidato.C))
        self.assertEqual(self.remoto(), self.B)
        return estado, candidato, resultado.observacion

    def caduca_entre_intentos(self, cambio, motivo):
        estado, candidato, obs = self.primer_push_fallido()
        if "head" in cambio:
            self.push(cambio["head"], "refs/pull/1/head")
        vistas = (replace(obs.prs[0], **cambio),) + obs.prs[1:]
        final, efectos = paso(estado, replace(obs, prs=vistas))
        self.assertEqual(final.pausa, Pausa("head-cambiado-en-tren"))
        self.assertEqual(final.entradas[0].admision, Rechazado(motivo))
        self.assertEqual([e.tipo for e in efectos], ["avisar-caducidad"])
        self.assertEqual(final.trenes[0].fase.intentos, 1)
        self.assertEqual(self.remoto(), self.B)
        self.assertNotEqual(self.remoto(), candidato.C)
        repetido, efectos = paso(final, obs)
        self.assertEqual(repetido.entradas[0].admision, Rechazado(motivo))
        self.assertEqual(efectos, ())

    def test_reintento_head_cambiado_caduca_sin_promover(self):
        nuevo = self.commit({"comun": "base\n", "pr-1": "sin APTO\n"},
                            (self.miembros[0][1],))
        self.caduca_entre_intentos({"head": nuevo, "apto": False}, Motivo.HEAD)

    def test_reintento_pr_cerrado_caduca_sin_promover(self):
        self.caduca_entre_intentos({"abierto": False}, Motivo.CERRADO)

    def retener_y_recuperar(self, motivo, cambio):
        estado, candidato, obs = self.primer_push_fallido()
        retenida = replace(obs, prs=(replace(obs.prs[0], **cambio),) + obs.prs[1:])
        estado, efectos = paso(estado, retenida)
        self.assertEqual(efectos, ())
        self.assertIsNone(estado.pausa)
        self.assertEqual(estado.entradas[0].admision, Retenido(motivo))
        self.assertEqual(estado.trenes[0].fase.intentos, 1)
        # Sobrevive al ledger y a lecturas frescas sin el resultado de IO anterior.
        estado = desde_datos(datos(estado))
        fresca = replace(retenida, main=None, candidato=None, tren_id="", intento_promocion=0)
        for _ in range(3):
            estado, efectos = paso(estado, fresca)
            self.assertEqual(efectos, ())
            self.assertEqual(estado.trenes[0].fase.intentos, 1)
        self.assertEqual(self.remoto(), self.B)
        sana = replace(fresca, prs=obs.prs)
        siguiente, efectos = paso(estado, sana)
        self.assertEqual(paso(estado, sana), (siguiente, efectos))
        self.assertEqual(efectos, (efecto_promover(estado.trenes[0].id, 2),))
        llegada = ejecutar(efectos[0], siguiente, sana, self.git)
        final, efectos = paso(siguiente, llegada.observacion)
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
        self.assert_status_aterrizaje(final, efectos)
        self.assertEqual(self.remoto(), candidato.C)

    def test_retencion_incompleta_recupera_sin_repetir_resultado_io(self):
        self.retener_y_recuperar(Motivo.INCIERTO, {"completa": False})

    def test_retencion_apto_recupera_sin_repetir_resultado_io(self):
        self.retener_y_recuperar(Motivo.APTO, {"apto": False})

    def test_retencion_no_reinicia_limite_de_tres_intentos(self):
        estado, candidato, obs = self.primer_push_fallido()
        claves = {efecto_promover(estado.trenes[0].id, 1).clave}
        for intento in range(1, LIMITE_PROMOCION + 1):
            retenida = replace(obs, prs=(replace(obs.prs[0], apto=False),) + obs.prs[1:])
            estado, efectos = paso(estado, retenida)
            self.assertEqual(efectos, ())
            self.assertEqual(estado.trenes[0].fase.intentos, intento)
            sana = replace(obs, main=None, tren_id="", intento_promocion=0)
            estado, efectos = paso(estado, sana)
            if intento == LIMITE_PROMOCION:
                self.assertEqual(estado.pausa, Pausa("promocion-rechazada"))
                self.assertEqual(efectos, ())
                break
            self.assertEqual(len(efectos), 1)
            self.assertNotIn(efectos[0].clave, claves)
            claves.add(efectos[0].clave)
            # Rechazo antes de enviar para los siguientes intentos también.
            def sin_enviar(args, **kwargs):
                if "push" in args:
                    return subprocess.CompletedProcess(args, 1,
                        f"!\t{args[-1]}\t[rejected] (stale info)\n", "")
                return subprocess.run(args, **kwargs)
            self.git.ejecutar = sin_enviar
            obs = ejecutar(efectos[0], estado, sana, self.git).observacion
            self.git.ejecutar = subprocess.run
        self.assertEqual(len(claves), LIMITE_PROMOCION)
        self.assertEqual(self.remoto(), self.B)

    def test_ejecutor_no_empuja_miembro_no_admitido(self):
        estado, candidato, obs = self.primer_push_fallido()
        efecto = efecto_promover(estado.trenes[0].id, 1)
        casos = ({"head": "f" * 40}, {"abierto": False}, {"apto": False},
                 {"completa": False}, {"hallazgos": 1}, {"ci": "pendiente"})
        for cambio in casos:
            with self.subTest(cambio=cambio):
                llamadas = []

                def registrar(args, **kwargs):
                    llamadas.append(args)
                    return subprocess.run(args, **kwargs)

                self.git.ejecutar = registrar
                negativa = replace(obs, prs=(replace(obs.prs[0], **cambio),) + obs.prs[1:])
                resultado = ejecutar(efecto, estado, negativa, self.git)
                self.assertFalse(any("push" in args for args in llamadas))
                self.assertTrue(any("ls-remote" in args for args in llamadas))
                self.assertEqual(resultado.observacion.main, EnBase(self.B, candidato.C))
                self.assertEqual(self.remoto(), self.B)
        self.git.ejecutar = subprocess.run
        resultado = ejecutar(efecto, estado, replace(obs, prs=()), self.git)
        self.assertEqual(self.remoto(), self.B)
        self.assertEqual(resultado.observacion.main, EnBase(self.B, candidato.C))

    def test_tick_tras_aterrizar_no_caduca_pr_merged(self):
        estado, candidato, obs = self.armar()
        estado, efecto = self.certificar(estado, candidato, obs)
        llegada = ejecutar(efecto, estado, obs, self.git)
        cerradas = tuple(replace(v, abierto=False) for v in obs.prs)
        estado, efectos = paso(estado, replace(llegada.observacion, prs=cerradas))
        self.assertEqual(estado.trenes[0].fase, Terminal("ATERRIZADO"))
        self.assert_status_aterrizaje(estado, efectos)
        admisiones = tuple(e.admision for e in estado.entradas)
        fresca = replace(obs, base=candidato.C, prs=cerradas, candidato=None,
                         tren_id="", intento_promocion=0, main=None)
        siguiente, efectos = paso(estado, fresca)
        self.assertEqual(efectos, ())
        self.assertIsNone(siguiente.pausa)
        self.assertEqual(tuple(e.admision for e in siguiente.entradas), admisiones)

    def test_ejecutor_pendiente_reconcilia_ack_perdido_con_pr_cerrados(self):
        estado, candidato, obs = self.armar()
        estado, efecto = self.certificar(estado, candidato, obs)

        def sin_lectura(args, **kwargs):
            if "ls-remote" in args:
                raise subprocess.TimeoutExpired(args, 120)
            return subprocess.run(args, **kwargs)

        self.git.ejecutar = sin_lectura
        pendiente = ejecutar(efecto, estado, obs, self.git)
        self.assertFalse(pendiente.completo)
        self.assertIsInstance(pendiente.observacion.main, Incierto)
        self.assertEqual(self.remoto(), candidato.C)
        llamadas = []

        def registrar(args, **kwargs):
            llamadas.append(args)
            return subprocess.run(args, **kwargs)

        self.git.ejecutar = registrar
        cerrada = replace(obs, prs=tuple(replace(v, abierto=False) for v in obs.prs))
        llegada = ejecutar(efecto, estado, cerrada, self.git)
        self.assertTrue(llegada.completo)
        self.assertFalse(any("push" in args for args in llamadas))
        final, efectos = paso(estado, llegada.observacion)
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))
        self.assertIsNone(final.pausa)
        self.assert_status_aterrizaje(final, efectos)

    def test_candidato_con_lectura_incompleta_se_conserva_y_solicita_ci(self):
        estado, efecto, obs = self.congelar()
        incompleta = replace(obs, prs=(replace(obs.prs[0], completa=False),) + obs.prs[1:])
        resultado = ejecutar(efecto, estado, incompleta, self.git)
        self.assertTrue(resultado.completo)
        estado, efectos = paso(estado, resultado.observacion)
        self.assertIsInstance(estado.trenes[0].fase, Armado)
        self.assertEqual(estado.trenes[0].fase.candidato, resultado.observacion.candidato)
        self.assertEqual(estado.entradas[0].admision, Retenido(Motivo.INCIERTO))
        self.assertEqual(efectos, ())
        estado = desde_datos(datos(estado))
        estado, efectos = paso(estado, incompleta)
        self.assertIsInstance(estado.trenes[0].fase, Armado)
        self.assertEqual(efectos, ())
        estado, efectos = paso(estado, obs)
        self.assertIsInstance(estado.trenes[0].fase, EsperandoCI)
        self.assertEqual([e.tipo for e in efectos], ["solicitar-ci"])
        publicar = ejecutar(efectos[0], estado, obs, self.git)
        self.assertTrue(publicar.completo)
        self.assertEqual(self.remoto("refs/heads/tren/" + estado.trenes[0].id),
                         resultado.observacion.candidato.C)

    def test_conflicto_con_lectura_incompleta_no_se_pierde(self):
        miembros = []
        for pr in (4, 5):
            head = self.commit({"comun": "ticket {}\n".format(pr)}, (self.B,))
            self.push(head, "refs/pull/{}/head".format(pr))
            miembros.append((pr, head))
        estado, efecto, obs = self.congelar(miembros)
        incompleta = replace(obs, prs=(replace(obs.prs[0], completa=False),) + obs.prs[1:])
        resultado = ejecutar(efecto, estado, incompleta, self.git)
        self.assertTrue(resultado.completo)
        final, efectos = paso(estado, resultado.observacion)
        self.assertEqual(final.pausa, Pausa("conflicto-en-cadena", pr=5, archivos=("comun",)))
        self.assertIsInstance(final.trenes[0].fase, Congelado)
        self.assertEqual(efectos, ())

    def test_rama_ocupada_con_lectura_incompleta_no_se_pierde(self):
        estado, candidato, obs = self.armar()
        estado, efectos = paso(estado, obs)
        ref = "refs/heads/tren/" + estado.trenes[0].id
        self.push(self.B, ref)
        incompleta = replace(obs, prs=(replace(obs.prs[0], completa=False),) + obs.prs[1:])
        resultado = ejecutar(efectos[0], estado, incompleta, self.git)
        self.assertTrue(resultado.completo)
        final, efectos = paso(estado, resultado.observacion)
        self.assertEqual(final.pausa, Pausa("rama-tren-ocupada"))
        self.assertEqual(self.remoto(ref), self.B)
        self.assertEqual(efectos, ())

    def test_evento_promoviendo_anterior_sigue_legible(self):
        estado, candidato, obs = self.armar()
        estado, efecto = self.certificar(estado, candidato, obs)
        evento = datos(estado)
        evento["trenes"][0]["fase"].pop("reintento_pendiente", None)
        anterior = desde_datos(evento)
        self.assertEqual(anterior, estado)
        llegada = ejecutar(efecto, anterior, obs, self.git)
        final, _ = paso(anterior, llegada.observacion)
        self.assertEqual(final.trenes[0].fase, Terminal("ATERRIZADO"))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(RevisionR1Tests(nombre) for nombre in sorted(RevisionR1Tests.__dict__)
                              if nombre.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
