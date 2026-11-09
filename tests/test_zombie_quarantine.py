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


# --- Round 2 (R26-R29) ------------------------------------------------------------
import contextlib  # noqa: E402
import io  # noqa: E402
from unittest import mock  # noqa: E402

from claude_account_move import cli  # noqa: E402
from claude_account_move import quarantine_zombies as qz  # noqa: E402

SENTINEL = b"SENTINEL-bytes-of-a-file-that-was-already-there"


def plan_of(h):
    paths = common.Paths(h.home, h.state)
    items, problems = qz.plan(paths, st.scan(paths))
    return paths, items, problems


def card_renames():
    """Wrap os.rename and os.replace: record every use on a card file, then run
    the real call (so the old code finishes and the assertion names the reason)."""
    calls = []
    real_rename, real_replace = os.rename, os.replace

    def make(real, name):
        def wrapped(src, dst, *a, **k):
            if os.path.basename(str(src)).startswith("local_"):
                calls.append((name, str(src), str(dst)))
            return real(src, dst, *a, **k)
        return wrapped
    return calls, make(real_rename, "rename"), make(real_replace, "replace")


class NoReplaceMove(Base):
    """R26: every move is one atomic no-replace rename."""

    def test_apply_never_uses_rename_or_replace_for_a_card(self):
        w = World(self.h)
        paths, items, _ = plan_of(self.h)
        calls, rn, rp = card_renames()
        with mock.patch.object(os, "rename", rn), mock.patch.object(os, "replace", rp):
            res = qz.apply_plan(paths, items)
        self.assertEqual(calls, [], "a card was moved with a replacing call")
        self.assertEqual(res["moved"], len(w.zombies))

    def test_apply_destination_that_appears_before_the_move_is_never_replaced(self):
        w = World(self.h)
        paths, items, _ = plan_of(self.h)
        items = items[:1]
        src = items[0]["source"]
        src_sum = sha(src)
        planted = []
        real_write = qz.write_json

        def hook(path, doc, *a, **k):
            real_write(path, doc, *a, **k)
            for e in doc.get("entries", []):
                if e.get("status") == "moving" and not planted:
                    os.makedirs(os.path.dirname(e["destination"]), exist_ok=True)
                    with open(e["destination"], "wb") as fh:
                        fh.write(SENTINEL)
                    planted.append(e["destination"])
        with mock.patch.object(qz, "write_json", hook):
            res = qz.apply_plan(paths, items)
        self.assertTrue(planted, "the hook never ran: the move is not journaled first")
        with open(planted[0], "rb") as fh:
            self.assertEqual(fh.read(), SENTINEL, "the file that was already there was replaced")
        self.assertTrue(os.path.isfile(src))
        self.assertEqual(sha(src), src_sum)
        self.assertEqual(res["moved"], 0)
        self.assertEqual(res["failed"], 1)
        self.assertEqual(res["entries"][0]["status"], "failed")

    def test_undo_source_that_appears_before_the_move_is_never_replaced(self):
        w = World(self.h)
        rep = self.apply_ok()
        dest = os.path.join(backups(self.h)[0], rel_to_support(self.h, w.zombies[0]))
        src = real(w.zombies[0])
        dest_sum = sha(dest)
        planted = []
        real_sha, real_makedirs = qz.sha256_file, os.makedirs

        def plant():
            if not planted and not os.path.lexists(src):
                with open(src, "wb") as fh:
                    fh.write(SENTINEL)
                planted.append(src)

        def sha_hook(p, *a, **k):
            out = real_sha(p, *a, **k)
            if os.path.realpath(p) == os.path.realpath(dest):
                plant()
            return out

        def mk_hook(p, *a, **k):
            out = real_makedirs(p, *a, **k)
            if os.path.realpath(p) == os.path.dirname(src):
                plant()
            return out
        paths = common.Paths(self.h.home, self.h.state)
        with mock.patch.object(qz, "sha256_file", sha_hook), \
                mock.patch.object(os, "makedirs", mk_hook):
            res = qz.undo(paths, rep["journal"])
        self.assertTrue(planted, "the hook never ran")
        with open(src, "rb") as fh:
            self.assertEqual(fh.read(), SENTINEL, "the file that was already there was replaced")
        self.assertTrue(os.path.isfile(dest))
        self.assertEqual(sha(dest), dest_sum)
        self.assertGreaterEqual(res["kept"] + res["failed"], 1)

    def test_undo_never_uses_rename_or_replace_for_a_card(self):
        w = World(self.h)
        rep = self.apply_ok()
        calls, rn, rp = card_renames()
        paths = common.Paths(self.h.home, self.h.state)
        with mock.patch.object(os, "rename", rn), mock.patch.object(os, "replace", rp):
            res = qz.undo(paths, rep["journal"])
        self.assertEqual(calls, [], "a card was moved back with a replacing call")
        self.assertEqual(res["restored"], len(w.zombies))


