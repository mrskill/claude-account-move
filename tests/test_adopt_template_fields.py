"""ORACLE for CAM-ADOPT-TEMPLATE-FIELDS, written before the implementation.

An adopted card is built from an allow-list (W1-W3), never as a copy of the
template card: on 25.11.2026 fifteen adopted cards inherited `isStarred: true`,
`prs`, `spawnedFrom`, `folderPlace` and more from the template.

Red on release 71b7e79 means: `adopt_orphans.CARD_FIELDS` is missing (read with
getattr, so its absence is a test failure, not an import error) and `build_card`
copies every template key.

Assumptions the contract leaves open: `originCwd` is present (as "") when the cwd
is empty (W3 says it is "that same cwd" with no exception, only `folderPlace` has
one); `effort` and `sessionSettings` of the template are any JSON values.
"""
import copy
import hashlib
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import test_adopt_orphans as ao  # noqa: E402
from fixture import v5  # noqa: E402

from claude_account_move import adopt_orphans as ao_module  # noqa: E402

W1_FIELDS = frozenset([
    "sessionId", "cliSessionId", "cwd", "originCwd", "folderPlace", "title",
    "createdAt", "lastActivityAt", "completedTurns", "model", "effort",
    "sessionSettings", "isArchived", "isStarred"])

TPL_LID = "11111111-2222-4333-8444-555555555555"
TPL_NAME = "local_%s.json" % TPL_LID
NEW_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ORPHAN_ID = "99999999-8888-5777-9666-555555555555"

# every per-session key the 25.11.2026 template leaked, and one the app adds later
LEAKY = {
    "isStarred": True,
    "isArchived": True,
    "prs": [{"repo": "mrskill/claude-sessions", "number": 12}],
    "seenCommentIds": ["c1", "c2"],
    "spawnedFrom": {"sessionId": "local_spawner"},
    "bridgeSessionIds": ["b1"],
    "peerReceipts": {"peer": {"at": 1}},
    "postTurnSummary": "judge summary",
    "lastAssistantCliSessionId": "abcabcab-0000-4000-8000-000000000000",
    "chromePermissionMode": "skip_all_permission_checks",
    "writtenRepositories": ["/judge/repo"],
    "shellFolders": ["/judge/shell"],
    "folderPlace": {"cwd": "/judge/folder", "realPath": "/judge/folder"},
    "toolSurfaceSnapshot": {"tools": ["Bash", "Edit"]},
    "systemPromptSnapshot": "judge prompt",
    "someFutureField": {"anything": [1, 2, 3]},
    "id": TPL_LID,
    "file": TPL_NAME,
}


def rich_template(**over):
    tpl = {"sessionId": "local_" + TPL_LID, "cliSessionId": "77777777-0000-4000-8000-000000000000",
           "cwd": "/judge/cwd", "originCwd": "/judge/cwd", "title": "judge title",
           "createdAt": 1700000000000, "lastActivityAt": 1700000999000,
           "completedTurns": 41, "model": "opus-x", "effort": "high",
           "sessionSettings": {"thinking": {"budget": 7}, "flags": ["a", "b"]}}
    tpl.update(copy.deepcopy(LEAKY))
    tpl.update(over)
    return tpl


def orphan(cwd="/work/orphan", **over):
    o = {"id": ORPHAN_ID, "cwd": cwd, "first": "first prompt", "title": "Orphan title",
         "prompts": 3, "created_ms": 1760000000000, "last_ms": 1760000150000}
    o.update(over)
    return o


def fields():
    return getattr(ao_module, "CARD_FIELDS", None)


def build(tpl, orp):
    return ao_module.build_card(TPL_NAME, tpl, orp, NEW_ID)


