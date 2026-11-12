"""Dobles con la semántica de persistencia medida en 4.0.4."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from test_pueue_desk import Fixture, ROOT, control


def task(task_id=7, status=None, created="2026-11-10T12:00:00Z", group="hasito"):
    return {"id": task_id, "created_at": created, "command": "echo prueba", "original_command": "echo prueba",
            "path": "/proyecto con espacios", "envs": {"MARCADOR": "original"}, "group": group,
            "dependencies": [], "priority": 0, "label": "prueba",
            "status": status or {"Running": {"start": "2026-11-12T01:00:00Z", "enqueued_at": "2026-11-12T00:00:00Z"}}}


class MemoryPueue:
    def __init__(self, path):
        self.path = path
        self.queue = json.loads(path.read_text())
        self.calls = []
        self.fail_flush = False
        self.fail_restart = False
        for item in self.queue["tasks"].values():
            for name in ("Running", "Paused"):
                if name in item["status"]:
                    detail = item["status"][name]
                    item["status"] = {"Done": {**detail, "end": "2026-11-12T02:00:00Z", "result": "Killed"}}
        control.atomic_json(path, self.queue)

    def status(self):
        return copy.deepcopy(self.queue)

    def call(self, *args):
        self.calls.append(list(args))
        if args[0] == "restart":
            if self.fail_restart:
                raise subprocess.CalledProcessError(1, args)
            self.queue["tasks"][args[-1]]["status"] = {"Queued": {"enqueued_at": "2026-11-12T02:01:00Z"}}
            # Deliberadamente NO guarda: es lo que hace Pueue 4.0.4.
        elif args[0] == "pause":
            self.queue["groups"][args[-1]]["status"] = "Paused"
            if not self.fail_flush:
                control.atomic_json(self.path, self.queue)
        elif args[0] == "start":
            self.queue["groups"][args[-1]]["status"] = "Running"
            control.atomic_json(self.path, self.queue)
        else:
            raise AssertionError(args)
        return ""


class RecoveryTests(Fixture):
    def prepare(self, tasks=None, host="desk", boot="boot-1"):
        self.queue_path = self.root / "data/state.json"
        groups = {g: {"status": "Running", "parallel_tasks": 3} for g in control.limits_for(host)}
        if host == "desk":
            groups["jale"]["status"] = "Paused"
        queue = {"groups": groups, "tasks": tasks or {"7": task()}}
        control.atomic_json(self.queue_path, queue)
        with control.locked(self.directory):
            control.prepare_start(self.queue_path, self.directory, host, boot)
        self.fake = MemoryPueue(self.queue_path)
        return self.fake

    def recover(self):
        with control.locked(self.directory):
            control.recover(self.fake, self.directory, self.queue_path)

    def records(self):
        return [json.loads(line) for line in (self.directory / "guardia.log").read_text().splitlines()]

    def test_interrupted_requeued_same_id_and_all_properties_durable(self):
        original = task()
        fake = self.prepare()
        self.recover()
        actual = fake.status()["tasks"]["7"]
        self.assertEqual(set(fake.status()["tasks"]), {"7"})
        self.assertIn("Queued", actual["status"])
        for key in set(original) - {"status"}:
            self.assertEqual(actual[key], original[key])
        self.assertEqual(json.loads(self.queue_path.read_text())["tasks"]["7"], actual)
        self.assertEqual(fake.calls, [["restart", "--in-place", "7"], ["pause", "--wait", "-g", "hasito"]])

    def test_recovery_twice_same_state_one_restart(self):
        self.prepare()
        self.recover()
        state = (self.directory / "guardia-state.json").read_bytes()
        queue = self.queue_path.read_bytes()
        calls = list(self.fake.calls)
        self.recover()
        self.assertEqual((self.directory / "guardia-state.json").read_bytes(), state)
        self.assertEqual(self.queue_path.read_bytes(), queue)
        self.assertEqual(self.fake.calls, calls)

    def test_second_interruption_stays_failed_with_reason(self):
        self.prepare()
        self.recover()
        self.fake.queue["tasks"]["7"]["status"] = {"Running": {"start": "2026-11-12T03:00:00Z", "enqueued_at": "2026-11-12T02:01:00Z"}}
        control.atomic_json(self.queue_path, self.fake.queue)
        with control.locked(self.directory):
            control.prepare_start(self.queue_path, self.directory, "desk", "boot-2")
        self.fake = MemoryPueue(self.queue_path)
        self.recover()
        self.assertEqual(self.fake.status()["tasks"]["7"]["status"]["Done"]["result"], "Killed")
        self.assertEqual(self.fake.calls, [])
        state = control.load_control(self.directory)
        self.assertIn("Segundo reinicio", state["recovery"][control.task_key(task())]["motivo"])
        self.assertEqual(self.records()[-1]["evento"], "recuperacion_rechazada")
        before = len(self.records())
        self.recover()
        self.assertEqual(len(self.records()), before)

    def test_ordinary_failures_and_manual_kills_untouched(self):
        failed = task(8, {"Done": {"start": "2026-11-12T00:00:00Z", "end": "2026-11-12T00:01:00Z", "enqueued_at": "2026-11-11T23:00:00Z", "result": {"Failed": 2}}})
        killed = task(9, {"Done": {"start": "2026-11-12T00:00:00Z", "end": "2026-11-12T00:01:00Z", "enqueued_at": "2026-11-11T23:00:00Z", "result": "Killed"}})
        self.prepare({"7": task(), "8": failed, "9": killed})
        self.recover()
        self.assertEqual(self.fake.status()["tasks"]["8"], failed)
        self.assertEqual(self.fake.status()["tasks"]["9"], killed)
        self.assertEqual([c for c in self.fake.calls if c[0] == "restart"], [["restart", "--in-place", "7"]])

    def test_own_failure_during_shutdown_not_retried(self):
        self.prepare()
        self.fake.queue["tasks"]["7"]["status"]["Done"]["result"] = {"Failed": 5}
        self.recover()
        self.assertEqual(self.fake.calls, [])

    def test_failed_flush_never_releases_startup_gate(self):
        self.prepare()
        self.fake.fail_flush = True
        with self.assertRaisesRegex(RuntimeError, "No se confirmó"):
            self.recover()
        self.assertTrue(control.load_control(self.directory)["starting"])
        # Si pueued perdió el restart, el mismo Done/Killed prueba que no se ejecutó.
        self.fake = MemoryPueue(self.queue_path)
        self.recover()
        self.assertEqual(self.fake.calls[0], ["restart", "--in-place", "7"])
        record = control.load_control(self.directory)["recovery"][control.task_key(task())]
        self.assertEqual(record["phase"], "encolada")

    def test_restart_failure_keeps_reconcilable_intent(self):
        self.prepare()
        self.fake.fail_restart = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.recover()
        self.fake.fail_restart = False
        self.recover()
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "restart"]), 2)
        self.assertEqual(control.load_control(self.directory)["recovery"][control.task_key(task())]["phase"], "encolada")

    def test_reused_id_new_creation_gets_own_recovery(self):
        self.prepare()
        self.recover()
        self.prepare({"7": task(created="2026-11-12T12:00:00Z")})
        self.recover()
        self.assertIn("Queued", self.fake.status()["tasks"]["7"]["status"])
        self.assertEqual(len(control.load_control(self.directory)["recovery"]), 2)

    def test_mini_same_boot_refuses_orphans_and_new_boot_recovers(self):
        for previous_boot, expected in (("boot-1", "Done"), ("boot-anterior", "Queued")):
            with self.subTest(previous_boot=previous_boot):
                control.save_control(self.directory, {"version": 2, "owned": [], "recovery": {}, "pending": {}, "boot": previous_boot})
                self.prepare({"7": task(group="suites")}, host="mini")
                self.recover()
                self.assertIn(expected, self.fake.status()["tasks"]["7"]["status"])
                if expected == "Done":
                    self.assertIn("hijos vivos", self.records()[-1]["motivo"])
                    self.assertFalse(self.fake.calls)

    def test_capture_stop_preserves_manual_pause_and_claims_running_groups(self):
        fake = self.prepare()
        self.recover()
        fake.queue["groups"]["hasito"]["status"] = "Running"
        state = control.load_control(self.directory)
        state["owned"] = []
        control.save_control(self.directory, state)
        control.capture_stop(fake, self.directory, "desk")
        self.assertIn("hasito", control.load_control(self.directory)["owned"])
        self.assertNotIn("jale", control.load_control(self.directory)["owned"])
        self.assertTrue(control.load_control(self.directory)["starting"])
        self.assertEqual({g["status"] for g in fake.status()["groups"].values()}, {"Paused"})

    def test_start_closes_empty_and_delayed_groups_before_daemon(self):
        self.prepare({"7": task(status={"Stashed": {"enqueue_at": "2026-11-13T00:00:00Z"}})})
        queue = json.loads(self.queue_path.read_text())
        self.assertEqual({g["status"] for g in queue["groups"].values()}, {"Paused"})
        state = control.load_control(self.directory)
        self.assertNotIn("jale", state["owned"])
        self.assertIn("hasito", state["owned"])
        self.assertEqual(state["pending"], {})

    def test_corrupt_or_compressed_state_is_not_replaced(self):
        path = self.root / "state.json"
        path.write_text("roto")
        with self.assertRaises(ValueError):
            control.prepare_start(path, self.directory, "desk", "boot")
        self.assertEqual(path.read_text(), "roto")
        path.with_suffix(".json.gz").touch()
        with self.assertRaisesRegex(ValueError, "compress_state_file"):
            control.prepare_start(path, self.directory, "desk", "boot")


class DockerGuardTests(Fixture):
    def test_docker_down_pauses_every_managed_group_and_logs_docker(self):
        (self.root / "docker-caido").touch()
        # No hace falta /proc legible para cerrar la admisión por Docker.
        self.guard()
        self.assertEqual({g["status"] for g in self.pueue.status()["groups"].values()}, {"Paused"})
        records = [json.loads(line) for line in (self.directory / "guardia.log").read_text().splitlines()]
        self.assertEqual([r["motivo"] for r in records if r["evento"] == "pausado"], ["docker"] * 4)

    def test_docker_returns_low_pressure_resumes_only_owned_pauses(self):
        self.pueue.call("pause", "--wait", "-g", "jale")
        (self.root / "docker-caido").touch()
        self.guard()
        (self.root / "docker-caido").unlink()
        self.metrics()
        self.guard()
        groups = self.pueue.status()["groups"]
        self.assertEqual(groups["jale"]["status"], "Paused")
        self.assertEqual({groups[g]["status"] for g in groups if g != "jale"}, {"Running"})

    def test_docker_returns_high_pressure_does_not_resume(self):
        (self.root / "docker-caido").touch()
        self.guard()
        (self.root / "docker-caido").unlink()
        self.metrics(cpu=31)
        self.guard()
        self.assertFalse(any(c[1] == "start" for c in self.calls()))

    def test_docker_missing_or_timeout_is_not_ready(self):
        self.assertFalse(control.docker_ready(self.bin / "no-existe"))
        with patch.object(control.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 3)) as call:
            self.assertFalse(control.docker_ready("docker-configurable"))
        self.assertEqual(call.call_args.kwargs["timeout"], 3)

    def test_pending_recovery_prevents_resume_even_when_docker_ready(self):
        control.save_control(self.directory, {"version": 2, "owned": [], "recovery": {}, "pending": {}, "starting": True})
        self.metrics()
        self.guard()
        self.assertEqual({g["status"] for g in self.pueue.status()["groups"].values()}, {"Paused"})


class HostAndMiniTests(unittest.TestCase):
    def test_mini_launchagent_runs_supervisor_and_keeps_alive(self):
        import plistlib
        template = plistlib.loads((ROOT / "launchagents/io.pao.pueue.plist").read_bytes())
        self.assertTrue(template["RunAtLoad"])
        self.assertTrue(template["KeepAlive"])
        self.assertIn("@HOME@/.local/lib/pao-pueue-desk/host.py", template["ProgramArguments"])

    def test_host_refuses_live_pid_and_does_not_touch_queue(self):
        spec = importlib.util.spec_from_file_location("host", ROOT / "host.py")
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        with patch.object(host.os, "kill") as kill, patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "pueued\n", "")):
            pid = unittest.mock.Mock()
            pid.exists.return_value = True
            pid.read_text.return_value = "123"
            with self.assertRaisesRegex(RuntimeError, "aún existe"):
                host.ensure_stopped(unittest.mock.Mock(), pid)
            kill.assert_called_once_with(123, 0)

    def test_stale_pid_reused_by_another_program_is_removed_without_killing_it(self):
        spec = importlib.util.spec_from_file_location("host", ROOT / "host.py")
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        pid = unittest.mock.Mock()
        pid.exists.return_value = True
        pid.read_text.return_value = "123"
        pueue = unittest.mock.Mock()
        pueue.status.side_effect = subprocess.CalledProcessError(1, "status")
        with patch.object(host.os, "kill") as kill, patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "otro-programa\n", "")):
            host.ensure_stopped(pueue, pid)
        kill.assert_called_once_with(123, 0)
        pid.unlink.assert_called_once_with()
