"""Regresiones de C1 y W1–W5; todos los servicios y sockets son dobles."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_recovery as recovery_tests
from test_recovery import MemoryPueue, task
import test_mini as mini_tests
from test_pueue_desk import ROOT, Fixture

spec = importlib.util.spec_from_file_location("host_r2", ROOT / "host.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
control = host.control


def killed(task_id=7, group="hasito"):
    return task(task_id, {"Done": {"start": "2026-11-12T01:00:00Z", "end": "2026-11-12T02:00:00Z",
                                 "enqueued_at": "2026-11-12T00:00:00Z", "result": "Killed"}}, group=group)


def event(module, message, level="INFO"):
    return f"02:00:00 {level:5} {module}: {message}\n"


KILL_ALL = event("pueue::daemon::process_handler::kill", "Killing all running tasks")
FINISH = event("pueue::daemon::process_handler::finish", "Task 7 finished with result: Killed")
PROTOCOL = "pueue_lib::network::protocol"


class ReviewRecoveryTests(Fixture):
    prepare = recovery_tests.RecoveryTests.prepare
    recover = recovery_tests.RecoveryTests.recover
    records = recovery_tests.RecoveryTests.records

    def test_C1_mini_clean_stop_does_not_prove_detached_suite_dead(self):
        control.save_control(self.directory, {"version": 2, "owned": [], "recovery": {}, "pending": {},
                                              "boot": "boot-1", "clean_stop": True})
        self.prepare({"7": task(group="suites")}, host="mini", boot="boot-1")
        self.recover()
        self.assertFalse(self.fake.calls, "Una suite en otra sesión puede seguir viva tras killpg")
        record = control.load_control(self.directory)["recovery"][control.task_key(task())]
        self.assertEqual(record["phase"], "fallida")
        self.assertIn("hijos vivos", record["motivo"])

    def audit_prepare(self, audit, tasks=None):
        control.save_control(self.directory, {"version": 2, "owned": [], "recovery": {}, "pending": {},
                                              "boot": "boot-1", "audit_active": True})
        (self.directory / "daemon-audit.log").write_text(audit)
        return self.prepare(tasks or {"7": killed()})

    def test_W2_direct_daemon_SIGTERM_recovers_Done_Killed_once_same_id(self):
        self.audit_prepare(KILL_ALL + FINISH)
        self.recover()
        self.recover()
        self.assertIn("Queued", self.fake.status()["tasks"]["7"]["status"])
        self.assertEqual([c for c in self.fake.calls if c[0] == "restart"], [["restart", "--in-place", "7"]])

    def test_W2_manual_kill_pending_at_shutdown_is_not_recovered(self):
        for selection in ("TaskIds(\n            [\n                7,\n            ],\n        )",
                          'Group(\n            "hasito",\n        )', "All"):
            with self.subTest(selection=selection):
                manual = event(PROTOCOL, "Received message: Kill(\n    KillRequest {\n        tasks: "
                               + selection + ",\n        signal: None,\n    },\n)", "DEBUG")
                self.audit_prepare(manual + KILL_ALL + FINISH)
                self.recover()
                self.assertFalse(self.fake.calls)

    def test_W2_manual_kill_overrides_concurrent_stop_capture(self):
        self.prepare()
        state = control.load_control(self.directory)
        state["audit_active"] = True
        control.save_control(self.directory, state)
        manual = event(PROTOCOL, "Received message: Kill(\n    KillRequest {\n"
                       "        tasks: TaskIds([7]),\n        signal: None,\n    },\n)", "DEBUG")
        (self.directory / "daemon-audit.log").write_text(manual + KILL_ALL + FINISH)
        # capture_stop vio Running; al detenerse pueued, el kill termina en disco.
        control.prepare_start(self.queue_path, self.directory, "desk", "boot-1")
        self.fake = MemoryPueue(self.queue_path)
        self.recover()
        self.assertFalse(self.fake.calls)
        self.assertNotIn("7", control.load_control(self.directory)["pending"])

    def test_W2_manual_kill_finished_then_new_attempt_can_recover(self):
        manual = event(PROTOCOL, "Received message: Kill(\n    KillRequest {\n"
                       "        tasks: TaskIds([7]),\n        signal: None,\n    },\n)", "DEBUG")
        self.audit_prepare(manual + FINISH + KILL_ALL + FINISH)
        self.recover()
        self.assertIn("Queued", self.fake.status()["tasks"]["7"]["status"])

    def test_W2_historical_kill_all_is_not_a_later_daemon_crash(self):
        self.audit_prepare(KILL_ALL + FINISH + event("pueue::daemon::process_handler::spawn", "Started task: sleep 600") + FINISH)
        self.recover()
        self.assertFalse(self.fake.calls)

    def test_W2_client_shutdown_is_not_attributed_to_crash(self):
        self.audit_prepare(event(PROTOCOL, "Received message: DaemonShutdown(\n    Graceful,\n)", "DEBUG")
                           + KILL_ALL + FINISH)
        self.recover()
        self.assertFalse(self.fake.calls)

    def test_W2_historical_Killed_and_ordinary_failure_remain_untouched(self):
        failed = killed(8)
        failed["status"]["Done"]["result"] = {"Failed": 3}
        self.audit_prepare(FINISH + KILL_ALL, {"7": killed(), "8": failed})
        self.recover()
        self.assertFalse(self.fake.calls)

    def test_W3_crash_after_intent_before_restart_reconciles_same_attempt(self):
        self.prepare()
        save = control.save_control

        class Crash(BaseException):
            pass

        def crash(directory, state):
            save(directory, state)
            if any(r["phase"] == "intentando" for r in state["recovery"].values()):
                raise Crash()

        with patch.object(control, "save_control", side_effect=crash):
            with self.assertRaises(Crash):
                control.recover(self.fake, self.directory, self.queue_path)
        self.assertFalse(self.fake.calls)
        control.recover(self.fake, self.directory, self.queue_path)
        self.assertIn("Queued", self.fake.status()["tasks"]["7"]["status"])
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "restart"]), 1)

    def test_W3_crash_after_restart_before_flush_only_confirms_queue(self):
        self.prepare()
        self.fake.fail_flush = True
        with self.assertRaises(RuntimeError):
            self.recover()
        self.fake.fail_flush = False
        self.recover()
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "restart"]), 1)
        self.assertIn("Queued", json.loads(self.queue_path.read_text())["tasks"]["7"]["status"])
        self.assertEqual(control.load_control(self.directory)["recovery"][control.task_key(task())]["phase"], "encolada")

    def test_W4_guard_timeout_captures_task_started_just_before_failure(self):
        self.prepare({"7": task(status={"Queued": {"enqueued_at": "2026-11-12T00:00:00Z"}})})
        fake = self.fake
        fake.binary = "pueue"
        config = self.root / "pueue.json"
        config.write_text(json.dumps({"shared": {"pueue_directory": str(self.queue_path.parent),
                                                 "pid_path": str(self.root / "pueue.pid")}}))
        events = []

        class Daemon:
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                events.append("terminate")
                self.returncode = 0
                fake.queue["tasks"]["7"] = killed()
                control.atomic_json(fake.path, fake.queue)

            def wait(self, **kwargs):
                return self.returncode

        def guard(*args):
            fake.queue["groups"]["hasito"]["status"] = "Running"
            fake.queue["tasks"]["7"] = task()
            control.atomic_json(fake.path, fake.queue)
            raise subprocess.TimeoutExpired("pueue start -g jale", 10)

        capture = control.capture_stop

        def capture_before_terminate(*args):
            events.append("capture")
            return capture(*args)

        with patch.object(host, "ensure_stopped"), patch.object(host, "boot_id", return_value="boot-1"), \
                patch.object(host.signal, "signal"), patch.object(host.subprocess, "Popen", return_value=Daemon()), \
                patch.object(host.subprocess, "check_output", side_effect=["pueue 4.0.4", "pueued 4.0.4"]), \
                patch.object(control, "apply_mode"), patch.object(control, "guard", side_effect=guard), \
                patch.object(control, "capture_stop", side_effect=capture_before_terminate):
            with self.assertRaises(subprocess.TimeoutExpired):
                host.supervise(fake, "pueued", config, self.directory, "desk", "docker")
        self.assertEqual(events, ["capture", "terminate"])
        control.prepare_start(self.queue_path, self.directory, "desk", "boot-1")
        self.fake = MemoryPueue(self.queue_path)
        self.recover()
        self.assertIn("Queued", self.fake.status()["tasks"]["7"]["status"])

    def test_W1_reinstall_validates_default_client_with_managed_config_present(self):
        library = self.root / "biblioteca"
        library.mkdir()
        original = json.dumps(control.managed_settings(self.root, "mini"))
        (library / "pueue.yml").write_text(original)
        client = self.root / "Library/Application Support/pueue/pueue.yml"
        client.parent.mkdir(parents=True)
        client.write_text(json.dumps({"shared": {"unix_socket_path": "/otro/socket"}}))
        with self.assertRaisesRegex(ValueError, "unix_socket_path"):
            control.managed_configuration(self.root, "mini", library)
        self.assertEqual((library / "pueue.yml").read_text(), original)

    def test_W1_custom_paths_rejected_for_both_hosts_before_write(self):
        for target in ("desk", "mini"):
            for key in ("unix_socket_path", "pueue_directory", "runtime_directory"):
                for syntax in ("json", "yaml"):
                    with self.subTest(host=target, key=key, syntax=syntax):
                        config = (self.root / "Library/Application Support/pueue/pueue.yml" if target == "mini"
                                  else self.root / ".config/pueue/pueue.yml")
                        config.parent.mkdir(parents=True, exist_ok=True)
                        config.write_text(json.dumps({"shared": {key: "/otra/cola"}}) if syntax == "json"
                                          else f"shared:\n  {key}: /otra/cola\n")
                        with patch.dict(control.os.environ, {}, clear=True):
                            with self.assertRaisesRegex(ValueError, key):
                                control.managed_configuration(self.root, target, self.root / "biblioteca")
                        self.assertFalse((self.root / "biblioteca").exists())


class ReviewMiniTests(unittest.TestCase):
    setUp = mini_tests.MiniDeploymentTests.setUp
    launch = mini_tests.MiniDeploymentTests.launch
    pueue_call = mini_tests.MiniDeploymentTests.pueue_call

    def test_W5_pause_wait_precedes_service_replacement_and_closes_admission(self):
        events = []
        original_launch = self.launch

        def pueue_call(*args):
            events.append(args)
            self.assertEqual(args, ("pause", "--wait", "-g", "suites"))
            self.queue["groups"]["suites"]["status"] = "Paused"
            (self.data / "state.json").write_text(json.dumps(self.queue))

        def launch(*args, **kwargs):
            if args[0] in ("disable", "bootout"):
                self.assertEqual(self.queue["groups"]["suites"]["status"], "Paused")
                events.append(args)
            return original_launch(*args, **kwargs)

        self.pueue.call.side_effect = pueue_call
        with patch("test_mini.mini.launch", side_effect=launch):
            from test_mini import mini
            mini.change("install", False, self.home, self.pueue)
        self.assertEqual(events[0], ("pause", "--wait", "-g", "suites"))
        state = mini.control.load_control(self.home / ".local/state/pao/pueue-desk")
        self.assertIn("suites", state["owned"])

    def test_W1_custom_socket_aborts_mini_before_any_service_change(self):
        config = self.data / "pueue.yml"
        config.write_text(json.dumps({"shared": {"unix_socket_path": "/otro/socket"}}))
        from test_mini import mini
        with self.assertRaisesRegex(ValueError, "unix_socket_path"):
            mini.change("install", False, self.home, self.pueue)
        self.assertFalse(self.calls)
        self.assertFalse(self.pueue.call.called)

    def test_W5_task_racing_preflight_aborts_before_disable(self):
        def pause(*args):
            self.queue["groups"]["suites"]["status"] = "Paused"
            self.queue["tasks"]["5"] = task(5, group="suites")

        self.pueue.call.side_effect = pause
        from test_mini import mini
        with self.assertRaisesRegex(RuntimeError, "termine la cola"):
            mini.change("install", False, self.home, self.pueue)
        self.assertFalse(any(c[0] in ("disable", "bootout", "bootstrap") for c in self.calls))
