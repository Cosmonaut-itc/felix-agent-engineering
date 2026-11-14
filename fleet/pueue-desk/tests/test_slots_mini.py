"""Lugares por grupo en la Mini (U3.1): autoridad durable, canal rack y guardia.

Siempre contra un Pueue en proceso; nunca se ejecuta un binario del host.
"""
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_pueue_desk import control

PROFILE = dict(control.MINI_PROFILE)


class Crash(BaseException):
    """El proceso muere justo después del efecto de una RPC: nadie la captura."""


class FakePueue:
    def __init__(self, groups):
        self.groups = {g: {"status": "Running", "parallel_tasks": n} for g, n in groups.items()}
        self.log = []
        self.fail_at = None   # esa RPC falla sin efecto
        self.crash_at = None  # esa RPC tiene efecto y el proceso muere
        self.ignore = set()   # parallel sin efecto en estos grupos

    def rpc(self, args, effect):
        index = len(self.log)
        self.log.append(list(args))
        if index == self.fail_at:
            raise subprocess.CalledProcessError(1, ["pueue", *args])
        value = effect()
        if index == self.crash_at:
            raise Crash()
        return value

    def status(self):
        return self.rpc(["status"], lambda: {"groups": copy.deepcopy(self.groups), "tasks": {}})

    def call(self, *args):
        args = [str(a) for a in args]

        def effect():
            if args[0] == "parallel":
                if args[3] not in self.ignore:
                    self.groups[args[3]]["parallel_tasks"] = int(args[1])
            elif args[0] in ("pause", "start"):
                self.groups[args[-1]]["status"] = "Paused" if args[0] == "pause" else "Running"
            else:
                raise AssertionError(args)
            return ""
        return self.rpc(args, effect)

    def parallel(self, group, value):
        self.call("parallel", control.validate_limit(value), "-g", group)

    def vector(self):
        return {g: self.groups[g]["parallel_tasks"] for g in PROFILE}

    def changes(self):
        return [c for c in self.log if c != ["status"]]


class SlotFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "state"
        self.directory.mkdir()
        self.pueue = FakePueue(PROFILE)
        self.assertTrue(control.adopt(self.directory, self.pueue.status()))
        (self.directory / "control.lock").touch()
        self.pueue.log.clear()
        self.load, self.level, self.serial = 0.0, "1", 0

        def run(args, **kwargs):
            self.assertEqual(args, ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
            return subprocess.CompletedProcess(args, 0, self.level + "\n", "")
        for mocked in (patch.object(control.os, "getloadavg", side_effect=lambda: (self.load, 0, 0)),
                       patch.object(control.os, "cpu_count", return_value=10),
                       patch.object(control.subprocess, "run", side_effect=run)):
            mocked.start()
            self.addCleanup(mocked.stop)

    def view(self):
        with control.Store(self.directory / control.STORE, readonly=True) as store:
            return store.view()

    def request(self, op="slots", group="suites", delta=-1, action_id=None, dry=False, expected=None, undo_of=None):
        if action_id is None:
            self.serial += 1
            action_id = f"accion-{self.serial:04d}"
        if expected is None:
            view = self.view()
            expected = {"revision": view["revision"], "policy_hash": view["policy_hash"], "slots": self.pueue.vector()}
        value = {"schema": control.HOST_SCHEMA, "op": op, "action_id": action_id, "dry_run": dry, "expected": expected}
        if op == "slots":
            value["change"] = {"group": group, "delta": delta}
        else:
            value["undo_of"] = undo_of
        return value

    def rack(self, request):
        return control.rack(request if isinstance(request, str) else json.dumps(request), self.pueue, self.directory)

    def status(self, action_id):
        return self.rack({"schema": control.HOST_SCHEMA, "op": "status", "action_id": action_id})

    def caps(self):
        return self.rack({"schema": control.HOST_SCHEMA, "op": "capabilities"})["control"]

    def guard(self):
        with control.locked(self.directory):
            control.guard(self.pueue, None, self.directory, host="mini")

    def owned(self):
        target = self.directory / "guardia-state.json"
        return set(json.loads(target.read_text())) if target.exists() else set()

    def records(self, event):
        path = self.directory / "guardia.log"
        rows = [json.loads(r) for r in path.read_text().splitlines()] if path.exists() else []
        return [r for r in rows if r["evento"] == event]

    def tree(self, root=None):
        root = root or self.directory
        return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in sorted(root.rglob("*")) if p.is_file()}


