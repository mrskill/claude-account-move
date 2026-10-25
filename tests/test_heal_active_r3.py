"""Round 3 tests for heal-active: stale log lines, ambiguous orgs, atomic swap,
relative links, stale plans, the step journal and its recovery."""
import contextlib
import fcntl
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from fixture import CC, LAM, Home, tree_digest, uid  # noqa: E402
from test_heal_active import Chain, check_healed  # noqa: E402
from claude_account_move import cli, common, heal_active  # noqa: E402

X = uid(0x31, 1)
O1, O2, O3 = uid(0x32, 2), uid(0x33, 3), uid(0x34, 4)
Y = uid(0x35, 5)


def stamp(age):
    return (datetime.now() - age).strftime("%Y-%m-%d %H:%M:%S")


def shape(root):
    """Names, link targets and file bytes; directory mtimes ignored."""
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            p = os.path.join(dp, n)
            if os.path.islink(p):
                out[p] = ("link", os.readlink(p))
            elif os.path.isdir(p):
                out[p] = ("dir",)
            else:
                with open(p, "rb") as fh:
                    out[p] = ("file", fh.read())
    return out


class Crash(BaseException):
    """Simulates a process death: nothing in the tool catches it."""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-heal-r3-")
        self.h = Home(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = common.Paths(self.h.home, self.h.state)

    def sp(self, store, *parts):
        return os.path.join(self.h.support, store, *parts)

    def logline(self, text):
        d = os.path.join(self.h.home, "Library", "Logs", "Claude")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "main.log"), "a") as fh:
            fh.write(text + "\n")

    def enotdir(self, store, acc, org, age=timedelta(minutes=1)):
        self.logline("%s [error] Failed to save session local_x: ENOTDIR: not a "
                     "directory, mkdir '%s' at ensureStorageDir"
                     % (stamp(age), self.sp(store, acc, org)))

    def okline(self, store, acc, org, age=timedelta(seconds=30)):
        self.logline("%s [info] Saved session local_y to %s/local_y.json"
                     % (stamp(age), self.sp(store, acc, org)))

    def link_pair(self, store, acc, alias_org, real_org, cards=2):
        """alias_org -> real_org (a real dir with cards) under one account."""
        real = self.h.pair(store, acc, real_org)
        for k in range(cards):
            self.h.card(real, 700 + k, uid(700 + k))
        os.symlink(real, self.sp(store, acc, alias_org))
        return real

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        e = self.h.env()
        e.update(env or {})
        with mock.patch.dict(os.environ, e), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(["heal-active", "--home", self.h.home, "--state-dir",
                             self.h.state] + list(argv))
        return code, out.getvalue() + err.getvalue()

    def identity(self, app, cli_acc, cli_org):
        os.makedirs(self.h.support, exist_ok=True)
        with open(os.path.join(self.h.home, ".claude.json"), "w") as fh:
            json.dump({"oauthAccount": {"accountUuid": cli_acc,
                                        "organizationUuid": cli_org}}, fh)
        with open(os.path.join(self.h.support, "config.json"), "w") as fh:
            json.dump({"lastKnownAccountUuid": app}, fh)

    def journal(self):
        return os.path.join(self.h.state, "heal-active-journal.json")


