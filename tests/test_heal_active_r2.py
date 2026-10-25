"""Round 2 tests for heal-active: the pair comes from the app log, --from-log."""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from fixture import CC, LAM, Home, tree_digest, uid  # noqa: E402
from test_heal_active import STUB_OPEN, Chain, check_healed, set_identity  # noqa: E402

APP = uid(0x41, 1)      # the app account (4183be94 in the field)
REAL_ORG = uid(0x25, 2)  # the org the app really writes to (252e7efa)
CLI_ORG = uid(0x52, 3)   # the CLI org (52bfdf75), unrelated to the app pair


def stamp(age):
    return (datetime.now() - age).strftime("%Y-%m-%d %H:%M:%S")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-heal-r2-")
        self.h = Home(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def logline(self, text):
        d = os.path.join(self.h.home, "Library", "Logs", "Claude")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "main.log"), "a") as fh:
            fh.write(text + "\n")

    def enotdir(self, store, acc, org, age, tasks=False):
        path = os.path.join(self.h.support, store, acc, org)
        if tasks:
            self.logline("%s [error] [CCDScheduledTasks] Failed to save scheduled "
                         "tasks: ENOTDIR: not a directory, open '%s/scheduled-tasks"
                         ".json.tmp'" % (stamp(age), path))
        else:
            self.logline("%s [error] Failed to save session local_x: ENOTDIR: not a "
                         "directory, mkdir '%s' at ensureStorageDir" % (stamp(age), path))
        return path

    def heal(self, *a, **e):
        return self.h.run("heal-active", *a, **e)


class LogPair(Base):
    def test_mismatched_identity_chooses_the_log_pair(self):
        ch = Chain(self.h, acc=APP, org=REAL_ORG)
        set_identity(self.h, APP, CLI_ORG)               # CLI org is another org
        self.logline("%s [info] [connector-owner] account changed" % stamp(timedelta(hours=1)))
        self.enotdir(CC, APP, REAL_ORG, timedelta(hours=1))
        r = self.heal("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for s in (CC, LAM):
            check_healed(ch, s)
            self.assertFalse(os.path.lexists(os.path.join(self.h.support, s, APP, CLI_ORG)))

    def test_log_line_before_account_change_is_ignored(self):
        ch = Chain(self.h, acc=APP, org=REAL_ORG)
        stale = uid(0x99, 1)
        self.enotdir(CC, APP, stale, timedelta(hours=3))
        self.logline("%s [info] [connector-owner] account changed" % stamp(timedelta(hours=2)))
        # no pair line after the change: falls back to app account + CLI org
        set_identity(self.h, APP, REAL_ORG)
        r = self.heal("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for s in (CC, LAM):
            check_healed(ch, s)

    def test_disagreement_without_log_refuses(self):
        Chain(self.h, acc=APP, org=REAL_ORG)
        os.makedirs(os.path.join(self.h.support, CC, APP, uid(0x26, 4)))
        set_identity(self.h, APP, CLI_ORG)
        before = tree_digest(self.h.support)
        r = self.heal("--apply")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("disagree", r.stdout.lower())
        self.assertIn(APP, r.stdout)
        self.assertIn(CLI_ORG, r.stdout)
        self.assertEqual(tree_digest(self.h.support), before)
        self.assertNotIn("%s/%s" % (APP, CLI_ORG), r.stdout)

    def test_explicit_active_pair_overrides_disagreement(self):
        ch = Chain(self.h, acc=APP, org=REAL_ORG)
        set_identity(self.h, APP, CLI_ORG)
        r = self.heal("--apply", "--active-pair", "%s/%s" % (APP, REAL_ORG))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for s in (CC, LAM):
            check_healed(ch, s)


class FromLog(Base):
    def test_heals_while_the_app_is_running(self):
        ch = Chain(self.h, acc=APP, org=REAL_ORG)
        self.enotdir(CC, APP, REAL_ORG, timedelta(minutes=2))
        self.enotdir(LAM, APP, REAL_ORG, timedelta(minutes=1), tasks=True)
        r = self.heal("--from-log", "--apply", CLAUDE_ACCOUNT_MOVE_PGREP=STUB_OPEN)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for s in (CC, LAM):
            check_healed(ch, s)

    def test_apply_without_from_log_still_refuses_while_running(self):
        Chain(self.h, acc=APP, org=REAL_ORG)
        before = tree_digest(self.h.support)
        r = self.heal("--apply", CLAUDE_ACCOUNT_MOVE_PGREP=STUB_OPEN)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(tree_digest(self.h.support), before)

    def test_lines_older_than_ten_minutes_are_ignored(self):
        Chain(self.h, acc=APP, org=REAL_ORG)
        self.enotdir(CC, APP, REAL_ORG, timedelta(minutes=11))
        before = tree_digest(self.h.support)
        r = self.heal("--from-log", "--apply", CLAUDE_ACCOUNT_MOVE_PGREP=STUB_OPEN)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nothing to do", r.stdout.lower())
        self.assertEqual(tree_digest(self.h.support), before)

    def test_scheduled_tasks_line_shape_is_recognised(self):
        ch = Chain(self.h, acc=APP, org=REAL_ORG)
        self.enotdir(CC, APP, REAL_ORG, timedelta(minutes=1), tasks=True)
        r = self.heal("--from-log", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        check_healed(ch, CC)
        self.assertTrue(os.path.islink(ch.active[LAM]))   # no line for LAM: untouched

    def test_pair_already_real_is_nothing_to_do(self):
        Chain(self.h, acc=APP, org=REAL_ORG)
        self.enotdir(CC, APP, REAL_ORG, timedelta(minutes=1))
        self.assertEqual(self.heal("--from-log", "--apply").returncode, 0)
        before = tree_digest(self.h.support)
        r = self.heal("--from-log", "--apply")
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing to do", r.stdout.lower())
        self.assertEqual(tree_digest(self.h.support), before)

    def test_dangling_pair_blocks_everything(self):
        ch = Chain(self.h, acc=APP, org=REAL_ORG)
        shutil.rmtree(ch.real[LAM])
        self.enotdir(CC, APP, REAL_ORG, timedelta(minutes=1))
        self.enotdir(LAM, APP, REAL_ORG, timedelta(minutes=1))
        before = tree_digest(self.h.support)
        r = self.heal("--from-log", "--apply")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(tree_digest(self.h.support), before)


class Deploy(unittest.TestCase):
    def test_plist_runs_from_log_every_minute(self):
        p = os.path.join(os.path.dirname(HERE), "deploy", "com.neurovision.heal-active.plist")
        with open(p) as fh:
            t = fh.read()
        self.assertIn("heal-active --from-log --apply", t)
        self.assertIn("<integer>60</integer>", t)
        self.assertIn("heal-active.log", t)


if __name__ == "__main__":
    unittest.main()