class RequestTests(SlotFixture):
    def test_closed_schema_rejects_before_any_rpc(self):
        good = self.request()
        bad = []
        for change in ({"delta": True}, {"delta": 0}, {"delta": 2}, {"delta": 1.0}, {"delta": "1"},
                       {"group": "ajeno"}, {"group": None}):
            value = copy.deepcopy(good)
            value["change"].update(change)
            bad.append(value)
        for mutate in (lambda v: v.update(extra=1), lambda v: v.pop("change"), lambda v: v.update(dry_run=0),
                       lambda v: v["expected"]["slots"].update(suites=True),
                       lambda v: v["expected"]["slots"].pop("rapidas"),
                       lambda v: v["expected"]["slots"].update(ajeno=1),
                       lambda v: v["expected"].update(revision=-1), lambda v: v["expected"].update(revision=True),
                       lambda v: v.update(action_id="corto"), lambda v: v.update(action_id="../../etc/xx"),
                       lambda v: v.update(schema="rack-action@1"), lambda v: v.update(op="mode"),
                       lambda v: v.update(op="undo")):
            value = copy.deepcopy(good)
            mutate(value)
            bad.append(value)
        undo = self.request("undo", undo_of="accion-9999")
        undo["undo_of"] = undo["action_id"]
        bad.append(undo)
        raw = json.dumps(good)
        bad += [raw[:-1] + ', "op": "slots"}', raw.replace('"dry_run": false', '"dry_run": NaN'),
                raw[:-1] + ', "pad": "' + "x" * 16384 + '"}', "[]", "no es json"]
        for value in bad:
            answer = self.rack(value)
            self.assertEqual((answer["status"], answer["error"]), ("rejected", {"code": "invalid_request"}), value)
        self.assertEqual(self.pueue.log, [])
        self.assertEqual(self.view()["revision"], 0)

    def test_check_slots_bounds_cap_and_monotonic_reduction(self):
        check = control.check_slots
        self.assertIsNone(check(PROFILE, {"suites": 2, "default": 1, "rapidas": 3}))
        self.assertEqual(check(PROFILE, {"suites": 0, "default": 1, "rapidas": 3}), "min")
        self.assertEqual(check(PROFILE, {"suites": 4, "default": 1, "rapidas": 3}), "capacity")
        # 8 se admite, 9 no: el máximo sólo se alcanza con un tope mayor.
        self.assertIsNone(check(PROFILE, {"suites": 8, "default": 1, "rapidas": 3}, cap=20))
        self.assertEqual(check(PROFILE, {"suites": 9, "default": 1, "rapidas": 3}, cap=20), "max")
        self.assertIsNone(check(PROFILE, {"suites": 1, "default": 1, "rapidas": 1}))
        # Ya por encima del tope: sólo bajar, y sin subir ningún otro grupo.
        over = {"suites": 4, "default": 2, "rapidas": 3}
        self.assertIsNone(check(over, {"suites": 3, "default": 2, "rapidas": 3}))
        self.assertEqual(check(over, {"suites": 5, "default": 1, "rapidas": 2}), "capacity")
        self.assertEqual(check(over, {"suites": 4, "default": 2, "rapidas": 3}), "capacity")
        self.assertEqual(check(over, {"suites": 4, "default": 2, "rapidas": 4}), "capacity")


