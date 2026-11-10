"""host-reservation@1 crash windows: each cut proves first that hostgate reached its barrier."""
import json
import os
import signal
import subprocess
import sys
import unittest

from test_host_reservation_cli import HostHarness, SUPPORT


class HostCrash(HostHarness):
    def setUp(self):
        super().setUp()
        self.start_server()

    def test_enqueue_and_stop_before_reserve(self):
        heads = [self.submit(key=key, enqueue_only=True) for key in ("a", "b")]
        self.assertEqual([code for code, _ in heads], [0, 0], heads)
        a, b = (value for _, value in heads)
        jobs = self.ledger("SELECT id,content FROM jobs ORDER BY seq")
        self.assertEqual([job for job, _ in jobs], [a["job"], b["job"]])
        self.assertEqual({json.loads(content)["execution"]["backend"] for _, content in jobs}, {"host-reservation@1"})
        empty = self.hstate()
        self.assertEqual((empty["pending"], empty["reservations"], empty["budget"]["retained"]["slots"]), ([], [], 0))
        (self.hroot / ".pause-before_unknown").touch()
        late = subprocess.Popen([sys.executable, "-I", "-B", str(SUPPORT), str(self.hroot), "fleet",
                                 "--state-dir", str(self.state), "recover"], cwd=str(self.project), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_server, late)
        self.until(lambda: bool(self.rounds()) or late.poll() is not None, message="a round or recover exit")
        self.assertTrue(self.rounds(), "FACT: recover opened the FIFO head's host round")
        self.until(lambda: (self.hroot / ".at-before_unknown").exists(), message="S1 paused before the CAS")
        paused = int((self.hroot / ".at-before_unknown").read_text())
        self.assertEqual([(r["ordinal"], r["state"]) for r in self.rounds()], [(1, "prepared")])
        self.read_round(a["job"], "prepared")
        self.assertEqual(self.log(".offers"), [self.rounds()[0]["operation_id"]], "H paired the paused S")
        (self.hroot / ".pause-retired").touch()
        recovery = subprocess.Popen([sys.executable, "-I", "-B", str(SUPPORT), str(self.hroot), "fleet",
                                     "--state-dir", str(self.state), "recover"], cwd=str(self.project), env=self.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_server, recovery)
        self.until(lambda: (self.hroot / ".at-retired").exists(), message="committed retirement")
        self.read_round(a["job"], "retired")
        self.assertEqual(self.hstate()["budget"]["retained"]["slots"], 0)
        (self.hroot / ".held-retired").unlink()
        recovery.communicate(timeout=60)  # S2 reserves the same head in round 2
        self.until(lambda: any(name == "exit" and int(pid) != paused for pid, name in self.trace()),
                   message="S2 exit")
        retired, current = self.rounds()
        self.assertEqual((retired["state"], retired["reason"]), ("retired", "recovered_before_send"))
        self.assertEqual((current["attempt"], current["ordinal"], current["source"]), (a["attempt"], 2, "reply"),
                         "FACT: the head reserved in round 2")
        (self.hroot / ".held-before_unknown").unlink()
        late.communicate(timeout=60)
        self.until(lambda: (str(paused), "exit") in self.trace(), message="S1 exit")
        self.assertEqual(self.log(".reservar"), [current["operation_id"]], "FACT: S1 never sent late")
        self.assertEqual([r["state"] for r in self.rounds()], ["retired", "held"])
        self.read_round(a["job"], "held")
        hstate = self.hstate()
        self.assertEqual([r["key"] for r in hstate["reservations"]], [current["key"]])
        self.assertEqual(hstate["budget"]["retained"]["slots"], 1)
        self.assertEqual(self.status(b["job"])["state"], "queued")
        self.assertEqual(self.ledger("SELECT id,content FROM jobs ORDER BY seq"), jobs)

    def test_lost_commit_ack_keeps_operation(self):
        (self.hroot / ".cut").write_text("after_commit")
        submitted, current = self.reserved()
        self.assertTrue((self.hroot / ".cut-hit").exists(), "hostgate reached after_commit")
        operation = "fleet:{}:1".format(submitted["attempt"])
        self.assertEqual(self.log(".reservar"), [operation, operation], "FACT: the same operation repeated")
        self.assertEqual((current["operation_id"], current["source"]), (operation, "same-channel-replay"))
        self.assertIs(current["receipt"]["replayed"], True)
        hstate = self.hstate()
        self.assertEqual([r["key"] for r in hstate["reservations"]], [current["key"]])
        self.assertEqual((current["key"]["generation"], hstate["budget"]["retained"]["slots"]), (1, 1))
        self.assertEqual(self.ledger("SELECT attempt,generation FROM slot"), [(submitted["attempt"], 1)])

    def test_cut_end_and_restart_cannot_adopt(self):
        (self.hroot / ".wire").write_text("cut")
        code, submitted = self.submit()
        self.assertEqual((code, submitted.get("accepted")), (0, True), submitted)
        self.assertTrue(self.rounds(), "FACT: dispatch wrote a host round")
        self.until(lambda: self.exits() == 1, message="S exit")
        self.assertTrue((self.hroot / ".wire-hit").exists(), "hostgate cut its reply after DATA")
        held, = self.rounds()
        self.read_round(submitted["job"], "held")
        self.assertEqual((held["state"], held["reason"], held["key"]), ("held", "channel_lost", None),
                         "FACT: no positive evidence, held")
        self.assertEqual(len(self.hstate()["reservations"]), 1, "FACT: hostgate committed one reservation")
        offers = self.log(".offers")
        self.stop_server()
        self.start_server()
        code, recovered = self.call("recover")
        self.assertEqual(code, 3, recovered)
        self.assertIn(submitted["attempt"], [entry["attempt"] for entry in recovered["still_uncertain"]])
        self.assertEqual(self.log(".offers"), offers, "FACT: recover made no second offer")
        self.assertEqual(self.log(".reservar"), [held["operation_id"]])
        self.assertEqual([(r["state"], r["reason"]) for r in self.rounds()], [("held", "channel_lost")])
        self.read_round(submitted["job"], "held")
        hstate = self.hstate()
        self.assertEqual((len(hstate["reservations"]), hstate["budget"]["retained"]["slots"]), (1, 1))

    def test_supervisor_death_retains_confirmation_without_adoption(self):
        from fleet import native
        (self.hroot / ".pause-confirmed").touch()
        code, submitted = self.submit()
        self.assertTrue(self.rounds(), "FACT: dispatch wrote a host round")
        self.until(lambda: (self.hroot / ".at-confirmed").exists(), message="S confirmed before death")
        self.read_round(submitted["job"], "confirmed")
        confirmed, = self.rounds()
        self.assertEqual([r["key"] for r in self.hstate()["reservations"]], [confirmed["key"]],
                         "H committed the confirmation before the cut")
        identity = json.loads(self.ledger("SELECT supervisor_identity FROM host_rounds")[0][0])
        self.assertTrue(native.confirmed(identity))
        self.assertEqual(identity["pid"], int((self.hroot / ".at-confirmed").read_text()))
        os.kill(identity["pid"], signal.SIGKILL)
        self.until(lambda: not native.confirmed(identity), message="original S is dead")
        offers = self.log(".offers")
        code, recovered = self.call("recover")
        self.assertEqual(code, 3, recovered)
        self.read_round(submitted["job"], "held")
        held, = self.rounds()
        self.assertEqual((held["reason"], held["key"], held["receipt"], held["source"]),
                         ("supervisor_lost", confirmed["key"], confirmed["receipt"], "reply"))
        self.assertEqual(self.log(".offers"), offers, "FACT: no PID adoption or second offer")
        self.assertEqual(self.attempt(submitted["job"])["gate"], "closed")
        self.assertEqual(self.ledger("SELECT attempt,generation FROM slot"), [(submitted["attempt"], 1)])
        self.assertEqual(self.hstate()["budget"]["retained"]["slots"], 1)
        self.assertFalse((self.root / "counter").exists())


if __name__ == "__main__":
    unittest.main()
