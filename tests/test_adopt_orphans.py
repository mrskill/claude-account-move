"""ORACLE for `adopt` (CAM-ADOPT-ORPHANS), written before the implementation.

Contract: _Deliverables/task-outputs/move-program/adopt-orphans/CONTRACT.md
(items A1-A8 and G1-G3). One or more tests per item, all on a fake HOME only.

An orphan is a transcript with a real user prompt, no card (any account, any org,
any tracked store) naming it, and either a `session-owner` line or a version-5 id.

Red on release 1002377 means: the subcommand `adopt` is unknown (parse error, exit
2) or the check/note of G1-G3 is missing. Every refusal test pins a refusal code
that is not the parse error and a positive property, so a missing subcommand
cannot pass it.

Assumptions the contract leaves open (the judge may tighten them): the layout of
the dry-run lines (only the facts are searched for, not their order); the journal
file name (found through the path printed in the output, else the new files in the
state directory); the title keys of `custom-title` / `ai-title` lines
(`customTitle` / `aiTitle`, with a plain `title` beside them).
"""
import contextlib
import copy
import glob
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from fixture import CC, LAM, Home, tree_digest, uid, v5  # noqa: E402
from test_cli import A, B, O1, O2, build_source, switch_to_target  # noqa: E402
import test_auto  # noqa: E402

from claude_account_move import cli, common  # noqa: E402

CMD = "adopt"
STUB_OPEN = os.path.join(HERE, "stub_pgrep_open.sh")
PARSE_ERROR = 2
NO_CARD = "\u0431\u0435\u0437 \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0438"
CHECK_NAME = "\u0441\u0435\u0441\u0441\u0438\u0438 " + NO_CARD
PROBLEM = "[\u041f\u0420\u041e\u0411\u041b\u0415\u041c\u0410]"
NOTE_RE = "(?i)orphan|\u0431\u0435\u0437 \u043a\u0430\u0440\u0442\u043e\u0447|\u0441\u0435\u0441\u0441\u0438|vanish|\u043f\u0440\u043e\u043f\u0430\u0434|\u0438\u0441\u0447\u0435\u0437"
EXIT_FAIL = 3


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def active_dir(h):
    return os.path.join(h.support, CC, A, O1)


def cards(d):
    return sorted(f for f in os.listdir(d) if f.startswith("local_") and f.endswith(".json"))


def load(path):
    with open(path) as fh:
        return json.load(fh)


def card_for(d, sid):
    """The single card of `d` whose cliSessionId is sid, as (name, dict), or None."""
    hits = []
    for f in cards(d):
        try:
            data = load(os.path.join(d, f))
        except ValueError:
            continue
        if data.get("cliSessionId") == sid:
            hits.append((f, data))
    assert len(hits) <= 1, hits
    return hits[0] if hits else None


def all_cards_naming(h, sid):
    out = []
    for dp, _, fns in os.walk(h.support):
        for f in fns:
            if f.startswith("local_") and f.endswith(".json"):
                try:
                    if load(os.path.join(dp, f)).get("cliSessionId") == sid:
                        out.append(os.path.join(dp, f))
                except ValueError:
                    pass
    return out


def transcript_state(h):
    out = {}
    for f in glob.glob(os.path.join(h.projects, "*.jsonl")):
        st = os.stat(f)
        with open(f, "rb") as fh:
            out[f] = (fh.read(), st.st_mtime_ns)
    return out


def state_files(h):
    out = set()
    for dp, _, fns in os.walk(h.state):
        for n in fns:
            out.add(os.path.join(dp, n))
    return out


def find_journal(h, out, before):
    """The journal of an apply: a state file the output names, else the one new
    state file with `adopt` in its path."""
    for m in re.findall(re.escape(h.state) + r"[^\s'\"]+", out):
        m = m.rstrip(".,;:)")
        if os.path.isfile(m):
            return m
    new = [p for p in state_files(h) - before if "adopt" in p.lower()
           and os.path.isfile(p)]
    return sorted(new)[0] if new else None


