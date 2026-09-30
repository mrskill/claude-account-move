import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import CC, ENTRY, LAM, Home, tree_digest, uid  # noqa: E402

A, O1 = uid(1), uid(2)       # source account and organization
B, O2 = uid(3), uid(4)       # target account and organization


def build_source(h, empty_cid_live=False):
    """Account A with three cards (+1 in the agent store), all with transcripts."""
    d = h.pair(CC, A, O1)
    for n in (11, 12, 13):
        h.card(d, n, uid(n))
        h.transcript(uid(n))
    lam = h.pair(LAM, A, O1)
    h.card(lam, 14, uid(14))
    h.transcript(uid(14))
    if empty_cid_live:
        name = h.card(d, 15, "")
        h.transcript(name[len("local_"):-len(".json")])
    h.accounts(cli=A, app=A)
    h.log((A, O1))
    return d


def switch_to_target(h):
    h.pair(CC, B, O2)
    h.pair(LAM, B, O2)
    h.accounts(cli=B, app=B)
    h.log((A, O1), (B, O2))


def parse(r):
    return json.loads(r.stdout)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-test-")
        self.h = Home(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class EmptyHome(Base):
    def test_prepare_and_finish_on_empty_home(self):
        self.assertEqual(self.h.run("prepare").returncode, 4)
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 4)

    def test_support_without_pairs(self):
        os.makedirs(os.path.join(self.h.support, CC))
        self.assertEqual(self.h.run("prepare").returncode, 4)

    def test_no_arguments_is_an_input_error(self):
        self.assertEqual(self.h.run().returncode, 2)


