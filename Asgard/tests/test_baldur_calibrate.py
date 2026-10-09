"""Baldur calibration (spec build step 8): real hours in, the best setting that runs low out.

The named rules: only a setting that estimates low on average may win; a dial the trial days
can't tell apart stays where it is; nothing changes until you accept; accepting needs
calibrate.MIN_DAYS days; settings and the active calibration never disagree.
"""
import contextlib
import datetime as dt
import io
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT, ROOT / "apps" / "baldur", ROOT / "tests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from asgard import muninn  # noqa: E402
from baldur import calibrate, cli, store  # noqa: E402
from baldur import settings as config  # noqa: E402
from test_baldur import MuninnCase  # noqa: E402

LATER = dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 9, 30)


def at(day, hh, mm=0):
    """A local time in September 2026."""
    return dt.datetime(2026, 9, day, hh, mm).astimezone()


class CalibrationCase(MuninnCase):
    """Days with two commits 100 minutes apart (10:00 and 11:40), no calendar.

    With a gap of 105 m or more they are one session of 100 m plus the lead-in; below that, two
    sessions of max(lead-in, 15 m). A real 1h45m a day is then fitted exactly by lead-in 15 m.
    """

    def setUp(self):
        super().setUp()
        self.settings = config.update({"project_keys": ["PROJ"], "history_days": 3650})

    def days(self, n, real, first=1):
        for d in range(first, first + n):
            self.commit(at(d, 10), "PROJ-1")
            self.commit(at(d, 11, 40), "PROJ-1")
            if real is not None:
                calibrate.note(self.con, dt.date(2026, 9, d), real, today=TODAY)

    def fit(self):
        return calibrate.fit(self.con, self.settings, now=LATER)


class FitTests(CalibrationCase):
    def test_the_best_setting_that_runs_low_wins(self):
        self.days(10, 105)
        result = self.fit()
        self.assertEqual(result.current.dials, calibrate.Dials(120, 30, 0.5))
        self.assertAlmostEqual(result.current.bias, 15)            # 130 m rounds to 2h00m: 15 m high a day
        self.assertFalse(result.current.runs_low)
        self.assertEqual(result.best.dials, calibrate.Dials(120, 15, 0.5))
        self.assertEqual((result.best.mae, result.best.bias), (0, 0))
        self.assertEqual(result.tried, 9 * 5 * 6)
        self.assertTrue(result.enough and result.changes)

    def test_only_a_setting_that_estimates_low_on_average_may_win(self):
        """Named rule: lead-in 30 m misses by +2 m a day, lead-in 15 m by -13 m; the low one wins."""
        self.days(10, 118)
        result = self.fit()
        self.assertAlmostEqual(result.current.mae, 2)
        self.assertGreater(result.current.bias, 0)
        self.assertEqual(result.best.dials.lead_in_minutes, 15)
        self.assertLessEqual(result.best.bias, 0)
        self.assertAlmostEqual(result.best.mae, 13)
        for day, minutes in result.best.estimates.items():
            self.assertLessEqual(minutes, result.actual[day] + 15, day)

    def test_no_setting_runs_low_means_nothing_to_accept(self):
        self.days(10, 10)                               # every setting gives at least 15 m
        result = self.fit()
        self.assertIsNone(result.best)
        with self.assertRaisesRegex(calibrate.CalibrationError, "estimates low on average"):
            calibrate.accept(self.con, self.settings, result)

    def test_a_dial_the_days_cant_tell_apart_stays(self):
        """No day has calendar data, so the meeting weight can't be measured and isn't moved."""
        self.settings = config.update({"ambient_weight": 0.7})
        self.days(10, 105)
        result = self.fit()
        self.assertEqual(result.unmeasured, ["ambient_weight"])
        self.assertEqual(result.best.dials.ambient_weight, 0.7)
        self.assertEqual(result.tried, 9 * 5 * 6, "0.7 is on the grid already")

    def test_ties_keep_your_gap_then_go_down(self):
        """Every gap from 105 m up gives the same days; 120 m is yours, so it stays."""
        self.days(10, 105)
        self.assertEqual(self.fit().best.dials.idle_gap_minutes, 120)
        self.settings = config.update({"idle_gap_minutes": 100})        # off the grid: tried as well
        result = self.fit()
        self.assertEqual(result.best.dials.idle_gap_minutes, 100)
        self.assertEqual(result.tried, 10 * 5 * 6)

    def test_days_without_commits_are_left_out(self):
        self.days(10, 105)
        calibrate.note(self.con, dt.date(2026, 9, 20), 300, today=TODAY)   # a day of design talk
        result = self.fit()
        self.assertEqual(result.left_out, [dt.date(2026, 9, 20)])
        self.assertEqual(len(result.actual), 10)
        self.assertEqual(result.best.bias, 0, "the invisible day doesn't make a high setting look low")

    def test_ticket_figures_are_compared_too(self):
        self.days(10, 105)
        calibrate.note(self.con, dt.date(2026, 9, 1), 90, "proj-1", today=TODAY)
        result = self.fit()
        self.assertAlmostEqual(result.best.ticket_mae, 15)
        self.assertAlmostEqual(result.current.ticket_mae, 30)

    def test_nothing_noted_or_nothing_collected(self):
        with self.assertRaisesRegex(calibrate.CalibrationError, "No real hours noted"):
            self.fit()
        calibrate.note(self.con, dt.date(2026, 9, 2), 60, today=TODAY)
        with self.assertRaisesRegex(calibrate.CalibrationError, "no commits of yours"):
            self.fit()