def make_template(h, d, extra=None):
    """The newest parsable card of the active pair, a rich one. Every other card is
    aged so that it is the newest by file time and by lastActivityAt."""
    import time
    old = time.time() - 7200
    for f in cards(d):
        os.utime(os.path.join(d, f), (old, old))
    lid = uid(90, 5)
    body = {"sessionId": "local_" + lid, "id": lid, "file": "local_%s.json" % lid,
            "cliSessionId": uid(90), "cwd": "/old/cwd", "title": "template title",
            "model": "opus-x", "createdAt": 1700000000000,
            "lastActivityAt": int(time.time() * 1000) + 1000, "completedTurns": 9,
            "nested": {"list": [1, 2, 3], "flag": True}, "padding": "x" * 500}
    if extra:
        body.update(extra)
    path = os.path.join(d, "local_%s.json" % lid)
    with open(path, "w") as fh:
        json.dump(body, fh)
    return path, body, lid


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-adopt-")
        self.h = Home(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def world(self):
        """Account A/O1 active, four cards with transcripts, a rich template card."""
        build_source(self.h)
        self.d = active_dir(self.h)
        self.tpl_path, self.tpl, self.tpl_lid = make_template(self.h, self.d)

    def run_adopt(self, *args, **envx):
        r = self.h.run(CMD, *args, **envx)
        return r, r.stdout + r.stderr


# --- A1: dry run lists orphans, writes nothing -----------------------------------

class A1DryRun(Base):
    def test_a1_dry_run_lists_every_fact_and_writes_nothing(self):
        self.world()
        long_prompt = "Q" * 80 + "TAIL-NOT-SHOWN"
        sid = v5(1)
        first, last = self.h.orphan(sid, cwd="/work/video-song",
                                    prompts=[long_prompt] + ["p%d" % i for i in range(6)])
        before_tree = tree_digest(self.h.root)
        r, out = self.run_adopt()
        self.assertEqual(r.returncode, 0, out)
        self.assertIn(sid, out)
        self.assertIn("/work/video-song", out)
        self.assertIn("Q" * 80, out)                      # first prompt cut to 80
        self.assertNotIn("TAIL-NOT-SHOWN", out)
        self.assertNotIn("Q" * 81, out)
        bare = out.replace(sid, "")
        self.assertTrue(re.search(r"(?<![0-9A-Za-z./-])7(?![0-9A-Za-z./-])", bare),
                        "the number of prompts (7) is printed: " + out)
        self.assertTrue(re.search(r"2025-1[01]|2025|\b\d{1,2}\.\d{2}\b", out),
                        "a last-activity time is printed: " + out)
        self.assertEqual(tree_digest(self.h.root), before_tree, "dry run writes nothing anywhere")

    def test_a1_selection_rules(self):
        self.world()
        self.h.orphan(v5(11), prompts=["org-v5-with-owner"])               # orphan
        self.h.orphan(v5(12), prompts=["org-v5-no-owner"], owner=False)     # orphan
        self.h.orphan(uid(13), prompts=["org-v4-with-owner"])               # orphan (owner line)
        self.h.orphan(uid(14), prompts=["not-orphan-v4-no-owner"], owner=False)
        self.h.orphan(v5(15), tool_result_only=True)                       # no real prompt
        cc_other = self.h.pair(CC, B, O2)                                  # card in ANOTHER account
        self.h.card(cc_other, 21, v5(16))
        self.h.orphan(v5(16), prompts=["has-card-other-account"])
        lam = self.h.pair(LAM, A, O1)                                      # card in the other store
        self.h.card(lam, 22, v5(17))
        self.h.orphan(v5(17), prompts=["has-card-agent-store"])
        r, out = self.run_adopt()
        self.assertEqual(r.returncode, 0, out)
        for n in (11, 12):
            self.assertIn(v5(n), out)
        self.assertIn(uid(13), out)
        for sid in (uid(14), v5(15), v5(16), v5(17)):
            self.assertNotIn(sid, out)
        for sid in ("not-orphan-v4-no-owner", "has-card-other-account",
                    "has-card-agent-store"):
            self.assertNotIn(sid, out)

    def test_a1_no_orphans_is_a_clean_zero(self):
        self.world()
        r, out = self.run_adopt()
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn("Traceback", out)


# --- A2: apply writes one card per orphan, a deep copy of the template -----------

class A2Apply(Base):
    def setup_orphans(self):
        self.world()
        self.s1, self.s2, self.s3 = v5(31), v5(32), v5(33)
        self.t1 = self.h.orphan(self.s1, cwd="/work/one", prompts=["one a", "one b"],
                                titles=[("ai-title", "Draft title"),
                                        ("ai-title", "Final title")])
        self.t2 = self.h.orphan(self.s2, cwd="/work/two", prompts=["two a"],
                                titles=[("custom-title", "Named by me")],
                                first_ms=1760100000000)
        self.long = "L" * 100
        self.t3 = self.h.orphan(self.s3, cwd="/work/three",
                                prompts=[self.long, "three b", "three c"],
                                first_ms=1760200000000)

    def test_a2_one_card_per_orphan_with_the_set_fields(self):
        self.setup_orphans()
        before = set(cards(self.d))
        tpl_sum = sha(self.tpl_path)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        new = sorted(set(cards(self.d)) - before)
        self.assertEqual(len(new), 3, new)
        for name in new:
            self.assertTrue(re.match(
                r"^local_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.json$",
                name), name)
        self.assertEqual(sha(self.tpl_path), tpl_sum, "the template file is untouched")
        for sid, cwd, title, turns, ts in (
                (self.s1, "/work/one", "Final title", 2, self.t1),
                (self.s2, "/work/two", "Named by me", 1, self.t2),
                (self.s3, "/work/three", "L" * 60, 3, self.t3)):
            hit = card_for(self.d, sid)
            self.assertIsNotNone(hit, sid)
            name, data = hit
            self.assertIn(name, new)
            self.assertEqual(data["cwd"], cwd)
            self.assertEqual(data["title"], title)
            self.assertEqual(data["completedTurns"], turns)
            self.assertIs(type(data["completedTurns"]), int)
            self.assertIs(type(data["createdAt"]), int)
            self.assertIs(type(data["lastActivityAt"]), int)
            self.assertEqual(data["createdAt"], ts[0])
            self.assertEqual(data["lastActivityAt"], ts[1])
            # deep copy of the template: fields not set stay as they were
            self.assertEqual(data["model"], "opus-x")
            self.assertEqual(data["nested"], self.tpl["nested"])
            self.assertEqual(data["padding"], self.tpl["padding"])
            # fields equal to the template's own local id carry the new id
            new_lid = name[len("local_"):-len(".json")]
            self.assertEqual(data["id"], new_lid)
            self.assertEqual(data["sessionId"], "local_" + new_lid)
            self.assertEqual(data["file"], name)

    def test_a2_one_orphan_card_exists_per_orphan_across_the_stores(self):
        self.setup_orphans()
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        for sid in (self.s1, self.s2, self.s3):
            self.assertEqual(len(all_cards_naming(self.h, sid)), 1, sid)
        # nothing lands outside the active pair of claude-code-sessions
        self.assertEqual(sorted(os.listdir(os.path.join(self.h.support, LAM))), [])

    def test_a2_refuses_while_the_app_runs(self):
        self.setup_orphans()
        before_cards = cards(self.d)
        r, out = self.run_adopt("--apply", CLAUDE_ACCOUNT_MOVE_PGREP=STUB_OPEN)
        self.assertNotIn(r.returncode, (0, PARSE_ERROR), out)
        self.assertEqual(cards(self.d), before_cards)
        for sid in (self.s1, self.s2, self.s3):
            self.assertEqual(all_cards_naming(self.h, sid), [])
        # and the same command with the app closed does write (a missing subcommand
        # cannot pass the refusal test above)
        r2, out2 = self.run_adopt("--apply")
        self.assertEqual(r2.returncode, 0, out2)
        self.assertEqual(len(all_cards_naming(self.h, self.s1)), 1)


# --- A3: no overwrite, journal intent before the write ---------------------------

class A3Safety(Base):
    def test_a3_journal_names_the_path_sha_orphan_and_operation(self):
        self.world()
        sid = v5(41)
        self.h.orphan(sid, prompts=["journal me"])
        before = state_files(self.h)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        name, data = card_for(self.d, sid)
        path = os.path.join(self.d, name)
        journal = find_journal(self.h, out, before)
        self.assertIsNotNone(journal, "the output names the journal: " + out)
        with open(journal) as fh:
            text = fh.read()
        self.assertIn(sid, text)
        self.assertIn(name, text)
        self.assertIn(sha(path), text)
        self.assertGreaterEqual(len([l for l in text.splitlines() if name in l]), 2,
                                "an intent line and a done line mention the card")
        leftovers = [f for f in os.listdir(self.d)
                     if not (f.startswith("local_") and f.endswith(".json"))]
        self.assertEqual(leftovers, [], "no temp file is left in the pair")

    def run_inprocess(self, *argv, fixed=None):
        out = io.StringIO()
        err = io.StringIO()
        real = os.urandom

        def urandom(n):
            return fixed if (fixed is not None and n == 16) else real(n)
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(common, "claude_app_running", lambda: False))
            es.enter_context(mock.patch.object(os, "urandom", urandom))
            es.enter_context(contextlib.redirect_stdout(out))
            es.enter_context(contextlib.redirect_stderr(err))
            try:
                code = cli.main(list(argv) + ["--home", self.h.home,
                                              "--state-dir", self.h.state])
            except SystemExit as exc:          # the parse error of the old code
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    def test_a3_an_existing_name_is_refused_never_overwritten(self):
        self.world()
        sid = v5(42)
        self.h.orphan(sid, prompts=["clash"])
        fixed = bytes(range(1, 17))
        clash_name = "local_%s.json" % uuid.UUID(bytes=fixed, version=4)
        clash_path = os.path.join(self.d, clash_name)
        with open(clash_path, "w") as fh:
            fh.write("SENTINEL-DO-NOT-TOUCH")
        before = set(cards(self.d))
        code, out = self.run_inprocess(CMD, "--apply", fixed=fixed)
        self.assertNotEqual(code, PARSE_ERROR, out)
        self.assertEqual(open(clash_path).read(), "SENTINEL-DO-NOT-TOUCH")
        self.assertEqual(set(cards(self.d)), before, "nothing was written for the clash")
        self.assertIsNone(card_for(self.d, sid))
        self.assertIn(sid, out, "the refused orphan is reported by id")


