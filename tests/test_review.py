"""Cases that came out of the pre-release review (wait vs fail, missing store,
settings loss, task baseline, crash-safe journal)."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import CC, LAM, Home, uid  # noqa: E402
from test_cli import A, B, O1, O2, build_source, parse, switch_to_target  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-review-")
        self.h = Home(self.tmp)
        self.hb = os.path.join(self.tmp, "heartbeat")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class WaitVersusFail(Base):
    def test_live_sync_job_means_wait_dead_one_means_fail(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        open(self.hb, "w").close()
        r = self.h.run("finish", "--wait-login", "0", "--wait-sync", "0",
                       "--heartbeat", self.hb, "--json")
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(parse(r)["verdict"], "wait")
        old = time.time() - 7200
        os.utime(self.hb, (old, old))
        r = self.h.run("finish", "--wait-login", "0", "--wait-sync", "0",
                       "--heartbeat", self.hb)
        self.assertEqual(r.returncode, 3)

    def test_wait_ends_with_success_once_delivered(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        open(self.hb, "w").close()
        import subprocess
        from fixture import ENTRY
        p = subprocess.Popen([sys.executable, ENTRY, "finish", "--wait-login", "0",
                              "--wait-sync", "20", "--heartbeat", self.hb, "--json"],
                             env=self.h.env(), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        time.sleep(1.0)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out)


class MissingTargetStore(Base):
    def test_agent_store_without_target_directory_fails_then_sync_creates_it(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        self.h.pair(CC, B, O2)
        self.h.accounts(cli=B, app=B)
        self.h.log((A, O1), (B, O2))
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        lam_target = os.path.join(self.h.support, LAM, B, O2)
        self.assertTrue(os.path.isdir(lam_target))
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 0)
        shutil.rmtree(os.path.join(self.h.support, LAM, B))
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual(r.returncode, 3)
        bad = [c for c in parse(r)["checks"] if not c["ok"]]
        self.assertTrue(any(c["id"] == LAM + ":target_names" for c in bad))


class Settings(Base):
    def put(self, data):
        d = os.path.join(self.h.home, ".claude")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "settings.json"), "w") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))

    def test_settings_lost_or_broken_after_the_move_fail(self):
        build_source(self.h)
        self.put({"permissions": {"allow": ["a", "b"]}, "cleanupPeriodDays": 30,
                  "hooks": {"Stop": [{"hooks": [{"command": "x"}]}]}})
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.h.run("sync", "--apply")
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(any("cleanupPeriodDays" in n for n in parse(r)["notes"]))
        os.unlink(os.path.join(self.h.home, ".claude", "settings.json"))
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 3)
        self.put("{broken")
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 3)
        self.put({"permissions": {"allow": ["a"]}, "cleanupPeriodDays": 30,
                  "hooks": {"Stop": [{"hooks": [{"command": "x"}]}]}})
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 3)

    def test_no_settings_at_all_is_fine(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.h.run("sync", "--apply")
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 0)


class OtherHome(Base):
    def test_snapshot_of_another_home_is_rejected_and_holds_no_home_path(self):
        build_source(self.h)
        rep = parse(self.h.run("prepare", "--json"))
        man = os.path.join(self.h.state, "moves", rep["move_id"], "manifest.json")
        with open(man) as fh:
            self.assertNotIn(self.h.home, fh.read())
        other = Home(os.path.join(self.tmp, "second"))
        other.state = self.h.state
        build_source(other)
        r = other.run("finish", "--wait-login", "0", "--allow-same-account",
                      "--move-id", rep["move_id"])
        self.assertEqual(r.returncode, 7)


class Tasks(Base):
    def put_tasks(self, d, tasks):
        with open(os.path.join(d, "scheduled-tasks.json"), "w") as fh:
            fh.write(tasks if isinstance(tasks, str) else json.dumps(tasks))

    def test_broken_tasks_file_makes_the_snapshot_not_ready(self):
        d = build_source(self.h)
        self.put_tasks(d, "{broken")
        r = self.h.run("prepare", "--json")
        self.assertEqual(r.returncode, 3)
        self.assertFalse(parse(r)["ready"])
        obs = [c for c in parse(r)["checks"] if c["id"] == "observation"][0]
        self.assertFalse(obs["ok"])

    def test_recorded_tasks_must_arrive(self):
        d = build_source(self.h)
        self.put_tasks(d, {"scheduledTasks": [{"id": "t1", "enabled": True,
                                               "approvedPermissions": ["x"]}]})
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.h.run("sync", "--apply")
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual(r.returncode, 3)
        self.assertTrue(any(c["id"] == "tasks" and not c["ok"]
                            for c in parse(r)["checks"]))
        self.put_tasks(os.path.join(self.h.support, CC, B, O2),
                       {"scheduledTasks": [{"id": "t1", "enabled": True,
                                            "approvedPermissions": ["x"]}]})
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 0)


class Journal(Base):
    def test_existing_destination_is_never_replaced(self):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from claude_account_move import sync_cards
        src = os.path.join(self.tmp, "local_a.json")
        dst_dir = os.path.join(self.tmp, "dst")
        os.makedirs(dst_dir)
        with open(src, "w") as fh:
            fh.write("new")
        with open(os.path.join(dst_dir, "local_a.json"), "w") as fh:
            fh.write("old")           # appeared after the plan was made
        copied, failed = sync_cards.apply_plan(
            [{"store": CC, "name": "local_a.json", "src": src, "dst_dir": dst_dir,
              "create_dir": False}], os.path.join(self.tmp, "j.jsonl"))
        self.assertEqual((copied, failed), (0, 1))
        with open(os.path.join(dst_dir, "local_a.json")) as fh:
            self.assertEqual(fh.read(), "old")
        self.assertEqual(os.listdir(dst_dir), ["local_a.json"])

    def test_journal_has_intent_before_result_and_undo_is_exact(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        jpath = os.path.join(self.h.state, [f for f in os.listdir(self.h.state)
                                            if f.startswith("sync-")][0])
        with open(jpath) as fh:
            recs = [json.loads(line) for line in fh]
        ops = [r["op"] for r in recs]
        self.assertEqual(ops, ["begin"] + ["intent", "done"] * 4)
        with open(jpath, "w") as fh:          # simulate a crash after the 1st intent
            fh.write(json.dumps(recs[0]) + "\n" + json.dumps(recs[1]) + "\n")
        target = os.path.join(self.h.support, CC, B, O2)
        before = sorted(os.listdir(target))
        r = self.h.run("sync", "--undo", jpath)
        self.assertEqual(r.returncode, 0)
        after = sorted(os.listdir(target))
        self.assertEqual(len(after), len(before) - (1 if recs[1]["path"].startswith(os.path.realpath(target)) else 0))

    def test_conflicting_session_ids_are_reported_not_copied(self):
        src = build_source(self.h)
        other = self.h.pair(CC, uid(5), uid(6))
        self.h.card(other, 11, uid(99))         # same card name, different session
        self.assertEqual(self.h.run("prepare").returncode, 3)   # also a duplicate
        switch_to_target(self.h)
        r = self.h.run("sync", "--apply")
        self.assertEqual(r.returncode, 3)
        self.assertIn("conflict, not copied", r.stdout)
        target = os.path.join(self.h.support, CC, B, O2)
        self.assertNotIn("local_%s.json" % uid(11, 9), os.listdir(target))

    def test_newest_copy_with_a_session_id_beats_a_newer_empty_one(self):
        src = build_source(self.h)
        other = self.h.pair(CC, uid(5), uid(6))
        self.h.card(other, 11, "", act=int(time.time() * 1000) + 10 ** 6)
        switch_to_target(self.h)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        target = os.path.join(self.h.support, CC, B, O2)
        with open(os.path.join(target, "local_%s.json" % uid(11, 9))) as fh:
            self.assertEqual(json.load(fh)["cliSessionId"], uid(11))


if __name__ == "__main__":
    unittest.main()