class AcceptTests(CalibrationCase):
    def test_accepting_writes_settings_and_one_active_calibration(self):
        self.days(10, 105)
        cid = calibrate.accept(self.con, self.settings, self.fit())
        self.assertEqual(config.load().values["lead_in_minutes"], 15)
        row = self.con.execute("SELECT * FROM calibration_runs WHERE id = ?", (cid,)).fetchone()
        self.assertEqual((row["gap_minutes"], row["lead_in_minutes"], row["ambient_weight"], row["days_used"],
                          row["mean_abs_error_min"], row["bias_min"], row["is_active"]), (120, 15, 0.5, 10, 0, 0, 1))
        self.assertEqual(self.events("calibration.accepted")[0]["to"], "gap 120m, lead-in 15m, weight 0.5")
        # Estimates made with these settings say which calibration they came from.
        self.settings = config.load()
        store.run(self.con, self.settings, dt.date(2026, 9, 1), dt.date(2026, 9, 1), now=LATER)
        self.assertEqual(self.con.execute("SELECT calibration_id FROM estimate_runs").fetchone()[0], cid)
        # Changing a dial by hand: later runs no longer claim the calibration.
        self.settings = config.update({"idle_gap_minutes": 90})
        self.assertIsNone(calibrate.active(self.con, self.settings))
        # A second accept replaces the first; only one is active.
        self.settings = config.update({"idle_gap_minutes": 120, "lead_in_minutes": 45})
        second = calibrate.accept(self.con, self.settings, self.fit())
        active = [r[0] for r in self.con.execute("SELECT id FROM calibration_runs WHERE is_active = 1")]
        self.assertEqual(active, [second])
        self.assertEqual(self.events("calibration.accepted")[-1]["replaced"], cid)
        self.assertTrue(muninn.integrity.check(self.con).ok)

    def test_accepting_needs_enough_days(self):
        self.days(calibrate.MIN_DAYS - 1, 105)
        result = self.fit()
        self.assertFalse(result.enough)
        with self.assertRaisesRegex(calibrate.CalibrationError, f"at least {calibrate.MIN_DAYS} days"):
            calibrate.accept(self.con, self.settings, result)
        self.assertEqual(config.load().values["lead_in_minutes"], 30)

    def test_nothing_to_accept_when_you_already_fit_best(self):
        self.settings = config.update({"lead_in_minutes": 15})
        self.days(10, 105)
        result = self.fit()
        self.assertFalse(result.changes)
        with self.assertRaisesRegex(calibrate.CalibrationError, "already fit best"):
            calibrate.accept(self.con, self.settings, result)

    def test_a_refused_write_puts_the_settings_file_back(self):
        self.days(10, 105)
        before = config.settings_path().read_bytes()
        with mock.patch.object(muninn, "emit", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                calibrate.accept(self.con, self.settings, self.fit())
        self.assertEqual(config.settings_path().read_bytes(), before)
        self.assertEqual(self.con.execute("SELECT count(*) FROM calibration_runs").fetchone()[0], 0)


class NoteTests(CalibrationCase):
    def test_noting_changes_a_figure_rather_than_adding_one(self):
        self.assertEqual(calibrate.note(self.con, dt.date(2026, 9, 1), 300, today=TODAY), "added")
        self.assertEqual(calibrate.note(self.con, dt.date(2026, 9, 1), 330, text="fixed", today=TODAY), "changed")
        self.assertEqual(calibrate.actuals(self.con), {dt.date(2026, 9, 1): {None: 330}})
        self.assertTrue(calibrate.forget(self.con, dt.date(2026, 9, 1)))
        self.assertFalse(calibrate.forget(self.con, dt.date(2026, 9, 1)))

    def test_what_isnt_a_real_figure_is_refused(self):
        for args, message in (((dt.date(2026, 10, 1), 60), "hasn't happened"),
                              ((dt.date(2026, 9, 1), 1441), "0 to 1440"),
                              ((dt.date(2026, 9, 1), 60, "not-a-key"), "isn't a Jira key")):
            with self.assertRaisesRegex(calibrate.CalibrationError, message):
                calibrate.note(self.con, *args, today=TODAY)
        calibrate.note(self.con, dt.date(2026, 9, 1), 60, today=TODAY)
        calibrate.note(self.con, dt.date(2026, 9, 1), 45, "proj-7", today=TODAY)
        with self.assertRaisesRegex(calibrate.CalibrationError, "more than the day's total"):
            calibrate.note(self.con, dt.date(2026, 9, 1), 30, "PROJ-8", today=TODAY)
        self.assertEqual(calibrate.actuals(self.con)[dt.date(2026, 9, 1)], {None: 60, "PROJ-7": 45})


class CalibrationCliTests(CalibrationCase):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_note_list_calibrate_and_accept(self):
        self.days(10, None)
        for d in range(1, 11):
            code, out, err = self.cli("actual", f"2026-09-{d:02d}", "1h45m")
            self.assertEqual(code, 0, err)
        self.assertIn("Enough days to calibrate", out)
        code, out, _ = self.cli("actuals", "--from", "2026-09-01", "--to", "2026-09-10")
        self.assertIn("Tue 2026-09-01       1h45m     2h00m        +15m", out)
        code, out, err = self.cli("calibrate")
        self.assertEqual(code, 0, err)
        self.assertIn("Best that runs low", out)
        self.assertIn("Accept it with: cli.py calibrate --accept", out)
        self.assertIn("can't tell settings apart for: ambient weight", out)
        code, out, err = self.cli("calibrate", "--accept")
        self.assertEqual(code, 0, err)
        self.assertIn("Accepted calibration", out)
        self.assertEqual(config.load().values["lead_in_minutes"], 15)

    def test_messages_not_tracebacks(self):
        code, _, err = self.cli("calibrate")
        self.assertEqual(code, 1)
        self.assertIn("No real hours noted", err)
        code, _, err = self.cli("actual", "2026-09-01")
        self.assertIn("Give the real time too", err)
        code, _, err = self.cli("actual", "2999-01-01", "1h")
        self.assertIn("hasn't happened", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# The independent review of 2026-10-09 (docs/review-2026-10-09.md)
# --------------------------------------------------------------------------

class ReviewFindingsTests(CalibrationCase):
    def test_r5_a_day_every_setting_estimates_alike_isnt_counted(self):
        """Ten days at 1h58m real, where lead-in 15m (yours) runs low and 30m runs HIGH; an eleventh day's lone
        commit at 00:05 rounds to 0 under every setting. Counting it made lead-in 30m pass as "runs low"."""
        self.settings = config.update({"lead_in_minutes": 15})
        self.days(10, 118)
        self.commit(dt.datetime(2026, 9, 20, 0, 5).astimezone(), "PROJ-1")
        calibrate.note(self.con, dt.date(2026, 9, 20), 360, today=TODAY)
        result = self.fit()
        self.assertEqual(result.flat, [dt.date(2026, 9, 20)])
        self.assertNotIn(dt.date(2026, 9, 20), result.actual)
        self.assertEqual(result.best.dials.lead_in_minutes, 15)
        self.assertFalse(any(result.best.estimates[d] > result.actual[d] for d in result.days))
        with self.assertRaisesRegex(calibrate.CalibrationError, "already fit best"):
            calibrate.accept(self.con, self.settings, result)

    def test_r12_baldur_json_is_written_while_muninn_is_locked(self):
        self.days(10, 105)
        seen = []
        real = config.update

        def spy(changes, path=None):
            seen.append(self.con.in_transaction)
            return real(changes, path)

        with mock.patch.object(config, "update", spy):
            calibrate.accept(self.con, self.settings, self.fit())
        self.assertEqual(seen, [True], "the file is written inside the transaction that records it")

    def test_r13_a_settings_folder_that_cant_be_written_is_a_message(self):
        self.days(10, 105)
        before = config.settings_path().read_bytes()
        with mock.patch.object(config, "update", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaisesRegex(calibrate.CalibrationError, "Couldn't write baldur.json .*nothing was changed"):
                calibrate.accept(self.con, self.settings, self.fit())
        self.assertEqual(config.settings_path().read_bytes(), before)
        self.assertEqual(self.con.execute("SELECT count(*) FROM calibration_runs").fetchone()[0], 0)

    def test_r16_real_hours_are_noted_only_for_your_projects(self):
        for key in ("OPS-7", "SHA-256", "UTF-8"):
            with self.subTest(key), self.assertRaisesRegex(calibrate.CalibrationError, "isn't in your projects"):
                calibrate.note(self.con, dt.date(2026, 9, 1), 60, key, today=TODAY, projects=["PROJ"])
        calibrate.note(self.con, dt.date(2026, 9, 1), 60, "proj-7", today=TODAY, projects=["PROJ"])
        self.assertEqual(calibrate.actuals(self.con)[dt.date(2026, 9, 1)], {"PROJ-7": 60})
        # A key noted before the fix can still be forgotten.
        calibrate.note(self.con, dt.date(2026, 9, 2), 30, "OPS-7", today=TODAY)
        self.assertTrue(calibrate.forget(self.con, dt.date(2026, 9, 2), "ops-7"))
        self.assertNotIn(dt.date(2026, 9, 2), calibrate.actuals(self.con))