class MutateTests(SlotFixture):
    def test_apply_down_up_and_host_side_cap(self):
        down = self.rack(self.request(delta=-1))
        self.assertEqual((down["status"], down["revision"]), ("applied", 1))
        self.assertEqual(down["result"]["desired"], {"suites": 2, "default": 1, "rapidas": 3})
        self.assertEqual(self.pueue.vector(), down["result"]["desired"])
        self.assertEqual(self.pueue.log, [["status"], ["status"], ["parallel", "2", "-g", "suites"],
                                          ["status"], ["status"]])
        self.assertEqual(self.status(down["action_id"]), down)
        up = self.rack(self.request(group="rapidas", delta=1))
        self.assertEqual((up["status"], up["revision"], self.pueue.vector()["rapidas"]), ("applied", 2, 4))
        # Tope lleno (7): el host rechaza aunque el panel lo mande con el estado visto.
        self.pueue.log.clear()
        full = self.rack(self.request(group="default", delta=1))
        self.assertEqual((full["status"], full["error"]), ("rejected", {"code": "capacity"}))
        self.assertEqual(self.pueue.changes(), [])
        self.assertEqual(self.view()["revision"], 2)
        floor = self.rack(self.request(group="default", delta=-1))
        self.assertEqual(floor["error"], {"code": "min"})
        print(f"\nAPLICAR: -1 suites -> {down['status']} r{down['revision']}; +1 rapidas -> {up['status']} "
              f"r{up['revision']}; +1 default con 7 -> {full['error']['code']}; -1 default en 1 -> min")

    def test_over_cap_accepts_only_monotonic_reduction(self):
        with control.Store(self.directory / control.STORE) as store, store.transaction():
            store.db.execute("UPDATE desired SET slots=4 WHERE grp='suites'")
            store.db.execute("UPDATE desired SET slots=2 WHERE grp='default'")
        self.pueue.groups["suites"]["parallel_tasks"], self.pueue.groups["default"]["parallel_tasks"] = 4, 2
        self.assertEqual(self.rack(self.request(group="rapidas", delta=1))["error"], {"code": "capacity"})
        self.assertEqual(self.rack(self.request(group="suites", delta=-1))["status"], "applied")
        self.assertEqual(self.pueue.vector(), {"suites": 3, "default": 2, "rapidas": 3})
        self.assertEqual(self.rack(self.request(group="default", delta=-1))["status"], "applied")
        self.assertEqual(self.rack(self.request(group="default", delta=1))["error"], {"code": "capacity"})

    def test_reductions_before_increases_and_unconfirmed_reduction_stops(self):
        target = {"suites": 2, "default": 1, "rapidas": 4}
        self.assertEqual(control.converge(self.pueue, target), target)
        # La bajada se confirma con una lectura antes de la subida.
        self.assertEqual(self.pueue.log, [["status"], ["parallel", "2", "-g", "suites"], ["status"],
                                          ["parallel", "4", "-g", "rapidas"], ["status"]])
        # 0 es ilimitado: cuenta como exceso y se reduce primero.
        self.pueue = FakePueue({"suites": 0, "default": 1, "rapidas": 1})
        self.pueue.ignore = {"suites"}
        with self.assertRaises(ValueError):
            control.converge(self.pueue, target)
        self.assertEqual(self.pueue.changes(), [["parallel", "2", "-g", "suites"]])

    def test_dry_run_plans_without_effects(self):
        before = self.tree()
        answer = self.rack(self.request(group="rapidas", delta=-1, dry=True))
        self.assertEqual(answer["status"], "dry_run")
        self.assertEqual(answer["result"]["plan"], {"decreases": ["rapidas"], "increases": []})
        self.assertEqual(answer["result"]["desired"], {"suites": 3, "default": 1, "rapidas": 2})
        self.assertEqual(self.rack(self.request(group="rapidas", delta=1, dry=True))["error"], {"code": "capacity"})
        # La deriva se ve en el ensayo y no se repara: el ensayo no es un recuperador.
        self.pueue.groups["suites"]["parallel_tasks"] = 0
        drift = self.rack(self.request(dry=True, expected={**self.request()["expected"], "slots": PROFILE}))
        self.assertEqual((drift["status"], (drift["error"] or {}).get("code")), ("conflict", "drift"))
        self.assertEqual(drift["result"]["observed"]["suites"], 0)
        self.assertEqual(self.pueue.changes(), [])
        self.assertEqual(self.tree(), before)
        self.pueue.groups["suites"]["parallel_tasks"] = 3
        # El ID del ensayo no queda tomado.
        real = self.request(group="rapidas", delta=-1)
        real["action_id"] = answer["action_id"]
        self.assertEqual(self.rack(real)["status"], "applied")
        # Sin instalación: no crea ni el directorio ni control.lock.
        missing = self.root / "sin-instalar"
        self.assertEqual(control.rack(json.dumps(self.request(dry=True)), self.pueue, missing)["error"],
                         {"code": "not_adopted"})
        self.assertFalse(missing.exists())
        (self.directory / "control.lock").unlink()
        self.assertEqual(self.rack(self.request(dry=True))["error"], {"code": "not_adopted"})
        self.assertFalse((self.directory / "control.lock").exists())
        print("\nENSAYO: plan", answer["result"]["plan"], "; deriva visible sin reparar; árbol idéntico; "
              "sin instalación no crea nada")

    def test_same_id_same_receipt_other_body_conflict(self):
        request = self.request()
        first = self.rack(request)
        self.pueue.log.clear()
        self.assertEqual(self.rack(request), first)
        self.assertEqual(self.pueue.log, [])
        other = dict(request, change={"group": "rapidas", "delta": -1})
        self.assertEqual(self.rack(other)["error"], {"code": "idempotency_conflict"})
        self.assertEqual((self.view()["revision"], self.pueue.log), (1, []))

    def test_stale_drift_and_busy(self):
        expected = self.request()["expected"]
        for change in ({"revision": 3}, {"policy_hash": "0" * 64}, {"slots": {**PROFILE, "suites": 2}}):
            answer = self.rack(self.request(expected={**expected, **change}))
            self.assertEqual((answer["status"], (answer["error"] or {}).get("code")), ("conflict", "stale_state"))
        self.pueue.groups["rapidas"]["parallel_tasks"] = 5
        answer = self.rack(self.request(expected=expected))
        self.assertEqual((answer["status"], (answer["error"] or {}).get("code")), ("conflict", "drift"))
        self.assertEqual(self.caps()["reason"], "drift")
        self.pueue.groups["rapidas"]["parallel_tasks"] = 3
        self.pueue.log.clear()
        self.pueue.fail_at = 2  # parallel falla: queda una intención parcial
        partial = self.rack(self.request())
        self.assertEqual(partial["status"], "partial")
        self.pueue.fail_at = None
        busy = self.rack(self.request(group="rapidas"))
        self.assertEqual(busy["error"], {"code": "busy"})
        self.assertEqual(self.caps()["pending"], {"action_id": partial["action_id"], "phase": "partial"})
        self.assertEqual(self.caps()["reason"], "busy")

    def test_undo_is_exact_and_single(self):
        first = self.rack(self.request(delta=-1))
        dry = self.rack(self.request("undo", undo_of=first["action_id"], dry=True))
        self.assertEqual(dry["result"]["plan"], {"decreases": [], "increases": ["suites"]})
        undo = self.rack(self.request("undo", undo_of=first["action_id"]))
        self.assertEqual((undo["status"], undo["revision"]), ("applied", 2))
        self.assertEqual(self.pueue.vector(), PROFILE)
        self.assertEqual(undo["result"]["undo_of"], first["action_id"])
        for undo_of in (first["action_id"], undo["action_id"], "accion-nunca"):
            answer = self.rack(self.request("undo", undo_of=undo_of))
            self.assertEqual(answer["error"], {"code": "undo_conflict"}, undo_of)
        a = self.rack(self.request(group="rapidas", delta=-1))
        self.rack(self.request(group="suites", delta=-1))
        self.assertEqual(self.rack(self.request("undo", undo_of=a["action_id"]))["error"], {"code": "undo_conflict"})
        self.assertEqual(self.pueue.vector(), {"suites": 2, "default": 1, "rapidas": 2})
        # Mismo vector por otro camino: la revisión se movió y el deshacer viejo ya no vale.
        x = self.rack(self.request(group="rapidas", delta=-1))
        self.rack(self.request(group="rapidas", delta=1))
        self.rack(self.request(group="rapidas", delta=-1))
        self.assertEqual(self.pueue.vector(), x["result"]["desired"])
        self.assertEqual(self.rack(self.request("undo", undo_of=x["action_id"]))["error"], {"code": "undo_conflict"})
        self.assertEqual(self.pueue.vector(), {"suites": 2, "default": 1, "rapidas": 1})

    def test_adopt_requires_profile_and_never_overwrites(self):
        other = self.root / "otra"
        other.mkdir()
        for vector in ({**PROFILE, "suites": 2}, {**PROFILE, "rapidas": 0}):
            with self.assertRaises(ValueError):
                control.adopt(other, FakePueue(vector).status())
            self.assertEqual(list(other.iterdir()), [])
        self.rack(self.request(delta=-1))
        before = (self.directory / control.STORE).read_bytes()
        self.assertFalse(control.adopt(self.directory, FakePueue(PROFILE).status()))
        self.assertEqual((self.directory / control.STORE).read_bytes(), before)
        self.assertEqual(self.view()["desired"]["suites"], 2)


