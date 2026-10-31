"""inventory: what is left on this Mac from every account. Read-only."""
import json
import os
import shutil
import tempfile
import unittest

from fixture import CC, LAM, Home, tree_digest, uid

HERE = os.path.dirname(os.path.abspath(__file__))
PGREP_OPEN = os.path.join(HERE, "stub_pgrep_open.sh")
A, B, C = uid(0xA), uid(0xB), uid(0xC)
ORG_A, ORG_B = uid(0xF1), uid(0xF2)
T_NEW, T_OLD = 1790000000000, 1780000000000
TITLE = "SECRET-TITLE-do-not-print"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-inv-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.h = Home(self.tmp)

    def scene(self):
        """A is the app and CLI account with one openable session; B is an old
        account with four cards (two with a transcript, one without, one with
        no session id), one deletion mark; one transcript no card names."""
        h = self.h
        h.accounts(cli=A, app=A)
        a = h.pair(CC, A, ORG_A)
        h.card(a, 1, uid(0x11), act=T_NEW, title=TITLE)
        h.transcript(uid(0x11), last_ms=T_NEW)
        b = h.pair(CC, B, ORG_B)
        h.card(b, 2, uid(0x21), act=T_OLD, title=TITLE)
        h.card(b, 3, uid(0x22), act=T_OLD - 1000, title=TITLE)
        h.card(b, 4, uid(0x23), act=T_OLD - 2000, title=TITLE)
        h.card(b, 5, "", act=T_OLD - 3000, title=TITLE)
        open(os.path.join(b, "deleted_" + uid(0x29)), "w").close()
        h.transcript(uid(0x21), last_ms=T_OLD)
        h.transcript(uid(0x22), last_ms=T_OLD)
        h.transcript(uid(0x99), last_ms=T_OLD)
        return a, b

    def inv(self, *extra, **envx):
        r = self.h.run("inventory", "--json", *extra, **envx)
        return r.returncode, json.loads(r.stdout), r

    @staticmethod
    def account(doc, acc):
        return next(a for a in doc["accounts"] if a["account"] == acc)


