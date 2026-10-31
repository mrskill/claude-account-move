"""Oracle for heal-active symlinks inside the moved tree.

The pair folder moves as one tree. A relative link whose target, normalized
lexically from the link's own directory, stays at or below the moved folder
points to the same file after the move, so it is safe. A relative link that
escapes the folder is refused. Any absolute link is refused, whether it points
into the folder (it would keep pointing at the old place) or outside it (kept
by test_heal_active_r4 AnySymlink.test_absolute_symlink_refuses).
A dangling link that stays inside the tree is allowed."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from fixture import CC, tree_digest  # noqa: E402
from test_heal_active_r3 import Base  # noqa: E402
from test_heal_active import Chain  # noqa: E402
from claude_account_move import heal_active  # noqa: E402

NANOID_JS = "#!/usr/bin/env node\nconsole.log('nanoid');\n"


def write(path, text="data\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def read(path):
    with open(path) as fh:
        return fh.read()


class UnitRule(unittest.TestCase):
    """relative_link_problem(root) in isolation."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cam-relink-unit-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(self.root)

    def test_node_modules_bin_link_inside_the_tree_is_allowed(self):
        nm = os.path.join(self.root, "x", "outputs", "node_modules")
        write(os.path.join(nm, "nanoid", "bin", "nanoid.js"), NANOID_JS)
        os.makedirs(os.path.join(nm, ".bin"))
        os.symlink("../nanoid/bin/nanoid.js", os.path.join(nm, ".bin", "nanoid"))
        self.assertIsNone(heal_active.relative_link_problem(self.root))

    def test_deep_relative_link_inside_the_tree_is_allowed(self):
        write(os.path.join(self.root, "d", "e"))
        os.makedirs(os.path.join(self.root, "a", "b"))
        os.symlink("../../d/e", os.path.join(self.root, "a", "b", "c"))
        self.assertIsNone(heal_active.relative_link_problem(self.root))

    def test_relative_link_escaping_by_one_level_is_refused(self):
        write(os.path.join(self.tmp, "outside"))
        os.symlink("../outside", os.path.join(self.root, "f"))
        msg = heal_active.relative_link_problem(self.root)
        self.assertIsNotNone(msg)
        self.assertIn("relative symlink", msg)

    def test_relative_link_leaving_and_reentering_by_parent_name_is_refused(self):
        write(os.path.join(self.root, "g"))
        os.symlink("../%s/g" % os.path.basename(self.root),
                   os.path.join(self.root, "f"))
        msg = heal_active.relative_link_problem(self.root)
        self.assertIsNotNone(msg)
        self.assertIn("relative symlink", msg)

    def test_absolute_link_to_a_file_inside_the_tree_is_refused(self):
        target = os.path.join(self.root, "g")
        write(target)
        os.symlink(target, os.path.join(self.root, "f"))
        msg = heal_active.relative_link_problem(self.root)
        self.assertIsNotNone(msg)
        self.assertIn("absolute symlink", msg)

    def test_absolute_link_to_a_file_outside_the_tree_is_refused(self):
        outside = tempfile.mkdtemp(prefix="cam-relink-out-")
        self.addCleanup(shutil.rmtree, outside, True)
        target = os.path.join(outside, "far.txt")
        write(target)
        os.symlink(target, os.path.join(self.root, "f"))
        msg = heal_active.relative_link_problem(self.root)
        self.assertIsNotNone(msg)
        self.assertIn("absolute symlink", msg)

    def test_dangling_relative_link_inside_the_tree_is_allowed(self):
        os.makedirs(os.path.join(self.root, "a"))
        os.symlink("../missing/nowhere.js", os.path.join(self.root, "a", "f"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "a", "f")))
        self.assertIsNone(heal_active.relative_link_problem(self.root))


class EndToEnd(Base):
    """heal-active through the CLI."""

    def test_node_modules_link_survives_the_move(self):
        ch = Chain(self.h)
        real = ch.real[CC]
        nm = os.path.join(real, "local_9ab72ba5-0000", "outputs", "node_modules")
        write(os.path.join(nm, "nanoid", "bin", "nanoid.js"), NANOID_JS)
        os.makedirs(os.path.join(nm, ".bin"))
        os.symlink("../nanoid/bin/nanoid.js", os.path.join(nm, ".bin", "nanoid"))
        before = read(os.path.join(nm, ".bin", "nanoid"))
        self.assertEqual(before, NANOID_JS)
        code, text = self.run_cli("--apply")
        self.assertEqual(code, 0, text)
        link = os.path.join(ch.active[CC], "local_9ab72ba5-0000", "outputs",
                            "node_modules", ".bin", "nanoid")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), "../nanoid/bin/nanoid.js")
        resolved = os.path.realpath(link)
        self.assertEqual(read(resolved), before)
        self.assertTrue(resolved.startswith(os.path.realpath(ch.active[CC]) + os.sep),
                        resolved)

    def test_absolute_link_into_the_real_folder_refuses(self):
        ch = Chain(self.h)
        real = ch.real[CC]
        write(os.path.join(real, "sub", "g"))
        os.symlink(os.path.join(real, "sub", "g"), os.path.join(real, "abs.lnk"))
        before = tree_digest(self.h.support)
        code, text = self.run_cli("--apply")
        self.assertNotEqual(code, 0, text)
        self.assertIn("absolute symlink", text)
        self.assertEqual(tree_digest(self.h.support), before)


if __name__ == "__main__":
    unittest.main()