# --- A4: idempotent --------------------------------------------------------------

class A4Idempotent(Base):
    def test_a4_second_run_writes_nothing(self):
        self.world()
        sids = [v5(51), v5(52)]
        for s in sids:
            self.h.orphan(s, prompts=["again " + s[:8]])
        r1, out1 = self.run_adopt("--apply")
        self.assertEqual(r1.returncode, 0, out1)
        snap = tree_digest(self.h.support)
        n_cards = len(cards(self.d))
        r2, out2 = self.run_adopt("--apply")
        self.assertEqual(r2.returncode, 0, out2)
        self.assertEqual(tree_digest(self.h.support), snap)
        self.assertEqual(len(cards(self.d)), n_cards)
        for s in sids:
            self.assertEqual(len(all_cards_naming(self.h, s)), 1)
        r3, out3 = self.run_adopt()          # and the dry run no longer lists them
        for s in sids:
            self.assertNotIn(s, out3)


# --- A5: undo removes only what the journal created, only if unchanged -----------

class A5Undo(Base):
    def test_a5_undo_removes_only_unchanged_created_files(self):
        self.world()
        sids = [v5(61), v5(62), v5(63)]
        for s in sids:
            self.h.orphan(s, prompts=["undo " + s[:8]])
        pre_cards = set(cards(self.d))
        before = state_files(self.h)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        journal = find_journal(self.h, out, before)
        self.assertIsNotNone(journal, out)
        created = {s: card_for(self.d, s)[0] for s in sids}
        p_ok, p_edit, p_inode = (os.path.join(self.d, created[s]) for s in sids)
        with open(p_edit, "a") as fh:                   # content changed after the apply
            fh.write(" ")
        data = open(p_inode, "rb").read()               # same bytes, another inode
        tmp = p_inode + ".swap"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p_inode)
        r2, out2 = self.run_adopt("--undo", journal)
        self.assertNotEqual(r2.returncode, PARSE_ERROR, out2)
        self.assertFalse(os.path.exists(p_ok), "the untouched created card is removed")
        self.assertTrue(os.path.exists(p_edit), "a changed file is left")
        self.assertTrue(os.path.exists(p_inode), "a file with another inode is left")
        self.assertIn(created[sids[1]], out2, "the ambiguous file is reported")
        self.assertIn(created[sids[2]], out2)
        self.assertTrue(pre_cards <= set(cards(self.d)), "no card that existed is removed")

    def test_a5_clean_undo_restores_the_pair(self):
        self.world()
        sid = v5(64)
        self.h.orphan(sid, prompts=["undo clean"])
        pre = tree_digest(self.d)
        before = state_files(self.h)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        journal = find_journal(self.h, out, before)
        self.assertIsNotNone(journal, out)
        r2, out2 = self.run_adopt("--undo", journal)
        self.assertEqual(r2.returncode, 0, out2)
        self.assertEqual(tree_digest(self.d), pre)