class FullMove(Base):
    def test_move_with_sync_repair(self):
        build_source(self.h)
        r = self.h.run("prepare", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = parse(r)
        self.assertTrue(rep["ready"])
        self.assertFalse(rep["write_verified"])
        ids = {c["id"]: c["ok"] for c in rep["checks"]}
        self.assertTrue(all(ids.values()), ids)
        self.assertIn("static_accessibility", ids)
        switch_to_target(self.h)
        r = self.h.run("finish", "--wait-login", "3", "--json")
        self.assertEqual(r.returncode, 3, r.stderr)
        rep = parse(r)
        self.assertEqual(rep["stores"][CC]["names_missing_in_target"], 3)
        self.assertEqual(rep["stores"][CC]["names_missing_anywhere"], 0)
        dry = self.h.run("sync")
        self.assertEqual(dry.returncode, 0)
        self.assertIn("missing in the target pair(s): 4", dry.stdout)
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 3)
        ap = self.h.run("sync", "--apply")
        self.assertEqual(ap.returncode, 0, ap.stdout + ap.stderr)
        r = self.h.run("finish", "--wait-login", "3", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rep = parse(r)
        self.assertEqual(rep["stores"][CC]["names_in_target"], 3)
        self.assertEqual(rep["stores"][LAM]["names_in_target"], 1)
        self.assertEqual(rep["accounts"]["target"], B)
        self.assertEqual(rep["accounts"]["source_from_snapshot"], A)

    def test_empty_source_directory_is_not_a_failure(self):
        src = build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        for f in os.listdir(src):            # old account's folder ends up empty
            os.unlink(os.path.join(src, f))
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(parse(r)["stores"][CC]["lagging_dirs"], 0)

    def test_card_missing_everywhere_is_a_loss(self):
        src = build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        os.unlink(os.path.join(src, "local_%s.json" % uid(11, 9)))
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(parse(r)["stores"][CC]["names_missing_anywhere"], 1)

    def test_lost_transcript_is_a_loss(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        os.unlink(os.path.join(self.h.projects, uid(12) + ".jsonl"))
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(parse(r)["stores"][CC]["transcripts_gone_since_snapshot"], 1)

    def test_target_copy_with_empty_session_id(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        target = os.path.join(self.h.support, CC, B, O2)
        path = os.path.join(target, "local_%s.json" % uid(11, 9))
        os.unlink(path)
        self.h.card(target, 11, "")
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual(r.returncode, 3)
        self.assertGreaterEqual(parse(r)["stores"][CC]["target_copy_empty_cid"], 1)

    def test_same_account_needs_the_flag(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 6)
        r = self.h.run("finish", "--wait-login", "0", "--allow-same-account")
        self.assertEqual(r.returncode, 0, r.stdout)


class Login(Base):
    def test_reasons(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual((r.returncode, parse(r)["login"]["reason"]),
                         (6, "app_not_switched"))
        switch_to_target(self.h)
        self.h.accounts(cli=A, app=B)
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual((r.returncode, parse(r)["login"]["reason"]),
                         (6, "cli_lags"))
        self.h.accounts(raw_cli="{broken")
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual((r.returncode, parse(r)["login"]["reason"]),
                         (6, "identity_invalid"))
        os.unlink(os.path.join(self.h.home, ".claude.json"))
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual((r.returncode, parse(r)["login"]["reason"]),
                         (6, "identity_invalid"))

    def test_waits_for_the_cli_and_then_succeeds(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        self.h.accounts(cli=A, app=B)
        t0 = time.monotonic()
        p = subprocess.Popen([sys.executable, ENTRY, "finish", "--wait-login", "8",
                              "--json"], env=self.h.env(), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        time.sleep(1.5)
        self.h.accounts(cli=B, app=B)
        out, _ = p.communicate(timeout=30)
        elapsed = time.monotonic() - t0
        self.assertEqual(p.returncode, 0, out)
        self.assertGreaterEqual(json.loads(out)["login"]["waited_s"], 1.3)
        self.assertLess(elapsed, 7.5)

    def test_never_logging_in_times_out_at_the_limit(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.h.accounts(cli=A, app=B)
        t0 = time.monotonic()
        r = self.h.run("finish", "--wait-login", "1.5")
        self.assertEqual(r.returncode, 6)
        self.assertGreaterEqual(time.monotonic() - t0, 1.4)


class Baseline(Base):
    def test_no_snapshot_is_exit_7(self):
        build_source(self.h)
        self.h.accounts(cli=A, app=A)
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 7)

    def test_bad_move_id_and_tampering(self):
        build_source(self.h)
        rep = parse(self.h.run("prepare", "--json"))
        self.assertEqual(self.h.run("finish", "--wait-login", "0", "--move-id",
                                    "../../etc").returncode, 7)
        self.assertEqual(self.h.run("finish", "--wait-login", "0", "--move-id",
                                    "20260101T000000000Z-abcdef12-abcdef12"
                                    ).returncode, 7)
        bp = os.path.join(self.h.state, "moves", rep["move_id"], "before.json")
        with open(bp, "a") as fh:
            fh.write(" ")
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 7)

    def test_old_snapshot_is_rejected(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        r = self.h.run("finish", "--wait-login", "0", "--max-age-hours", "0")
        self.assertEqual(r.returncode, 7)

    def test_unready_snapshot_needs_the_diagnostic_flag(self):
        build_source(self.h, empty_cid_live=True)
        r = self.h.run("prepare", "--json")
        self.assertEqual(r.returncode, 3)
        rep = parse(r)
        self.assertFalse(rep["ready"])
        self.assertFalse(dict((c["id"], c["ok"]) for c in rep["checks"])["empty_cid_live"])
        self.assertFalse(os.path.exists(os.path.join(self.h.state,
                                                     "current-ready-move-id")))
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 7)
        r = self.h.run("finish", "--wait-login", "0", "--allow-same-account",
                       "--allow-unready-baseline", "--move-id", rep["move_id"])
        self.assertEqual(r.returncode, 3)

    def test_pointer_moves_only_on_ready(self):
        build_source(self.h)
        first = parse(self.h.run("prepare", "--json"))["move_id"]
        self.h.card(os.path.join(self.h.support, CC, A, O1), 16, "")
        self.h.transcript(uid(16, 9))
        self.assertEqual(self.h.run("prepare").returncode, 3)
        with open(os.path.join(self.h.state, "current-ready-move-id")) as fh:
            self.assertEqual(fh.read().strip(), first)


class Safety(Base):
    def test_state_directory_must_not_overlap_inputs(self):
        build_source(self.h)
        for bad in (os.path.join(self.h.support, "state"), self.h.home,
                    os.path.join(self.h.home, ".claude", "projects", "x"),
                    self.h.support):
            r = self.h.run("prepare", "--state-dir", bad)
            self.assertEqual(r.returncode, 2, bad)

    def test_nothing_outside_state_changes(self):
        build_source(self.h)
        before = tree_digest(self.h.home)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        after_switch = tree_digest(self.h.home)
        self.h.run("finish", "--wait-login", "0")
        self.assertEqual(tree_digest(self.h.home), after_switch)
        self.assertNotEqual(before, after_switch)
        self.assertTrue(os.path.isdir(self.h.state))

    def test_full_copy_is_opt_in(self):
        build_source(self.h)
        rep = parse(self.h.run("prepare", "--json"))
        self.assertFalse(os.path.exists(os.path.join(
            self.h.state, "moves", rep["move_id"], "cards-copy")))
        rep = parse(self.h.run("prepare", "--json", "--full-copy"))
        self.assertGreaterEqual(rep["cards_copied"], 4)

    def test_json_mode_prints_exactly_one_document(self):
        build_source(self.h)
        r = self.h.run("prepare", "--json")
        json.loads(r.stdout)           # raises on any extra text
        self.assertIn("VERDICT", r.stderr)

    def test_sync_never_overwrites_and_can_be_undone(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        target = os.path.join(self.h.support, CC, B, O2)
        keep = os.path.join(target, "local_%s.json" % uid(11, 9))
        with open(keep, "w") as fh:
            fh.write('{"cliSessionId": "keep-me"}')
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        with open(keep) as fh:
            self.assertIn("keep-me", fh.read())
        journal = [f for f in os.listdir(self.h.state) if f.startswith("sync-")][0]
        self.assertEqual(self.h.run("sync", "--undo", os.path.join(
            self.h.state, journal)).returncode, 0)
        self.assertEqual(sorted(f for f in os.listdir(target)
                                if f.startswith("local_")),
                         ["local_%s.json" % uid(11, 9)])

    def test_sync_does_not_copy_deleted_sessions(self):
        src = build_source(self.h)
        open(os.path.join(src, "deleted_%s" % uid(12, 9)), "w").close()
        self.assertEqual(self.h.run("prepare").returncode, 3)   # zombie
        switch_to_target(self.h)
        self.h.run("sync", "--apply")
        target = os.path.join(self.h.support, CC, B, O2)
        self.assertNotIn("local_%s.json" % uid(12, 9), os.listdir(target))


if __name__ == "__main__":
    unittest.main()
