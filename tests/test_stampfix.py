"""Counterexamples from the stamp review. Each fails on the previous commit."""
import errno
import fcntl
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import CC, Home, uid  # noqa: E402
from test_cli import A, B, O1, O2, build_source, parse  # noqa: E402

from claude_account_move import stamp_titles  # noqa: E402
from claude_account_move import store as st  # noqa: E402
from claude_account_move import sync_cards  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402
from claude_account_move.panel_snapshot import path_id  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-stampfix-")
        self.h = Home(self.tmp)
        self.d = self.h.pair(CC, A, O1)
        self.h.accounts(cli=A, app=A)
        self.h.log((A, O1))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def card(self, n=11, title="Fix the login"):
        name = self.h.card(self.d, n, uid(n), title=title)
        self.h.transcript(uid(n))
        return os.path.join(self.d, name)

    def read(self, p):
        with open(p, "rb") as fh:
            return fh.read()

    def paths(self):
        return Paths(self.h.home, self.h.state)

    def plan(self):
        paths = self.paths()
        idx = st.scan(paths)
        return idx, stamp_titles.plan_stamps(idx, st.transcripts(paths))

    def journal(self):
        names = sorted(f for f in os.listdir(self.h.state) if f.startswith("stamp-"))
        return os.path.join(self.h.state, names[-1])

    def stamped_card(self):
        p = self.card()
        original = self.read(p)
        self.assertEqual(self.h.run("stamp", "--apply").returncode, 0)
        stamped = self.read(p)
        self.assertNotEqual(stamped, original)
        return p, original, stamped

    def undo(self):
        return stamp_titles.undo_stamps(self.journal(), path_id(self.h.home),
                                        os.path.realpath(self.h.support))

    def no_leftovers(self):
        return [f for f in os.listdir(self.d) if f.startswith((".stamp-", ".undo-"))]


class S1UndoNeverLosesTheCard(Base):
    def check_failure(self, patcher):
        p, original, stamped = self.stamped_card()
        with patcher:
            res = self.undo()
        self.assertTrue(os.path.exists(p), "the card vanished from its path")
        self.assertEqual(self.read(p), stamped)          # still the stamped card
        self.assertEqual(res["restored"], 0)
        self.assertEqual(res["ambiguous"], 1)
        self.assertEqual(res["status"], "ok")
        # and a later undo, once the fault is gone, completes
        res = self.undo()
        self.assertEqual(res["restored"], 1)
        self.assertEqual(self.read(p), original)

    def test_temp_file_creation_fails(self):
        self.check_failure(mock.patch.object(stamp_titles.tempfile, "mkstemp",
                                             side_effect=OSError(errno.ENOSPC, "full")))

    def test_fsync_fails(self):
        self.check_failure(mock.patch.object(stamp_titles.os, "fsync",
                                             side_effect=OSError(errno.EIO, "io")))

    def test_chmod_fails(self):
        self.check_failure(mock.patch.object(stamp_titles.os, "chmod",
                                             side_effect=OSError(errno.EPERM, "perm")))

    def test_link_fails_for_lack_of_space_the_card_stays_beside_its_name(self):
        p, original, stamped = self.stamped_card()
        with mock.patch.object(stamp_titles.os, "link",
                               side_effect=OSError(errno.ENOSPC, "full")):
            res = self.undo()
        self.assertEqual((res["restored"], res["ambiguous"]), (0, 1))
        self.assertEqual(len(res["aside"]), 1)
        self.assertEqual(self.read(res["aside"][0]), stamped)     # nothing lost
        self.assertFalse(os.path.exists(p))                        # and no blind rename
        os.rename(res["aside"][0], p)                              # manual recovery
        self.assertEqual(self.undo()["restored"], 1)
        self.assertEqual(self.read(p), original)

    def test_no_overwriting_fallback_when_a_newer_card_appears_during_recovery(self):
        p, original, stamped = self.stamped_card()
        rp = os.path.realpath(p)
        created = {"n": 0}
        real_lexists = os.path.lexists

        def lexists(path):
            ans = real_lexists(path)
            if path == rp and not ans and not created["n"]:
                created["n"] = 1                   # the app writes a newer card now
                with open(rp, "w") as fh:
                    fh.write('{"newer": true}')
            return ans

        with mock.patch.object(stamp_titles.os, "link",
                               side_effect=OSError(errno.ENOSPC, "full")), \
                mock.patch.object(stamp_titles.os.path, "lexists", lexists):
            res = self.undo()
        if created["n"]:
            self.assertEqual(self.read(p), b'{"newer": true}')    # never overwritten
        self.assertTrue(res["aside"])
        self.assertEqual(self.read(res["aside"][0]), stamped)

    def test_a_newer_card_that_appeared_meanwhile_is_kept_and_so_is_ours(self):
        p, original, stamped = self.stamped_card()
        real_link = os.link
        rp = os.path.realpath(p)

        def newer_appears(src, dst, **kw):
            if dst == rp:
                with open(p, "w") as fh:
                    fh.write('{"newer": true}')
            return real_link(src, dst, **kw)

        with mock.patch.object(stamp_titles.os, "link", side_effect=newer_appears):
            res = self.undo()
        self.assertEqual(self.read(p), b'{"newer": true}')
        self.assertEqual(res["ambiguous"], 1)
        aside = [f for f in os.listdir(self.d) if f.startswith(".undo-")]
        self.assertEqual(len(aside), 1)
        self.assertEqual(self.read(os.path.join(self.d, aside[0])), stamped)


