"""ORACLE for `heal-active` and its guards (written before the implementation).

Defect measured 25.10.2026: after an account move the ACTIVE pair directory
<store>/<account>/<org> was a SYMLINK (chain 52a19267/52bfdf75 ->
e27f45d5/e17aefa9 -> 4183be94/252e7efa). The app reads through a symlinked pair
but every write fails with ENOTDIR (main.log: "Failed to save session local_...:
ENOTDIR: not a directory ... at ensureStorageDir"), so new session cards were
silently lost. Invariant: the active pair directory is never a symlink.

Specified here:
  1. `heal-active` (dry run; writes only with --apply; refuses while the app
     runs): active symlink whose realpath R is a real directory -> remove the
     symlink, move R to the active path, leave a symlink at R's old path that
     points to the active path. Every other alias still resolves, card count
     identical, cards moved (same inodes), nothing left behind.
  2. refuses (no write, non-zero) when realpath(active) is missing or outside
     the store; real directory -> exit 0 "nothing to do".
  3. the active account/org come from read_identity (app account + CLI org),
     or --active-pair; never hard-coded.
  4. `finish` fails (3) while the active pair of either store is a symlink,
     naming store and path, and tells to run `heal-active --apply` with the app
     quit. `auto` runs the heal step after login and before verification.
  5. `prepare` fails readiness on a recent (<= 2 days) ENOTDIR line of main.log
     that names a sessions-store pair which is still a symlink.

Class OracleSelfCheck proves the checker itself rejects the three wrong heals
(copy instead of move, old path left missing, one link level only); it needs no
implementation and is green by design. Everything else is red until
`heal-active` exists. Fake HOME only; the real ~/Library is never touched.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from fixture import CC, LAM, Home, tree_digest, uid  # noqa: E402
from test_cli import A, B, O1, O2, build_source, parse, switch_to_target  # noqa: E402

STORES = (CC, LAM)
ACC = uid(0x52, 1)          # active account  (52a19267 in the field report)
ORG = uid(0x53, 2)          # active organization (52bfdf75)
MID = (uid(0xe2, 3), uid(0xe1, 4))      # first link target  (e27f45d5/e17aefa9)
REAL = (uid(0x41, 5), uid(0x25, 6))     # the real directory (4183be94/252e7efa)
STUB_OPEN = os.path.join(HERE, "stub_pgrep_open.sh")


def set_identity(h, acc, org):
    """App account and CLI organization, the two sources read_identity uses."""
    os.makedirs(h.support, exist_ok=True)
    with open(os.path.join(h.home, ".claude.json"), "w") as fh:
        json.dump({"oauthAccount": {"accountUuid": acc, "organizationUuid": org}}, fh)
    with open(os.path.join(h.support, "config.json"), "w") as fh:
        json.dump({"lastKnownAccountUuid": acc}, fh)


def cards_via(path):
    return sorted(f for f in os.listdir(path) if f.startswith("local_"))


def resolves_to(link, target):
    """True when the symlink at `link` resolves to the same place as `target`."""
    return os.path.realpath(link) == os.path.realpath(target)


def listing(store_dir):
    """Every account/org entry name under a store, to prove nothing is left."""
    out = []
    for acc in sorted(os.listdir(store_dir)):
        d = os.path.join(store_dir, acc)
        out.append(acc)
        if os.path.isdir(d) and not os.path.islink(d):
            out.extend(acc + "/" + o for o in sorted(os.listdir(d)))
    return out


class Chain(object):
    """Active pair -> MID pair -> REAL pair, per store, plus extra aliases."""

    def __init__(self, h, acc=ACC, org=ORG, relative=False):
        self.h, self.acc, self.org = h, acc, org
        self.real, self.mid, self.active, self.other, self.far = {}, {}, {}, {}, {}
        self.names = {}
        self.inodes = {}
        n = int(acc[:8], 16)
        mid_names = (uid(n + 0x100, 3), uid(n + 0x101, 4))
        real_names = (uid(n + 0x200, 5), uid(n + 0x201, 6))
        by = uid(n + 0x300, 7)
        if acc == ACC:
            mid_names, real_names = MID, REAL
        for i, store in enumerate(STORES):
            base = os.path.join(h.support, store)
            r = os.path.join(base, *real_names)
            os.makedirs(r)
            self.real[store] = r
            self.names[store] = [h.card(r, (n & 0xfff) * 100 + 10 * i + k, uid(n + 10 * i + k))
                                 for k in range(3)]
            self.inodes[store] = {n: os.stat(os.path.join(r, n)).st_ino
                                  for n in self.names[store]}
            os.makedirs(os.path.join(base, mid_names[0]), exist_ok=True)
            m = os.path.join(base, *mid_names)
            self.mid[store] = m
            self._link(r, m, relative)                      # MID -> REAL
            os.makedirs(os.path.join(base, acc), exist_ok=True)
            a = os.path.join(base, acc, org)
            self.active[store] = a
            self._link(m, a, relative)                      # ACTIVE -> MID (chain)
            # a bystander alias that points straight at REAL, one through MID,
            # and one for another account that must stay a plain symlink.
            os.makedirs(os.path.join(base, by), exist_ok=True)
            o = os.path.join(base, by, uid(n + 0x301, 8))
            self._link(r, o, relative)
            self.other[store] = o
            f = os.path.join(base, by, uid(n + 0x302, 9))
            self._link(m, f, relative)
            self.far[store] = f
        set_identity(h, acc, org)
        h.log((acc, org))

    @staticmethod
    def _link(target, link, relative):
        os.symlink(os.path.relpath(target, os.path.dirname(link)) if relative
                   else target, link)

    def count(self, store, via):
        return len(cards_via(via[store]))

    def totals(self):
        return {(s, k): len(cards_via(getattr(self, k)[s]))
                for s in STORES for k in ("active", "mid", "other", "far")}


def check_healed(ch, store):
    """Assertions every correct heal satisfies. Raises AssertionError."""
    a, r = ch.active[store], ch.real[store]
    assert os.path.isdir(a) and not os.path.islink(a), "active is not a real dir"
    assert cards_via(a) == sorted(ch.names[store]), "cards differ in active"
    for n in ch.names[store]:         # moved, not copied: same inode
        assert os.stat(os.path.join(a, n)).st_ino == ch.inodes[store][n], \
            "card %s was copied, not moved" % n
    assert os.path.islink(r), "the old real path is not a symlink (missing or dir)"
    assert resolves_to(r, a), "the old real path does not point to active"
    assert os.path.realpath(r) == os.path.realpath(a) != r
    for k in ("mid", "other", "far"):          # chains through R still resolve
        p = getattr(ch, k)[store]
        assert os.path.islink(p), "%s alias is no longer a symlink" % k
        assert os.path.isdir(p), "%s alias is broken" % k
        assert resolves_to(p, a), "%s alias does not reach the cards" % k
        assert cards_via(p) == sorted(ch.names[store]), "%s alias lost cards" % k
    # no leftovers beside the pair (temp dirs, backups)
    base = os.path.join(os.path.dirname(a))
    leftovers = [n for n in os.listdir(base) if n.startswith(".")]
    assert not leftovers, "left behind: %s" % leftovers


# --- reference heal and the three wrong ones: used only by OracleSelfCheck ---
def _real_chain_end(path):
    return os.path.realpath(path)


def ref_heal(ch, store):
    a = ch.active[store]
    r = os.path.realpath(a)
    os.unlink(a)
    os.rename(r, a)
    os.symlink(a, r)


def mutant_copy(ch, store):             # copies instead of moving
    a = ch.active[store]
    r = os.path.realpath(a)
    os.unlink(a)
    shutil.copytree(r, a)


def mutant_missing(ch, store):          # moves, leaves the old path missing
    a = ch.active[store]
    r = os.path.realpath(a)
    os.unlink(a)
    os.rename(r, a)


def mutant_one_level(ch, store):        # resolves only one link level
    a = ch.active[store]
    first = os.path.join(os.path.dirname(a), os.readlink(a)) \
        if not os.path.isabs(os.readlink(a)) else os.readlink(a)
    os.unlink(a)
    if os.path.islink(first):           # first is MID, itself a symlink
        os.unlink(first)
        os.mkdir(first)                 # fake "real dir" at the middle level
        for n in cards_via(ch.real[store]):
            shutil.copy2(os.path.join(ch.real[store], n), first)
    os.rename(first, a)
    os.symlink(a, first)


class OracleSelfCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-heal-self-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def fresh(self, relative=False):
        h = Home(tempfile.mkdtemp(dir=self.tmp))
        return Chain(h, relative=relative)

    def test_reference_heal_passes_the_checker(self):
        for rel in (False, True):
            ch = self.fresh(rel)
            for s in STORES:
                ref_heal(ch, s)
                check_healed(ch, s)

    def test_copy_instead_of_move_is_rejected(self):
        ch = self.fresh()
        mutant_copy(ch, CC)
        with self.assertRaises(AssertionError):
            check_healed(ch, CC)

    def test_old_path_left_missing_is_rejected(self):
        ch = self.fresh()
        mutant_missing(ch, CC)
        with self.assertRaises(AssertionError):
            check_healed(ch, CC)

    def test_one_link_level_is_rejected(self):
        ch = self.fresh()
        mutant_one_level(ch, CC)
        with self.assertRaises(AssertionError):
            check_healed(ch, CC)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-heal-")
        self.h = Home(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def heal(self, *extra, **envx):
        return self.h.run("heal-active", *extra, **envx)

    def digest(self):
        return tree_digest(self.h.support)


class HealActive(Base):
    def test_dry_run_reports_and_writes_nothing(self):
        ch = Chain(self.h)
        before = self.digest()
        r = self.heal()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.digest(), before)
        for s in STORES:
            self.assertIn(s, r.stdout)
            self.assertIn("%s/%s" % (ACC, ORG), r.stdout)
            self.assertTrue(os.path.islink(ch.active[s]))
        self.assertIn("--apply", r.stdout)         # says how to really do it

    def test_apply_heals_both_stores(self):
        ch = Chain(self.h)
        r = self.heal("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for s in STORES:
            check_healed(ch, s)

    def test_apply_heals_with_relative_links(self):
        ch = Chain(self.h, relative=True)
        r = self.heal("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for s in STORES:
            check_healed(ch, s)

    def test_card_count_is_identical_through_every_path(self):
        ch = Chain(self.h)
        before = ch.totals()
        self.assertTrue(all(v == 3 for v in before.values()), before)
        self.assertEqual(self.heal("--apply").returncode, 0)
        self.assertEqual(ch.totals(), before)

    def test_card_bytes_survive(self):
        ch = Chain(self.h)
        data = {}
        for s in STORES:
            for n in ch.names[s]:
                with open(os.path.join(ch.real[s], n), "rb") as fh:
                    data[(s, n)] = fh.read()
        self.assertEqual(self.heal("--apply").returncode, 0)
        for (s, n), b in data.items():
            with open(os.path.join(ch.active[s], n), "rb") as fh:
                self.assertEqual(fh.read(), b)

    def test_second_apply_is_nothing_to_do(self):
        Chain(self.h)
        self.assertEqual(self.heal("--apply").returncode, 0)
        before = self.digest()
        r = self.heal("--apply")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("nothing to do", r.stdout.lower())
        self.assertEqual(self.digest(), before)

    def test_only_the_symlinked_store_is_changed(self):
        ch = Chain(self.h)
        # LAM store is already healthy: active is a real dir with cards.
        a = ch.active[LAM]
        os.unlink(a)
        os.rename(ch.real[LAM], a)
        os.symlink(a, ch.real[LAM])
        lam_before = tree_digest(os.path.join(self.h.support, LAM))
        r = self.heal("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        check_healed(ch, CC)
        self.assertEqual(tree_digest(os.path.join(self.h.support, LAM)), lam_before)

    def test_json_report(self):
        Chain(self.h)
        r = self.heal("--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rep = parse(r)
        self.assertEqual(rep["command"], "heal-active")
        self.assertEqual(rep["exit_code"], 0)


class Refusals(Base):
    def refused(self, r, *needles):
        """A real refusal of an existing command: not the usage error of an
        unknown subcommand (exit 2, 'invalid choice'), with a reason printed."""
        text = r.stdout + r.stderr
        self.assertNotIn("invalid choice", text)
        self.assertNotEqual(r.returncode, 0, text)
        self.assertNotIn("Traceback", text)
        for n in needles:
            self.assertIn(n.lower(), text.lower())

    def test_app_running_refuses_apply_without_writes(self):
        Chain(self.h)
        before = self.digest()
        r = self.heal("--apply", CLAUDE_ACCOUNT_MOVE_PGREP=STUB_OPEN)
        self.refused(r, "quit")
        self.assertEqual(self.digest(), before)

    def test_dangling_active_refuses_without_writes(self):
        ch = Chain(self.h)
        shutil.rmtree(ch.real[CC])                 # CC chain ends nowhere
        before = self.digest()
        r = self.heal("--apply")
        self.refused(r, CC)
        self.assertEqual(self.digest(), before)

    def test_dangling_in_one_store_blocks_the_other_too(self):
        ch = Chain(self.h)
        shutil.rmtree(ch.real[LAM])
        before = self.digest()
        r = self.heal("--apply")
        self.refused(r)
        self.assertEqual(self.digest(), before)    # all or nothing

    def test_target_outside_the_store_refuses(self):
        ch = Chain(self.h)
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside)
        self.h.card(outside, 90, uid(90))
        a = ch.active[CC]
        os.unlink(a)
        os.symlink(outside, a)
        before = self.digest()
        out_before = tree_digest(outside)
        r = self.heal("--apply")
        self.refused(r, CC, "outside")
        self.assertEqual(self.digest(), before)
        self.assertEqual(tree_digest(outside), out_before)
        self.assertTrue(os.path.islink(a))

    def test_target_in_another_store_refuses(self):
        ch = Chain(self.h)
        a = ch.active[CC]
        os.unlink(a)
        os.symlink(ch.real[LAM], a)                # resolves into the other store
        before = self.digest()
        r = self.heal("--apply")
        self.refused(r, CC)
        self.assertEqual(self.digest(), before)

    def test_real_active_dir_is_nothing_to_do(self):
        h = self.h
        for s in STORES:
            d = h.pair(s, ACC, ORG)
            h.card(d, 1, uid(1))
        set_identity(h, ACC, ORG)
        h.log((ACC, ORG))
        before = self.digest()
        r = self.heal("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nothing to do", r.stdout.lower())
        self.assertEqual(self.digest(), before)

    def test_missing_active_pair_everywhere_is_not_a_crash(self):
        Chain(self.h)
        set_identity(self.h, uid(0x77, 1), uid(0x78, 2))   # names a pair that is absent
        before = self.digest()
        r = self.heal("--apply")
        self.assertEqual(self.digest(), before)
        self.assertNotIn("invalid choice", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)


class IdentitySource(Base):
    def test_active_pair_follows_identity_not_a_constant(self):
        ch1 = Chain(self.h, acc=uid(0x91, 1), org=uid(0x92, 2))
        # a second, symlinked pair of ANOTHER account must stay untouched
        ch2 = Chain(self.h, acc=uid(0x93, 1), org=uid(0x94, 2))
        set_identity(self.h, uid(0x91, 1), uid(0x92, 2))
        self.h.log((uid(0x91, 1), uid(0x92, 2)))
        self.assertEqual(self.heal("--apply").returncode, 0)
        for s in STORES:
            self.assertFalse(os.path.islink(ch1.active[s]))
            self.assertTrue(os.path.islink(ch2.active[s]), "non-active pair touched")

    def test_changing_the_identity_changes_the_target(self):
        a = Chain(self.h, acc=uid(0xa1, 1), org=uid(0xa2, 2))
        b = Chain(self.h, acc=uid(0xb1, 1), org=uid(0xb2, 2))
        set_identity(self.h, uid(0xb1, 1), uid(0xb2, 2))
        self.h.log((uid(0xb1, 1), uid(0xb2, 2)))
        self.assertEqual(self.heal("--apply").returncode, 0)
        for s in STORES:
            self.assertFalse(os.path.islink(b.active[s]))
            self.assertTrue(os.path.islink(a.active[s]))

    def test_explicit_active_pair_wins(self):
        ch = Chain(self.h)
        other = Chain(self.h, acc=uid(0xc1, 1), org=uid(0xc2, 2))
        r = self.heal("--apply", "--active-pair", "%s/%s" % (uid(0xc1, 1), uid(0xc2, 2)))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for s in STORES:
            self.assertFalse(os.path.islink(other.active[s]))
            self.assertTrue(os.path.islink(ch.active[s]))


class FinishGuard(Base):
    """finish: exit 3 while an active pair is a symlink; names store and path."""

    def moved(self):
        src = build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        set_identity(self.h, B, O2)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        return src

    def symlink_active(self, store):
        a = os.path.join(self.h.support, store, B, O2)
        real = os.path.join(self.h.support, store, B, uid(0xd0, 1))
        os.rename(a, real)
        os.symlink(real, a)
        return a

    def finish(self, *extra):
        return self.h.run("finish", "--wait-login", "0", *extra)

    def test_baseline_finish_is_green(self):
        self.moved()
        self.assertEqual(self.finish().returncode, 0)

    def test_symlinked_active_pair_fails_each_store(self):
        for store in STORES:
            with self.subTest(store=store):
                self.tearDown()
                self.setUp()
                self.moved()
                a = self.symlink_active(store)
                r = self.finish()
                self.assertEqual(r.returncode, 3, r.stdout)
                self.assertIn(store, r.stdout)
                self.assertIn(a, r.stdout)
                self.assertIn("heal-active --apply", r.stdout)
                self.assertIn("quit", r.stdout.lower())

    def test_both_stores_are_named(self):
        self.moved()
        paths = [self.symlink_active(s) for s in STORES]
        r = self.finish("--json")
        self.assertEqual(r.returncode, 3, r.stdout)
        for p in paths:
            self.assertIn(p, r.stdout)

    def test_finish_is_green_again_after_heal(self):
        self.moved()
        self.symlink_active(CC)
        self.symlink_active(LAM)
        self.assertEqual(self.finish().returncode, 3)
        r = self.heal_apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.finish().returncode, 0)

    def heal_apply(self):
        return self.h.run("heal-active", "--apply")


class AutoStep(Base):
    """auto: the heal step runs after login and before verification."""

    def test_heal_runs_between_login_and_finish(self):
        from claude_account_move import auto_move, cli, common
        from claude_account_move.common import Paths, read_identity
        import test_auto
        build_source(self.h)
        events = []

        def login(cli_mod, paths, source, allow_same, limit, poll, out):
            switch_to_target(self.h)
            set_identity(self.h, B, O2)
            r = self.h.run("sync", "--apply")
            assert r.returncode == 0, r.stdout
            for s in STORES:        # the field state: active pair is a symlink
                a = os.path.join(self.h.support, s, B, O2)
                real = os.path.join(self.h.support, s, B, uid(0xd0, 1))
                os.rename(a, real)
                os.symlink(real, a)
            events.append("login")
            return read_identity(Paths(self.h.home, self.h.state)), None

        def spy(name):
            real = getattr(cli, name)

            def f(*a, **k):
                events.append(name)
                return real(*a, **k)
            return f

        env = dict(test_auto.ENVS)
        with mock.patch.dict(os.environ, env), contextlib.ExitStack() as es:
            self.assertTrue(hasattr(cli, "cmd_heal_active"), "cmd_heal_active missing")
            es.enter_context(mock.patch.object(cli, "cmd_heal_active",
                                               spy("cmd_heal_active")))
            es.enter_context(mock.patch.object(cli, "cmd_finish", spy("cmd_finish")))
            es.enter_context(mock.patch.object(common, "claude_app_running",
                                               lambda: False))
            es.enter_context(mock.patch.object(auto_move, "wait_app_switch", login))
            es.enter_context(mock.patch.object(auto_move, "CLI_SETTLE_MIN_S", 0.05))
            out = io.StringIO()
            es.enter_context(contextlib.redirect_stdout(out))
            es.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code = cli.main(["auto", "--home", self.h.home, "--state-dir", self.h.state])
        self.assertIn("cmd_heal_active", events)
        self.assertLess(events.index("login"), events.index("cmd_heal_active"))
        self.assertLess(events.index("cmd_heal_active"), events.index("cmd_finish"))
        self.assertEqual(code, 0, out.getvalue())
        for s in STORES:
            self.assertFalse(os.path.islink(os.path.join(self.h.support, s, B, O2)))


class PrepareEnotdir(Base):
    """prepare: a recent ENOTDIR line for a pair that is still a symlink fails."""

    def enotdir(self, store, acc, org, age):
        ts = (datetime.now() - age).strftime("%Y-%m-%d %H:%M:%S")
        path = os.path.join(self.h.support, store, acc, org)
        with open(self.h.home + "/Library/Logs/Claude/main.log", "a") as fh:
            fh.write("%s [error] Failed to save session local_x: ENOTDIR: not a "
                     "directory, mkdir '%s' at ensureStorageDir\n" % (ts, path))
        return path

    def symlink_pair(self, store, acc, org):
        a = os.path.join(self.h.support, store, acc, org)
        real = os.path.join(self.h.support, store, acc, uid(0xd0, 1))
        os.rename(a, real)
        os.symlink(real, a)
        return a

    def setUp(self):
        Base.setUp(self)
        build_source(self.h)

    def test_baseline_without_enotdir_is_ready(self):
        self.assertEqual(self.h.run("prepare").returncode, 0)

    def test_recent_enotdir_on_symlink_fails(self):
        for store in STORES:
            with self.subTest(store=store):
                self.tearDown()
                Base.setUp(self)
                build_source(self.h)
                self.symlink_pair(store, A, O1)
                path = self.enotdir(store, A, O1, timedelta(hours=1))
                r = self.h.run("prepare", "--json")
                self.assertEqual(r.returncode, 3, r.stdout)
                rep = parse(r)
                self.assertFalse(rep["ready"])
                self.assertIn("ENOTDIR", r.stdout)
                self.assertIn(path, r.stdout)

    def test_enotdir_older_than_two_days_is_ignored(self):
        self.symlink_pair(CC, A, O1)
        self.enotdir(CC, A, O1, timedelta(days=3))
        self.assertEqual(self.h.run("prepare").returncode, 0)

    def test_enotdir_just_inside_two_days_counts(self):
        self.symlink_pair(CC, A, O1)
        self.enotdir(CC, A, O1, timedelta(days=1, hours=23))
        self.assertEqual(self.h.run("prepare").returncode, 3)

    def test_recent_enotdir_but_pair_is_a_real_dir_is_ready(self):
        self.enotdir(CC, A, O1, timedelta(hours=1))      # healed since
        self.assertEqual(self.h.run("prepare").returncode, 0)

    def test_enotdir_outside_a_sessions_store_is_ignored(self):
        self.symlink_pair(CC, A, O1)
        ts = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        with open(self.h.home + "/Library/Logs/Claude/main.log", "a") as fh:
            fh.write("%s [error] ENOTDIR: not a directory, mkdir '%s/other/x/y'\n"
                     % (ts, self.h.support))
        self.assertEqual(self.h.run("prepare").returncode, 0)


if __name__ == "__main__":
    unittest.main()
