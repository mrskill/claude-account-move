"""ORACLE for `quarantine-zombies` (written before the implementation).

Brief: _Deliverables/task-outputs/move-program/zombie-quarantine-brief.md, rulings R22-R25.

A "zombie card" is a card local_<id>.json of a session the panel deleted (a
deleted_<id> mark exists in the same store). `prepare` fails its `zombies` check
on them. The command moves them aside, never deletes:

  R22  dry run by default, one line per REAL file (aliases deduped by realpath);
       --apply refuses while the app runs; moves each card to
       <state-dir>/zombie-cards-<UTC stamp>/<path relative to the support dir>;
       never overwrites, never follows a symlink out of the support dir; writes a
       journal (source, destination, sha256, size); --undo JOURNAL puts cards back
       only where the source path is free.
  R23  a card whose real path is outside the support dir is never moved; it is
       reported and the command exits nonzero.
  R24  `auto` quarantines after the app is closed, on a NEW move only, when the
       zombies check would fail; `--no-quarantine` turns it off; a failure stops
       auto before prepare.
  R25  class guard: every card kind the `zombies` check counts is cleared, so
       after --apply `check_zombies` is ok.

Red on the old code means: the subcommand is unknown (parse error, exit 2) or the
behaviour is missing. Every refusal test also pins the refusal exit code (3, never
the parse-error 2) and a positive property, so a missing subcommand cannot pass it.
Fake HOME only.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from fixture import CC, LAM, Home, tree_digest, uid  # noqa: E402
from test_cli import A, B, O1, O2, build_source  # noqa: E402
import test_auto  # noqa: E402

from claude_account_move import common  # noqa: E402
from claude_account_move import pre_move_check as pre  # noqa: E402
from claude_account_move import store as st  # noqa: E402

CMD = "quarantine-zombies"
STUB_OPEN = os.path.join(HERE, "stub_pgrep_open.sh")
EXIT_FAIL = 3
PARSE_ERROR = 2


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def cname(n):
    return "local_%s.json" % uid(n, 9)


def tomb(d, n):
    open(os.path.join(d, "deleted_%s" % uid(n, 9)), "w").close()


def link(target, where):
    os.makedirs(os.path.dirname(where), exist_ok=True)
    os.symlink(target, where)


def real(p):
    return os.path.realpath(p)


def backups(h):
    return sorted(glob.glob(os.path.join(h.state, "zombie-cards-*")))


def rel_to_support(h, p):
    return os.path.relpath(real(p), real(h.support))


def all_state_files(h, pattern):
    out = []
    for dp, _, fns in os.walk(h.state):
        for n in fns:
            if re.match(pattern, n):
                out.append(os.path.join(dp, n))
    return out


class World(object):
    """Fixture store: four real zombie cards (two stores x two pairs), one live
    card, an alias directory and the marks. Paths are real paths."""

    def __init__(self, h):
        self.h = h
        self.cc_a = h.pair(CC, A, O1)
        self.cc_b = h.pair(CC, B, O2)
        self.lam_a = h.pair(LAM, A, O1)
        self.lam_b = h.pair(LAM, B, O2)
        self.live = os.path.join(self.cc_a, h.card(self.cc_a, 30, uid(30)))
        self.zombies = []
        for d, n in ((self.cc_a, 21), (self.cc_b, 21), (self.lam_a, 22),
                     (self.lam_b, 22)):
            self.zombies.append(os.path.join(d, h.card(d, n, uid(n))))
        tomb(self.cc_a, 21)           # the mark lives in one pair only
        tomb(self.lam_a, 22)
        # an alias of a whole pair: the same real files again
        self.alias = os.path.join(h.support, CC, A, uid(5))
        link(self.cc_a, self.alias)
        h.accounts(cli=A, app=A)
        self.sums = {p: sha(p) for p in self.zombies}
        self.live_sum = sha(self.live)

    def marks(self):
        return sorted(glob.glob(os.path.join(self.h.support, "*", "*", "*", "deleted_*")))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-zq-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.h = Home(self.tmp)

    def refused(self, r, *needles):
        self.assertEqual(r.returncode, EXIT_FAIL, r.stdout + r.stderr)
        text = (r.stdout + r.stderr).lower()
        self.assertNotIn("traceback", text)
        for n in needles:
            self.assertIn(n.lower(), text)

    def apply_ok(self):
        r = self.h.run(CMD, "--apply", "--json")
        self.assertEqual(r.returncode, 0, "exit %s: %s" % (r.returncode, r.stdout + r.stderr))
        return json.loads(r.stdout)


class DryRun(Base):
    def test_lists_each_real_file_once_and_writes_nothing(self):
        w = World(self.h)
        before = tree_digest(self.tmp)
        r = self.h.run(CMD)
        self.assertEqual(r.returncode, 0, "exit %s: %s" % (r.returncode, r.stdout + r.stderr))
        self.assertEqual(tree_digest(self.tmp), before)
        self.assertEqual(backups(self.h), [])
        for p in w.zombies:
            self.assertEqual(r.stdout.count(real(p)), 1, (p, r.stdout))
        self.assertNotIn(w.live, r.stdout)
        self.assertNotIn(real(w.live), r.stdout)

    def test_alias_path_is_not_listed_as_a_separate_card(self):
        w = World(self.h)
        r = self.h.run(CMD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(os.path.join(w.alias, cname(21)), r.stdout)

    def test_nothing_to_do_is_ok(self):
        build_source(self.h)
        r = self.h.run(CMD, "--apply")
        self.assertEqual(r.returncode, 0, "exit %s: %s" % (r.returncode, r.stdout + r.stderr))
        self.assertEqual(backups(self.h), [])


class Apply(Base):
    def test_moves_every_zombie_under_the_relative_path(self):
        w = World(self.h)
        rels = {p: rel_to_support(self.h, p) for p in w.zombies}
        rep = self.apply_ok()
        bk = backups(self.h)
        self.assertEqual(len(bk), 1, bk)
        self.assertRegex(os.path.basename(bk[0]), r"^zombie-cards-\d{8}[T_-]?\d{6}Z?$|^zombie-cards-.+")
        for p in w.zombies:
            self.assertFalse(os.path.lexists(real(p)), "still at the source: %s" % p)
            dest = os.path.join(bk[0], rels[p])
            self.assertTrue(os.path.isfile(dest), dest)
            self.assertEqual(sha(dest), w.sums[p])
        self.assertTrue(os.path.isfile(w.live))
        self.assertEqual(sha(w.live), w.live_sum)
        self.assertTrue(rep.get("journal"))

    def test_marks_and_live_cards_stay(self):
        w = World(self.h)
        marks = w.marks()
        self.assertTrue(marks)
        self.apply_ok()
        self.assertEqual(w.marks(), marks)
        self.assertTrue(os.path.islink(w.alias))

    def test_journal_has_source_destination_sha_and_size(self):
        w = World(self.h)
        rep = self.apply_ok()
        jp = rep["journal"]
        self.assertTrue(os.path.isfile(jp), jp)
        self.assertTrue(real(jp).startswith(real(self.h.state)), jp)
        with open(jp, encoding="utf-8") as fh:
            text = fh.read()
        json.loads(text)
        bk = backups(self.h)[0]
        for p in w.zombies:
            self.assertIn(w.sums[p], text)
            self.assertIn(real(p), text)
            self.assertIn(os.path.join(bk, rel_to_support(self.h, p)), text)
            self.assertIn(str(os.path.getsize(os.path.join(bk, rel_to_support(self.h, p)))), text)

    def test_apply_twice_is_a_noop_the_second_time(self):
        w = World(self.h)
        self.apply_ok()
        before = tree_digest(self.h.state)
        r = self.h.run(CMD, "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(tree_digest(self.h.state), before)
        self.assertTrue(w.zombies)

    def test_no_deletion_code_path(self):
        """Every byte of every zombie card still exists somewhere under the tree."""
        w = World(self.h)
        self.apply_ok()
        present = set()
        for dp, _, fns in os.walk(self.tmp):
            for n in fns:
                if n.startswith("local_"):
                    present.add(sha(os.path.join(dp, n)))
        for s in w.sums.values():
            self.assertIn(s, present)


class AppRunning(Base):
    def test_apply_refuses_while_the_app_runs_without_writes(self):
        World(self.h)
        before = tree_digest(self.tmp)
        r = self.h.run(CMD, "--apply", CLAUDE_ACCOUNT_MOVE_PGREP=STUB_OPEN)
        self.refused(r, "quit")
        self.assertEqual(tree_digest(self.tmp), before)

    def test_dry_run_is_allowed_while_the_app_runs(self):
        w = World(self.h)
        r = self.h.run(CMD, CLAUDE_ACCOUNT_MOVE_PGREP=STUB_OPEN)
        self.assertEqual(r.returncode, 0, "exit %s: %s" % (r.returncode, r.stdout + r.stderr))
        self.assertIn(real(w.zombies[0]), r.stdout)


class NeverOverwrite(Base):
    def test_second_card_at_a_taken_destination_never_replaces_the_first(self):
        w = World(self.h)
        src = w.zombies[0]
        first_sum = w.sums[src]
        self.apply_ok()
        # a new card appears at the same source path (same relative path in a
        # second backup, possibly the same second): nothing already saved may change
        with open(real(src), "w") as fh:
            json.dump({"cliSessionId": uid(21), "title": "second", "padding": "y" * 500}, fh)
        second_sum = sha(real(src))
        self.assertNotEqual(first_sum, second_sum)
        r = self.h.run(CMD, "--apply")
        self.assertNotEqual(r.returncode, PARSE_ERROR, r.stdout + r.stderr)
        saved = {}
        for bk in backups(self.h):
            p = os.path.join(bk, rel_to_support(self.h, src))
            if os.path.isfile(p):
                saved[bk] = sha(p)
        self.assertIn(first_sum, saved.values(), "the first backup was overwritten or lost")
        in_place = sha(real(src)) if os.path.isfile(real(src)) else None
        self.assertTrue(second_sum in saved.values() or in_place == second_sum,
                        "the second card was lost")


class Outside(Base):
    def outside_card(self, w):
        out = os.path.join(self.tmp, "outside")
        os.makedirs(out)
        with open(os.path.join(out, cname(21)), "w") as fh:
            json.dump({"cliSessionId": uid(21), "title": "x"}, fh)
        link(out, os.path.join(self.h.support, CC, A, uid(6)))
        return os.path.join(out, cname(21))

    def test_pair_symlinked_outside_is_reported_and_never_moved(self):
        w = World(self.h)
        card = self.outside_card(w)
        before = (sha(card), os.stat(card).st_ino)
        r = self.h.run(CMD, "--apply")
        self.refused(r, real(card))
        self.assertTrue(os.path.isfile(card))
        self.assertEqual((sha(card), os.stat(card).st_ino), before)
        for bk in backups(self.h):
            self.assertEqual(glob.glob(os.path.join(bk, "**", "outside", "**"), recursive=True), [])

    def test_card_file_symlinked_outside_is_never_followed(self):
        w = World(self.h)
        outer = os.path.join(self.tmp, "outer.json")
        with open(outer, "w") as fh:
            json.dump({"cliSessionId": uid(23), "title": "o"}, fh)
        d = self.h.pair(CC, A, uid(7))
        link(outer, os.path.join(d, cname(23)))
        tomb(d, 23)
        before = sha(outer)
        r = self.h.run(CMD, "--apply")
        self.refused(r, real(outer))
        self.assertEqual(sha(outer), before)
        self.assertTrue(os.path.isfile(outer))

    def test_dry_run_reports_the_outside_card(self):
        w = World(self.h)
        card = self.outside_card(w)
        r = self.h.run(CMD)
        self.assertNotEqual(r.returncode, PARSE_ERROR, r.stdout + r.stderr)
        self.assertIn(real(card), r.stdout + r.stderr)


class Undo(Base):
    def test_undo_puts_every_card_back_byte_for_byte(self):
        w = World(self.h)
        rep = self.apply_ok()
        r = self.h.run(CMD, "--undo", rep["journal"])
        self.assertEqual(r.returncode, 0, "exit %s: %s" % (r.returncode, r.stdout + r.stderr))
        for p in w.zombies:
            self.assertTrue(os.path.isfile(real(p)), p)
            self.assertEqual(sha(real(p)), w.sums[p])

    def test_undo_never_overwrites_a_taken_source(self):
        w = World(self.h)
        rep = self.apply_ok()
        taken = real(w.zombies[0])
        with open(taken, "w") as fh:
            fh.write("{\"cliSessionId\": \"new\"}")
        taken_sum = sha(taken)
        r = self.h.run(CMD, "--undo", rep["journal"])
        self.assertNotEqual(r.returncode, PARSE_ERROR, r.stdout + r.stderr)
        self.assertEqual(sha(taken), taken_sum, "undo overwrote a file")
        dest = os.path.join(backups(self.h)[0], rel_to_support(self.h, w.zombies[0]))
        self.assertTrue(os.path.isfile(dest), "the card must stay in the backup")
        self.assertEqual(sha(dest), w.sums[w.zombies[0]])
        self.assertIn(taken, r.stdout + r.stderr)
        for p in w.zombies[1:]:
            self.assertEqual(sha(real(p)), w.sums[p])

    def test_undo_with_a_missing_journal_is_a_clean_failure(self):
        World(self.h)
        r = self.h.run(CMD, "--undo", os.path.join(self.tmp, "no-such-journal.json"))
        self.assertEqual(r.returncode, EXIT_FAIL, r.stdout + r.stderr)
        self.assertNotIn("traceback", (r.stdout + r.stderr).lower())


def add_zombie(h, n, alias=True):
    """A deleted session whose card still lives in the account A pair."""
    d = os.path.join(h.support, CC, A, O1)
    path = os.path.join(d, h.card(d, n, uid(n)))
    tomb(d, n)
    return path


class AutoBase(test_auto.Base):
    def run_auto2(self, *extra, **kw):
        try:
            return self.run_auto(*extra, **kw)
        except SystemExit as e:           # parse error of the old code
            return e.code, "", "", None


class AutoQuarantine(AutoBase):
    def test_new_move_clears_zombies_then_prepares(self):
        build_source(self.h)
        zs = [add_zombie(self.h, n) for n in (21, 22, 23)]
        link(os.path.join(self.h.support, CC, A, O1), os.path.join(self.h.support, CC, A, uid(5)))
        sums = [sha(z) for z in zs]
        code, out, _, m = self.run_auto2()
        self.assertEqual(code, 0, out)
        self.assertEqual(m["cmd_prepare"].call_count, 1)
        bk = backups(self.h)
        self.assertEqual(len(bk), 1, bk)
        line = [l for l in out.splitlines() if "zombie-cards-" in l]
        self.assertTrue(line, out)
        self.assertTrue(re.search(r"\b3\b", line[0]), line[0])
        for z, s in zip(zs, sums):
            self.assertFalse(os.path.exists(z))
            dest = os.path.join(bk[0], rel_to_support(self.h, z))
            self.assertEqual(sha(dest), s)
        self.assertTrue(re.search(u"[\\u0400-\\u04ff]", line[0]), "the line is Russian: " + line[0])

    def test_no_zombies_means_no_quarantine_step(self):
        build_source(self.h)
        code, out, _, m = self.run_auto2()
        self.assertEqual(code, 0, out)
        self.assertEqual(backups(self.h), [])
        self.assertNotIn("zombie-cards-", out)

    def test_no_quarantine_flag_leaves_the_failure_to_prepare(self):
        build_source(self.h)
        z = add_zombie(self.h, 21)
        s = sha(z)
        code, out, _, m = self.run_auto2("--no-quarantine")
        self.assertNotIn(code, (0, PARSE_ERROR), out)
        self.assertEqual(m["cmd_prepare"].call_count, 1)
        self.assertEqual(sha(z), s)
        self.assertEqual(backups(self.h), [])

    def test_quarantine_failure_stops_before_prepare(self):
        build_source(self.h)
        add_zombie(self.h, 21)
        outdir = os.path.join(self.tmp, "outside")
        os.makedirs(outdir)
        with open(os.path.join(outdir, cname(21)), "w") as fh:
            json.dump({"cliSessionId": uid(21)}, fh)
        link(outdir, os.path.join(self.h.support, CC, A, uid(6)))
        code, out, _, m = self.run_auto2()
        self.assertNotIn(code, (0, PARSE_ERROR), out)
        self.assertEqual(m["cmd_prepare"].call_count, 0)
        self.assertIn(self.h.state, out)
        self.assertTrue(os.path.isfile(os.path.join(outdir, cname(21))))

    def test_resume_does_not_quarantine(self):
        build_source(self.h)
        first = add_zombie(self.h, 21)
        code, out, _, m = self.run_auto2("--wait-login", "0.1", settle=0.05,
                                         wait=self.user_logs_in("cli_lags"))
        self.assertEqual(code, 6, out)            # the move is left unfinished
        self.assertEqual(len(backups(self.h)), 1, "the new move must have quarantined")
        self.assertFalse(os.path.exists(first))
        late = add_zombie(self.h, 24)             # a new zombie shows up before the rerun

        def fix(cmd):
            self.h.accounts(cli=B, app=B)
            return 0

        code, out, _, m = self.run_auto2(wait=self.user_logs_in(), cli_login=fix)
        self.assertEqual(m["cmd_prepare"].call_count, 0, "it must resume, not start anew")
        self.assertTrue(os.path.isfile(late), "a resumed move must not quarantine")
        self.assertEqual(len(backups(self.h)), 1)


# --- R25: the cleanup and the check cannot drift apart ---------------------------

def kind_direct(h):
    d = h.pair(CC, A, O1)
    h.card(d, 40, uid(40))
    tomb(d, 40)


def kind_alias_copy(h):
    d = h.pair(CC, A, O1)
    h.card(d, 41, uid(41))
    tomb(d, 41)
    link(d, os.path.join(h.support, CC, A, uid(5)))
    d2 = h.pair(CC, B, O2)
    h.card(d2, 41, uid(41))             # a separate copy in another pair


def kind_card_only_in_other_pair(h):
    h.pair(CC, A, O1)
    tomb(os.path.join(h.support, CC, A, O1), 42)
    d2 = h.pair(CC, B, O2)
    h.card(d2, 42, uid(42))


def kind_agent_store(h):
    d = h.pair(LAM, A, O1)
    h.card(d, 43, uid(43))
    tomb(d, 43)


def kind_agent_store_other_account(h):
    d = h.pair(LAM, A, O1)
    tomb(d, 44)
    d2 = h.pair(LAM, B, O2)
    h.card(d2, 44, uid(44))


KINDS = {"direct": kind_direct, "alias_copy": kind_alias_copy,
         "card_only_in_other_pair": kind_card_only_in_other_pair,
         "agent_store": kind_agent_store,
         "agent_store_other_account": kind_agent_store_other_account}


class ClassGuard(unittest.TestCase):
    def zombies_ok(self, h):
        return pre.check_zombies(st.scan(common.Paths(h.home, h.state)))

    def one(self, builders):
        tmp = tempfile.mkdtemp(prefix="cam-zq-class-")
        self.addCleanup(shutil.rmtree, tmp, True)
        h = Home(tmp)
        for b in builders:
            b(h)
        live_d = h.pair(CC, A, O1)
        live = os.path.join(live_d, h.card(live_d, 50, uid(50)))
        h.accounts(cli=A, app=A)
        ok, detail = self.zombies_ok(h)
        self.assertFalse(ok, "fixture must hold zombies the check counts: " + detail)
        r = h.run(CMD, "--apply")
        self.assertEqual(r.returncode, 0, "exit %s: %s" % (r.returncode, r.stdout + r.stderr))
        ok, detail = self.zombies_ok(h)
        self.assertTrue(ok, "the check still sees zombies after quarantine: " + detail)
        self.assertTrue(os.path.isfile(live))

    def test_every_kind_alone(self):
        for name, build in sorted(KINDS.items()):
            with self.subTest(kind=name):
                self.one([build])

    def test_all_kinds_together(self):
        tmp = tempfile.mkdtemp(prefix="cam-zq-class-")
        self.addCleanup(shutil.rmtree, tmp, True)
        h = Home(tmp)
        World(h)
        ok, detail = self.zombies_ok(h)
        self.assertFalse(ok, detail)
        r = h.run(CMD, "--apply")
        self.assertEqual(r.returncode, 0, "exit %s: %s" % (r.returncode, r.stdout + r.stderr))
        ok, detail = self.zombies_ok(h)
        self.assertTrue(ok, detail)


if __name__ == "__main__":
    unittest.main()