class S2OneDocumentPlan(Base):
    def test_title_changed_between_scan_and_plan_is_planned_coherently(self):
        p = self.card(11, "Old")
        paths = self.paths()
        idx = st.scan(paths)                     # scan sees the title "Old"
        with open(p) as fh:
            doc = json.load(fh)
        doc["title"] = "New"
        with open(p, "w") as fh:
            json.dump(doc, fh)
        items, _ = stamp_titles.plan_stamps(idx, st.transcripts(paths))
        self.assertEqual(items[0]["title"], "New")
        self.assertTrue(items[0]["new"].startswith("New "))
        os.makedirs(self.h.state)
        jp = os.path.join(self.h.state, "j.jsonl")
        res = stamp_titles.apply_stamps(items, jp, path_id(self.h.home))
        self.assertEqual(res["written"], 1)
        with open(p) as fh:
            self.assertTrue(json.load(fh)["title"].startswith("New "))
        self.assertEqual(stamp_titles.undo_stamps(
            jp, path_id(self.h.home), os.path.realpath(self.h.support))["restored"], 1)
        with open(p) as fh:
            self.assertEqual(json.load(fh)["title"], "New")

    def test_session_id_changed_between_scan_and_plan_uses_the_new_binding(self):
        p = self.card(11, "Title")
        self.h.transcript(uid(12), last_ms=1_700_000_000_000)    # another session
        paths = self.paths()
        idx = st.scan(paths)
        with open(p) as fh:
            doc = json.load(fh)
        doc["cliSessionId"] = uid(12)
        with open(p, "w") as fh:
            json.dump(doc, fh)
        items, _ = stamp_titles.plan_stamps(idx, st.transcripts(paths))
        import datetime
        want = datetime.datetime.fromtimestamp(1_700_000_000).astimezone().strftime(
            "%d.%m %H:%M")
        self.assertTrue(items[0]["new"].endswith(want))


