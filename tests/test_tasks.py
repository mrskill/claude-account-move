import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import CC  # noqa: E402
from test_cli import B, O2, Base, build_source, parse, switch_to_target  # noqa: E402

NAME = "scheduled-tasks.json"


def put(d, tasks):
    with open(os.path.join(d, NAME), "w") as fh:
        json.dump({"scheduledTasks": tasks}, fh)


def get_path(p):
    with open(p) as fh:
        return json.load(fh)["scheduledTasks"]


def get(d):
    return get_path(os.path.join(d, NAME))


def task(tid, created=1787838292000, enabled=True):
    return {"id": tid, "enabled": enabled, "createdAt": created,
            "approvedPermissions": ["x"]}


class TaskMigration(Base):
    def setup_move(self):
        src = build_source(self.h)
        put(src, [task("t1"), task("t2", created="1787838292655"), task("t3")])
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        dst = os.path.join(self.h.support, CC, B, O2)
        put(dst, [task("own")])
        return src, dst

    def test_dry_run_writes_nothing(self):
        src, dst = self.setup_move()
        before = open(os.path.join(dst, NAME)).read()
        r = self.h.run("sync")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("scheduled tasks missing in the target: 3", r.stdout)
        self.assertEqual(open(os.path.join(dst, NAME)).read(), before)
        self.assertEqual([f for f in os.listdir(dst) if "bak" in f], [])

    def test_apply_merges_by_id_backs_up_and_coerces(self):
        src, dst = self.setup_move()
        r = self.h.run("sync", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tasks = get(dst)
        self.assertEqual(sorted(t["id"] for t in tasks), ["own", "t1", "t2", "t3"])
        for t in tasks:
            self.assertIsInstance(t["createdAt"], int)
        self.assertEqual({t["id"]: t["createdAt"] for t in tasks}["t2"],
                         1787838292655)
        baks = [f for f in os.listdir(dst) if f.startswith(NAME + ".bak-move-")]
        self.assertEqual(len(baks), 1)
        self.assertEqual([t["id"] for t in get_path(os.path.join(dst, baks[0]))],
                         ["own"])
        self.assertEqual([f for f in os.listdir(dst) if f.startswith(".tmp-tasks")], [])
        self.assertEqual(len(get(src)), 3)          # source untouched
        r2 = self.h.run("sync", "--apply")
        self.assertEqual(r2.returncode, 0, r2.stdout)
        self.assertEqual(len([f for f in os.listdir(dst)
                              if f.startswith(NAME + ".bak-move-")]), 1)

    def test_target_entry_wins_over_source_with_same_id(self):
        src, dst = self.setup_move()
        put(dst, [task("t1", created=5, enabled=False)])
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        t1 = [t for t in get(dst) if t["id"] == "t1"]
        self.assertEqual(len(t1), 1)
        self.assertEqual(t1[0]["createdAt"], 5)
        self.assertFalse(t1[0]["enabled"])

    def test_apply_creates_file_when_target_has_none(self):
        src, dst = self.setup_move()
        os.unlink(os.path.join(dst, NAME))
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        self.assertEqual(sorted(t["id"] for t in get(dst)), ["t1", "t2", "t3"])

    def test_finish_fails_on_wrong_types_and_names_the_task(self):
        src, dst = self.setup_move()
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for bad, field in ((task("t2", created="1787838292655"), "createdAt"),
                           (task("t2", enabled="yes"), "enabled")):
            put(dst, [task("t1"), bad, task("t3")])
            r = self.h.run("finish", "--wait-login", "0", "--json")
            self.assertNotEqual(r.returncode, 0, field)
            chk = [c for c in parse(r)["checks"] if c["id"] == "tasks"][0]
            self.assertFalse(chk["ok"], field)
            self.assertIn("t2", chk["detail"])
            self.assertIn(field, chk["detail"])


if __name__ == "__main__":
    unittest.main()