# --- A6: no active pair or no template: refuse, exit 3 ---------------------------

class A6Refusals(Base):
    def test_a6_no_parsable_template_card(self):
        d = self.h.pair(CC, A, O1)
        for i in (1, 2):
            with open(os.path.join(d, "local_%s.json" % uid(70 + i, 9)), "w") as fh:
                fh.write("{not json")
        self.h.accounts(cli=A, app=A)
        self.h.log((A, O1))
        sid = v5(71)
        self.h.orphan(sid, prompts=["no template"])
        before = tree_digest(self.h.support)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, EXIT_FAIL, out)
        self.assertTrue(out.strip())
        self.assertNotIn("Traceback", out)
        self.assertEqual(tree_digest(self.h.support), before)

    def test_a6_no_active_pair(self):
        d = self.h.pair(CC, A, O1)                  # a pair with a good card, but the
        self.h.card(d, 72, uid(72))                 # app log and the accounts name none
        self.h.orphan(v5(72), prompts=["no pair"])
        before = tree_digest(self.h.support)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, EXIT_FAIL, out)
        self.assertTrue(out.strip())
        self.assertNotIn("Traceback", out)
        self.assertEqual(tree_digest(self.h.support), before)


# --- A7: transcripts are never written -------------------------------------------

class A7Transcripts(Base):
    def test_a7_transcript_bytes_and_times_unchanged(self):
        self.world()
        for n in (81, 82):
            self.h.orphan(v5(n), prompts=["keep me %d" % n])
        want = transcript_state(self.h)
        before = state_files(self.h)
        r0, o0 = self.run_adopt()
        self.assertEqual(r0.returncode, 0, o0)
        self.assertEqual(transcript_state(self.h), want)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(transcript_state(self.h), want)
        journal = find_journal(self.h, out, before)
        self.assertIsNotNone(journal, out)
        r2, o2 = self.run_adopt("--undo", journal)
        self.assertEqual(r2.returncode, 0, o2)
        self.assertEqual(transcript_state(self.h), want)