class Counts(Base):
    def test_per_folder_per_account_and_transcripts(self):
        self.scene()
        code, doc, _ = self.inv()
        self.assertEqual(code, 0)
        self.assertEqual((doc["command"], doc["exit"], doc["read_complete"]),
                         ("inventory", 0, True))
        self.assertEqual((doc["app_account"], doc["cli_account"]), (A, A))
        b = self.account(doc, B)
        self.assertFalse(b["app"] or b["cli"])
        self.assertEqual((b["cards"], b["with_transcript"]), (4, 2))
        f = doc["folders"][b["folders"][0]]
        self.assertEqual((f["store"], f["cards"], f["deleted"]), (CC, 4, 1))
        self.assertEqual((f["with_transcript"], f["without_transcript"],
                          f["no_session_id"], f["scheduled_tasks"]), (2, 1, 1, 0))
        self.assertEqual(f["folder"], "%s/%s" % (B, ORG_B))
        self.assertEqual(f["aliases"], [{"account": B, "org": ORG_B, "kind": "dir"}])
        self.assertTrue(b["last_activity"].startswith("2026-05-28T"))
        self.assertEqual(doc["transcripts"],
                         {"total": 4, "with_card": 3, "without_card": 1})
        for f in doc["folders"]:
            self.assertEqual(f["with_transcript"] + f["without_transcript"]
                             + f["no_session_id"], f["cards"])

    def test_text_verdict_and_no_titles(self):
        self.scene()
        r = self.h.run("inventory")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(TITLE, r.stdout)
        self.assertIn(A, r.stdout)
        self.assertIn(B, r.stdout)
        last = [ln for ln in r.stdout.splitlines() if ln.strip()][-1]
        self.assertEqual(last, "2 accounts have session data on this Mac; 3 of 5 "
                               "sessions can still be opened (transcript on disk)")
        self.assertNotIn(TITLE, self.inv()[2].stdout)

    def test_scheduled_tasks_are_counted(self):
        _, b = self.scene()
        with open(os.path.join(b, "scheduled-tasks.json"), "w") as fh:
            json.dump({"scheduledTasks": [{"id": "t1", "createdAt": 1},
                                          {"id": "t2", "createdAt": 2}]}, fh)
        _, doc, _ = self.inv()
        self.assertEqual(doc["folders"][self.account(doc, B)["folders"][0]]
                         ["scheduled_tasks"], 2)

    def test_uppercase_session_id_in_a_card_finds_its_transcript(self):
        h = self.h
        h.accounts(cli=A, app=A)
        a = h.pair(CC, A, ORG_A)
        h.card(a, 1, uid(0x11).upper(), act=T_NEW)
        h.transcript(uid(0x11))
        _, doc, _ = self.inv()
        self.assertEqual(self.account(doc, A)["with_transcript"], 1)
        self.assertEqual(doc["transcripts"]["with_card"], 1)

    def test_card_copied_into_two_folders_counts_once_per_account(self):
        h = self.h
        h.accounts(cli=A, app=A)
        a1 = h.pair(CC, A, ORG_A)
        a2 = h.pair(CC, A, ORG_B)
        h.card(a1, 1, uid(0x11), act=T_OLD)
        h.card(a2, 1, uid(0x11), act=T_NEW)
        h.transcript(uid(0x11))
        _, doc, _ = self.inv()
        a = self.account(doc, A)
        self.assertEqual((a["cards"], a["with_transcript"], len(a["folders"])), (1, 1, 2))
        self.assertTrue(a["last_activity"].startswith("2026-09-21T"))
        r = self.h.run("inventory")
        self.assertIn("1 account has session data on this Mac; 1 of 1 session can "
                      "still be opened", r.stdout)

    def test_both_stores_are_listed_separately(self):
        self.scene()
        lam = self.h.pair(LAM, B, ORG_B)
        self.h.card(lam, 7, uid(0x77), act=T_OLD)
        _, doc, _ = self.inv()
        b = self.account(doc, B)
        self.assertEqual(sorted(doc["folders"][i]["store"] for i in b["folders"]),
                         [CC, LAM])
        self.assertEqual(b["cards"], 5)
        self.assertLessEqual(b["with_transcript"], b["cards"])

    def test_linked_folder_is_one_folder_shared_by_two_accounts(self):
        h = self.h
        h.accounts(cli=A, app=A)
        a = h.pair(CC, A, ORG_A)
        h.card(a, 1, uid(0x11), act=T_NEW)
        h.card(a, 2, uid(0x12), act=T_NEW)
        os.makedirs(os.path.join(h.support, CC, B))
        os.symlink(a, os.path.join(h.support, CC, B, ORG_B))
        _, doc, _ = self.inv()
        self.assertEqual(len(doc["folders"]), 1)
        kinds = {(x["account"], x["kind"]) for x in doc["folders"][0]["aliases"]}
        self.assertEqual(kinds, {(A, "dir"), (B, "link")})
        self.assertEqual(self.account(doc, A)["folders"], [0])
        self.assertEqual(self.account(doc, B)["folders"], [0])
        self.assertEqual(self.account(doc, B)["cards"], 2)
        self.assertIn("shared with 1 other account", self.h.run("inventory").stdout)


class Ordering(Base):
    def test_app_account_first_without_folders_then_newest(self):
        self.scene()
        self.h.accounts(app=C)
        _, doc, _ = self.inv()
        self.assertEqual([a["account"] for a in doc["accounts"]], [C, A, B])
        c = self.account(doc, C)
        self.assertEqual((c["app"], c["cli"], c["cards"], c["folders"],
                          c["last_activity"]), (True, False, 0, [], None))

    def test_cli_account_without_folders_is_listed_last(self):
        self.scene()
        self.h.accounts(cli=C)
        _, doc, _ = self.inv()
        self.assertEqual([a["account"] for a in doc["accounts"]], [A, B, C])
        self.assertTrue(self.account(doc, C)["cli"])


