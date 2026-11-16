"""U18: verificación previa al aterrizaje; sólo host remoto.

Origen git real, GitHub y lanzador falsos de U13 y un gancho falso: un guion
Python que registra cada llamada y responde según `gancho-plan.json`. Cada
tick pasa por `pao-tren` con el config privado, como en producción; sólo se
sustituye el transporte de gh. Ninguna prueba importa símbolos nuevos:
`remoto-u18.sh` corre la misma clase sobre a1d24ec1, donde debe fallar.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from unittest.mock import patch
import unittest

import test_activo as activo
import test_juicio
import test_u17
from dominio import Terminal, datos
from observador import GitHubGit

REPO = activo.REPO
ATERRIZADO = Terminal("ATERRIZADO")
PREFIJO = "verificacion-previa:"

GANCHO = r'''
import json
from pathlib import Path
import subprocess
import sys
import time

raiz = Path(sys.argv[1])
operacion = sys.argv[-1]
solicitud = json.loads(sys.stdin.read())
with (raiz / "gancho.log").open("a") as log:
    log.write(json.dumps({"operacion": operacion, "solicitud": solicitud}) + "\n")
eco = {"B": solicitud["B"], "C": solicitud["C"]}
if operacion == "cancelar":
    print(json.dumps(dict(eco, resultado="cancelado")))
    sys.exit(0)
plan = json.loads((raiz / "gancho-plan.json").read_text())
llamadas = [json.loads(l) for l in (raiz / "gancho.log").read_text().splitlines()]
n = sum(1 for l in llamadas if l["operacion"] == "verificar")
paso = plan[min(n, len(plan)) - 1]
modo = paso.get("modo", "responder")
if modo == "dormir":
    time.sleep(paso["s"])
elif modo == "mover-main":
    subprocess.run(["git", "-C", paso["origen"], "update-ref", "refs/heads/main",
                    paso["a"]], check=True)
elif modo == "timeout":
    time.sleep(60)
elif modo == "salida":
    sys.exit(3)
elif modo == "mal-formada":
    print("no es JSON")
    sys.exit(0)
elif modo == "sin-eco":
    print(json.dumps({"resultado": "seguir"}))
    sys.exit(0)
print(json.dumps(dict(eco, **paso.get("respuesta", {"resultado": "seguir"}))))
'''

SEGUIR = {"respuesta": {"resultado": "seguir"}}


def esperar(motivo):
    return {"respuesta": {"resultado": "esperar", "motivo": motivo}}


def iso(segundos):
    return datetime.fromtimestamp(segundos, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ContextoU18(unittest.TestCase):
    comando = activo.ActivoTests.comando
    commit = activo.ActivoTests.commit
    push = activo.ActivoTests.push
    remoto = activo.ActivoTests.remoto
    ledger = activo.ActivoTests.ledger
    estado = activo.ActivoTests.estado
    lineas = activo.ActivoTests.lineas
    hijo = activo.ActivoTests.hijo
    cli = test_juicio.JuicioTests.cli
    juicio = test_juicio.JuicioTests.juicio
    posts = test_juicio.JuicioTests.posts

    def setUp(self):
        activo.ActivoTests.setUp(self)
        self.guion = self.raiz / "gancho.py"
        self.guion.write_text(GANCHO)
        self.config = dict(self.contexto.config)
        self.privada = self.raiz / "config-privada.json"
        self.plan([SEGUIR])
        self.escribir()

    def escribir(self, gancho=True, plazo=30, autorizados=()):
        config = dict(self.config, heads_autorizados=list(autorizados))
        if gancho:
            config["verificacion_previa"] = {
                "argv": [sys.executable, "-I", str(self.guion), str(self.raiz)],
                "plazo_s": plazo}
        self.privada.write_text(json.dumps({"repos": {REPO: config}}))

    def plan(self, pasos):
        (self.raiz / "gancho-plan.json").write_text(json.dumps(pasos))

    def llamadas(self, operacion=None):
        return [l for l in (json.loads(t) for t in self.lineas("gancho.log"))
                if operacion is None or l["operacion"] == operacion]

    def crear(self, config, **kwargs):
        return GitHubGit(config, ejecutar=self.contexto.gh, **kwargs)

    def tick(self, ahora):
        args = activo.cli.parser().parse_args(["tick", "--repo", REPO,
                                               "--config", str(self.privada)])
        with patch.object(activo.cli, "GitHubGit", side_effect=self.crear):
            codigo, salida = activo.cli.ejecutar(args, None, ahora, self.contexto.git,
                                                 self.contexto.hasito, None,
                                                 self.contexto.escritor)
        self.assertEqual(codigo, 0, salida)
        return salida

    def hasta(self, condicion, inicio, limite=25, pausa=False):
        """Ticks cada 120 s; sin `pausa`, ninguno puede pausar el carril."""
        for n in range(limite):
            ahora = inicio + n * 120
            self.tick(ahora)
            estado = self.estado()
            if not pausa:
                self.assertIsNone(estado.pausa, datos(estado.pausa))
            if condicion(estado):
                return ahora, estado
        self.fail("no convergió: " + json.dumps(datos(self.estado()))[:3000])

    def primera_verificacion(self, inicio=100):
        return self.hasta(lambda e: self.llamadas("verificar"), inicio)

    def hasta_pausa(self, inicio):
        return self.hasta(lambda e: e.pausa is not None, inicio, pausa=True)

    def aterrizado(self, estado):
        return bool(estado.trenes) and estado.trenes[-1].fase == ATERRIZADO

    def assert_sin_rojo(self):
        # Ni comentario, ni status failure, ni devolución: ningún motivo es del PR.
        self.assertEqual(self.posts("/comments"), [])
        self.assertFalse([p for p in self.posts("/statuses/")
                          if p["payload"]["state"] == "failure"])
        with self.ledger() as ledger:
            tipos = [json.loads(r[0])["tipo"] for r in ledger.db.execute(
                "SELECT payload FROM effects")]
        self.assertNotIn("devolver", tipos)

    def assert_en_b(self):
        self.assertEqual(self.remoto(), self.B)
        self.assertFalse([p for p in self.lineas("pushes.log")
                          if p.endswith(":refs/heads/main")])

    def assert_pausa_verificacion(self, estado, detalle):
        self.assertEqual(datos(estado.pausa), {"_tipo": "Pausa", "motivo": "infraestructura",
            "pr": None, "main": "", "archivos": [], "detalle": detalle})
        expediente = self.cli("estado", "--juicio")[1]["juicio"]
        self.assertEqual(sorted(a["decision"] for a in expediente["admitidas"]),
                         ["abortar", "reanudar"])

    def assert_cancelado(self, n=1):
        canceladas = self.llamadas("cancelar")
        self.assertEqual(len(canceladas), n, canceladas)
        C = self.llamadas("verificar")[-1]["solicitud"]["C"]
        for llamada in canceladas:
            self.assertEqual((llamada["solicitud"]["B"], llamada["solicitud"]["C"]),
                             (self.B, C))


class RegresionesTests(ContextoU18):
    def test_esperar_no_empuja_y_seguir_aterriza_con_el_contrato(self):
        self.plan([esperar("drain-no-listo"), esperar("ventana-congelada"), SEGUIR])
        ahora, _ = self.primera_verificacion()
        for n in (1, 2):
            # Esperar: no empuja, no pausa, sin feedback; el tick siguiente consulta.
            self.assert_en_b()
            self.assert_sin_rojo()
            self.assertEqual(len(self.llamadas("verificar")), n)
            self.tick(ahora + n * 120)
            self.assertIsNone(self.estado().pausa)
        _, final = self.hasta(self.aterrizado, ahora + 360)
        C = self.remoto()
        self.assertNotEqual(C, self.B)
        verificaciones = self.llamadas("verificar")
        self.assertEqual(len(verificaciones), 3)
        solicitud = verificaciones[-1]["solicitud"]
        tren = final.trenes[-1]
        self.assertEqual(solicitud["contrato"], "pao-tren/verificacion-previa@1")
        self.assertEqual(solicitud["operacion"], "verificar")
        self.assertEqual({k: solicitud[k] for k in ("repo", "tren", "intento", "B", "C",
                                                    "plazo_s")},
                         {"repo": REPO, "tren": tren.id, "intento": 1, "B": self.B,
                          "C": C, "plazo_s": 30})
        self.assertEqual(solicitud["prs"], [{"pr": pr, "head": head}
                                            for pr, head in self.miembros])
        self.assertEqual(solicitud["arbol"],
                         self.comando(self.origen, "rev-parse", C + "^{tree}"))
        # El almacén del tren contiene B y C: el gancho calcula B..C sin fetch.
        self.comando(Path(solicitud["almacen"]), "cat-file", "-e", C + "^{commit}")
        # Las tres consultas son la misma clave del intento 1, sin consumirlo.
        self.assertEqual({v["solicitud"]["intento"] for v in verificaciones}, {1})
        self.assertEqual(self.llamadas("cancelar"), [])
        self.assertEqual(sum(p.endswith(":refs/heads/main")
                             for p in self.lineas("pushes.log")), 1)
        self.assert_sin_rojo()

    def test_cada_salida_no_valida_pausa_infraestructura_sin_rojo(self):
        self.escribir(plazo=3)
        casos = [({"respuesta": {"resultado": "pausar", "motivo": "redeploy-indeterminado"}},
                  "redeploy-indeterminado"),
                 ({"modo": "timeout"}, "plazo-agotado"),
                 ({"modo": "mal-formada"}, "salida-mal-formada"),
                 ({"modo": "salida"}, "salida-no-cero"),
                 ({"modo": "sin-eco"}, "salida-mal-formada"),
                 ({"respuesta": {"resultado": "quizas", "motivo": "x"}}, "resultado-desconocido"),
                 ({"respuesta": {"resultado": "esperar"}}, "salida-mal-formada")]
        self.plan([caso for caso, _ in casos] + [SEGUIR])
        ahora = 100
        for n, (_, detalle) in enumerate(casos, 1):
            ahora, estado = self.hasta_pausa(ahora)
            self.assert_pausa_verificacion(estado, PREFIJO + detalle)
            self.assertIsInstance(estado.trenes[-1].fase, activo.Promoviendo)
            self.assert_en_b()
            self.assert_sin_rojo()
            self.assertEqual(len(self.llamadas("verificar")), n)
            # La pausa retira lo que el gancho dejó a medias en ese mismo tick.
            self.assert_cancelado(n)
            self.juicio("reanudar")
            ahora += 120
        _, final = self.hasta(self.aterrizado, ahora)
        # Cada reanudación es un intento nuevo y vuelve a verificar.
        self.assertEqual([v["solicitud"]["intento"] for v in self.llamadas("verificar")],
                         list(range(1, len(casos) + 2)))
        self.assertEqual(final.trenes[-1].fase.resultado, "ATERRIZADO")
        self.assert_sin_rojo()

    def test_abortar_tras_pausa_libera_sin_rojo(self):
        self.plan([{"respuesta": {"resultado": "pausar", "motivo": "congelada-indeterminada"}}])
        ahora, estado = self.hasta_pausa(100)
        self.assert_pausa_verificacion(estado, PREFIJO + "congelada-indeterminada")
        self.juicio("abortar")
        self.tick(ahora + 120)
        estado = self.estado()
        self.assertIsNone(estado.pausa)
        self.assertEqual(estado.trenes[0].fase, Terminal("ABORTADO"))
        self.assertTrue(all(e.tren is None and e.terminal is None for e in estado.entradas))
        self.assert_en_b()
        self.assert_cancelado()
        self.assert_sin_rojo()

    def test_b_cambia_entre_el_gancho_y_el_push(self):
        ajeno = self.commit({"comun": "base\n", "externo": "cambio\n"}, (self.B,))
        self.push(ajeno, "refs/heads/ajeno")
        self.plan([{"modo": "mover-main", "origen": str(self.origen), "a": ajeno}])
        _, estado = self.hasta_pausa(100)
        self.assertEqual(estado.pausa.motivo, "escritor-externo")
        self.assertEqual(estado.pausa.main, ajeno)
        self.assertEqual(estado.trenes[0].fase, Terminal("REARMAR"))
        # El resultado del gancho valía para B: el lease rechazó el push.
        self.assertEqual(self.remoto(), ajeno)
        self.assertEqual(len(self.llamadas("verificar")), 1)
        self.assert_cancelado()
        self.assert_sin_rojo()

    def test_ventana_autorizada_antes_espera_y_dentro_aterriza(self):
        _, head = self.miembros[0]
        self.escribir(gancho=False, autorizados=[
            {"head": head, "desde": iso(7200), "hasta": iso(10800)}])
        for ahora in range(100, 7200, 600):
            self.tick(ahora)
            estado = self.estado()
            self.assertIsNone(estado.pausa, datos(estado.pausa))
            self.assert_en_b()
        self.assertIsInstance(estado.trenes[0].fase, activo.Promoviendo)
        self.assert_sin_rojo()
        ahora, _ = self.hasta(self.aterrizado, 7200)
        self.assertNotEqual(self.remoto(), self.B)
        self.assertLess(ahora, 10800)
        self.assert_sin_rojo()

    def test_ventana_autorizada_vencida_pausa_y_reanuda_con_otra(self):
        _, head = self.miembros[1]
        self.escribir(gancho=False, autorizados=[
            {"head": head, "desde": iso(0), "hasta": iso(3600)}])
        ahora, estado = self.hasta_pausa(5000)
        self.assert_pausa_verificacion(estado, "ventana-autorizada-vencida")
        self.assert_en_b()
        self.assert_sin_rojo()
        # Felix fija otra ventana para el mismo head y reanuda.
        self.escribir(gancho=False, autorizados=[
            {"head": head, "desde": iso(0), "hasta": iso(99999)}])
        self.juicio("reanudar")
        self.hasta(self.aterrizado, ahora + 120)
        self.assert_sin_rojo()

    def test_ventana_vence_mientras_corre_el_gancho(self):
        _, head = self.miembros[2]
        self.plan([esperar("drain-no-listo")])
        ahora, _ = self.primera_verificacion()
        siguiente = ahora + 120
        self.escribir(autorizados=[{"head": head, "desde": iso(0), "hasta": iso(siguiente + 1)}])
        self.plan([esperar("drain-no-listo"), {"modo": "dormir", "s": 2}])
        self.tick(siguiente)
        self.assertEqual(len(self.llamadas("verificar")), 2)
        _, estado = self.hasta_pausa(siguiente + 120)
        self.assert_pausa_verificacion(estado, "ventana-autorizada-vencida")
        self.assert_en_b()
        self.assert_cancelado()
        self.assert_sin_rojo()

    def test_muerte_entre_el_gancho_y_el_push_vuelve_a_verificar(self):
        self.plan([SEGUIR, esperar("drain-no-listo"), SEGUIR])
        codigo = """