class FailureTests(SlotFixture):
    """Una falla en cada RPC de un -1 a suites: qué responde el host, qué ve Felix y cómo se recupera."""

    def sequence(self):
        self.rack(self.request(action_id="accion-sonda"))
        log = list(self.pueue.log)
        self.tearDown_reset()
        return log

    def tearDown_reset(self):
        self.doCleanups()
        self.setUp()

    def test_failure_and_crash_after_each_rpc(self):
        log = self.sequence()
        self.assertEqual(len(log), 5)
        target = {"suites": 2, "default": 1, "rapidas": 3}
        table = []
        for kind in ("falla", "caída"):
            for index, rpc in enumerate(log):
                self.tearDown_reset()
                request = self.request(action_id="accion-0042")
                setattr(self.pueue, "fail_at" if kind == "falla" else "crash_at", index)
                try:
                    answer = self.rack(request)["status"]
                except Crash:
                    answer = "proceso muerto"
                self.pueue.fail_at = self.pueue.crash_at = None
                seen = self.status("accion-0042")
                felix = seen["status"] if seen["status"] != "rejected" else seen["error"]["code"]
                pending = self.caps()["pending"]
                if felix == "not_found":
                    # El panel reenvía el mismo cuerpo una vez; el host aplica a lo sumo una vez.
                    resent = self.rack(request)["status"]
                    felix += f" -> reenvío {resent}"
                self.guard()
                final = self.status("accion-0042")
                parallels = [c for c in self.pueue.changes() if c[0] == "parallel"]
                table.append(f"{kind:5} en #{index} {' '.join(rpc):22} respuesta={answer:14} Felix ve={felix:24} "
                             f"tras guardia={final['status']}")
                if answer == "rejected":
                    self.assertEqual(index, 0)
                    self.assertEqual(self.view()["revision"], 0 if felix != "not_found -> reenvío applied" else 1)
                else:
                    self.assertEqual(final["status"], "applied")
                    self.assertEqual(self.view()["desired"], target)
                self.assertEqual(self.pueue.vector(), self.view()["desired"])
                self.assertIsNone(self.caps()["pending"])
                self.assertIsNone(self.caps()["reason"])
                # Nunca dos efectos para un ID: a lo sumo un parallel efectivo hacia 2.
                self.assertLessEqual(len([c for c in parallels if c == ["parallel", "2", "-g", "suites"]]), 2)
                self.assertNotIn(["parallel", "1", "-g", "suites"], parallels)
                self.assertTrue(pending is None or pending["action_id"] == "accion-0042")
        print("\nFALLAS POR RPC (-1 suites):\n" + "\n".join(table))