class ReadOnly(Base):
    def test_writes_nothing_and_does_not_create_the_state_directory(self):
        self.scene()
        before = tree_digest(self.tmp)
        self.assertEqual(self.inv()[0], 0)
        self.assertEqual(self.h.run("inventory").returncode, 0)
        self.assertEqual(tree_digest(self.tmp), before)
        self.assertFalse(os.path.exists(self.h.state))

    def test_allowed_while_the_app_runs(self):
        self.scene()
        code, doc, r = self.inv(CLAUDE_ACCOUNT_MOVE_PGREP=PGREP_OPEN)
        self.assertEqual(code, 0, r.stderr)
        self.assertTrue(doc["read_complete"])

    def test_state_dir_option_is_not_offered(self):
        self.scene()
        r = self.h.run("inventory", "--state-dir", self.h.state)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(self.h.state))


class ExitCodes(Base):
    def test_unreadable_card_is_exit_3_and_the_report_is_still_printed(self):
        _, b = self.scene()
        with open(os.path.join(b, "local_%s.json" % uid(0x31, 9)), "w") as fh:
            fh.write("{not json")
        code, doc, _ = self.inv()
        self.assertEqual((code, doc["exit"], doc["read_complete"]), (3, 3, False))
        self.assertTrue(doc["errors"])
        self.assertEqual(self.account(doc, A)["cards"], 1)
        self.assertEqual(self.h.run("inventory").returncode, 3)

    def test_errors_are_capped_at_twenty(self):
        _, b = self.scene()
        for n in range(30):
            with open(os.path.join(b, "local_%s.json" % uid(0x40 + n, 9)), "w") as fh:
                fh.write("{not json")
        code, doc, _ = self.inv()
        self.assertEqual(code, 3)
        self.assertEqual(len(doc["errors"]), 20)

    def test_malformed_account_file_is_an_incomplete_read(self):
        self.scene()
        self.h.accounts(raw_cli="{not json")
        code, doc, _ = self.inv()
        self.assertEqual((code, doc["read_complete"]), (3, False))
        self.assertIsNone(doc["cli_account"])
        self.assertTrue(any("CLI account" in e for e in doc["errors"]))

    def test_unreadable_transcript_folder_is_exit_3(self):
        self.scene()
        proj = self.h.projects
        os.chmod(proj, 0)
        self.addCleanup(os.chmod, proj, 0o755)
        code, doc, _ = self.inv()
        self.assertEqual((code, doc["read_complete"]), (3, False))
        self.assertTrue(any(proj in e for e in doc["errors"]), doc["errors"])
        self.assertEqual(self.h.run("inventory").returncode, 3)

    def test_unreadable_agent_mode_transcript_folder_is_exit_3(self):
        self.scene()
        d = os.path.join(self.h.pair(LAM, B, ORG_B), "local_" + uid(0x51),
                         ".claude", "projects", "p")
        os.makedirs(d)
        os.chmod(d, 0)
        self.addCleanup(os.chmod, d, 0o755)
        code, doc, _ = self.inv()
        self.assertEqual((code, doc["read_complete"]), (3, False))
        self.assertTrue(any(d in e for e in doc["errors"]), doc["errors"])

    def test_missing_optional_transcript_places_are_not_an_error(self):
        h = self.h
        h.accounts(cli=A, app=A)
        h.card(h.pair(CC, A, ORG_A), 1, uid(0x11), act=T_NEW)
        code, doc, _ = self.inv()
        self.assertEqual((code, doc["read_complete"], doc["errors"]), (0, True, []))

    def test_dangling_link_and_directory_are_not_transcripts(self):
        self.scene()
        proj = self.h.projects
        os.symlink(os.path.join(self.tmp, "nowhere.jsonl"),
                   os.path.join(proj, uid(0x23) + ".jsonl"))
        os.makedirs(os.path.join(proj, uid(0x88) + ".jsonl"))
        code, doc, _ = self.inv()
        self.assertEqual(code, 0)
        f = doc["folders"][self.account(doc, B)["folders"][0]]
        self.assertEqual((f["with_transcript"], f["without_transcript"]), (2, 1))
        self.assertEqual(doc["transcripts"]["total"], 4)

    def test_link_to_a_real_file_is_a_transcript(self):
        self.scene()
        other = os.path.join(self.tmp, "home", ".claude", "projects", "other")
        os.makedirs(other)
        os.symlink(os.path.join(self.h.projects, uid(0x23) + ".jsonl"),
                   os.path.join(other, uid(0x23) + ".jsonl"))   # dangling target
        self.h.transcript(uid(0x23))                            # now the target exists
        code, doc, _ = self.inv()
        self.assertEqual(code, 0)
        self.assertEqual(self.account(doc, B)["with_transcript"], 3)
        self.assertEqual(doc["transcripts"]["total"], 5)

    def test_invalid_activity_time_is_exit_3_and_never_a_crash(self):
        _, b = self.scene()
        for n, act in ((6, 253402300800000), (7, -5), (8, 10 ** 30)):
            self.h.card(b, n, uid(0x60 + n), act=act, title=TITLE)
        code, doc, r = self.inv()
        self.assertEqual((code, doc["read_complete"]), (3, False))
        b_acc = self.account(doc, B)
        self.assertEqual(b_acc["cards"], 7)
        self.assertTrue(b_acc["last_activity"].startswith("2026-05-28T"))
        bad = [e for e in doc["errors"] if "lastActivityAt" in e]
        self.assertEqual(len(bad), 3)
        self.assertTrue(all(b in e for e in bad))
        self.assertNotIn(TITLE, r.stdout)
        text = self.h.run("inventory")
        self.assertEqual(text.returncode, 3, text.stderr)
        self.assertIn("WARNING", text.stdout)
        self.assertNotIn(TITLE, text.stdout)

    def test_missing_account_files_are_not_an_error(self):
        h = self.h
        h.pair(CC, B, ORG_B)
        code, doc, _ = self.inv()
        self.assertEqual(code, 0)
        self.assertEqual((doc["app_account"], doc["cli_account"]), (None, None))
        self.assertEqual([a["account"] for a in doc["accounts"]], [B])

    def test_nothing_to_inventory_is_exit_4(self):
        code, doc, r = self.inv()
        self.assertEqual((code, doc["exit"], doc["read_complete"]), (4, 4, False))
        self.assertEqual((doc["folders"], doc["accounts"]), ([], []))
        self.assertTrue(doc["errors"])
        r = self.h.run("inventory")
        self.assertEqual(r.returncode, 4)
        self.assertIn("nothing to inventory", r.stderr)

    def test_cli_transcripts_alone_are_enough(self):
        self.h.transcript(uid(0x99))
        code, doc, _ = self.inv()
        self.assertEqual(code, 0)
        self.assertEqual(doc["transcripts"],
                         {"total": 1, "with_card": 0, "without_card": 1})
        self.assertEqual(doc["accounts"], [])
        r = self.h.run("inventory")
        self.assertIn("No account has session data on this Mac; 1 transcript on disk.",
                      r.stdout)

    def test_home_option_points_at_another_home(self):
        self.scene()
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        r = self.h.run("inventory", "--json", "--home", other)
        self.assertEqual(r.returncode, 4)

    def test_bad_option_is_exit_2(self):
        r = self.h.run("inventory", "--bogus")
        self.assertEqual(r.returncode, 2)

    def test_listed_in_help(self):
        r = self.h.run("--help")
        self.assertIn("inventory", r.stdout)


if __name__ == "__main__":
    unittest.main()