# --- A8: --only limits the run ---------------------------------------------------

class A8Only(Base):
    def test_a8_only_limits_the_run_and_reports_a_non_orphan(self):
        self.world()
        s1, s2, s3 = v5(91), v5(92), v5(93)
        for s in (s1, s2, s3):
            self.h.orphan(s, prompts=["only " + s[:8]])
        not_orphan = uid(11)                   # has a card from build_source
        r, out = self.run_adopt("--apply", "--only", s1, "--only", not_orphan)
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(len(all_cards_naming(self.h, s1)), 1)
        self.assertEqual(all_cards_naming(self.h, s2), [])
        self.assertEqual(all_cards_naming(self.h, s3), [])
        self.assertIn(not_orphan, out, "an id that is not an orphan is reported")
        self.assertEqual(len(all_cards_naming(self.h, not_orphan)), 1,
                         "and not written again")

    def test_a8_only_in_the_dry_run_lists_just_that_orphan(self):
        self.world()
        s1, s2 = v5(94), v5(95)
        for s in (s1, s2):
            self.h.orphan(s, prompts=["trial " + s[:8]])
        r, out = self.run_adopt("--only", s1)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn(s1, out)
        self.assertNotIn(s2, out)


# --- G1: prepare names orphans as a readiness note -------------------------------

