"""Oracle tests for CAM-RESUME-LANDED (rulings R16-R18).

`auto` resumed a move that had landed 41 hours earlier, so the new move skipped
prepare and stamp. A move whose newest failed `auto` report is older than
RESUME_MAX_AGE_H (6 hours, by file mtime) is landed: find_unfinished_move gives
None. A younger report still resumes. The resume line names the source account,
the account the app has now and the age in hours. Fake HOME only.
"""
import json
import os
import re
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from test_auto import Base  # noqa: E402  (only Base: its fixtures, no test cases)
from test_cli import A, B, build_source  # noqa: E402

from claude_account_move import auto_move  # noqa: E402
from claude_account_move import ru_text as ru  # noqa: E402
from claude_account_move.common import Paths, read_identity  # noqa: E402

ENV_LIMIT = "CLAUDE_ACCOUNT_MOVE_RESUME_MAX_AGE_H"
HOUR = 3600.0


class ResumeBase(Base):
    def setUp(self):
        Base.setUp(self)
        os.environ.pop(ENV_LIMIT, None)
        self.addCleanup(os.environ.pop, ENV_LIMIT, None)

    def stopped_move(self):
        """A move left unfinished by a first `auto` run: the app is in the new
        account B, the source was A, the newest report is a failed auto report.
        Returns the move dir."""
        build_source(self.h)
        code, out, _, _ = self.run_auto("--wait-login", "0.1", settle=0.05,
                                        wait=self.user_logs_in("cli_lags"))
        self.assertEqual(code, 6, out)
        moves = os.path.join(self.h.state, "moves")
        names = [n for n in os.listdir(moves) if not n.endswith(".partial")]
        self.assertEqual(len(names), 1, names)
        mdir = os.path.join(moves, names[0])
        self.assertTrue(os.path.isfile(os.path.join(mdir, "report-auto.json")))
        return mdir

    def age(self, mdir, hours):
        """Make the newest failed auto report `hours` old by file mtime; any
        finish report stays older than it, as in the live case."""
        t = time.time() - hours * HOUR
        auto = os.path.join(mdir, "report-auto.json")
        os.utime(auto, (t, t))
        fin = os.path.join(mdir, "report-finish.json")
        if os.path.isfile(fin):
            os.utime(fin, (t - 60, t - 60))

    def found(self):
        paths = Paths(self.h.home, self.h.state)
        return auto_move.find_unfinished_move(paths, read_identity(paths)["app"]["value"])


class AgedReport(ResumeBase):
    def test_r16_aged_report_is_landed_find_returns_none(self):
        mdir = self.stopped_move()
        self.age(mdir, 41)
        self.assertIsNone(self.found(), "a 41 hour old failed auto report resumes")

    def test_r16_just_under_the_limit_still_resumes(self):
        mdir = self.stopped_move()
        self.age(mdir, 5)
        found = self.found()
        self.assertTrue(found and found[0] == "resume", repr(found))
        self.assertEqual(found[3], A)

    def test_r16_just_over_the_limit_is_landed(self):
        mdir = self.stopped_move()
        self.age(mdir, 7)
        self.assertIsNone(self.found(), "a 7 hour old report resumes (limit is 6)")

    def test_r16_env_limit_overrides_default(self):
        mdir = self.stopped_move()
        self.age(mdir, 2)
        os.environ[ENV_LIMIT] = "1"
        self.assertIsNone(self.found(), "limit 1 h ignored for a 2 hour old report")
        os.environ[ENV_LIMIT] = "3"
        found = self.found()
        self.assertTrue(found and found[0] == "resume", repr(found))

    def test_r16_aged_report_without_finish_report_is_landed(self):
        mdir = self.stopped_move()
        fin = os.path.join(mdir, "report-finish.json")
        if os.path.isfile(fin):
            os.unlink(fin)
        self.age(mdir, 41)
        self.assertIsNone(self.found())

    def test_r16_auto_starts_a_new_move_after_an_aged_report(self):
        mdir = self.stopped_move()
        self.age(mdir, 41)
        old = os.path.basename(mdir)
        code, out, _, m = self.run_auto(wait=self.user_logs_in())
        self.assertGreaterEqual(m["cmd_prepare"].call_count, 1,
                                "auto resumed the landed move, prepare was skipped")
        self.assertIn(old, out, "the skipped landed move is not named")

    def test_r18_class_guard_every_resume_path_is_age_gated(self):
        """Every condition for resume is true (source differs from the account
        the app has now, snapshot ready, failed auto report). Only the age of
        the report varies: the answer must flip from resume to None."""
        mdir = self.stopped_move()
        results = {}
        for hours in (0, 1, 5, 7, 41, 24 * 30):
            self.age(mdir, hours)
            found = self.found()
            results[hours] = found[0] if found else None
        self.assertEqual(results, {0: "resume", 1: "resume", 5: "resume",
                                   7: None, 41: None, 24 * 30: None})