class W1AllowList(unittest.TestCase):
    def test_w1_card_fields_is_the_exact_frozenset(self):
        got = fields()
        self.assertIsInstance(got, frozenset,
                              "adopt_orphans.CARD_FIELDS is a module-level frozenset")
        self.assertEqual(got, W1_FIELDS)

    def test_w1_no_template_key_outside_the_allow_list_reaches_the_card(self):
        _name, card = build(rich_template(), orphan())
        allowed = fields() or frozenset()
        self.assertEqual(set(card) - allowed, set(),
                         "card keys outside adopt_orphans.CARD_FIELDS")
        self.assertLessEqual(set(card), W1_FIELDS)
        for key in LEAKY:
            if key not in W1_FIELDS:
                self.assertNotIn(key, card, key)


class W2FromTemplate(unittest.TestCase):
    def test_w2_model_effort_settings_copied_by_value_nothing_else_read(self):
        tpl = rich_template()
        _name, card = build(tpl, orphan())
        self.assertEqual(card.get("model"), "opus-x")
        self.assertEqual(card.get("effort"), "high")
        self.assertEqual(card.get("sessionSettings"),
                         {"thinking": {"budget": 7}, "flags": ["a", "b"]})
        card["sessionSettings"]["thinking"]["budget"] = 999
        card["sessionSettings"]["flags"].append("mutated")
        self.assertEqual(tpl["sessionSettings"],
                         {"thinking": {"budget": 7}, "flags": ["a", "b"]},
                         "mutating the card touches the template dict")
        # nothing else of the template: every card key not set by adopt is one of W2
        set_by_adopt = W1_FIELDS - {"model", "effort", "sessionSettings"}
        self.assertEqual(set(card) - set_by_adopt, {"model", "effort", "sessionSettings"})

    def test_w2_absent_template_fields_stay_absent(self):
        tpl = rich_template()
        del tpl["effort"]
        del tpl["sessionSettings"]
        _name, card = build(tpl, orphan())
        self.assertNotIn("effort", card)
        self.assertNotIn("sessionSettings", card)
        self.assertEqual(set(card), W1_FIELDS - {"effort", "sessionSettings"},
                         "exact key set of a card from a template without effort")


class W3SetByAdopt(unittest.TestCase):
    def test_w3_ids_cwd_origin_folder_place(self):
        name, card = build(rich_template(), orphan(cwd="/work/orphan"))
        self.assertEqual(name, "local_%s.json" % NEW_ID)
        self.assertEqual(card.get("sessionId"), "local_" + NEW_ID)
        self.assertEqual(card.get("cliSessionId"), ORPHAN_ID)
        self.assertEqual(card.get("cwd"), "/work/orphan")
        self.assertEqual(card.get("originCwd"), "/work/orphan")
        self.assertEqual(card.get("folderPlace"),
                         {"cwd": "/work/orphan", "realPath": "/work/orphan"})

    def test_w3_title_times_turns_as_today(self):
        _name, card = build(rich_template(), orphan())
        self.assertEqual(card.get("title"), "Orphan title")
        self.assertEqual(card.get("createdAt"), 1760000000000)
        self.assertEqual(card.get("lastActivityAt"), 1760000150000)
        self.assertEqual(card.get("completedTurns"), 3)
        for k in ("createdAt", "lastActivityAt", "completedTurns"):
            self.assertIs(type(card.get(k)), int, k)
        self.assertEqual(card.get("originCwd"), "/work/orphan")

    def test_w3_not_archived_not_starred_even_when_the_template_is(self):
        tpl = rich_template(isStarred=True, isArchived=True)
        _name, card = build(tpl, orphan())
        self.assertIn("isStarred", card)
        self.assertIn("isArchived", card)
        self.assertIs(card["isStarred"], False)
        self.assertIs(card["isArchived"], False)

    def test_w3_not_archived_not_starred_when_the_template_has_no_such_keys(self):
        tpl = rich_template()
        del tpl["isStarred"]
        del tpl["isArchived"]
        _name, card = build(tpl, orphan())
        self.assertIs(card.get("isStarred"), False, "isStarred is set always")
        self.assertIs(card.get("isArchived"), False, "isArchived is set always")

    def test_w3_cwd_falls_back_to_the_template_cwd(self):
        _name, card = build(rich_template(cwd="/tpl/cwd"), orphan(cwd=""))
        self.assertEqual(card.get("cwd"), "/tpl/cwd")
        self.assertEqual(card.get("originCwd"), "/tpl/cwd")
        self.assertEqual(card.get("folderPlace"), {"cwd": "/tpl/cwd", "realPath": "/tpl/cwd"})

    def test_w3_no_folder_place_when_the_cwd_is_empty(self):
        tpl = rich_template()
        del tpl["cwd"]          # the template still carries a folderPlace of another folder
        _name, card = build(tpl, orphan(cwd=""))
        self.assertEqual(card.get("cwd"), "")
        self.assertNotIn("folderPlace", card)
        self.assertEqual(card.get("originCwd"), "")