class G1Prepare(Base):
    def test_g1_prepare_notes_the_orphans_by_name_with_the_count(self):
        build_source(self.h)
        self.h.orphan(v5(101), prompts=["video pipeline for the song"])
        self.h.orphan(v5(102), prompts=["second lost session"], owner=False)
        r = self.h.run("prepare")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)             # a note, not a failure
        self.assertIn("video pipeline for the song", out)
        self.assertIn("second lost session", out)
        lines = [l for l in out.splitlines() if re.search(r"\b2\b", l)
                 and re.search(NOTE_RE, l)]
        self.assertTrue(lines, "the count 2 is on a line about the sessions: " + out)

    def test_g1_prepare_without_orphans_says_nothing_of_them(self):
        build_source(self.h)
        r = self.h.run("prepare")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn("orphan", out.lower())


# --- G2: auto adopts after heal-active -------------------------------------------

class G2Auto(test_auto.Base):
    def test_g2_auto_adopts_and_records_the_journal(self):
        build_source(self.h)
        sid = v5(111)
        self.h.orphan(sid, cwd="/work/lost", prompts=["lost video session"])
        code, out, err, m = self.run_auto()
        self.assertEqual(code, 0, out)
        target = os.path.join(self.h.support, CC, B, O2)
        hit = card_for(target, sid)
        self.assertIsNotNone(hit, "the orphan has a card in the new active pair: " + out)
        self.assertEqual(hit[1]["cwd"], "/work/lost")
        self.assertEqual(len(all_cards_naming(self.h, sid)), 1)
        mentions = [p for p in re.findall(re.escape(self.h.state) + r"[^\s'\"]+", out)
                    if "adopt" in p.lower() and os.path.isfile(p.rstrip(".,;:)"))]
        self.assertTrue(mentions, "the report names the adopt journal: " + out)
        for line in out.splitlines():
            self.assertFalse("adopt" in line and "--apply" in line,
                             "auto never tells the Creator to run adopt by hand: " + line)

    def test_g2_auto_without_orphans_makes_no_adopt_journal(self):
        build_source(self.h)
        code, out, err, m = self.run_auto()
        self.assertEqual(code, 0, out)
        self.assertEqual([p for p in glob.glob(os.path.join(self.h.state, "*adopt*"))], [])


# --- G3: the post-move check ------------------------------------------------------

class G3Finish(Base):
    def moved(self):
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        r = self.h.run("sync", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_g3_orphans_make_the_verdict_not_ok(self):
        build_source(self.h)
        prompts = ["orphan prompt %d" % i for i in range(6)]
        for i, p in enumerate(prompts):
            self.h.orphan(v5(120 + i), prompts=[p])
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        r = self.h.run("finish", "--wait-login", "3", "--json")
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertNotEqual(r.returncode, PARSE_ERROR, out)
        self.assertIn(CHECK_NAME, out)
        self.assertIn(PROBLEM, out)
        shown = [p for p in prompts if p in out]
        self.assertEqual(len(shown), 5, "up to 5 are named by first prompt: %s" % shown)
        rep = json.loads(r.stdout)
        chk = [c for c in rep["checks"] if NO_CARD in (c.get("detail", "") + c.get("id", ""))
               or NO_CARD in str(c)]
        self.assertTrue(chk and not chk[0]["ok"], rep["checks"])
        self.assertTrue(re.search(r"\b6\b", chk[0]["detail"]), "the total is named: " + str(chk[0]))

    def test_g3_no_orphans_the_check_is_ok(self):
        self.moved()
        r = self.h.run("finish", "--wait-login", "3", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rep = json.loads(r.stdout)
        chk = [c for c in rep["checks"] if NO_CARD in str(c)]
        self.assertTrue(chk and chk[0]["ok"], "the check exists and is ok: %s" % rep["checks"])


if __name__ == "__main__":
    unittest.main()
