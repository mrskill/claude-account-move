"""inside_physical (common.py): physical identity instead of path strings, for
the overlap refusals. A case-variant spelling is one folder on a case-insensitive
volume (macOS default). Fake paths in a temp dir only."""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import Home  # noqa: E402

from claude_account_move import common  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402


def volume_is_case_insensitive():
    d = tempfile.mkdtemp(prefix="cam-case-")
    try:
        open(os.path.join(d, "x"), "w").close()
        return os.path.exists(os.path.join(d, "X"))
    finally:
        shutil.rmtree(d, True)


CASE_INSENSITIVE = volume_is_case_insensitive()


class InsidePhysical(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-inphys-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.d = os.path.join(self.tmp, "payload")
        os.makedirs(os.path.join(self.d, "child"))
        os.makedirs(os.path.join(self.tmp, "sibling"))

    @unittest.skipUnless(CASE_INSENSITIVE, "case-sensitive volume")
    def test_same_folder_under_two_spellings(self):
        variant = os.path.join(self.tmp, "PAYLOAD")
        self.assertTrue(common.inside_physical(self.d, variant))
        self.assertTrue(common.inside_physical(variant, self.d))

    @unittest.skipUnless(CASE_INSENSITIVE, "case-sensitive volume")
    def test_child_under_a_case_variant_parent(self):
        child = os.path.join(self.tmp, "PAYLOAD", "child")
        self.assertTrue(common.inside_physical(child, self.d))
        self.assertFalse(common.inside_physical(self.d, child))

    def test_parent_and_child(self):
        child = os.path.join(self.d, "child")
        self.assertTrue(common.inside_physical(child, self.d))
        self.assertFalse(common.inside_physical(self.d, child))
        self.assertTrue(common.inside_physical(self.d, self.d))

    def test_sibling_is_not_inside(self):
        sib = os.path.join(self.tmp, "sibling")
        self.assertFalse(common.inside_physical(sib, self.d))
        self.assertFalse(common.inside_physical(self.d, sib))

    def test_a_path_that_does_not_exist_is_no_conflict_on_its_own(self):
        gone = os.path.join(self.tmp, "elsewhere", "nothing")
        self.assertFalse(common.inside_physical(gone, self.d))
        self.assertFalse(common.inside_physical(self.d, gone))


@unittest.skipUnless(CASE_INSENSITIVE, "case-sensitive volume")
class OverlapProblemCaseVariant(unittest.TestCase):
    def test_state_dir_spelled_as_a_case_variant_of_a_protected_path(self):
        tmp = tempfile.mkdtemp(prefix="cam-ovl-")
        self.addCleanup(shutil.rmtree, tmp, True)
        h = Home(tmp)
        os.makedirs(h.support)
        paths = Paths(h.home, h.state)
        variant = os.path.join(os.path.dirname(h.support), os.path.basename(h.support).upper())
        self.assertIsNotNone(common.overlap_problem(variant, paths),
                             "a case variant of the support folder passed as a state dir")


if __name__ == "__main__":
    unittest.main()