class StaleLogLines(Base):
    """S0-1"""

    def test_error_overtaken_by_a_write_to_another_pair_is_skipped(self):
        real = self.link_pair(CC, X, O1, O2)        # O1 -> O2 (old alias)
        self.enotdir(CC, X, O1, timedelta(minutes=2))
        self.okline(CC, X, O2)                       # the app now writes to O2
        before = tree_digest(self.h.support)
        code, text = self.run_cli("--from-log", "--apply")
        self.assertEqual(code, 0, text)
        self.assertEqual(tree_digest(self.h.support), before)
        self.assertTrue(os.path.islink(self.sp(CC, X, O1)))
        self.assertTrue(os.path.isdir(real) and not os.path.islink(real))
        self.assertIn("skipped", text.lower())

    def test_error_before_an_account_change_is_ignored(self):
        self.link_pair(CC, X, O1, O2)
        self.enotdir(CC, X, O1, timedelta(minutes=3))
        self.logline("%s [info] [connector-owner] account changed"
                     % stamp(timedelta(minutes=2)))
        before = tree_digest(self.h.support)
        code, text = self.run_cli("--from-log", "--apply")
        self.assertEqual(code, 0, text)
        self.assertEqual(tree_digest(self.h.support), before)

    def test_error_followed_by_a_write_to_the_same_pair_still_heals(self):
        self.link_pair(CC, X, O1, O2)
        self.enotdir(CC, X, O1, timedelta(minutes=2))
        self.okline(CC, X, O1)
        code, text = self.run_cli("--from-log", "--apply")
        self.assertEqual(code, 0, text)
        self.assertFalse(os.path.islink(self.sp(CC, X, O1)))
        self.assertTrue(os.path.islink(self.sp(CC, X, O2)))


class OrgChoice(Base):
    """S0-2"""

    def setUp(self):
        Base.setUp(self)
        self.link_pair(CC, X, O2, O1)                # X/O1 real, X/O2 -> O1
        self.h.pair(LAM, X, O1)

    def test_cli_on_another_account_never_picks_the_org(self):
        self.identity(app=X, cli_acc=Y, cli_org=O2)
        before = tree_digest(self.h.support)
        code, text = self.run_cli("--apply")
        self.assertNotEqual(code, 0, text)
        self.assertIn("disagree", text.lower())
        self.assertEqual(tree_digest(self.h.support), before)
        self.assertTrue(os.path.islink(self.sp(CC, X, O2)))

    def test_several_orgs_and_no_log_refuse_even_for_the_cli_org(self):
        self.identity(app=X, cli_acc=X, cli_org=O3)      # O3 does not exist
        before = tree_digest(self.h.support)
        code, text = self.run_cli("--apply")
        self.assertNotEqual(code, 0, text)
        self.assertEqual(tree_digest(self.h.support), before)

    def test_explicit_pair_is_accepted(self):
        self.identity(app=X, cli_acc=Y, cli_org=O2)
        code, text = self.run_cli("--apply", "--active-pair", "%s/%s" % (X, O2))
        self.assertEqual(code, 0, text)
        self.assertFalse(os.path.islink(self.sp(CC, X, O2)))


class AtomicSwap(Base):
    """S0-3"""

    def test_active_path_never_missing_and_inode_verified(self):
        ch = Chain(self.h)
        a = ch.active[CC]
        ino = os.stat(ch.real[CC]).st_ino
        seen = []
        real_swap = heal_active.rename_swap

        def spy(x, y):
            seen.append(os.path.lexists(a))
            real_swap(x, y)
            seen.append(os.path.lexists(a))
        real_unlink = os.unlink

        def no_unlink_of_a(p, *k, **kw):
            self.assertNotEqual(os.path.abspath(str(p)), os.path.abspath(a))
            return real_unlink(p, *k, **kw)
        with mock.patch.object(heal_active, "rename_swap", spy), \
                mock.patch("os.unlink", no_unlink_of_a):
            code, text = self.run_cli("--apply")
        self.assertEqual(code, 0, text)
        self.assertTrue(seen and all(seen), seen)
        self.assertEqual(os.stat(a).st_ino, ino)
        check_healed(ch, CC)

    def test_refuses_without_the_atomic_exchange(self):
        Chain(self.h)
        before = tree_digest(self.h.support)
        with mock.patch.object(heal_active, "_swap_fn",
                               side_effect=heal_active.SwapUnavailable("no")):
            code, text = self.run_cli("--apply")
        self.assertNotEqual(code, 0, text)
        self.assertIn("atomic", text.lower())
        self.assertEqual(tree_digest(self.h.support), before)

    def test_no_leftover_temp_links(self):
        ch = Chain(self.h)
        self.assertEqual(self.run_cli("--apply")[0], 0)
        for s in (CC, LAM):
            base = os.path.dirname(ch.real[s])
            self.assertEqual([n for n in os.listdir(base) if n.startswith(".")], [])


