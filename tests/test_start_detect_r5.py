"""CAM-START-DETECT oracle round 5 (head 082bf9a): the CC start signal is the
latest claude-code-sessions Loaded line naming the CLI PAIR, account AND org
(contract C3). heal_active.fresh_root_loads compares only the account, so a
latest line B/O1 with the CLI org O2 is wrongly a start. ASCII only, fixture
Home only.

  (1) wait_app_start: latest CC line B/O1, CLI org O2 exists, no fresh card -> None
  (2) verify-start, same setup, app identity B -> app_not_started, exit 3
  (3) positive control: latest CC line B/O2, CLI org O2 -> start returned, ok
  (4) wait_app_start: earlier B/O2 then later B/O1 after the mark -> None
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import CC, LAM  # noqa: E402
from test_cli import A, B, O1, O2, build_source  # noqa: E402
from test_start_detect import Say, append_log, loaded_line  # noqa: E402
from test_start_detect_r3 import VerifyBase  # noqa: E402
from test_start_detect_r4 import age_cards  # noqa: E402

from claude_account_move import auto_move, common, heal_active  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402


class WaitOrg(VerifyBase):
    def setUp(self):
        super(WaitOrg, self).setUp()
        p = mock.patch.object(common, "claude_app_running", lambda: True)
        p.start()
        self.addCleanup(p.stop)

    def prep(self):
        build_source(self.h)
        self.h.pair(CC, B, O2)
        self.h.pair(CC, B, O1)
        self.h.pair(LAM, B, O2)
        self.h.accounts(cli=B, app=B, org=O2)
        age_cards(self.h)
        return Paths(self.h.home, self.h.state)

    def wait(self, paths, mark):
        return auto_move.wait_app_start(paths, B, mark, 0.4, 0.02, Say())

    def test_1_latest_line_with_another_org_is_not_a_start(self):
        paths = self.prep()
        mark = heal_active.log_mark(paths)
        append_log(self.h, loaded_line(self.h, CC, acc=B, org=O1))
        self.assertIsNone(self.wait(paths, mark),
                          "B/O1 is not the CLI pair B/O2: no start")

    def test_3a_positive_control_latest_line_is_the_cli_pair(self):
        paths = self.prep()
        mark = heal_active.log_mark(paths)
        append_log(self.h, loaded_line(self.h, CC, acc=B, org=O2))
        res = self.wait(paths, mark)
        self.assertIsNotNone(res)
        self.assertEqual(tuple(res[CC]), (B, O2))

    def test_4_the_later_line_decides_even_after_a_matching_one(self):
        paths = self.prep()
        mark = heal_active.log_mark(paths)
        append_log(self.h, loaded_line(self.h, CC, acc=B, org=O2),
                   loaded_line(self.h, CC, acc=B, org=O1))
        self.assertIsNone(self.wait(paths, mark),
                          "latest line B/O1 cancels the earlier B/O2")


class VerifyOrg(VerifyBase):
    def test_2_verify_start_latest_line_other_org_is_app_not_started(self):
        self.finished_move()
        self.h.accounts(cli=B, app=B, org=O2)
        self.h.pair(CC, B, O1)
        append_log(self.h, loaded_line(self.h, CC, acc=B, org=O1))
        age_cards(self.h)
        code, out, err = self.run_verify(True)
        self.assertEqual(code, 3, out + err)
        self.assertEqual(self.verdict()["verdict"], "app_not_started")

    def test_3b_positive_control_verify_start_ok(self):
        self.finished_move()
        self.h.accounts(cli=B, app=B, org=O2)
        append_log(self.h, loaded_line(self.h, CC, acc=B, org=O2))
        code, out, err = self.run_verify(True)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.verdict()["verdict"], "ok")


if __name__ == "__main__":
    unittest.main()