import test_activo as activo
from observador import GitHubGit
from unittest.mock import patch
c = activo.Contexto(sys.argv[3], "antes-push")
args = activo.cli.parser().parse_args(["tick", "--repo", activo.REPO, "--config", sys.argv[4]])
def crear(config, **kwargs):
    return GitHubGit(config, ejecutar=c.gh, **kwargs)
with patch.object(activo.cli, "GitHubGit", side_effect=crear):
    for n in range(20):
        activo.cli.ejecutar(args, None, 100 + n * 120, c.git, c.hasito, None, c.escritor)
sys.exit(9)
"""
        hijo = self.hijo(codigo, str(self.privada))
        salida, error = hijo.communicate(timeout=120)
        self.assertEqual(hijo.returncode, 77, salida + error)
        self.assert_en_b()
        with self.ledger() as ledger:
            promover = [r for r in ledger.outbox(REPO)
                        if json.loads(r["payload"])["tipo"] == "promover"]
        self.assertEqual([r["estado"] for r in promover], ["ejecutando"])
        # El tick siguiente recupera el push y verifica otra vez: esperar gana.
        self.tick(5000)
        self.assert_en_b()
        self.assertIsNone(self.estado().pausa)
        self.assertEqual(len(self.llamadas("verificar")), 2)
        self.hasta(self.aterrizado, 5120)
        self.assertEqual(len(self.llamadas("verificar")), 3)
        self.assertEqual(sum(p.endswith(":refs/heads/main")
                             for p in self.lineas("pushes.log")), 1)
        self.assertEqual(self.llamadas("cancelar"), [])
        self.assert_sin_rojo()

    def test_pausa_manual_durante_la_espera_cancela_y_al_levantar_verifica(self):
        self.plan([esperar("drain-no-listo"), SEGUIR])
        ahora, _ = self.primera_verificacion()
        codigo, resultado = self.cli("pausa", "--motivo", "prueba")
        self.assertEqual(codigo, 0, resultado)
        self.tick(ahora + 120)
        estado = self.estado()
        self.assertEqual(estado.pausa.motivo, "manual")
        self.assert_cancelado()
        self.assertEqual(len(self.llamadas("verificar")), 1)
        self.assert_en_b()
        codigo, resultado = self.cli("pausa", "--levantar", "--motivo", "prueba")
        self.assertEqual(codigo, 0, resultado)
        self.hasta(self.aterrizado, ahora + 240)
        self.assertEqual(len(self.llamadas("verificar")), 2)
        self.assert_sin_rojo()


class SombraTests(test_u17.SombraTests):
    """Con gancho y ventana declarados, sombra, `entregar` y `tick --seco` no
    llaman al gancho; `remoto-u18.sh` compara sus eventos con a1d24ec1."""

    def setUp(self):
        test_u17.SombraTests.setUp(self)
        guion = self.raiz / "gancho.py"
        guion.write_text(GANCHO)
        (self.raiz / "gancho-plan.json").write_text(json.dumps([SEGUIR]))
        self.contexto.config["verificacion_previa"] = {
            "argv": [sys.executable, "-I", str(guion), str(self.raiz)], "plazo_s": 30}
        # Una autorización con ventana de un head ajeno a la prueba.
        self.contexto.config["heads_autorizados"] = [
            {"head": "e" * 40, "desde": iso(0), "hasta": iso(3600)}]
        self.privada.write_text(json.dumps({"repos": {REPO: self.contexto.config}}))

    def test_sombra_entregar_y_seco_sin_fetch_ni_cambios(self):
        test_u17.SombraTests.test_sombra_entregar_y_seco_sin_fetch_ni_cambios(self)
        self.assertFalse((self.raiz / "gancho.log").exists())


if __name__ == "__main__":
    unittest.main()