class S3ConditionalReplacement(Base):
    def inject_after_last_read(self, p):
        """Another writer updates the card at the last moment before publication."""
        real_replace, real_rename = os.replace, os.rename
        done = {"n": 0}
        rp = os.path.realpath(p)

        def mutate():
            with open(p) as fh:
                doc = json.load(fh)
            doc["completedTurns"] = 99
            with open(p, "w") as fh:
                json.dump(doc, fh)

        def replace(src, dst, **kw):
            if dst == rp and not done["n"]:
                done["n"] = 1
                mutate()
            return real_replace(src, dst, **kw)

        def rename(src, dst, **kw):
            if src == rp and not done["n"]:
                done["n"] = 1
                mutate()
            return real_rename(src, dst, **kw)

        return mock.patch.multiple(stamp_titles.os, replace=replace, rename=rename)

    def test_update_after_the_final_read_is_not_discarded(self):
        p = self.card()
        _, (items, _) = self.plan()
        os.makedirs(self.h.state)
        with self.inject_after_last_read(p):
            res = stamp_titles.apply_stamps(items, os.path.join(self.h.state, "j.jsonl"),
                                            path_id(self.h.home))
        self.assertEqual(res["written"], 0)
        doc = json.loads(self.read(p))
        self.assertEqual(doc["completedTurns"], 99)      # the newer document survived
        self.assertEqual(doc["title"], "Fix the login")
        self.assertEqual(self.no_leftovers(), [])

    def test_late_write_through_an_open_descriptor_is_kept_aside_nothing_overwritten(self):
        p = self.card()
        _, (items, _) = self.plan()
        os.makedirs(self.h.state)
        real_link = os.link
        rp = os.path.realpath(p)

        def late_write(src, dst, **kw):
            if dst == rp:                             # the old file is already aside
                for f in os.listdir(self.d):
                    if f.startswith(".stamp-old-"):
                        with open(os.path.join(self.d, f), "a") as fh:
                            fh.write(" ")
            return real_link(src, dst, **kw)

        with mock.patch.object(stamp_titles.os, "link", side_effect=late_write):
            res = stamp_titles.apply_stamps(items, os.path.join(self.h.state, "j.jsonl"),
                                            path_id(self.h.home))
        self.assertEqual(res["changed"], 1)
        aside = [f for f in os.listdir(self.d) if f.startswith(".stamp-old-")]
        self.assertEqual(len(aside), 1)                # the newer document is kept
        self.assertTrue(self.read(os.path.join(self.d, aside[0])).endswith(b" "))

    def test_two_writers_cannot_run_at_once_even_with_different_state_dirs(self):
        self.card()
        fd = os.open(os.path.realpath(self.h.support), os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            r = self.h.run("stamp", "--apply", "--json")
            self.assertEqual(r.returncode, 3, r.stdout)
            self.assertEqual(parse(r)["verdict"], "busy")
            other_state = os.path.join(self.tmp, "state-two")
            r = self.h.run("stamp", "--apply", "--state-dir", other_state)
            self.assertEqual(r.returncode, 3)
            self.assertEqual(self.h.run("sync", "--apply").returncode, 3)
        finally:
            os.close(fd)
        self.assertEqual(self.h.run("stamp", "--apply").returncode, 0)


class S4StoreRootOutsideSupport(Base):
    def test_a_symlinked_store_root_is_refused(self):
        build_source(self.h)
        external = os.path.join(self.tmp, "external-store")
        os.makedirs(os.path.join(external, A, O1))
        ext_card = self.h.card(os.path.join(external, A, O1), 21, uid(21))
        self.h.transcript(uid(21))
        store_link = os.path.join(self.h.support, "local-agent-mode-sessions")
        shutil.rmtree(store_link)
        os.symlink(external, store_link)
        before = self.read(os.path.join(external, A, O1, ext_card))
        for cmd in (("stamp", "--apply"), ("sync", "--apply"), ("prepare",)):
            r = self.h.run(*cmd)
            self.assertNotEqual(r.returncode, 0, cmd)
        self.assertEqual(self.read(os.path.join(external, A, O1, ext_card)), before)
        self.assertEqual(os.listdir(os.path.join(external, A, O1)), [ext_card])

    def test_apply_refuses_a_path_outside_support_even_if_planned(self):
        build_source(self.h)
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        name = self.h.card(outside, 31, uid(31))
        path = os.path.join(outside, name)
        raw = self.read(path).decode()
        item = {"path": path, "title": "t", "new": "t X", "when": None, "raw": raw}
        os.makedirs(self.h.state)
        res = stamp_titles.apply_stamps([item], os.path.join(self.h.state, "j.jsonl"),
                                        path_id(self.h.home),
                                        support_real=os.path.realpath(self.h.support))
        self.assertEqual((res["written"], res["skipped"]), (0, 1))
        self.assertEqual(self.read(path).decode(), raw)


class ObservationsAfterPlanning(Base):
    def test_unreadable_transcript_blocks_the_whole_stamp(self):
        ok = self.card(11, "Fine")
        bad = self.card(12, "Locked")
        tp = os.path.join(self.h.projects, uid(12) + ".jsonl")
        os.chmod(tp, 0)
        try:
            r = self.h.run("stamp", "--apply", "--json")
        finally:
            os.chmod(tp, 0o600)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(parse(r)["verdict"], "incomplete_observation")
        with open(ok) as fh:
            self.assertEqual(json.load(fh)["title"], "Fine")      # nothing written

    def test_conflicting_session_ids_behind_one_name_are_refused(self):
        a = self.card(11, "Shared")
        other = self.h.pair(CC, uid(5), uid(6))
        self.h.card(other, 11, uid(99), title="Shared")          # same name, other session
        self.h.transcript(uid(99), last_ms=1_900_000_000_000)
        before = self.read(a)
        r = self.h.run("stamp", "--apply", "--json")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(self.read(a), before)

    def test_card_that_becomes_unreadable_while_planning_is_reported(self):
        p = self.card(11, "x")
        paths = self.paths()
        idx = st.scan(paths)
        with open(p, "w") as fh:
            fh.write("{oops")
        stamp_titles.plan_stamps(idx, st.transcripts(paths))
        self.assertFalse(idx.obs.complete())


class JournalAcknowledgement(Base):
    def test_failed_acknowledgement_does_not_disable_undo(self):
        p = self.card()
        original = self.read(p)
        _, (items, _) = self.plan()
        os.makedirs(self.h.state)
        jp = os.path.join(self.h.state, "j.jsonl")
        real_add = sync_cards.Journal.add

        def add(self, **rec):
            if rec.get("op") == "done":
                raise OSError(errno.EIO, "transient")
            return real_add(self, **rec)

        with mock.patch.object(sync_cards.Journal, "add", add):
            res = stamp_titles.apply_stamps(items, jp, path_id(self.h.home))
        self.assertEqual((res["written"], res["unacknowledged"], res["failed"]), (1, 1, 0))
        undone = stamp_titles.undo_stamps(jp, path_id(self.h.home),
                                          os.path.realpath(self.h.support))
        self.assertEqual(undone["restored"], 1)
        self.assertEqual(self.read(p), original)


class SyncUndoLateWrite(Base):
    def test_write_through_an_open_descriptor_during_undo_is_not_deleted(self):
        support = os.path.join(self.tmp, "support")
        src_dir, dst_dir = os.path.join(support, "a"), os.path.join(support, "b")
        os.makedirs(src_dir)
        os.makedirs(dst_dir)
        src = os.path.join(src_dir, "local_a.json")
        with open(src, "w") as fh:
            fh.write('{"x": 1}')
        jp = os.path.join(self.tmp, "j.jsonl")
        sync_cards.apply_plan([{"store": CC, "name": "local_a.json", "src": src,
                                "dst_dir": dst_dir, "create_dir": False}], jp, "h")
        real_sha = sync_cards.sha256_file

        def writer_appends(path):
            digest = real_sha(path)
            if os.path.basename(path).startswith(".undo-"):
                with open(path, "a") as fh:       # an app with the file open
                    fh.write(" late")
            return digest

        with mock.patch.object(sync_cards, "sha256_file", writer_appends):
            res = sync_cards.undo(jp, "h", os.path.realpath(support))
        dst = os.path.join(dst_dir, "local_a.json")
        self.assertEqual(res["removed"], 0)
        self.assertEqual(res["ambiguous"], 1)
        with open(dst) as fh:
            self.assertTrue(fh.read().endswith(" late"))


class AppMustBeClosed(Base):
    def stub(self, code):
        p = os.path.join(self.tmp, "pgrep-%d.sh" % code)
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\nexit %d\n" % code)
        os.chmod(p, 0o755)
        return p

    def test_apply_and_undo_refuse_while_the_app_runs(self):
        p = self.card()
        original = self.read(p)
        r = self.h.run("stamp", "--apply", "--json",
                       CLAUDE_ACCOUNT_MOVE_PGREP=self.stub(0))
        self.assertEqual(r.returncode, 3, r.stdout)
        rep = parse(r)
        self.assertEqual(rep["verdict"], "app_running")
        self.assertIn("Quit the Claude app first", rep["reason"])
        self.assertEqual(self.read(p), original)
        self.assertFalse(os.path.exists(self.h.state))
        self.assertEqual(self.h.run("stamp", "--apply").returncode, 0)   # app closed
        r = self.h.run("stamp", "--undo", self.journal(),
                       CLAUDE_ACCOUNT_MOVE_PGREP=self.stub(0))
        self.assertEqual(r.returncode, 3)
        self.assertIn("Quit the Claude app first", r.stdout)
        self.assertNotEqual(self.read(p), original)                       # still stamped

    def test_dry_run_does_not_need_the_app_closed(self):
        self.card()
        r = self.h.run("stamp", CLAUDE_ACCOUNT_MOVE_PGREP=self.stub(0))
        self.assertEqual(r.returncode, 0)

    def test_the_check_function_and_its_failure_mode(self):
        from claude_account_move import common
        with mock.patch.dict(os.environ, {"CLAUDE_ACCOUNT_MOVE_PGREP": self.stub(0)}):
            self.assertTrue(common.claude_app_running())
        with mock.patch.dict(os.environ, {"CLAUDE_ACCOUNT_MOVE_PGREP": self.stub(1)}):
            self.assertFalse(common.claude_app_running())
        with mock.patch.dict(os.environ, {"CLAUDE_ACCOUNT_MOVE_PGREP": self.stub(2)}):
            self.assertTrue(common.claude_app_running())          # cannot tell: refuse
        with mock.patch.dict(os.environ, {"CLAUDE_ACCOUNT_MOVE_PGREP":
                                          os.path.join(self.tmp, "missing")}):
            self.assertTrue(common.claude_app_running())

    def test_function_can_be_replaced_in_process(self):
        import io
        import contextlib
        from claude_account_move import cli
        self.card()
        buf = io.StringIO()
        with mock.patch.object(cli.common, "claude_app_running", return_value=True), \
                contextlib.redirect_stdout(buf):
            rc = cli.main(["stamp", "--apply", "--json", "--home", self.h.home,
                           "--state-dir", self.h.state])
        self.assertEqual(rc, 3)
        self.assertEqual(json.loads(buf.getvalue())["verdict"], "app_running")

    def test_both_process_patterns_are_asked(self):
        from claude_account_move import common
        log = os.path.join(self.tmp, "calls.txt")
        p = os.path.join(self.tmp, "pgrep-log.sh")
        with open(p, "w") as fh:
            fh.write('#!/bin/sh\necho "$@" >> "%s"\nexit 1\n' % log)
        os.chmod(p, 0o755)
        with mock.patch.dict(os.environ, {"CLAUDE_ACCOUNT_MOVE_PGREP": p}):
            self.assertFalse(common.claude_app_running())
        with open(log) as fh:
            calls = fh.read()
        self.assertIn("-x Claude", calls)
        self.assertIn("/Applications/Claude.app/Contents/", calls)

    def journal(self):
        names = sorted(f for f in os.listdir(self.h.state) if f.startswith("stamp-"))
        return os.path.join(self.h.state, names[-1])

    def read(self, p):
        with open(p, "rb") as fh:
            return fh.read()


class ReportedPathsAndTranscriptReads(Base):
    def test_apply_reports_every_path_kept_beside_its_card(self):
        import contextlib
        import io
        from claude_account_move import cli
        p = self.card()
        rp = os.path.realpath(p)
        real_link = os.link

        def late_write(src, dst, **kw):
            if dst == rp:
                for f in os.listdir(self.d):
                    if f.startswith(".stamp-old-"):
                        with open(os.path.join(self.d, f), "a") as fh:
                            fh.write(" ")
            return real_link(src, dst, **kw)

        buf = io.StringIO()
        with mock.patch.object(cli.common, "claude_app_running", return_value=False), \
                mock.patch.object(stamp_titles.os, "link", side_effect=late_write), \
                contextlib.redirect_stdout(buf):
            rc = cli.main(["stamp", "--apply", "--json", "--home", self.h.home,
                           "--state-dir", self.h.state])
        self.assertEqual(rc, 3)
        rep = json.loads(buf.getvalue())
        aside = rep["result"]["aside"]
        self.assertEqual(len(aside), 1)
        self.assertTrue(os.path.basename(aside[0]).startswith(".stamp-old-"))
        self.assertTrue(os.path.exists(aside[0]))

    def test_apply_prints_the_kept_path_in_text_mode(self):
        import contextlib
        import io
        from claude_account_move import cli
        p = self.card()
        rp = os.path.realpath(p)
        real_link = os.link

        def late_write(src, dst, **kw):
            if dst == rp:
                for f in os.listdir(self.d):
                    if f.startswith(".stamp-old-"):
                        with open(os.path.join(self.d, f), "a") as fh:
                            fh.write(" ")
            return real_link(src, dst, **kw)

        buf = io.StringIO()
        with mock.patch.object(cli.common, "claude_app_running", return_value=False), \
                mock.patch.object(stamp_titles.os, "link", side_effect=late_write), \
                contextlib.redirect_stdout(buf):
            cli.main(["stamp", "--apply", "--home", self.h.home,
                      "--state-dir", self.h.state])
        self.assertIn("file kept beside its card", buf.getvalue())
        self.assertIn(".stamp-old-", buf.getvalue())

    def test_a_transcript_read_error_is_an_observation_error(self):
        self.card()
        paths = self.paths()
        idx = st.scan(paths)
        with mock.patch.object(st, "_last_message", side_effect=OSError(errno.EIO, "io")):
            items, stats = stamp_titles.plan_stamps(idx, st.transcripts(paths))
        self.assertFalse(idx.obs.complete())
        self.assertTrue(any("transcript unreadable" in e for e in idx.obs.errors))
        self.assertEqual(items, [])

    def test_late_change_keeps_undo_working_for_the_published_card(self):
        p = self.card()
        original = self.read(p)
        _, (items, _) = self.plan()
        os.makedirs(self.h.state)
        jp = os.path.join(self.h.state, "j.jsonl")
        rp = os.path.realpath(p)
        real_link = os.link

        def late_write(src, dst, **kw):
            if dst == rp:
                for f in os.listdir(self.d):
                    if f.startswith(".stamp-old-"):
                        with open(os.path.join(self.d, f), "a") as fh:
                            fh.write(" ")
            return real_link(src, dst, **kw)

        with mock.patch.object(stamp_titles.os, "link", side_effect=late_write):
            stamp_titles.apply_stamps(items, jp, path_id(self.h.home))
        res = stamp_titles.undo_stamps(jp, path_id(self.h.home),
                                       os.path.realpath(self.h.support))
        self.assertEqual(res["restored"], 1)
        self.assertEqual(self.read(p), original)


if __name__ == "__main__":
    unittest.main()