class GuardTests(SlotFixture):
    def test_lost_parallel_is_reconciled_before_opening(self):
        self.rack(self.request(delta=-1))
        # Reinicio de pueued: parallel en memoria perdido (0 = ilimitado) y grupos en pausa.
        self.pueue.groups = {"suites": {"status": "Paused", "parallel_tasks": 0},
                             "default": {"status": "Paused", "parallel_tasks": 1},
                             "rapidas": {"status": "Paused", "parallel_tasks": 5}}
        self.pueue.log.clear()
        self.guard()
        self.assertEqual(self.pueue.changes(), [["parallel", "3", "-g", "rapidas"], ["parallel", "2", "-g", "suites"]])
        self.assertEqual(self.pueue.vector(), {"suites": 2, "default": 1, "rapidas": 3})
        # La pausa ambigua no es de la guardia: no la abre sola.
        self.assertTrue(all(g["status"] == "Paused" for g in self.pueue.groups.values()))
        self.assertEqual(self.records("reconciliado")[-1]["observado"], {"suites": 2, "default": 1, "rapidas": 3})
        # Felix la traspasa por ssh con `control.py reanudar suites --host mini`.
        self.pueue.log.clear()
        with patch.object(control, "Pueue", return_value=self.pueue), \
                patch.object(control.Path, "home", return_value=self.root / "home"), \
                patch.object(sys, "argv", ["control.py", "reanudar", "suites", "--host", "mini"]):
            (self.root / "home/.local/state/pao").mkdir(parents=True)
            os.symlink(self.directory, self.root / "home/.local/state/pao/pueue-mini")
            control.main()
        self.assertEqual(self.pueue.changes(), [["start", "-g", "suites"]])
        self.assertEqual(self.records("relevo_a_guardia")[-1]["grupo"], "suites")
        # Una subida perdida también se repone, después de las bajadas.
        self.pueue.groups["suites"]["parallel_tasks"] = 1
        self.pueue.groups["default"]["parallel_tasks"] = 4
        self.pueue.log.clear()
        self.guard()
        self.assertEqual(self.pueue.changes(), [["parallel", "1", "-g", "default"], ["parallel", "2", "-g", "suites"]])

    def test_unreconciled_or_unreadable_never_opens(self):
        self.rack(self.request(delta=-1))
        self.pueue.groups["suites"].update(status="Paused", parallel_tasks=0)
        control.save_owned(self.directory, {"suites"})
        self.pueue.ignore = {"suites"}
        self.pueue.log.clear()
        with self.assertRaises(ValueError):
            self.guard()
        self.assertNotIn(["start", "-g", "suites"], self.pueue.changes())
        self.assertEqual(self.owned(), {"suites"})
        self.assertEqual(len(self.records("sin_reconciliar")), 1)
        self.pueue.ignore = set()
        self.guard()
        self.assertEqual(self.pueue.changes()[-1], ["start", "-g", "suites"])
        # Almacén ilegible: se puede pausar, nunca abrir ni tocar parallel ni aplicar una tabla.
        for content in (b"no es sqlite" * 50, None):
            store = self.directory / control.STORE
            if content is None:
                store.unlink()
            else:
                store.write_bytes(content)
            self.pueue.groups["suites"].update(status="Paused", parallel_tasks=0)
            control.save_owned(self.directory, {"suites"})
            self.pueue.log.clear()
            self.load = 25
            with self.assertRaises(control.StoreError):
                self.guard()
            self.assertIn(["pause", "--wait", "-g", "default"], self.pueue.changes())
            self.load = 0
            with self.assertRaises(control.StoreError):
                self.guard()
            self.assertFalse([c for c in self.pueue.changes() if c[0] in ("start", "parallel")])
            reason = "unreadable" if content else "not_adopted"
            self.assertEqual(self.caps()["reason"], reason)
            self.assertEqual(self.rack(self.request(expected={"revision": 1, "policy_hash": "a" * 64,
                                                              "slots": PROFILE}))["error"], {"code": reason})
            self.assertFalse([c for c in self.pueue.changes() if c[0] in ("start", "parallel")])
            for group in ("default", "rapidas", "suites"):
                self.pueue.groups[group]["status"] = "Running"

    def test_rapidas_ladder_intact_after_a_change(self):
        self.rack(self.request(group="rapidas", delta=-1))
        for load, owned in ((21, {"suites", "default"}), (31, {"suites", "default", "rapidas"}),
                            (25, {"suites", "default", "rapidas"}), (11.9, set())):
            self.load = load
            self.guard()
            self.assertEqual(self.owned(), owned)
        self.assertEqual(self.pueue.vector(), {"suites": 3, "default": 1, "rapidas": 2})
        self.assertFalse([c for c in self.pueue.changes() if c[0] == "parallel" and c[3] != "rapidas"])

    def test_ownership_is_written_before_pause(self):
        self.load = 25
        self.pueue.crash_at = 3  # status, status, pausa de suites, pausa de default: muere en la segunda
        index = None
        try:
            self.guard()
        except Crash:
            index = len(self.pueue.log) - 1
        self.assertEqual(self.pueue.log[index][:3], ["pause", "--wait", "-g"])
        self.assertIn(self.pueue.log[index][3], self.owned())


