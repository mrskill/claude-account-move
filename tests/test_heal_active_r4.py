"""Round 4 tests for heal-active: one current pair per tick, no symlinks inside
the moved tree, a protected journal step, one pair function in finish."""
import argparse
import os
import sys
import unittest
from datetime import timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from fixture import CC, LAM, uid  # noqa: E402
from test_heal_active import Chain, check_healed  # noqa: E402
from test_heal_active_r3 import (Base, O1, O2, O3, X, Y, shape)  # noqa: E402
from claude_account_move import cli, heal_active  # noqa: E402

A, B = O1, O2


class PingPong(Base):
    def test_old_error_never_takes_the_folder_from_the_current_pair(self):
        real = self.link_pair(CC, X, A, O3)            # A -> O3 (real)
        self.enotdir(CC, X, A, timedelta(minutes=4))
        code, text = self.run_cli("--from-log", "--apply")
        self.assertEqual(code, 0, text)
        self.assertFalse(os.path.islink(self.sp(CC, X, A)))
        # the org switches to B (a link to A), no "account changed" line
        os.symlink(self.sp(CC, X, A), self.sp(CC, X, B))
        self.enotdir(CC, X, B, timedelta(minutes=2))
        code, text = self.run_cli("--from-log", "--apply")
        self.assertEqual(code, 0, text)
        self.assertFalse(os.path.islink(self.sp(CC, X, B)))
        self.assertTrue(os.path.islink(self.sp(CC, X, A)))
        before = shape(self.h.support)
        for _ in range(3):                              # old A line still in window
            code, text = self.run_cli("--from-log", "--apply")
            self.assertEqual(code, 0, text)
            self.assertEqual(shape(self.h.support), before)
        self.assertTrue(os.path.isdir(self.sp(CC, X, B))
                        and not os.path.islink(self.sp(CC, X, B)))

    def test_newest_line_of_any_kind_chooses_the_pair(self):
        self.link_pair(CC, X, A, O3)
        self.enotdir(CC, X, A, timedelta(minutes=4))
        self.okline(CC, X, B, timedelta(seconds=10))   # the app writes to B
        before = shape(self.h.support)
        code, text = self.run_cli("--from-log", "--apply")
        self.assertEqual(code, 0, text)
        self.assertEqual(shape(self.h.support), before)
        self.assertIn("skipped", text)

    def test_older_pair_error_after_current_one_is_not_touched(self):
        self.link_pair(CC, X, A, O3)
        self.link_pair(CC, X, B, uid(0x61, 1))
        self.enotdir(CC, X, A, timedelta(minutes=4))
        self.enotdir(CC, X, B, timedelta(minutes=1))   # B is the newest
        code, text = self.run_cli("--from-log", "--apply")
        self.assertEqual(code, 0, text)
        self.assertFalse(os.path.islink(self.sp(CC, X, B)))
        self.assertTrue(os.path.islink(self.sp(CC, X, A)))


class AnySymlink(Base):
    def test_dir_dot_alias_and_escaping_link_refuse(self):
        ch = Chain(self.h)
        r = ch.real[CC]
        os.symlink(".", os.path.join(r, "d"))
        os.symlink("d/../shared/local_x.json", os.path.join(r, "local_x.json"))
        before = shape(self.h.support)
        code, text = self.run_cli("--apply")
        self.assertNotEqual(code, 0, text)
        self.assertIn("symlink", text)
        self.assertEqual(shape(self.h.support), before)

    def test_absolute_symlink_refuses(self):
        ch = Chain(self.h)
        os.symlink("/etc/hosts", os.path.join(ch.real[CC], "abs.json"))
        before = shape(self.h.support)
        code, text = self.run_cli("--apply")
        self.assertNotEqual(code, 0, text)
        self.assertEqual(shape(self.h.support), before)

    def test_dot_link_alone_refuses(self):
        ch = Chain(self.h)
        os.symlink(".", os.path.join(ch.real[LAM], "d"))
        self.assertNotEqual(self.run_cli("--apply")[0], 0)


class ProtectedStep(Base):
    def rows(self):
        ch = Chain(self.h)
        return ch, [r for r in heal_active.plan(self.paths, (ch.acc, ch.org))
                    if r["state"] == "heal"]

    @staticmethod
    def failing_save(real_save):
        def save(paths, doc):
            if any(op["step"] == "swapped" for op in doc["ops"]):
                raise OSError(28, "No space left on device")
            real_save(paths, doc)
        return save

    def test_journal_failure_after_the_swap_is_rolled_back_and_verified(self):
        ch, rows = self.rows()
        before = shape(self.h.support)
        with mock.patch.object(heal_active, "_save_journal",
                               self.failing_save(heal_active._save_journal)):
            done, err, rb = heal_active.apply(self.paths, rows)
        self.assertEqual((done, rb), ([], []))
        self.assertIn("No space", err)
        self.assertEqual(shape(self.h.support), before)
        for s in (CC, LAM):
            self.assertTrue(os.path.islink(ch.active[s]))
            self.assertEqual(os.path.realpath(ch.active[s]), os.path.realpath(ch.real[s]))
        self.assertFalse(os.path.exists(self.journal()))

    def test_unverified_rollback_keeps_the_journal_and_never_says_rolled_back(self):
        ch, rows = self.rows()
        real_swap = heal_active.rename_swap
        calls = []

        def swap(x, y):
            calls.append(1)
            if len(calls) >= 2:                          # the undo swap fails
                raise OSError(28, "No space left on device")
            real_swap(x, y)
        with mock.patch.object(heal_active, "_save_journal",
                               self.failing_save(heal_active._save_journal)), \
                mock.patch.object(heal_active, "rename_swap", swap):
            code, text = self.run_cli("--apply")
        self.assertNotEqual(code, 0, text)
        self.assertIn("FAILED", text)
        self.assertNotIn("rolled back (verified)", text)
        self.assertTrue(os.path.exists(self.journal()))


class FinishPair(Base):
    def args(self):
        return argparse.Namespace(active_pair=None)

    def setup_two_orgs(self):
        self.link_pair(CC, X, A, O3)                    # X/A -> X/O3
        self.h.pair(LAM, X, O3)
        self.identity(app=X, cli_acc=X, cli_org=uid(0x77, 7))   # CLI org absent

    def test_historic_log_pair_that_is_a_symlink_fails_the_guard(self):
        self.setup_two_orgs()
        self.okline(CC, X, A, timedelta(hours=2))
        self.logline("2026-10-25 10:00:00 [info] [connector-owner] account changed")
        report = {"checks": []}
        code = cli._symlink_guard(self.paths, self.args(), report, 0)
        self.assertEqual(code, 3)
        self.assertFalse(report["checks"][-1]["ok"])
        self.assertIn(self.sp(CC, X, A), report["checks"][-1]["detail"])

    def test_undetermined_pair_adds_a_failed_check(self):
        self.setup_two_orgs()                           # two orgs, no log line
        report = {"checks": []}
        code = cli._symlink_guard(self.paths, self.args(), report, 0)
        self.assertEqual(code, 3)
        self.assertFalse(report["checks"][-1]["ok"])
        self.assertIn("cannot be determined", report["checks"][-1]["detail"])

    def test_determined_real_pair_passes(self):
        self.h.pair(CC, X, O3)
        self.h.pair(LAM, X, O3)
        self.identity(app=X, cli_acc=X, cli_org=O3)
        report = {"checks": []}
        self.assertEqual(cli._symlink_guard(self.paths, self.args(), report, 0), 0)
        self.assertEqual(report["checks"], [])


if __name__ == "__main__":
    unittest.main()
