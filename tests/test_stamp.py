import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import CC, LAM, Home, tree_digest, uid  # noqa: E402
from test_cli import A, B, O1, O2, build_source, parse  # noqa: E402

from claude_account_move import stamp_titles  # noqa: E402
from claude_account_move import store as st  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402
from claude_account_move.panel_snapshot import path_id  # noqa: E402

DOT = chr(0xB7)
LAST_MS = 1_750_000_000_000                     # fixed last real message


def local(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).astimezone().strftime("%d.%m %H:%M")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-stamp-")
        self.h = Home(self.tmp)
        self.d = self.h.pair(CC, A, O1)
        self.h.accounts(cli=A, app=A)
        self.h.log((A, O1))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def card(self, n, title="Fix the login", transcript=True):
        name = self.h.card(self.d, n, uid(n), title=title)
        if transcript:
            self.h.transcript(uid(n), last_ms=LAST_MS)
        return os.path.join(self.d, name)

    def read(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def journal(self):
        names = sorted(f for f in os.listdir(self.h.state) if f.startswith("stamp-"))
        self.assertTrue(names)
        return os.path.join(self.h.state, names[-1])

    def plan(self):
        paths = Paths(self.h.home, self.h.state)
        idx = st.scan(paths)
        return stamp_titles.plan_stamps(idx, st.transcripts(paths))


class Format(Base):
    def test_suffix_uses_local_time_of_the_last_real_message(self):
        p = self.card(11, "Fix the login")
        r = self.h.run("stamp", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout)
        with open(p) as fh:
            self.assertEqual(json.load(fh)["title"],
                             "Fix the login %s %s" % (DOT, local(LAST_MS)))

    def test_later_non_message_records_do_not_move_the_stamp(self):
        self.card(11)            # the fixture appends a later queue record
        items, _ = self.plan()
        self.assertEqual(items[0]["new"].split(" %s " % DOT)[-1], local(LAST_MS))

    def test_stamping_twice_does_not_double_the_suffix(self):
        p = self.card(11, "Fix the login")
        self.assertEqual(self.h.run("stamp", "--apply").returncode, 0)
        once = self.read(p)
        r = self.h.run("stamp", "--apply", "--json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(parse(r)["stats"]["planned"], 0)
        self.assertEqual(self.read(p), once)

    def test_an_older_stamp_is_replaced_not_stacked(self):
        p = self.card(11, "Fix the login %s 01.01 10:00" % DOT)
        self.h.run("stamp", "--apply")
        with open(p) as fh:
            title = json.load(fh)["title"]
        self.assertEqual(title, "Fix the login %s %s" % (DOT, local(LAST_MS)))
        self.assertEqual(title.count(DOT), 1)

    def test_stamp_with_hex_tail_and_apostrophes_is_cut_whole(self):
        self.assertEqual(
            stamp_titles.stamped("Run %s 12.08 10:00 deadbeef''" % DOT,
                                 datetime.datetime(2026, 9, 5, 14, 30)),
            "Run %s 05.09 14:30" % DOT)
        self.assertEqual(
            stamp_titles.stamped("X %s 01.09 10:00 %s 02.09 11:00" % (DOT, DOT),
                                 datetime.datetime(2026, 9, 5, 14, 30)),
            "X %s 05.09 14:30" % DOT)


class WhichCards(Base):
    def test_untouched_cases(self):
        no_tr = self.card(11, "No transcript", transcript=False)
        empty = self.card(12, "   ")
        odd = self.card(13, "Odd layout")
        with open(odd, "w") as fh:               # not reproducible byte for byte
            fh.write('{"cliSessionId":  "%s",\n   "title":"Odd layout" }' % uid(13))
        before = {p: self.read(p) for p in (no_tr, empty, odd)}
        r = self.h.run("stamp", "--apply", "--json")
        self.assertEqual(r.returncode, 0, r.stdout)
        stats = parse(r)["stats"]
        self.assertEqual((stats["no_transcript"], stats["empty_title"],
                          stats["unknown_format"], stats["planned"]), (1, 1, 1, 0))
        for p, raw in before.items():
            self.assertEqual(self.read(p), raw)

    def test_copies_of_one_session_get_one_stamp_and_links_are_written_once(self):
        first = self.card(11, "Shared")
        other = self.h.pair(CC, uid(5), uid(6))
        self.h.card(other, 11, uid(11), title="Shared")
        alias_parent = os.path.join(self.h.support, CC, uid(7))
        os.makedirs(alias_parent)
        os.symlink(self.d, os.path.join(alias_parent, uid(8)), target_is_directory=True)
        lam = self.h.pair(LAM, A, O1)
        self.h.card(lam, 11, uid(11), title="Shared")
        r = self.h.run("stamp", "--apply", "--json")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(parse(r)["result"]["written"], 3)
        titles = set()
        for d in (self.d, other, lam):
            with open(os.path.join(d, "local_%s.json" % uid(11, 9))) as fh:
                titles.add(json.load(fh)["title"])
        self.assertEqual(titles, {"Shared %s %s" % (DOT, local(LAST_MS))})

    def test_only_the_title_changes_and_the_layout_survives(self):
        p = self.card(11, "Layout")
        with open(p) as fh:
            doc = json.load(fh)
        with open(p, "w") as fh:                  # indented, trailing newline
            fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        os.chmod(p, 0o640)
        self.h.run("stamp", "--apply")
        raw = self.read(p).decode()
        doc["title"] = "Layout %s %s" % (DOT, local(LAST_MS))
        self.assertEqual(raw, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)


class DryRun(Base):
    def test_dry_run_lists_was_and_will_be_and_writes_nothing(self):
        self.card(11, "Fix the login")
        before = tree_digest(self.h.home)
        r = self.h.run("stamp")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Fix the login  ->  Fix the login %s %s" % (DOT, local(LAST_MS)),
                      r.stdout)
        self.assertIn("dry run", r.stdout)
        self.assertEqual(tree_digest(self.h.home), before)
        self.assertFalse(os.path.exists(self.h.state))
        r = self.h.run("stamp", "--json")
        self.assertEqual(parse(r)["dry_run"], True)
        self.assertFalse(os.path.exists(self.h.state))

    def test_incomplete_observation_refuses(self):
        self.card(11)
        with open(os.path.join(self.d, "local_bad.json"), "w") as fh:
            fh.write("{oops")
        self.assertEqual(self.h.run("stamp", "--apply").returncode, 3)

    def test_no_storage_is_4(self):
        self.assertEqual(Home(os.path.join(self.tmp, "x")).run("stamp").returncode, 4)


class ChangedMeanwhile(Base):
    def test_card_changed_between_plan_and_write_is_not_overwritten(self):
        p = self.card(11, "Fix the login")
        items, _ = self.plan()
        with open(p) as fh:
            doc = json.load(fh)
        doc["lastActivityAt"] += 1                # the app wrote the card meanwhile
        with open(p, "w") as fh:
            json.dump(doc, fh)
        changed = self.read(p)
        os.makedirs(self.h.state)
        res = stamp_titles.apply_stamps(items, os.path.join(self.h.state, "j.jsonl"),
                                        path_id(self.h.home))
        self.assertEqual((res["written"], res["changed"]), (0, 1))
        self.assertEqual(self.read(p), changed)
        self.assertEqual([f for f in os.listdir(self.d) if f.startswith(".stamp-")], [])

    def test_change_right_before_the_replace_is_not_overwritten(self):
        p = self.card(11, "Fix the login")
        items, _ = self.plan()
        os.makedirs(self.h.state)

        def app_writes(path):
            with open(path) as fh:
                doc = json.load(fh)
            doc["completedTurns"] = 99
            with open(path, "w") as fh:
                json.dump(doc, fh)

        res = stamp_titles.apply_stamps(items, os.path.join(self.h.state, "j.jsonl"),
                                        path_id(self.h.home), before_replace=app_writes)
        self.assertEqual(res["written"], 0)
        self.assertEqual(json.loads(self.read(p))["completedTurns"], 99)
        self.assertEqual(json.loads(self.read(p))["title"], "Fix the login")
        self.assertEqual([f for f in os.listdir(self.d) if f.startswith(".stamp-")], [])


class Undo(Base):
    def test_undo_restores_every_card_byte_for_byte(self):
        paths = [self.card(11, "One"), self.card(12, "Two %s 01.01 09:00" % DOT)]
        before = [self.read(p) for p in paths]
        self.assertEqual(self.h.run("stamp", "--apply").returncode, 0)
        self.assertNotEqual([self.read(p) for p in paths], before)
        r = self.h.run("stamp", "--undo", self.journal(), "--json")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(parse(r)["undo"]["restored"], 2)
        self.assertEqual([self.read(p) for p in paths], before)
        self.assertEqual([f for f in os.listdir(self.d) if f.startswith((".undo-", ".stamp-"))], [])

    def test_a_card_changed_after_the_stamp_is_kept(self):
        p = self.card(11, "One")
        self.h.run("stamp", "--apply")
        with open(p) as fh:
            doc = json.load(fh)
        doc["completedTurns"] = 77
        with open(p, "w") as fh:
            json.dump(doc, fh)
        kept = self.read(p)
        r = self.h.run("stamp", "--undo", self.journal(), "--json")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(parse(r)["undo"]["ambiguous"], 1)
        self.assertEqual(self.read(p), kept)

    def test_a_replaced_card_with_the_same_bytes_is_not_ours(self):
        p = self.card(11, "One")
        self.h.run("stamp", "--apply")
        raw = self.read(p)
        os.unlink(p)
        with open(p, "wb") as fh:                 # same bytes, new inode
            fh.write(raw)
        r = self.h.run("stamp", "--undo", self.journal())
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.read(p), raw)

    def test_interrupted_run_leaves_the_original_and_undo_is_clean(self):
        p = self.card(11, "One")
        original = self.read(p)
        items, _ = self.plan()
        os.makedirs(self.h.state)
        jp = os.path.join(self.h.state, "stamp-x.jsonl")

        def crash(path):
            raise RuntimeError("power cut")

        with self.assertRaises(RuntimeError):
            stamp_titles.apply_stamps(items, jp, path_id(self.h.home), before_replace=crash)
        self.assertEqual(self.read(p), original)
        r = self.h.run("stamp", "--undo", jp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.read(p), original)

    def test_journal_of_another_home_and_foreign_targets_are_rejected(self):
        self.card(11, "One")
        self.h.run("stamp", "--apply")
        jp = self.journal()
        other = Home(os.path.join(self.tmp, "second"))
        build_source(other)
        self.assertEqual(other.run("stamp", "--undo", jp).returncode, 2)
        with open(jp) as fh:
            lines = [json.loads(x) for x in fh if x.strip()]
        lines[0]["targets"] = [os.path.join(self.tmp, "elsewhere")]
        with open(jp, "w") as fh:
            fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
        r = self.h.run("stamp", "--undo", jp)
        self.assertEqual(r.returncode, 3)

    def test_malformed_records_are_damage_not_a_crash(self):
        self.card(11, "One")
        self.h.run("stamp", "--apply")
        jp = self.journal()
        with open(jp, "a") as fh:
            fh.write(json.dumps({"op": "intent", "op_id": "x"}) + "\n")
        r = self.h.run("stamp", "--undo", jp, "--json")
        self.assertIn(r.returncode, (2, 3))
        self.assertNotEqual(r.returncode, 1)


class Wiring(Base):
    def test_version_and_help(self):
        r = self.h.run("--version")
        self.assertIn("0.1.2", r.stdout)
        self.assertIn("stamp", self.h.run("--help").stdout)

    def test_stamp_does_not_disturb_a_finished_move(self):
        src = build_source(self.h)                 # same home, more cards
        self.assertEqual(self.h.run("prepare").returncode, 0)
        self.assertEqual(self.h.run("stamp", "--apply").returncode, 0)
        self.h.pair(CC, B, O2)
        self.h.pair(LAM, B, O2)
        self.h.accounts(cli=B, app=B)
        self.h.log((A, O1), (B, O2))
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        self.assertEqual(self.h.run("finish", "--wait-login", "0").returncode, 0)


if __name__ == "__main__":
    unittest.main()