class CliTests(SlotFixture):
    def test_rack_cli_reads_stdin_outside_the_guard_lock(self):
        home = self.root / "home"
        (home / ".local/state/pao").mkdir(parents=True)
        os.symlink(self.directory, home / ".local/state/pao/pueue-mini")
        output = io.StringIO()
        with patch.object(control, "Pueue", return_value=self.pueue) as made, \
                patch.object(control.Path, "home", return_value=home), \
                patch.object(sys, "argv", ["control.py", "rack", "--host", "mini"]), \
                patch.object(sys, "stdin", io.StringIO(json.dumps({"schema": control.HOST_SCHEMA, "op": "capabilities"}))), \
                redirect_stdout(output):
            control.main()
        made.assert_called_once_with("/opt/homebrew/bin/pueue")
        self.assertEqual(getattr(self.pueue, "timeout", None), control.RACK_PUEUE_TIMEOUT)
        answer = json.loads(output.getvalue())
        self.assertEqual(answer["capabilities"], {"slots": True, "undo": True, "dry_run": True, "mode": False,
                                                  "reorder": False, "restart_queue": False})
        self.assertEqual(answer["control"]["desired"], PROFILE)
        with patch.object(sys, "argv", ["control.py", "rack"]), self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()), patch.object(sys, "stderr", io.StringIO()):
                control.main()

    def test_busy_lock_answers_busy(self):
        import fcntl
        with (self.directory / "control.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with patch.object(control, "LOCK_WAIT", 0.1):
                self.assertEqual(self.rack(self.request())["error"], {"code": "busy"})
        self.assertEqual(self.pueue.log, [])


if __name__ == "__main__":
    unittest.main()
