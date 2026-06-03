"""Unit tests for core.py. Run with: python test_core.py"""

import os
import json
import tempfile
import unittest

import core


class TimestampTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(core.format_timestamp(0), "00:00")
        self.assertEqual(core.format_timestamp(720_000), "12:00")   # the 12:00 example
        self.assertEqual(core.format_timestamp(65_500), "01:05")    # floors sub-second


class EventTests(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("match1.mp4")

    def test_new_project_defaults(self):
        self.assertEqual(self.p["teams"], core.DEFAULT_TEAMS)
        self.assertEqual(self.p["stats"], core.DEFAULT_STATS)
        self.assertEqual(self.p["events"], [])

    def test_add_event_records_derived_display(self):
        ev = core.add_event(self.p, "Team A", "shots", 720_000)
        self.assertEqual(ev["timestamp_display"], "12:00")
        self.assertEqual(ev["team"], "Team A")
        self.assertEqual(len(self.p["events"]), 1)

    def test_add_event_rejects_unknown(self):
        with self.assertRaises(ValueError):
            core.add_event(self.p, "Team Z", "shots", 0)
        with self.assertRaises(ValueError):
            core.add_event(self.p, "Team A", "nonsense", 0)

    def test_undo_pops_last(self):
        core.add_event(self.p, "Team A", "shots", 1000)
        core.add_event(self.p, "Team B", "shots", 2000)
        popped = core.undo(self.p)
        self.assertEqual(popped["team"], "Team B")
        self.assertEqual(len(self.p["events"]), 1)

    def test_undo_empty_is_safe(self):
        self.assertIsNone(core.undo(self.p))
        self.assertEqual(self.p["events"], [])

    def test_rename_teams_remaps_events(self):
        core.add_event(self.p, "Team A", "shots", 1000)
        core.add_event(self.p, "Team B", "shots", 2000)
        core.rename_teams(self.p, ["Arsenal", "Chelsea"])
        counts = core.derive_counts(self.p)
        self.assertEqual(counts["Arsenal"]["shots"], 1)
        self.assertEqual(counts["Chelsea"]["shots"], 1)
        self.assertNotIn("Team A", counts)

    def test_rename_rejects_count_change(self):
        with self.assertRaises(ValueError):
            core.rename_teams(self.p, ["Only One"])

    def test_add_custom_stat(self):
        core.add_stat(self.p, "corners")
        self.assertIn("corners", self.p["stats"])
        core.add_stat(self.p, "corners")  # idempotent
        self.assertEqual(self.p["stats"].count("corners"), 1)
        core.add_event(self.p, "Team A", "corners", 0)  # usable immediately


class DerivedTests(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("m.mp4")
        core.add_event(self.p, "Team A", "shots", 1000)
        core.add_event(self.p, "Team A", "shots", 2000)
        core.add_event(self.p, "Team A", "left_crosses", 3000)
        core.add_event(self.p, "Team B", "shots", 4000)

    def test_counts(self):
        counts = core.derive_counts(self.p)
        self.assertEqual(counts["Team A"]["shots"], 2)
        self.assertEqual(counts["Team A"]["left_crosses"], 1)
        self.assertEqual(counts["Team B"]["shots"], 1)
        self.assertEqual(counts["Team B"]["left_crosses"], 0)  # zero-filled

    def test_csv_shape(self):
        text = core.to_csv(self.p)
        rows = [r for r in text.splitlines() if r]
        self.assertEqual(rows[0], "team,shots,shots_on_target,left_crosses,right_crosses")
        self.assertEqual(rows[1], "Team A,2,0,1,0")
        self.assertEqual(rows[2], "Team B,1,0,0,0")

    def test_events_json(self):
        payload = json.loads(core.to_events_json(self.p))
        self.assertEqual(len(payload["events"]), 4)
        self.assertEqual(payload["video"], "m.mp4")


class PersistenceTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = core.new_project("game.mp4")
            core.add_stat(p, "corners")
            core.add_event(p, "Team A", "corners", 5000)
            p["position_ms"] = 5000
            core.save_project(p, d)
            loaded = core.load_project("game.mp4", d)
            self.assertEqual(loaded, p)

    def test_load_missing_returns_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            loaded = core.load_project("never_saved.mp4", d)
            self.assertEqual(loaded["events"], [])
            self.assertEqual(loaded["video"], "never_saved.mp4")


class PeriodClockTests(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("m.mp4")
        # first half kicks at video 3:00, whistle 50:00; second half kicks 65:00
        core.set_period_anchor(self.p, "first_half", "start", 180_000)
        core.set_period_anchor(self.p, "first_half", "end", 3_000_000)
        core.set_period_anchor(self.p, "second_half", "start", 3_900_000)

    def clk(self, video_ms):
        return core.match_clock(video_ms, self.p["periods"])

    def test_first_half_normal(self):
        # 3:00 + 12:00 = video 15:00 -> game 12:00
        self.assertEqual(self.clk(900_000)["display"], "12:00")

    def test_first_half_stoppage(self):
        # 3:00 + 46:00 = video 49:00 -> 45:00 +1:00
        c = self.clk(180_000 + 46 * 60_000)
        self.assertEqual(c["display"], "45:00 +1:00")
        self.assertEqual(c["phase"], "stoppage")

    def test_second_half_start_is_46(self):
        # second-half kick 65:00 + 1:00 = video 66:00 -> 46:00 (NOT 45:00 +1:00)
        c = self.clk(3_900_000 + 60_000)
        self.assertEqual(c["display"], "46:00")
        self.assertEqual(c["period_type"], "second_half")

    def test_half_time_gap_is_break(self):
        # between whistle 50:00 and kick 65:00
        c = self.clk(3_300_000)
        self.assertEqual(c["phase"], "break")
        self.assertEqual(c["display"], "HT")

    def test_pre_match(self):
        self.assertEqual(self.clk(60_000)["phase"], "pre")

    def test_extra_time(self):
        core.add_extra_time(self.p)
        core.set_period_anchor(self.p, "et_first", "start", 6_000_000)
        # et_first kick + 1:00 -> 91:00
        self.assertEqual(self.clk(6_060_000)["display"], "91:00")
        # past 15 min regulation -> 105:00 +X
        c = self.clk(6_000_000 + 16 * 60_000)
        self.assertEqual(c["display"], "105:00 +1:00")

    def test_no_anchors_is_unset(self):
        fresh = core.new_project("x.mp4")
        self.assertEqual(core.match_clock(0, fresh["periods"])["phase"], "unset")

    def test_validation_flags_overlap(self):
        core.set_period_anchor(self.p, "first_half", "end", 4_000_000)  # after 2H start
        self.assertTrue(core.validate_periods(self.p))


class ArchiveStatTests(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("m.mp4")
        core.add_event(self.p, "Team A", "left_crosses", 1000)

    def test_delete_hides_from_grid_but_keeps_events(self):
        core.delete_stat(self.p, "left_crosses")
        self.assertNotIn("left_crosses", self.p["stats"])
        self.assertIn("left_crosses", self.p["archived_stats"])
        self.assertEqual(len(self.p["events"]), 1)          # event preserved
        self.assertNotIn("left_crosses", core.to_csv(self.p))  # not a column

    def test_restore_brings_back_with_counts(self):
        core.delete_stat(self.p, "left_crosses")
        core.restore_stat(self.p, "left_crosses")
        self.assertIn("left_crosses", self.p["stats"])
        self.assertEqual(core.derive_counts(self.p)["Team A"]["left_crosses"], 1)

    def test_add_stat_unarchives(self):
        core.delete_stat(self.p, "left_crosses")
        core.add_stat(self.p, "left_crosses")
        self.assertIn("left_crosses", self.p["stats"])
        self.assertNotIn("left_crosses", self.p["archived_stats"])


class BackfillTests(unittest.TestCase):
    def test_old_kickoff_file_migrates_to_periods(self):
        with tempfile.TemporaryDirectory() as d:
            # a pre-periods saved file with a single kickoff_ms
            old = {"video": "old.mp4", "teams": ["Team A", "Team B"],
                   "stats": ["shots"], "archived_stats": [], "events": [],
                   "position_ms": 0, "kickoff_ms": 180_000}
            with open(os.path.join(d, "old.mp4.json"), "w") as fh:
                json.dump(old, fh)
            loaded = core.load_project("old.mp4", d)
            self.assertNotIn("kickoff_ms", loaded)
            fh_period = next(p for p in loaded["periods"] if p["type"] == "first_half")
            self.assertEqual(fh_period["video_start_ms"], 180_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
