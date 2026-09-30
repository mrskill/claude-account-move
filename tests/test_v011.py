"""Regression cases for the 0.1.1 review: each one failed on 0.1.0 (see the
table in the release notes) and passes now."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fixture import CC, LAM, Home, tree_digest, uid  # noqa: E402
from test_cli import A, B, O1, O2, build_source, parse, switch_to_target  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-v011-")
        self.h = Home(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def moved(self):
        """Source built, prepared, switched, synced: a clean finish would be 0."""
        src = build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        return src, os.path.join(self.h.support, CC, B, O2)

    def finish(self, *extra):
        return self.h.run("finish", "--wait-login", "0", "--json", *extra)

    def journal(self):
        names = [f for f in os.listdir(self.h.state) if f.startswith("sync-")]
        self.assertTrue(names)
        return os.path.join(self.h.state, sorted(names)[-1])


class C1UndoOwnership(Base):
    def test_same_bytes_but_not_our_file_is_kept(self):
        _, target = self.moved()
        victim = os.path.join(target, "local_%s.json" % uid(11, 9))
        with open(victim, "rb") as fh:
            data = fh.read()
        os.unlink(victim)                        # another writer recreates it
        with open(victim, "wb") as fh:           # identical bytes, new inode
            fh.write(data)
        r = self.h.run("sync", "--undo", self.journal(), "--json")
        self.assertTrue(os.path.exists(victim))
        self.assertEqual(r.returncode, 3)
        self.assertEqual(parse(r)["undo"]["ambiguous"], 1)
        self.assertEqual(sorted(f for f in os.listdir(target) if f.startswith("local_")),
                         ["local_%s.json" % uid(11, 9)])

    def test_interrupted_intent_does_not_authorise_deleting_a_foreign_card(self):
        build_source(self.h)
        switch_to_target(self.h)
        target = os.path.join(self.h.support, CC, B, O2)
        card = self.h.card(target, 11, uid(11))
        path = os.path.join(target, card)
        with open(path, "rb") as fh:
            import hashlib
            digest = hashlib.sha256(fh.read()).hexdigest()
        from claude_account_move import sync_cards
        from claude_account_move.panel_snapshot import path_id
        os.makedirs(self.h.state)
        jp = os.path.join(self.h.state, "crash.jsonl")
        with open(jp, "w") as fh:
            fh.write(json.dumps({"op": "begin", "home_id": path_id(self.h.home),
                                 "op_id": "x", "version": 1,
                                 "targets": [os.path.realpath(target)]}) + "\n")
            fh.write(json.dumps({"op": "intent", "path": path, "src": "s",
                                 "sha256": digest, "dev": 1, "ino": 1,
                                 "op_id": "x"}) + "\n")
        r = self.h.run("sync", "--undo", jp)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(r.returncode, 3)

    def test_journal_of_another_home_is_rejected(self):
        self.moved()
        jp = self.journal()
        other = Home(os.path.join(self.tmp, "second"))
        build_source(other)
        r = other.run("sync", "--undo", jp)
        self.assertEqual(r.returncode, 2)
        self.assertIn("another home", r.stdout)

    def test_undo_removes_what_the_run_published(self):
        _, target = self.moved()
        r = self.h.run("sync", "--undo", self.journal(), "--json")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(parse(r)["undo"]["removed"], 4)
        self.assertEqual([f for f in os.listdir(target) if f.startswith("local_")], [])


class M6Journal(Base):
    def test_torn_tail_keeps_the_intact_prefix(self):
        _, target = self.moved()
        jp = self.journal()
        with open(jp) as fh:
            lines = fh.read().split("\n")
        lines = [ln for ln in lines if ln]
        with open(jp, "w") as fh:               # cut the last record in half
            fh.write("\n".join(lines[:-1]) + "\n" + lines[-1][:15])
        r = self.h.run("sync", "--undo", jp, "--json")
        rep = parse(r)["undo"]
        self.assertEqual(rep["status"], "torn_tail")
        self.assertGreaterEqual(rep["removed"], 3)
        self.assertEqual(r.returncode, 3)

    def test_malformed_records_are_an_input_error_not_a_crash(self):
        build_source(self.h)
        os.makedirs(self.h.state)
        for body in ("[1, 2]\n", '{"no_op": 1}\n', "not json at all\n", ""):
            jp = os.path.join(self.h.state, "bad.jsonl")
            with open(jp, "w") as fh:
                fh.write(body)
            r = self.h.run("sync", "--undo", jp)
            self.assertEqual(r.returncode, 2, body)

    def test_two_runs_never_share_a_journal(self):
        self.moved()
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        names = [f for f in os.listdir(self.h.state) if f.startswith("sync-")]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 2)

    def test_journal_is_never_opened_through_a_symlink(self):
        from claude_account_move import sync_cards
        real = os.path.join(self.tmp, "victim.txt")
        with open(real, "w") as fh:
            fh.write("keep")
        link = os.path.join(self.tmp, "j.jsonl")
        os.symlink(real, link)
        with self.assertRaises(OSError):
            sync_cards.Journal(link)
        with open(real) as fh:
            self.assertEqual(fh.read(), "keep")


class C2TargetBinding(Base):
    def rebind(self, target, n, cid):
        path = os.path.join(target, "local_%s.json" % uid(n, 9))
        os.unlink(path)
        self.h.card(target, n, cid)

    def test_wrong_nonempty_session_id_fails(self):
        _, target = self.moved()
        self.rebind(target, 11, uid(77))          # new id, no transcript
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(parse(r)["stores"][CC]["target_copy_wrong_cid"], 1)

    def test_id_of_another_existing_session_fails(self):
        _, target = self.moved()
        self.rebind(target, 11, uid(12))          # a real transcript, wrong session
        r = self.finish()
        self.assertEqual(r.returncode, 3)
        self.assertEqual(parse(r)["stores"][CC]["target_copy_wrong_cid"], 1)

    def test_stale_target_copy_fails_or_waits(self):
        _, target = self.moved()
        path = os.path.join(target, "local_%s.json" % uid(11, 9))
        os.unlink(path)
        self.h.card(target, 11, uid(11), act=1, turns=0)
        r = self.finish()
        self.assertEqual(r.returncode, 3)
        self.assertEqual(parse(r)["stores"][CC]["target_copy_regressed"], 1)
        hb = os.path.join(self.tmp, "hb")
        open(hb, "w").close()
        r = self.finish("--heartbeat", hb, "--wait-sync", "0")
        self.assertEqual(r.returncode, 5)


class C3TranscriptContent(Base):
    def test_all_transcripts_truncated_fails(self):
        self.moved()
        for f in os.listdir(self.h.projects):
            open(os.path.join(self.h.projects, f), "w").close()
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertGreaterEqual(parse(r)["transcripts_bad"], 4)

    def test_one_older_transcript_truncated_is_not_masked(self):
        self.moved()
        open(os.path.join(self.h.projects, uid(12) + ".jsonl"), "w").close()
        r = self.finish()
        self.assertEqual(r.returncode, 3)
        bad = [c for c in parse(r)["checks"] if c["id"] == "transcripts"][0]
        self.assertFalse(bad["ok"])

    def test_newest_unmeasurable_after_measured_before_fails(self):
        self.moved()
        for f in os.listdir(self.h.projects):      # only non-message records left
            with open(os.path.join(self.h.projects, f), "w") as fh:
                fh.write(json.dumps({"type": "queue-operation",
                                     "timestamp": "2026-01-01T00:00:00Z",
                                     "pad": "x" * 2000}) + "\n")
        r = self.finish()
        self.assertEqual(r.returncode, 3)
        ids = {c["id"]: c["ok"] for c in parse(r)["checks"]}
        self.assertFalse(ids["newest_message"])

    def test_unreadable_transcript_at_prepare_blocks_readiness(self):
        build_source(self.h)
        p = os.path.join(self.h.projects, uid(11) + ".jsonl")
        os.chmod(p, 0)
        try:
            r = self.h.run("prepare", "--json")
        finally:
            os.chmod(p, 0o600)
        self.assertEqual(r.returncode, 3)
        self.assertFalse(parse(r)["ready"])


class M1ActivePairOverride(Base):
    def test_override_naming_the_source_account_is_refused(self):
        self.moved()
        for cmd in (("finish", "--wait-login", "0"), ("sync",), ("sync", "--apply")):
            r = self.h.run(*cmd, "--active-pair", "%s/%s" % (A, O1))
            self.assertEqual(r.returncode, 2, cmd)

    def test_malformed_override_and_wrong_account_at_prepare(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare", "--active-pair", "nonsense").returncode, 2)
        self.assertEqual(self.h.run("prepare", "--active-pair",
                                    "%s/%s" % (B, O2)).returncode, 2)
        self.assertEqual(self.h.run("prepare", "--active-pair",
                                    "%s/%s" % (A, O1)).returncode, 0)

    def test_nothing_is_copied_into_the_source_by_a_bad_override(self):
        self.moved()
        extra = self.h.pair(CC, uid(5), uid(6))   # a card the source pair lacks
        self.h.card(extra, 30, uid(30))
        self.h.transcript(uid(30))
        before = tree_digest(self.h.support)
        self.h.run("sync", "--apply", "--active-pair", "%s/%s" % (A, O1))
        self.assertEqual(tree_digest(self.h.support), before)


class M2StateSymlinks(Base):
    def test_moves_symlink_into_the_store_is_refused(self):
        build_source(self.h)
        os.makedirs(self.h.state)
        store_dir = os.path.join(self.h.support, CC, A, O1)
        os.symlink(store_dir, os.path.join(self.h.state, "moves"))
        before = tree_digest(self.h.support)
        r = self.h.run("prepare", "--json")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(tree_digest(self.h.support), before)
        self.assertEqual(parse(r)["exit_code"], 1)

    def test_safe_subdir_refuses_links_and_escapes(self):
        from claude_account_move.common import Paths, safe_subdir
        build_source(self.h)
        paths = Paths(self.h.home, self.h.state)
        os.makedirs(self.h.state)
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.h.state, "moves"))
        with self.assertRaises(OSError):
            safe_subdir(paths, "moves")
        self.assertEqual(os.listdir(outside), [])


class M3Settings(Base):
    def put(self, data):
        d = os.path.join(self.h.home, ".claude")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "settings.json"), "w") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))

    def base(self):
        return {"permissions": {"allow": ["Read", "Write"], "deny": ["Bash(rm:*)"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "a"}]}]}}

    def test_replaced_permission_with_same_counts_is_detected(self):
        self.put(self.base())
        self.moved()
        s = self.base()
        s["permissions"]["allow"] = ["Read", "Edit"]
        self.put(s)
        r = self.finish()
        self.assertEqual(r.returncode, 3)
        self.assertIn("permissions_digest", [c for c in parse(r)["checks"]
                                             if c["id"] == "settings"][0]["detail"])

    def test_changed_hook_command_and_event_with_same_counts(self):
        self.put(self.base())
        self.moved()
        s = self.base()
        s["hooks"]["Stop"][0]["hooks"][0]["command"] = "b"
        self.put(s)
        self.assertEqual(self.finish().returncode, 3)
        s = self.base()
        s["hooks"] = {"Start": s["hooks"]["Stop"]}
        self.put(s)
        self.assertEqual(self.finish().returncode, 3)

    def test_reordered_permissions_are_not_a_change(self):
        self.put(self.base())
        self.moved()
        s = self.base()
        s["permissions"]["allow"] = ["Write", "Read"]
        self.put(s)
        self.assertEqual(self.finish().returncode, 0)

    def test_unreadable_settings_at_prepare_blocks_readiness(self):
        build_source(self.h)
        self.put("{broken")
        r = self.h.run("prepare", "--json")
        self.assertEqual(r.returncode, 3)
        ids = {c["id"]: c["ok"] for c in parse(r)["checks"]}
        self.assertFalse(ids["settings_readable"])


class M4SourceIdentity(Base):
    def test_unreadable_app_account_blocks_readiness(self):
        build_source(self.h)
        os.unlink(os.path.join(self.h.support, "config.json"))
        r = self.h.run("prepare", "--json")
        self.assertEqual(r.returncode, 3)
        self.assertFalse({c["id"]: c["ok"] for c in parse(r)["checks"]}["source_identity"])

    def test_ready_snapshot_without_a_source_is_rejected_at_finish(self):
        from claude_account_move import panel_snapshot as snap
        from claude_account_move import store as st
        from claude_account_move.common import Paths, read_identity
        build_source(self.h)
        paths = Paths(self.h.home, self.h.state)
        idx = st.scan(paths)
        tmap = st.transcripts(paths)
        ident = read_identity(paths)
        ident["app"] = {"status": "absent", "value": None}
        before = snap.build_before(paths, idx, tmap, ident)
        snap.write_snapshot(paths, before, True, idx)
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 7)


class M5SyncHonesty(Base):
    def test_no_target_pair_in_the_log_is_not_success(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        self.h.pair(CC, B, O2)
        self.h.accounts(cli=B, app=B)
        self.h.log((A, O1))                     # the log never names B
        for extra in ((), ("--apply",)):
            r = self.h.run("sync", *extra)
            self.assertEqual(r.returncode, 3, r.stdout)
            self.assertIn("cannot determine", r.stdout)

    def test_unreadable_source_card_is_not_success(self):
        src = build_source(self.h)
        switch_to_target(self.h)
        with open(os.path.join(src, "local_broken.json"), "w") as fh:
            fh.write("{oops")
        for extra in ((), ("--apply",)):
            self.assertEqual(self.h.run("sync", *extra).returncode, 3)
        target = os.path.join(self.h.support, CC, B, O2)
        self.assertEqual([f for f in os.listdir(target) if f.startswith("local_")], [])

    def test_dry_run_with_a_conflict_is_nonzero(self):
        build_source(self.h)
        other = self.h.pair(CC, uid(5), uid(6))
        self.h.card(other, 11, uid(99))
        switch_to_target(self.h)
        self.assertEqual(self.h.run("sync").returncode, 3)

    def test_complete_target_is_zero(self):
        self.moved()
        r = self.h.run("sync", "--json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(parse(r)["planned"], 0)

    def test_dry_run_lists_the_files(self):
        build_source(self.h)
        switch_to_target(self.h)
        r = self.h.run("sync")
        self.assertIn("would copy local_%s.json" % uid(11, 9), r.stdout)


class M7JsonEverywhere(Base):
    def test_every_outcome_prints_one_json_document(self):
        cases = [
            (("prepare",), 4),                                   # nothing installed
            (("finish", "--wait-login", "0"), 4),
            (("sync",), 4),
            (("prepare", "--state-dir", self.h.home), 2),
        ]
        for args, code in cases:
            r = self.h.run(*args, "--json")
            self.assertEqual(r.returncode, code, args)
            self.assertEqual(json.loads(r.stdout)["exit_code"], code, args)

    def test_no_baseline_login_timeout_and_sync_paths(self):
        build_source(self.h)
        self.assertEqual(parse(self.h.run("finish", "--wait-login", "0", "--json"))
                         ["exit_code"], 7)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertEqual((r.returncode, parse(r)["exit_code"]), (6, 6))
        switch_to_target(self.h)
        self.assertEqual(parse(self.h.run("sync", "--json"))["command"], "sync")
        r = self.h.run("sync", "--apply", "--json")
        self.assertEqual(parse(r)["copied"], 4)
        r = self.h.run("sync", "--undo", self.journal(), "--json")
        self.assertEqual(parse(r)["command"], "sync")
        r = self.h.run("sync", "--undo", os.path.join(self.tmp, "nope"), "--json")
        self.assertEqual((r.returncode, parse(r)["exit_code"]), (2, 2))


class M8TaskSchema(Base):
    def prepare_with(self, content):
        d = build_source(self.h)
        with open(os.path.join(d, "scheduled-tasks.json"), "w") as fh:
            fh.write(content if isinstance(content, str) else json.dumps(content))
        return self.h.run("prepare", "--json")

    def test_malformed_but_valid_json_is_not_an_empty_baseline(self):
        for bad in ({"scheduledTasks": {"task-1": {"enabled": True}}},
                    {"scheduledTasks": ["task-1"]},
                    {"scheduledTasks": [{"enabled": True}]},
                    {"scheduledTasks": [{"id": "a", "enabled": "yes"}]},
                    {"scheduledTasks": [{"id": "a", "approvedPermissions": "x"}]},
                    {"scheduledTasks": "none"},
                    [1, 2]):
            r = self.prepare_with(bad)
            self.assertEqual(r.returncode, 3, bad)
            self.assertFalse(parse(r)["ready"], bad)
            shutil.rmtree(self.h.state, ignore_errors=True)
            shutil.rmtree(os.path.join(self.h.home, "Library"), ignore_errors=True)
            shutil.rmtree(os.path.join(self.h.home, ".claude"), ignore_errors=True)

    def test_valid_empty_or_absent_list_is_fine(self):
        self.assertEqual(self.prepare_with({"scheduledTasks": []}).returncode, 0)
        shutil.rmtree(os.path.join(self.h.home, "Library"))
        shutil.rmtree(os.path.join(self.h.home, ".claude"))
        shutil.rmtree(self.h.state)
        self.assertEqual(self.prepare_with({"recordedSkips": []}).returncode, 0)


class M3DocumentedPrecedence(Base):
    def test_no_pairs_beats_missing_snapshot(self):
        os.makedirs(os.path.join(self.h.support, CC))
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 4)


if __name__ == "__main__":
    unittest.main()
