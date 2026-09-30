"""Regression cases for the 0.1.2 closure check. Each counterexample fails on
0.1.1 and passes now."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import CC, LAM, Home, uid  # noqa: E402
from test_cli import A, B, O1, O2, build_source, parse, switch_to_target  # noqa: E402

O3 = uid(8)


def u(*codes):
    return "".join(chr(c) for c in codes)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-v012-")
        self.h = Home(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def moved(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        return os.path.join(self.h.support, CC, B, O2)

    def finish(self):
        return self.h.run("finish", "--wait-login", "0", "--json")

    def write_journal(self, records):
        os.makedirs(self.h.state, exist_ok=True)
        jp = os.path.join(self.h.state, "hand.jsonl")
        with open(jp, "w") as fh:
            for r in records:
                fh.write(r if isinstance(r, str) else json.dumps(r))
                fh.write("\n")
        return jp


class C1DeletionRace(Base):
    def test_swap_between_check_and_delete_never_deletes_the_new_file(self):
        from claude_account_move import sync_cards
        support = os.path.join(self.tmp, "support")
        src_dir = os.path.join(support, "src")
        dst_dir = os.path.join(support, "dst")
        os.makedirs(src_dir)
        os.makedirs(dst_dir)
        src = os.path.join(src_dir, "local_a.json")
        with open(src, "w") as fh:
            fh.write('{"x": 1}')
        jp = os.path.join(self.tmp, "j.jsonl")
        copied, _ = sync_cards.apply_plan(
            [{"store": CC, "name": "local_a.json", "src": src, "dst_dir": dst_dir,
              "create_dir": False}], jp, "h1")
        self.assertEqual(copied, 1)
        dst = os.path.join(dst_dir, "local_a.json")
        real_sha = sync_cards.sha256_file
        state = {"armed": True}

        def swapping(path):
            digest = real_sha(path)
            if state["armed"] and os.path.basename(path).endswith("local_a.json"):
                state["armed"] = False      # another writer replaces the card now
                if os.path.exists(dst):
                    os.unlink(dst)
                with open(dst, "w") as fh:
                    fh.write('{"foreign": true}')
            return digest

        sync_cards.sha256_file = swapping
        try:
            sync_cards.undo(jp, "h1", os.path.realpath(support))
        finally:
            sync_cards.sha256_file = real_sha
        self.assertTrue(os.path.exists(dst), "the foreign card was deleted")
        with open(dst) as fh:
            self.assertIn("foreign", fh.read())

    def test_foreign_file_found_under_the_name_is_put_back(self):
        from claude_account_move import sync_cards
        support = os.path.join(self.tmp, "s")
        src_dir, dst_dir = os.path.join(support, "a"), os.path.join(support, "b")
        os.makedirs(src_dir)
        os.makedirs(dst_dir)
        src = os.path.join(src_dir, "local_a.json")
        with open(src, "w") as fh:
            fh.write("ours")
        jp = os.path.join(self.tmp, "j.jsonl")
        sync_cards.apply_plan([{"store": CC, "name": "local_a.json", "src": src,
                                "dst_dir": dst_dir, "create_dir": False}], jp, "h")
        dst = os.path.join(dst_dir, "local_a.json")
        os.unlink(dst)
        with open(dst, "w") as fh:
            fh.write("theirs")
        res = sync_cards.undo(jp, "h", os.path.realpath(support))
        self.assertEqual(res["ambiguous"], 1)
        with open(dst) as fh:
            self.assertEqual(fh.read(), "theirs")
        self.assertEqual([f for f in os.listdir(dst_dir) if f.startswith(".undo-")], [])


class C1OperationAndTargetBinding(Base):
    def begin(self, targets, op="op1"):
        from claude_account_move.panel_snapshot import path_id
        return {"op": "begin", "version": 1, "op_id": op, "targets": targets,
                "home_id": path_id(self.h.home)}

    def test_journal_naming_a_source_card_outside_its_targets_deletes_nothing(self):
        src = build_source(self.h)
        switch_to_target(self.h)
        victim = os.path.join(src, "local_%s.json" % uid(11, 9))
        st = os.stat(victim)
        import hashlib
        with open(victim, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        target = os.path.join(self.h.support, CC, B, O2)
        jp = self.write_journal([
            self.begin([os.path.realpath(target)]),
            {"op": "intent", "op_id": "op1", "path": victim, "src": "x",
             "sha256": digest, "dev": st.st_dev, "ino": st.st_ino}])
        r = self.h.run("sync", "--undo", jp)
        self.assertTrue(os.path.exists(victim))
        self.assertEqual(r.returncode, 3)

    def test_journal_mixing_operations_is_rejected(self):
        build_source(self.h)
        jp = self.write_journal([
            self.begin([]),
            {"op": "done", "op_id": "other", "path": "/x/local_a.json"}])
        self.assertEqual(self.h.run("sync", "--undo", jp).returncode, 2)


class C2EveryTargetCopy(Base):
    def test_a_second_target_folder_with_a_wrong_binding_is_found(self):
        self.moved()
        other = self.h.pair(LAM, B, O3)              # second organization of the target
        self.h.card(other, 14, uid(88), act=1, turns=0)   # wrong id, old activity
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(parse(r)["stores"][LAM]["target_copy_wrong_cid"], 1)

    def test_control_second_target_folder_with_a_correct_copy_passes(self):
        self.moved()
        other = self.h.pair(LAM, B, O3)
        self.h.card(other, 14, uid(14))
        self.assertEqual(self.finish().returncode, 0)


class C3TranscriptContent(Base):
    def corrupt_first_line(self, sid, old, new):
        p = os.path.join(self.h.projects, sid + ".jsonl")
        with open(p, "rb") as fh:
            data = fh.read()
        self.assertEqual(len(old), len(new))
        with open(p, "wb") as fh:
            fh.write(data.replace(old, new, 1))

    def test_same_length_corruption_in_the_middle_is_detected(self):
        self.moved()
        self.corrupt_first_line(uid(12), b"hello", b"h\x00llo")
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertGreaterEqual(parse(r)["transcripts_bad"], 1)

    def test_same_length_content_change_is_detected(self):
        self.moved()
        self.corrupt_first_line(uid(13), b"hello", b"jello")
        self.assertEqual(self.finish().returncode, 3)

    def test_legitimate_growth_is_not_a_change(self):
        self.moved()
        with open(os.path.join(self.h.projects, uid(11) + ".jsonl"), "a") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": "2099-01-01T00:00:00Z",
                                 "message": {"content": "more"}}) + "\n")
        self.assertEqual(self.finish().returncode, 0)

    def test_unparsable_lines_block_readiness_and_show_in_observation(self):
        build_source(self.h)
        with open(os.path.join(self.h.projects, uid(11) + ".jsonl"), "a") as fh:
            fh.write("{this is not json}\n")
        r = self.h.run("prepare", "--json")
        self.assertEqual(r.returncode, 3)
        rep = parse(r)
        self.assertFalse(rep["ready"])
        self.assertTrue(any("unparsable" in e for e in rep["observation"]["errors"]))

    def test_unterminated_last_line_of_a_live_session_is_not_an_error(self):
        build_source(self.h)
        with open(os.path.join(self.h.projects, uid(11) + ".jsonl"), "a") as fh:
            fh.write('{"type": "user", "timest')          # still being written
        self.assertEqual(self.h.run("prepare").returncode, 0)


class M3AllSettings(Base):
    def put(self, data):
        d = os.path.join(self.h.home, ".claude")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "settings.json"), "w") as fh:
            json.dump(data, fh)

    def test_a_key_outside_permissions_and_hooks_is_compared(self):
        self.put({"env": {"A": "1"}, "model": "x"})
        self.moved()
        self.put({"env": {"A": "2"}, "model": "x"})
        self.assertEqual(self.finish().returncode, 3)
        self.put({"env": {"A": "1"}, "model": "x"})
        self.assertEqual(self.finish().returncode, 0)


class M5EveryStoreWithWork(Base):
    def test_agent_store_only_without_any_target_is_not_success(self):
        lam = self.h.pair(LAM, A, O1)
        self.h.card(lam, 14, uid(14))
        self.h.transcript(uid(14))
        self.h.pair(CC, A, O1)
        self.h.accounts(cli=B, app=B)
        self.h.log((A, O1))
        for extra in ((), ("--apply",)):
            r = self.h.run("sync", *extra)
            self.assertEqual(r.returncode, 3, r.stdout)
            self.assertIn("cannot determine", r.stdout)


class M6JournalRecords(Base):
    def begin(self):
        from claude_account_move.panel_snapshot import path_id
        return {"op": "begin", "version": 1, "op_id": "o", "targets": [],
                "home_id": path_id(self.h.home)}

    def test_unhashable_path_is_damage_not_an_internal_error(self):
        build_source(self.h)
        jp = self.write_journal([self.begin(), {"op": "failed", "op_id": "o", "path": []}])
        r = self.h.run("sync", "--undo", jp, "--json")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(parse(r)["undo"]["status"], "damaged")

    def test_intent_without_fields_is_not_silently_skipped(self):
        build_source(self.h)
        jp = self.write_journal([self.begin(), {"op": "intent", "op_id": "o"}])
        r = self.h.run("sync", "--undo", jp, "--json")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(parse(r)["undo"]["status"], "damaged")

    def test_unknown_operation_is_damage(self):
        build_source(self.h)
        jp = self.write_journal([self.begin(), {"op": "erase", "op_id": "o"}])
        self.assertEqual(self.h.run("sync", "--undo", jp).returncode, 3)


class M7ParserAndInterrupt(Base):
    def test_usage_error_with_json_prints_a_report(self):
        r = self.h.run("finish", "--wait-login", "invalid", "--json")
        self.assertEqual(r.returncode, 2)
        rep = json.loads(r.stdout)
        self.assertEqual((rep["exit_code"], rep["verdict"]), (2, "input_error"))

    def test_usage_error_without_json_prints_no_json(self):
        r = self.h.run("finish", "--wait-login", "invalid")
        self.assertEqual((r.returncode, r.stdout), (2, ""))

    def test_keyboard_interrupt_with_json_prints_a_report(self):
        from claude_account_move import cli
        original = cli.cmd_prepare
        buf = io.StringIO()

        def boom(args):
            raise KeyboardInterrupt

        cli.cmd_prepare = boom
        try:
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["prepare", "--json"])
        finally:
            cli.cmd_prepare = original
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(buf.getvalue())["verdict"], "interrupted")


class M8TaskShape(Base):
    def prepare_with(self, content):
        d = build_source(self.h)
        with open(os.path.join(d, "scheduled-tasks.json"), "w") as fh:
            fh.write(json.dumps(content))
        return self.h.run("prepare", "--json")

    def test_null_collection_and_duplicate_ids_are_unreadable_observations(self):
        for bad in ({"scheduledTasks": None},
                    {"scheduledTasks": [{"id": "a", "enabled": True},
                                        {"id": "a", "enabled": False}]}):
            r = self.prepare_with(bad)
            self.assertEqual(r.returncode, 3, bad)
            self.assertFalse(parse(r)["ready"])
            shutil.rmtree(self.h.state, ignore_errors=True)
            shutil.rmtree(os.path.join(self.h.home, "Library"))
            shutil.rmtree(os.path.join(self.h.home, ".claude"))


class M1DocWording(unittest.TestCase):
    def test_no_contradictory_credential_claim(self):
        def flat(name):
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                return " ".join(fh.read().split())
        en, ru = flat("README.md"), flat("README.ru.md")
        self.assertNotIn("reads or stores a password", en)
        self.assertNotIn(u(1085, 1077, 32, 1095, 1080, 1090, 1072, 1077, 1090, 32, 1080, 32, 1085, 1077, 32, 1093, 1088, 1072, 1085, 1080, 1090), ru)
        self.assertIn("does not use or store", en)
        self.assertIn(u(1085, 1077, 32, 1080, 1089, 1087, 1086, 1083, 1100, 1079, 1091, 1077, 1090, 32, 1080, 32, 1085, 1077, 32, 1089, 1086, 1093, 1088, 1072, 1085, 1103, 1077, 1090), ru)


class M3InputErrorPrecedenceAndStderr(Base):
    def test_malformed_override_beats_missing_storage(self):
        for cmd in ("prepare", "finish", "sync"):
            r = self.h.run(cmd, "--active-pair", "nonsense", "--json")
            self.assertEqual(r.returncode, 2, cmd)
            self.assertEqual(json.loads(r.stdout)["exit_code"], 2)

    def test_report_write_warning_goes_to_stderr(self):
        from claude_account_move import cli
        os.makedirs(os.path.join(self.tmp, "mdir"))
        original = cli.write_json

        def failing(path, obj):
            raise OSError("disk full")

        cli.write_json = failing
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cli.emit(SimpleNamespace(json=False), cli.Out(False),
                         {"command": "prepare"}, os.path.join(self.tmp, "mdir"))
        finally:
            cli.write_json = original
        self.assertEqual(out.getvalue(), "")
        self.assertIn("warning", err.getvalue())


if __name__ == "__main__":
    unittest.main()
