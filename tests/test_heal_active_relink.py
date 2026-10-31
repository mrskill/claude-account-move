"""Oracle for heal-active symlinks inside the moved tree.

The pair folder moves as one tree. A relative link whose target, normalized
lexically from the link's own directory, stays at or below the moved folder
points to the same file after the move, so it is safe. A relative link that
escapes the folder is refused. Any absolute link is refused, whether it points
into the folder (it would keep pointing at the old place) or outside it (kept
by test_heal_active_r4 AnySymlink.test_absolute_symlink_refuses).
A dangling link that stays inside the tree is allowed."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from fixture import CC, LAM, tree_digest  # noqa: E402
from test_heal_active_r3 import Base, shape  # noqa: E402
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


class TraversalRound2(unittest.TestCase):
    """Round 2 (foreign review of 6b969cf). A relative link is allowed only
    when walking its target from the link's folder never leaves the root at any
    step and no folder it passes through is a symlink; only the last component
    may be a symlink or missing."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cam-relink-r2-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(self.root, "a"))
        os.makedirs(os.path.join(self.root, "b"))

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def refused(self):
        msg = heal_active.relative_link_problem(self.root)
        self.assertIsNotNone(msg)
        self.assertIn("relative symlink", msg)

    def test_directory_link_alone_is_allowed(self):
        os.symlink("../b", self.p("a", "d"))
        self.assertIsNone(heal_active.relative_link_problem(self.root))

    def test_finding1_leave_through_symlinked_folder_and_reenter_refuses(self):
        write(self.p("x"))
        os.symlink("../b", self.p("a", "d"))
        os.symlink("d/../../%s/x" % os.path.basename(self.root), self.p("a", "f"))
        self.assertEqual(read(self.p("a", "f")), "data\n")     # it does resolve now
        self.refused()

    def test_link_through_a_symlinked_folder_refuses_even_inside(self):
        write(self.p("b", "y"))
        os.symlink("../b", self.p("a", "d"))
        os.symlink("d/y", self.p("a", "g"))
        self.assertEqual(read(self.p("a", "g")), "data\n")
        self.refused()

    def test_link_to_the_parent_of_the_root_boundary_refuses(self):
        os.symlink("../..", self.p("a", "f"))
        self.refused()

    def test_resolved_containment_alone_refuses(self):
        # lexically a/d/../../outside is root/outside (inside), really it is
        # <tmp>/outside (outside the root)
        write(os.path.join(self.tmp, "outside"))
        os.symlink("../b", self.p("a", "d"))
        os.symlink("d/../../outside", self.p("a", "f"))
        self.refused()

    def test_final_component_may_be_a_symlink_or_missing(self):
        write(self.p("b", "real"))
        os.symlink("real", self.p("b", "hop"))
        os.symlink("../b/hop", self.p("a", "f"))        # last component is a link
        os.symlink("../b/missing", self.p("a", "g"))    # last component is missing
        self.assertIsNone(heal_active.relative_link_problem(self.root))


def _case_insensitive(directory):
    probe = os.path.join(directory, "caseprobe")
    write(probe)
    try:
        return os.path.exists(os.path.join(directory, "CASEPROBE"))
    finally:
        os.unlink(probe)


