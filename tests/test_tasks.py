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


OPEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub_pgrep_open.sh")


def read_bytes(p):
    with open(p, "rb") as fh:
        return fh.read()


class TaskRework(Base):
    def setup_move(self, extra=None):
        src = build_source(self.h)
        put(src, [task("t1"), task("t2", created="1787838292655")])
        if extra:
            extra(src)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        dst = os.path.join(self.h.support, CC, B, O2)
        return src, dst

    def test_apply_refused_while_app_runs_dry_run_allowed(self):
        src, dst = self.setup_move()
        put(dst, [task("own")])
        before = read_bytes(os.path.join(dst, NAME))
        r = self.h.run("sync", "--apply", CLAUDE_ACCOUNT_MOVE_PGREP=OPEN)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Quit the Claude app", r.stdout + r.stderr)
        self.assertEqual(read_bytes(os.path.join(dst, NAME)), before)
        self.assertEqual(self.h.run("sync", CLAUDE_ACCOUNT_MOVE_PGREP=OPEN).returncode, 0)

    def test_undo_restores_exact_bytes_and_removes_created_file(self):
        src, dst = self.setup_move()
        raw = b'{"scheduledTasks":[{"id":"own","createdAt":"7"}],\r\n"x":"\xc3\xa9"}\r\n'
        with open(os.path.join(dst, NAME), "wb") as fh:
            fh.write(raw)
        lam = os.path.join(self.h.support, "local-agent-mode-sessions", B, O2)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        self.assertNotEqual(read_bytes(os.path.join(dst, NAME)), raw)
        journal = [f for f in os.listdir(self.h.state) if f.startswith("sync-")][0]
        r = self.h.run("sync", "--undo", os.path.join(self.h.state, journal))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(read_bytes(os.path.join(dst, NAME)), raw)
        self.assertEqual([f for f in os.listdir(dst) if ".bak-move-" in f], [])

    def test_undo_removes_a_file_it_created(self):
        src, dst = self.setup_move()
        self.assertFalse(os.path.exists(os.path.join(dst, NAME)))
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(dst, NAME)))
        journal = [f for f in os.listdir(self.h.state) if f.startswith("sync-")][0]
        self.assertEqual(self.h.run("sync", "--undo",
                                    os.path.join(self.h.state, journal)).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(dst, NAME)))

    def test_undo_keeps_a_file_changed_after_apply(self):
        src, dst = self.setup_move()
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        put(dst, [task("user-edit")])
        journal = [f for f in os.listdir(self.h.state) if f.startswith("sync-")][0]
        r = self.h.run("sync", "--undo", os.path.join(self.h.state, journal))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([t["id"] for t in get(dst)], ["user-edit"])

    def test_tasks_written_into_a_directory_cards_create(self):
        src = build_source(self.h)
        put(src, [task("t1")])
        lam = os.path.join(self.h.support, "local-agent-mode-sessions", A_, O1_)
        put(lam, [task("l1")])
        self.assertEqual(self.h.run("prepare").returncode, 0)
        self.h.pair(CC, B, O2)                    # no local-agent-mode dir yet
        self.h.accounts(cli=B, app=B)
        self.h.log((A_, O1_), (B, O2))
        newdir = os.path.join(self.h.support, "local-agent-mode-sessions", B, O2)
        self.assertFalse(os.path.exists(newdir))
        r = self.h.run("sync", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([t["id"] for t in get(newdir)], ["l1"])

    def test_other_org_of_target_account_is_a_source(self):
        def extra(src):
            other = self.h.pair(CC, B, O3)
            put(other, [task("b1")])
        src, dst = self.setup_move(extra)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        self.assertIn("b1", [t["id"] for t in get(dst)])
        self.assertEqual([t["id"] for t in get(os.path.join(
            self.h.support, CC, B, O3))], ["b1"])      # source untouched

    def test_ids_not_in_the_snapshot_are_not_resurrected(self):
        src, dst = self.setup_move()
        put(src, [task("t1"), task("t2"), task("late")])     # appeared after prepare
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        self.assertEqual(sorted(t["id"] for t in get(dst)), ["t1", "t2"])

    def test_source_account_copy_beats_a_newer_foreign_copy(self):
        def extra(src):
            c = self.h.pair(CC, C_, O4_)
            e = task("t1")
            e["name"] = "foreign"
            put(c, [e])
        src, dst = self.setup_move(extra)
        mine = task("t1")
        mine["name"] = "source"
        put(src, [mine, task("t2")])
        os.utime(os.path.join(src, NAME), (1000, 1000))     # older than the foreign copy
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        names = {t["id"]: t.get("name") for t in get(dst)}
        self.assertEqual(names["t1"], "source")

    def test_one_validator_serves_sync_and_finish(self):
        from claude_account_move import store, sync_cards
        e = {"id": "x", "createdAt": "12", "enabled": "yes"}
        issues = store.task_type_issues(e)
        self.assertEqual(sorted(f for f, _ in issues), ["createdAt", "enabled"])
        self.assertTrue(sync_cards.coerce_task(e))
        self.assertEqual(e["createdAt"], 12)
        self.assertEqual([f for f, _ in store.task_type_issues(e)], ["enabled"])


from test_cli import A as A_, O1 as O1_  # noqa: E402
from fixture import uid  # noqa: E402
O3, C_, O4_ = uid(5), uid(6), uid(8)


if __name__ == "__main__":
    unittest.main()