class ResumeMessage(ResumeBase):
    def test_r17_young_report_resumes_and_message_names_accounts_and_age(self):
        mdir = self.stopped_move()
        self.age(mdir, 2)
        code, out, _, m = self.run_auto(wait=self.user_logs_in())
        self.assertEqual(m["cmd_prepare"].call_count, 0, "a young report must resume")
        lines = [ln for ln in out.splitlines() if mdir in ln]
        self.assertTrue(lines, "no resume line names the move dir: " + out)
        line = lines[0]
        self.assertIn(A, line, "the source account is missing: " + line)
        self.assertIn(B, line, "the account the app has now is missing: " + line)
        self.assertRegex(line, r"\b2\b", "the age in hours is missing: " + line)


class EnvFallback(ResumeBase):
    def test_r19_non_finite_env_values_fall_back_to_six_hours(self):
        mdir = self.stopped_move()
        self.age(mdir, 41)
        for bad in ("inf", "Infinity", "1e309", "nan", "-inf", "0", "-3", "abc"):
            with self.subTest(env=bad):
                os.environ[ENV_LIMIT] = bad
                self.assertIsNone(self.found(), "env %r is taken as a limit" % bad)

    def test_r19_fallback_still_resumes_a_young_report(self):
        mdir = self.stopped_move()
        self.age(mdir, 2)
        for bad in ("inf", "Infinity", "1e309", "nan"):
            with self.subTest(env=bad):
                os.environ[ENV_LIMIT] = bad
                found = self.found()
                self.assertTrue(found and found[0] == "resume", repr(found))


class ResumeAgeWithoutAutoReport(ResumeBase):
    def no_auto_report(self):
        mdir = self.stopped_move()
        os.unlink(os.path.join(mdir, "report-auto.json"))
        fin = os.path.join(mdir, "report-finish.json")
        if os.path.isfile(fin):
            with open(fin) as fh:
                self.assertNotEqual(json.load(fh).get("exit_code"), 0)
        found = self.found()
        self.assertTrue(found and found[0] == "resume", repr(found))
        return mdir

    def resume_line(self, out, mdir):
        lines = [ln for ln in out.splitlines() if mdir in ln]
        self.assertTrue(lines, "no resume line names the move dir: " + out)
        return lines[0]

    def test_r20_age_comes_from_before_json_mtime(self):
        mdir = self.no_auto_report()
        t = time.time() - 2 * HOUR
        os.utime(os.path.join(mdir, "before.json"), (t, t))
        code, out, _, m = self.run_auto(wait=self.user_logs_in())
        self.assertEqual(m["cmd_prepare"].call_count, 0)
        line = self.resume_line(out, mdir)
        self.assertRegex(line, r"\b2\b", "age from before.json is missing: " + line)
        self.assertNotRegex(line, r"\b0 \u0447", "age printed as 0 h: " + line)

    def test_r20_unreadable_before_json_age_is_unknown_never_zero(self):
        mdir = self.no_auto_report()
        real = os.path.getmtime

        def getmtime(path):
            if str(path).endswith("before.json"):
                raise OSError("no mtime")
            return real(path)
        with mock.patch("os.path.getmtime", getmtime):
            code, out, _, m = self.run_auto(wait=self.user_logs_in())
        self.assertEqual(m["cmd_prepare"].call_count, 0)
        line = self.resume_line(out, mdir)
        self.assertIn(u"\u0432\u043e\u0437\u0440\u0430\u0441\u0442 \u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u0435\u043d", line)
        self.assertNotRegex(line, r"\b0 \u0447")


class LiveIncidentLayout(ResumeBase):
    """08.11 move seen on 10.11: finish exit 0, a newer failed auto report, the
    app on the move's target (the new account B), the source was A."""

    def incident(self, hours):
        mdir = self.stopped_move()
        fin = os.path.join(mdir, "report-finish.json")
        with open(fin, "w") as fh:
            json.dump({"exit_code": 0, "verdict": "ok"}, fh)
        self.age(mdir, hours)
        return mdir

    @staticmethod
    def resume_prefix():
        return " ".join(ru.RESUME_MOVE.split(" ")[:3])

    def test_r21_aged_incident_runs_prepare_and_prints_no_resume_line(self):
        mdir = self.incident(41)
        self.assertIsNone(self.found())
        code, out, _, m = self.run_auto(wait=self.user_logs_in())
        self.assertGreaterEqual(m["cmd_prepare"].call_count, 1, out)
        self.assertNotIn(self.resume_prefix(), out)

    def test_r21_young_incident_still_resumes(self):
        mdir = self.incident(2)
        code, out, _, m = self.run_auto(wait=self.user_logs_in())
        self.assertEqual(m["cmd_prepare"].call_count, 0, out)
        self.assertIn(self.resume_prefix(), out)


if __name__ == "__main__":
    unittest.main()