class InterruptedJournal(Base):
    """R27: undo settles entries left as moving or failed."""

    def setup_journal(self, status):
        w = World(self.h)
        rep = self.apply_ok()
        with open(rep["journal"], encoding="utf-8") as fh:
            doc = json.load(fh)
        for e in doc["entries"]:
            e["status"] = status
        with open(rep["journal"], "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return w, rep, doc

    def test_moving_entry_with_the_card_in_the_backup_is_restored(self):
        w, rep, _ = self.setup_journal("moving")
        r = self.h.run(CMD, "--undo", rep["journal"])
        self.assertEqual(r.returncode, 0, "exit %s: %s" % (r.returncode, r.stdout + r.stderr))
        for p in w.zombies:
            self.assertTrue(os.path.isfile(real(p)), "not restored: %s" % p)
            self.assertEqual(sha(real(p)), w.sums[p])

    def test_failed_entry_with_the_card_in_the_backup_is_restored(self):
        w, rep, _ = self.setup_journal("failed")
        r = self.h.run(CMD, "--undo", rep["journal"])
        self.assertEqual(r.returncode, 0, "exit %s: %s" % (r.returncode, r.stdout + r.stderr))
        for p in w.zombies:
            self.assertTrue(os.path.isfile(real(p)), "not restored: %s" % p)
            self.assertEqual(sha(real(p)), w.sums[p])

    def test_moving_entry_with_both_names_present_is_kept_and_reported(self):
        w, rep, doc = self.setup_journal("moving")
        taken = real(w.zombies[0])
        with open(taken, "wb") as fh:
            fh.write(SENTINEL)
        r = self.h.run(CMD, "--undo", rep["journal"])
        self.assertEqual(r.returncode, EXIT_FAIL, r.stdout + r.stderr)
        self.assertIn(taken, r.stdout + r.stderr)
        with open(taken, "rb") as fh:
            self.assertEqual(fh.read(), SENTINEL)
        dest = [e["destination"] for e in doc["entries"] if e["source"] == taken][0]
        self.assertEqual(sha(dest), w.sums[w.zombies[0]])

    def test_unsettled_moving_entry_gives_a_nonzero_exit(self):
        """The card is in neither place: nothing can be restored, never success."""
        w, rep, doc = self.setup_journal("moving")
        lost = [e for e in doc["entries"] if e["source"] == real(w.zombies[0])][0]
        os.unlink(lost["destination"])
        r = self.h.run(CMD, "--undo", rep["journal"])
        self.assertEqual(r.returncode, EXIT_FAIL, r.stdout + r.stderr)
        self.assertFalse(os.path.lexists(lost["source"]))

    def test_moving_entry_with_a_changed_backup_is_not_restored(self):
        w, rep, doc = self.setup_journal("moving")
        e = [e for e in doc["entries"] if e["source"] == real(w.zombies[0])][0]
        with open(e["destination"], "wb") as fh:
            fh.write(SENTINEL)
        r = self.h.run(CMD, "--undo", rep["journal"])
        self.assertEqual(r.returncode, EXIT_FAIL, r.stdout + r.stderr)
        self.assertFalse(os.path.lexists(e["source"]), "a card with another sha was put back")

    def test_all_entries_unsettled_is_never_success_with_zeros(self):
        w, rep, doc = self.setup_journal("moving")
        for e in doc["entries"]:
            os.unlink(e["destination"])
        r = self.h.run(CMD, "--undo", rep["journal"])
        self.assertEqual(r.returncode, EXIT_FAIL, r.stdout + r.stderr)


class OutsideWithoutMarks(Base):
    """R28: an outside org folder holds both the mark and its card; no indexed
    store holds any mark."""

    def build(self):
        d = self.h.pair(CC, A, O1)
        live = os.path.join(d, self.h.card(d, 30, uid(30)))
        out = os.path.join(self.tmp, "outside-org")
        os.makedirs(out)
        card = os.path.join(out, cname(31))
        with open(card, "w") as fh:
            json.dump({"cliSessionId": uid(31), "title": "x"}, fh)
        tomb(out, 31)
        link(out, os.path.join(self.h.support, CC, A, uid(6)))
        self.h.accounts(cli=A, app=A)
        return live, card

    def test_apply_reports_the_card_exits_3_and_moves_nothing(self):
        live, card = self.build()
        before = tree_digest(self.tmp)
        r = self.h.run(CMD, "--apply")
        self.refused(r, real(card))
        self.assertEqual(tree_digest(self.tmp), before)
        self.assertEqual(backups(self.h), [])

    def test_dry_run_reports_the_card(self):
        live, card = self.build()
        r = self.h.run(CMD)
        self.assertEqual(r.returncode, EXIT_FAIL, r.stdout + r.stderr)
        self.assertIn(real(card), r.stdout + r.stderr)


# --- R29: the journal path survives a failing journal write -----------------------

def failing_journal_writes(fail_from=3):
    """write_json that fails on the Nth and later journal writes (the 3rd is the
    one after the first card moved)."""
    real_write = qz.write_json
    n = [0]

    def wrapped(path, obj, *a, **k):
        if "journal" in os.path.basename(str(path)):
            n[0] += 1
            if n[0] >= fail_from:
                raise OSError(28, "No space left on device (simulated)")
        return real_write(path, obj, *a, **k)
    return wrapped


class JournalWriteFails(Base):
    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = [CMD, "--apply", "--json", "--home", self.h.home,
                "--state-dir", self.h.state] + list(extra)
        with mock.patch.object(common, "claude_app_running", lambda: False), \
                mock.patch.object(qz, "write_json", failing_journal_writes()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_report_still_carries_the_journal_path(self):
        World(self.h)
        code, out, err = self.run_cli()
        self.assertNotEqual(code, 0, out)
        rep = json.loads(out)
        jp = rep.get("journal")
        self.assertTrue(jp, "no journal path in the report: %s" % out)
        self.assertTrue(os.path.isfile(jp), jp)


class AutoJournalWriteFails(AutoBase):
    def test_auto_stop_line_names_the_journal(self):
        build_source(self.h)
        add_zombie(self.h, 21)
        add_zombie(self.h, 22)
        with mock.patch.object(qz, "write_json", failing_journal_writes()):
            code, out, _, m = self.run_auto2()
        self.assertNotIn(code, (0, PARSE_ERROR), out)
        self.assertEqual(m["cmd_prepare"].call_count, 0)
        journals = all_state_files(self.h, r".*journal.*\.json$")
        self.assertTrue(journals, "no journal file was created")
        self.assertTrue(any(j in out for j in journals),
                        "the stop line does not name the journal: %s" % out)


if __name__ == "__main__":
    unittest.main()