class CaseFoldLoop(unittest.TestCase):
    """B: the loop check compares identity, not spelling."""

    def test_link_to_own_folder_in_other_case_refuses(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="cam-relink-case-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        if not _case_insensitive(tmp):
            self.skipTest("the temp volume is case-sensitive")
        root = os.path.join(tmp, "root")
        os.makedirs(os.path.join(root, "a"))
        os.symlink("../A", os.path.join(root, "a", "link"))
        msg = heal_active.relative_link_problem(root)
        self.assertIsNotNone(msg)
        self.assertIn("relative symlink", msg)


def add_node_modules(folder):
    nm = os.path.join(folder, "local_9ab72ba5-0000", "outputs", "node_modules")
    write(os.path.join(nm, "nanoid", "bin", "nanoid.js"), NANOID_JS)
    os.makedirs(os.path.join(nm, ".bin"))
    os.symlink("../nanoid/bin/nanoid.js", os.path.join(nm, ".bin", "nanoid"))
    return nm


class ApplyTimeRecheck(Base):
    """C: a link that was safe when planned must be refused at apply time if a
    folder it passes through became a symlink in between."""

    def test_traversed_folder_turned_into_symlink_after_plan_refuses(self):
        ch = Chain(self.h)
        real = ch.real[CC]
        os.makedirs(os.path.join(real, "a"))
        write(os.path.join(real, "b", "x"), "inside\n")
        os.symlink("../b/x", os.path.join(real, "a", "f"))
        rows = [r for r in heal_active.plan(self.paths, (ch.acc, ch.org))
                if r["state"] == "heal"]
        self.assertEqual(len(rows), 2, rows)                   # safe at plan time
        # a session process now replaces b by a link to the old parent's shared
        shared = os.path.join(os.path.dirname(real), "shared")
        write(os.path.join(shared, "x"), "elsewhere\n")
        shutil.rmtree(os.path.join(real, "b"))
        os.symlink("../shared", os.path.join(real, "b"))
        before = shape(self.h.support)
        done, err, rb = heal_active.apply(self.paths, rows)
        self.assertEqual((done, rb), ([], []), err)
        self.assertTrue(err)
        self.assertEqual(shape(self.h.support), before)
        self.assertTrue(os.path.islink(ch.active[CC]))
        self.assertTrue(os.path.isdir(real) and not os.path.islink(real))
        self.assertFalse(os.path.exists(self.journal()))


class RollbackKeepsLinks(Base):
    """D: an allowed internal link keeps its text and its resolution through a
    rollback and through a recovery."""

    def setup_tree(self):
        ch = Chain(self.h)
        nms = {s: add_node_modules(ch.real[s]) for s in (CC, LAM)}
        rows = [r for r in heal_active.plan(self.paths, (ch.acc, ch.org))
                if r["state"] == "heal"]
        self.assertEqual(len(rows), 2, rows)
        return ch, nms, rows

    def link_ok(self, root, rel_from_root):
        link = os.path.join(root, rel_from_root, "outputs", "node_modules", ".bin",
                            "nanoid")
        self.assertTrue(os.path.islink(link), link)
        self.assertEqual(os.readlink(link), "../nanoid/bin/nanoid.js")
        self.assertEqual(read(link), NANOID_JS)
        self.assertTrue(os.path.realpath(link).startswith(
            os.path.realpath(root) + os.sep))

    def test_rollback_after_a_failed_second_store_keeps_the_links(self):
        ch, nms, rows = self.setup_tree()
        before = shape(self.h.support)
        real_swap = heal_active.rename_swap
        calls = []

        def swap(x, y):
            calls.append((x, y))
            if len(calls) == 2:
                raise OSError(28, "No space left on device")
            real_swap(x, y)
        with mock.patch.object(heal_active, "rename_swap", swap):
            done, err, rb = heal_active.apply(self.paths, rows)
        self.assertEqual((done, rb), ([], []))
        self.assertIn("No space", err)
        self.assertEqual(shape(self.h.support), before)
        for s in (CC, LAM):
            self.link_ok(ch.real[s], "local_9ab72ba5-0000")
            self.link_ok(ch.active[s], "local_9ab72ba5-0000")     # via the alias

    def test_recovery_after_a_failed_rollback_keeps_the_links(self):
        ch, nms, rows = self.setup_tree()
        real_swap = heal_active.rename_swap
        calls = []

        def swap(x, y):
            calls.append((x, y))
            if len(calls) >= 2:
                raise OSError(28, "No space left on device")
            real_swap(x, y)
        with mock.patch.object(heal_active, "rename_swap", swap):
            self.assertNotEqual(self.run_cli("--apply")[0], 0)
        self.assertTrue(os.path.exists(self.journal()))
        with open(self.journal()) as fh:
            self.assertEqual(json.load(fh)["status"], "failed")
        code, text = self.run_cli("--apply")
        self.assertEqual(code, 0, text)
        for s in (CC, LAM):
            self.assertFalse(os.path.islink(ch.active[s]))
            self.link_ok(ch.active[s], "local_9ab72ba5-0000")


if __name__ == "__main__":
    unittest.main()