class RelativeLinks(Base):
    """S0-4"""

    def test_relative_link_leaving_the_folder_refuses(self):
        ch = Chain(self.h)
        os.symlink("../shared/local_z.json", os.path.join(ch.real[CC], "local_z.json"))
        before = tree_digest(self.h.support)
        code, text = self.run_cli("--apply")
        self.assertNotEqual(code, 0, text)
        self.assertIn("relative symlink", text)
        self.assertEqual(tree_digest(self.h.support), before)

    def test_relative_link_inside_the_folder_is_fine(self):
        ch = Chain(self.h)
        os.symlink(ch.names[CC][0], os.path.join(ch.real[CC], "alias.json"))
        code, text = self.run_cli("--apply")
        self.assertEqual(code, 0, text)
        self.assertEqual(os.path.realpath(os.path.join(ch.active[CC], "alias.json")),
                         os.path.realpath(os.path.join(ch.active[CC], ch.names[CC][0])))


class StalePlan(Base):
    """S1-5"""

    def test_target_replaced_after_the_plan_aborts_untouched(self):
        ch = Chain(self.h)
        rows = [r for r in heal_active.plan(self.paths, (ch.acc, ch.org))
                if r["state"] == "heal"]
        r = rows[0]
        os.rename(r["target"], r["target"] + ".gone")     # a different folder now
        os.mkdir(r["target"])
        code_before = tree_digest(self.h.support)
        done, err, rb = heal_active.apply(self.paths, rows)
        self.assertEqual((done, rb), ([], []))
        self.assertIn("inode", err)
        self.assertEqual(tree_digest(self.h.support), code_before)
        self.assertFalse(os.path.exists(self.journal()))

    def test_shared_target_keeps_only_the_confirmed_pair(self):
        real = self.link_pair(CC, X, O1, O3)
        os.symlink(real, self.sp(CC, X, O2))
        rows = [heal_active.plan_store(self.paths, CC, (X, o)) for o in (O1, O2)]
        kept = heal_active.dedupe(rows, (X, O2))
        self.assertEqual([r["pair"] for r in kept if r["state"] == "heal"], [[X, O2]])
        refused = heal_active.dedupe([heal_active.plan_store(self.paths, CC, (X, o))
                                      for o in (O1, O2)], None)
        self.assertTrue(all(r["state"] == "refuse" for r in refused))

    def test_two_pairs_sharing_a_target_with_no_current_one_change_nothing(self):
        real = self.link_pair(CC, X, O1, O3)
        os.symlink(real, self.sp(CC, X, O2))
        self.enotdir(CC, X, O1)
        self.enotdir(CC, X, O2)
        before = tree_digest(self.h.support)
        code, text = self.run_cli("--from-log", "--apply")
        self.assertNotEqual(code, 0, text)
        self.assertEqual(tree_digest(self.h.support), before)

    def test_busy_lock_blocks_before_any_plan(self):
        Chain(self.h)
        before = tree_digest(self.h.support)
        fd = os.open(os.path.realpath(self.h.support), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        r = self.h.run("heal-active", "--apply")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("another write operation", r.stdout + r.stderr)
        self.assertEqual(tree_digest(self.h.support), before)


class Journal(Base):
    """S1-6"""

    def two_rows(self):
        ch = Chain(self.h)
        rows = [r for r in heal_active.plan(self.paths, (ch.acc, ch.org))
                if r["state"] == "heal"]
        self.assertEqual(len(rows), 2)
        return ch, rows

    def test_journal_exists_before_the_first_change(self):
        ch, rows = self.two_rows()
        real_swap = heal_active.rename_swap
        seen = []

        def spy(x, y):
            seen.append(os.path.exists(self.journal()))
            real_swap(x, y)
        with mock.patch.object(heal_active, "rename_swap", spy):
            done, err, rb = heal_active.apply(self.paths, rows)
        self.assertIsNone(err)
        self.assertTrue(seen and all(seen))
        self.assertFalse(os.path.exists(self.journal()))      # closed on success

    def test_failure_in_the_second_store_rolls_the_first_back(self):
        ch, rows = self.two_rows()
        before = shape(self.h.support)
        real_swap = heal_active.rename_swap
        calls = []

        def swap(x, y):
            calls.append((x, y))
            if len(calls) == 2:
                raise OSError(28, "No space left on device")
            real_swap(x, y)
        with mock.patch.object(heal_active, "rename_swap", swap):
            done, err, rb = heal_active.apply(self.paths, rows)
        self.assertEqual((done, rb), ([], []))
        self.assertIn("No space", err)
        self.assertEqual(shape(self.h.support), before)
        for s in (CC, LAM):
            self.assertTrue(os.path.islink(ch.active[s]))
        self.assertFalse(os.path.exists(self.journal()))

    def test_failed_rollback_is_reported_with_paths_and_journal_kept(self):
        ch, rows = self.two_rows()
        real_swap = heal_active.rename_swap
        calls = []

        def swap(x, y):
            calls.append((x, y))
            if len(calls) >= 2:
                raise OSError(28, "No space left on device")
            real_swap(x, y)
        with mock.patch.object(heal_active, "rename_swap", swap):
            code, text = self.run_cli("--apply")
        self.assertNotEqual(code, 0, text)
        self.assertIn("FAILED", text)
        self.assertNotIn("everything was rolled back", text)
        self.assertIn(ch.active[CC], text)
        with open(self.journal()) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["status"], "failed")
        self.assertTrue(doc["errors"])

    def test_next_run_recovers_an_unfinished_journal_first(self):
        ch, rows = self.two_rows()
        real_swap = heal_active.rename_swap
        calls = []

        def swap(x, y):
            calls.append((x, y))
            if len(calls) >= 2:
                raise OSError(28, "No space left on device")
            real_swap(x, y)
        with mock.patch.object(heal_active, "rename_swap", swap):
            self.assertNotEqual(self.run_cli("--apply")[0], 0)
        self.assertTrue(os.path.exists(self.journal()))
        code, text = self.run_cli("--apply")          # real swaps again
        self.assertEqual(code, 0, text)
        self.assertIn("recovered", text)
        self.assertFalse(os.path.exists(self.journal()))
        for s in (CC, LAM):
            check_healed(ch, s)

    def test_crash_between_swap_and_link_is_completed_by_recovery(self):
        ch = Chain(self.h)
        rows = [r for r in heal_active.plan(self.paths, (ch.acc, ch.org))
                if r["state"] == "heal"][:1]
        real_rename = os.rename

        def boom(src, dst, *a, **k):
            if os.path.basename(str(src)).startswith(".heal-active-"):
                raise Crash()
            return real_rename(src, dst, *a, **k)
        with mock.patch("os.rename", boom):
            with self.assertRaises(Crash):
                heal_active.apply(self.paths, rows)
        self.assertTrue(os.path.exists(self.journal()))
        with open(self.journal()) as fh:
            self.assertEqual(json.load(fh)["ops"][0]["step"], "swapped")
        msgs, errs = heal_active.recover(self.paths)
        self.assertEqual(errs, [], errs)
        self.assertTrue(msgs)
        self.assertFalse(os.path.exists(self.journal()))
        check_healed(ch, rows[0]["store"])

    def test_unrecognised_state_is_an_error_and_keeps_the_journal(self):
        ch = Chain(self.h)
        rows = [r for r in heal_active.plan(self.paths, (ch.acc, ch.org))
                if r["state"] == "heal"][:1]
        with mock.patch("os.rename", side_effect=Crash()):
            with self.assertRaises(Crash):
                heal_active.apply(self.paths, rows)
        r = rows[0]
        shutil.rmtree(r["path"]) if os.path.isdir(r["path"]) and \
            not os.path.islink(r["path"]) else None
        os.unlink(r["target"]) if os.path.islink(r["target"]) else None
        msgs, errs = heal_active.recover(self.paths)
        self.assertTrue(errs)
        self.assertTrue(os.path.exists(self.journal()))
        code, text = self.run_cli("--apply")
        self.assertNotEqual(code, 0, text)


if __name__ == "__main__":
    unittest.main()
