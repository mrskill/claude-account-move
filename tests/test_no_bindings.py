"""Checks that the distribution carries no personal or machine-specific text.

The scanning mechanism is tested on invented words only. The real list of
private names is NOT part of this repository: keep it outside and apply it with
a separate script (see CONTRIBUTING notes in the README). Setting the variable
CLAUDE_ACCOUNT_MOVE_DENYLIST to a file of `re:<pattern>` or `cs:<pattern>` lines
makes the last test apply that list to this tree as well.
"""
import os
import re
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", ".selftest", "__pycache__"}
RUSSIAN_README = "README.ru.md"

# Generic, non-private classes of text that must never ship.
HOME_PATH = "/" + "Us" + "ers" + "/"
CYRILLIC = re.compile("[" + chr(0x400) + "-" + chr(0x4ff) + "]")
TYPOGRAPHY = re.compile("[" + chr(0x451) + chr(0x2014) + "]")


def package_files(root):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for f in fns:
            if f != ".DS_Store":
                yield os.path.join(dp, f)


def find_hits(root, patterns, skip_names=()):
    """[(relative path, pattern source)] for every file matching a pattern."""
    hits = []
    for path in package_files(root):
        rel = os.path.relpath(path, root)
        if rel in skip_names:
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for pat in patterns:
            if pat.search(text):
                hits.append((rel, pat.pattern))
    return hits


class Mechanism(unittest.TestCase):
    """The scanner itself, proven on invented words."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_finds_an_invented_word_case_insensitively(self):
        self.put("a/b.txt", "contains ZorBlax inside")
        self.put("c.txt", "clean")
        hits = find_hits(self.root, [re.compile("zorblax", re.I)])
        self.assertEqual(hits, [(os.path.join("a", "b.txt"), "zorblax")])

    def test_misses_a_word_split_by_concatenation_unless_fragment_listed(self):
        self.put("t.py", 'names = ["quill" + "moth"]')
        self.assertEqual(find_hits(self.root, [re.compile("quillmoth")]), [])
        self.assertEqual(len(find_hits(self.root, [re.compile('quill"')])), 1)

    def test_skips_vcs_and_cache_directories(self):
        self.put(".git/x", "zorblax")
        self.put("__pycache__/y", "zorblax")
        self.assertEqual(find_hits(self.root, [re.compile("zorblax")]), [])

    def test_generic_classes_are_detected(self):
        self.put("p.txt", "see %shome/x" % HOME_PATH)
        self.put("c.txt", chr(0x41f) + chr(0x440))
        self.put("t.txt", "a" + chr(0x2014) + "b")
        self.assertEqual(len(find_hits(self.root, [re.compile(re.escape(HOME_PATH))])), 1)
        self.assertEqual(len(find_hits(self.root, [CYRILLIC])), 1)
        self.assertEqual(len(find_hits(self.root, [TYPOGRAPHY])), 1)

    def test_russian_readme_is_the_only_cyrillic_exemption(self):
        self.put(RUSSIAN_README, chr(0x41f))
        self.put("other.md", chr(0x41f))
        hits = find_hits(self.root, [CYRILLIC], skip_names=(RUSSIAN_README,))
        self.assertEqual([h[0] for h in hits], ["other.md"])


class ThisDistribution(unittest.TestCase):
    def test_no_home_path_no_typography_marks(self):
        pats = [re.compile(re.escape(HOME_PATH)), TYPOGRAPHY]
        self.assertEqual(find_hits(ROOT, pats), [])

    def test_no_cyrillic_outside_the_russian_readme(self):
        self.assertEqual(find_hits(ROOT, [CYRILLIC], skip_names=(RUSSIAN_README,)), [])

    def test_external_denylist_when_provided(self):
        path = os.environ.get("CLAUDE_ACCOUNT_MOVE_DENYLIST")
        if not path:
            self.skipTest("no external denylist given")
        pats = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                kind, _, body = line.rstrip("\n").partition(":")
                if kind == "re":
                    pats.append(re.compile(body, re.I))
                elif kind == "cs":
                    pats.append(re.compile(body))
        self.assertEqual(find_hits(ROOT, pats), [])


if __name__ == "__main__":
    unittest.main()