class W4TemplateUntouched(unittest.TestCase):
    def test_w4_template_dict_unchanged_and_its_true_stays_its_own(self):
        tpl = rich_template()
        before = copy.deepcopy(tpl)
        _name, card = build(tpl, orphan())
        self.assertEqual(tpl, before, "build_card changed the template dict")
        # the card's False is not reached by touching the template
        self.assertIs(tpl["isStarred"], True)
        self.assertIs(card.get("isStarred"), False)


class W6Docs(unittest.TestCase):
    def test_w6_module_docstring_says_allow_list(self):
        doc = ao_module.__doc__ or ""
        self.assertFalse("deep copy of a template" in doc,
                         "module docstring still says 'deep copy of a template'")
        self.assertTrue(re.search(r"(?i)allow-?list", doc),
                        "module docstring does not say allow-list")

    def test_w6_changelog_says_allow_list(self):
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(re.search(r"(?i)allow-?list", text),
                        "CHANGELOG.md does not say the card is built from an allow-list")


class WEndToEnd(ao.Base):
    def test_w_apply_builds_cards_from_the_allow_list(self):
        ao.build_source(self.h)
        self.d = ao.active_dir(self.h)
        extra = copy.deepcopy(LEAKY)
        for k in ("id", "file"):
            extra.pop(k)
        extra.update({"effort": "high", "sessionSettings": {"thinking": {"budget": 7}}})
        self.tpl_path, self.tpl, self.tpl_lid = ao.make_template(self.h, self.d, extra)
        with open(self.tpl_path, "rb") as fh:
            tpl_bytes = fh.read()
        s1, s2 = v5(41), v5(42)
        self.h.orphan(s1, cwd="/work/one", prompts=["one a", "one b"])
        self.h.orphan(s2, cwd="/work/two", prompts=["two a"])
        before = set(ao.cards(self.d))
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(len(set(ao.cards(self.d)) - before), 2)
        with open(self.tpl_path, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(),
                             hashlib.sha256(tpl_bytes).hexdigest(),
                             "the template file is untouched")
        for sid, cwd in ((s1, "/work/one"), (s2, "/work/two")):
            hit = ao.card_for(self.d, sid)
            self.assertIsNotNone(hit, sid)
            name, data = hit
            self.assertEqual(set(data) - W1_FIELDS, set(), "keys outside the allow-list")
            self.assertEqual(data["sessionId"], name[:-len(".json")])
            self.assertEqual(data.get("originCwd"), cwd)
            self.assertEqual(data.get("folderPlace"), {"cwd": cwd, "realPath": cwd})
            self.assertIs(data.get("isStarred"), False)
            self.assertIs(data.get("isArchived"), False)
            self.assertEqual(data.get("model"), "opus-x")
            self.assertEqual(data.get("effort"), "high")
            self.assertEqual(data.get("sessionSettings"), {"thinking": {"budget": 7}})
        with open(self.tpl_path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), self.tpl)


if __name__ == "__main__":
    unittest.main()
