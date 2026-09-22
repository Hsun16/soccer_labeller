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
        self.assertEqual(rows[0], "team,shots,shots_on_target,left_crosses,right_crosses,possession_seconds,possession_percent")
        self.assertEqual(rows[1], "Team A,2,0,1,0,0,0.0")
        self.assertEqual(rows[2], "Team B,1,0,0,0,0,0.0")

    def test_events_json(self):
        payload = json.loads(core.to_events_json(self.p))
        self.assertEqual(len(payload["events"]), 4)
        self.assertEqual(payload["video"], "m.mp4")


class ChartHelperTests(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("m.mp4")
        core.set_period_anchor(self.p, "first_half", "start", 0)
        core.add_stat(self.p, "goal")

    def test_reorder_stats(self):
        order = list(reversed(self.p["stats"]))
        core.reorder_stats(self.p, order)
        self.assertEqual(self.p["stats"], order)
        with self.assertRaises(ValueError):
            core.reorder_stats(self.p, self.p["stats"][:-1])   # not a permutation

    def test_combined_stats_filters_unknown_and_blank(self):
        core.set_combined_stats(self.p, [
            {"name": "attack", "parts": ["shots", "nope"]},
            {"name": "", "parts": ["shots"]}])
        self.assertEqual(self.p["combined_stats"], [{"name": "attack", "parts": ["shots"]}])

    def test_goal_markers_timeline(self):
        core.add_event(self.p, "Team A", "goal", 300_000)     # 5:00
        core.add_event(self.p, "Team B", "goal", 1_500_000)   # 25:00
        s = core.timeline_series(self.p, ["shots"], 10, goal_stat="goal")
        self.assertEqual([g["label"] for g in s["goals"]], ["0\u201310", "20\u201330"])
        self.assertEqual([g["team"] for g in s["goals"]], [0, 1])
        self.assertIn("20\u201330", s["labels"])              # range extended to fit the goal

    def test_goal_markers_possession_half(self):
        core.add_event(self.p, "Team A", "goal", 600_000)
        core.set_possession(self.p, 0, 0); core.set_possession(self.p, 300_000, 1)
        self.p["position_ms"] = 700_000
        s = core.possession_by_window(self.p, "half", goal_stat="goal")
        self.assertEqual(s["goals"][0]["label"], "First half")

    def test_two_goals_same_bin_get_distinct_fracs(self):
        core.add_event(self.p, "Team A", "goal", 26 * 60_000)   # 26'
        core.add_event(self.p, "Team B", "goal", 28 * 60_000)   # 28'
        s = core.timeline_series(self.p, ["shots"], 10, goal_stat="goal")
        bin_goals = [g for g in s["goals"] if g["label"] == "20\u201330"]
        self.assertEqual(len(bin_goals), 2)                     # both present
        self.assertAlmostEqual(bin_goals[0]["frac"], 0.6)       # 26 -> 0.6 within 20-30
        self.assertAlmostEqual(bin_goals[1]["frac"], 0.8)       # 28 -> 0.8
        self.assertNotEqual(bin_goals[0]["frac"], bin_goals[1]["frac"])

    def test_apply_template_swaps_stats_and_archives(self):
        core.add_event(self.p, "Team A", "shots", 1000)         # event on a stat we'll drop
        core.apply_template(self.p, ["tackle", "goal"], [{"name": "x", "parts": ["tackle"]}])
        self.assertEqual(self.p["stats"], ["tackle", "goal"])
        self.assertIn("shots", self.p["archived_stats"])        # dropped, but kept
        self.assertEqual(len(self.p["events"]), 1)              # event survives
        self.assertEqual(self.p["combined_stats"], [{"name": "x", "parts": ["tackle"]}])

    def test_template_storage_roundtrip(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            core.upsert_template(d, "RKC set", ["shots", "goal"], [{"name": "a", "parts": ["shots"]}])
            core.upsert_template(d, "RKC set", ["shots"], [])    # overwrite same name
            ts = core.load_templates(d)
            self.assertEqual(len(ts), 1)
            self.assertEqual(ts[0]["stats"], ["shots"])
            core.delete_template(d, "RKC set")
            self.assertEqual(core.load_templates(d), [])

    def _proj(self, video, teams, events, label="", possession=None, position=0):
        p = core.new_project(video)
        p["teams"] = list(teams); p["match_label"] = label
        core.set_period_anchor(p, "first_half", "start", 0)
        for stat in {e[1] for e in events}:
            if stat not in p["stats"]:
                core.add_stat(p, stat)
        for team, stat, ms in events:
            core.add_event(p, team, stat, ms)
        for ms, h in (possession or []):
            core.set_possession(p, ms, h)
        p["position_ms"] = position
        return p

    def test_cross_match_count_combines_stats_and_resolves_team(self):
        # ECFC is team A in one match, team B in another; count = al + ar (combined)
        p1 = self._proj("m1.mp4", ["ECFC", "RKC"],
                         [("ECFC", "al", 1000), ("ECFC", "ar", 2000), ("RKC", "al", 3000)])
        p2 = self._proj("m2.mp4", ["RKC", "ECFC"],
                         [("ECFC", "al", 1000)])
        spec = {"kind": "count", "stats": ["al", "ar"]}
        out = core.cross_match_series([p1, p2], "ecfc", spec)   # case-insensitive
        self.assertEqual([(s["label"], s["value"]) for s in out["series"]],
                         [("m1", 2), ("m2", 1)])
        self.assertEqual(out["missing_team"], [])

    def test_cross_match_groups_split_match(self):
        # one match suspended -> two videos, same label "Cup tie"
        p1 = self._proj("d1.mp4", ["ECFC", "RKC"], [("ECFC", "shots", 1000)], label="Cup tie")
        p2 = self._proj("d2.mp4", ["ECFC", "RKC"], [("ECFC", "shots", 1000), ("ECFC", "shots", 2000)], label="Cup tie")
        out = core.cross_match_series([p1, p2], "ECFC", {"kind": "count", "stats": ["shots"]})
        self.assertEqual(len(out["series"]), 1)                 # merged into one match
        self.assertEqual(out["series"][0]["value"], 3)          # 1 + 2
        self.assertEqual(out["series"][0]["videos"], ["d1.mp4", "d2.mp4"])

    def test_cross_match_possession_ratio_aggregates_seconds(self):
        p = self._proj("m.mp4", ["ECFC", "RKC"], [],
                       possession=[(0, 0), (300_000, 1)], position=400_000)
        out = core.cross_match_series([p], "ECFC", {"kind": "possession"})
        self.assertTrue(out["ratio"])
        self.assertAlmostEqual(out["series"][0]["value"], 75.0)  # 300s of 400s

    def test_cross_match_flags_unresolved_team(self):
        p = self._proj("m.mp4", ["Foo", "Bar"], [("Foo", "shots", 1000)])
        out = core.cross_match_series([p], "ECFC", {"kind": "count", "stats": ["shots"]})
        self.assertEqual(out["series"], [])
        self.assertEqual(out["missing_team"], ["m.mp4"])


class ShotMapTests(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("m.mp4")
        self.p["teams"] = ["ECFC", "RKC"]
        core.set_period_anchor(self.p, "first_half", "start", 0)
        core.add_event(self.p, "ECFC", "shots", 300_000)   # 5'
        core.add_event(self.p, "RKC", "shots", 600_000)    # 10'
        core.add_event(self.p, "ECFC", "shots", 120_000)   # 2'
        self.ids = [e["id"] for e in self.p["events"]]

    def test_queue_is_chronological_and_unmarked(self):
        q = core.shot_queue(self.p)
        self.assertEqual([s["minute"] for s in q], [2.0, 5.0, 10.0])
        self.assertTrue(all(s["mark"] is None for s in q))

    def test_set_mark_validates_and_stores(self):
        eid = self.ids[0]
        core.set_shot_mark(self.p, eid, {"x0": 0.5, "y0": 0.1, "x1": 0.5, "y1": 0.02, "result": "goal"})
        self.assertEqual(self.p["shot_marks"][eid]["result"], "goal")
        with self.assertRaises(ValueError):
            core.set_shot_mark(self.p, eid, {"x0": 0.5, "y0": 0.1, "result": "rocket"})
        with self.assertRaises(ValueError):
            core.set_shot_mark(self.p, eid, {"x0": 1.4, "y0": 0.1, "result": "miss"})

    def test_mark_allows_missing_end_point(self):
        eid = self.ids[1]
        core.set_shot_mark(self.p, eid, {"x0": 0.3, "y0": 0.2, "result": "miss"})
        self.assertIsNone(self.p["shot_marks"][eid]["x1"])

    def test_shots_for_team_only_marked_and_correct_team(self):
        core.set_shot_mark(self.p, self.ids[0], {"x0": .5, "y0": .1, "x1": .5, "y1": 0, "result": "goal"})  # ECFC
        core.set_shot_mark(self.p, self.ids[1], {"x0": .4, "y0": .2, "x1": .4, "y1": 0, "result": "saved"})  # RKC
        ecfc = core.shots_for_team(self.p, 0)
        self.assertEqual(len(ecfc), 1)
        self.assertEqual(ecfc[0]["result"], "goal")
        self.assertEqual(len(core.shots_for_team(self.p, 1)), 1)

    def test_clear_mark(self):
        eid = self.ids[0]
        core.set_shot_mark(self.p, eid, {"x0": .5, "y0": .1, "result": "post"})
        core.clear_shot_mark(self.p, eid)
        self.assertNotIn(eid, self.p["shot_marks"])


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


class PossessionTests(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("m.mp4")

    def test_totals_and_percentage(self):
        core.set_possession(self.p, 0, 0)        # A from 0:00
        core.set_possession(self.p, 60_000, 1)   # B from 1:00
        core.set_possession(self.p, 90_000, 0)   # A from 1:30
        self.p["position_ms"] = 120_000          # frontier 2:00
        tot = core.possession_totals(self.p)
        # A: 60s + 30s = 90s ; B: 30s -> 75% / 25%
        self.assertEqual(tot["teams"][0]["seconds"], 90)
        self.assertEqual(tot["teams"][1]["seconds"], 30)
        self.assertEqual(tot["teams"][0]["pct"], 75.0)
        self.assertEqual(tot["teams"][1]["pct"], 25.0)

    def test_dead_ball_excluded_from_pct(self):
        core.set_possession(self.p, 0, 0)
        core.set_possession(self.p, 10_000, None)   # dead ball
        core.set_possession(self.p, 20_000, 1)
        self.p["position_ms"] = 30_000
        tot = core.possession_totals(self.p)
        self.assertEqual(tot["teams"][0]["seconds"], 10)
        self.assertEqual(tot["teams"][1]["seconds"], 10)
        self.assertEqual(tot["dead_ball_seconds"], 10)
        self.assertEqual(tot["teams"][0]["pct"], 50.0)

    def test_csv_has_possession_columns(self):
        core.set_possession(self.p, 0, 0)
        self.p["position_ms"] = 60_000
        header = core.to_csv(self.p).splitlines()[0]
        self.assertTrue(header.endswith("possession_seconds,possession_percent"))


class ColorAndPurgeTests(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("m.mp4")

    def test_set_colors(self):
        core.set_team_colors(self.p, ["#ffffff", "#000000"])
        self.assertEqual(self.p["team_colors"], ["#ffffff", "#000000"])

    def test_purge_removes_stat_and_events(self):
        core.add_event(self.p, "Team A", "left_crosses", 1000)
        core.delete_stat(self.p, "left_crosses")
        core.purge_stat(self.p, "left_crosses")
        self.assertNotIn("left_crosses", self.p["archived_stats"])
        self.assertTrue(all(e["stat"] != "left_crosses" for e in self.p["events"]))


class UnifiedUndoTests(unittest.TestCase):
    def test_undo_pops_newest_action(self):
        import time as _t
        p = core.new_project("m.mp4")
        core.add_event(p, "Team A", "shots", 1000)
        _t.sleep(0.01)
        core.set_possession(p, 2000, 0)            # newest action
        popped = core.undo(p)
        self.assertIn("holder", popped)            # the possession marker
        self.assertEqual(len(p["possession"]), 0)
        self.assertEqual(len(p["events"]), 1)
        popped2 = core.undo(p)                      # now the event
        self.assertEqual(popped2["stat"], "shots")


class ChartDataTests(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("m.mp4")
        # first half kicks at video 0; so match minute == video minute
        core.set_period_anchor(self.p, "first_half", "start", 0)
        core.set_period_anchor(self.p, "first_half", "end", 2_700_000)      # 45:00
        core.set_period_anchor(self.p, "second_half", "start", 2_700_000)   # base 45

    def test_match_minute(self):
        self.assertAlmostEqual(core.match_minute(600_000, self.p["periods"]), 10.0)   # 10:00
        # second-half: video 46:00 -> match minute 46
        self.assertAlmostEqual(core.match_minute(2_760_000, self.p["periods"]), 46.0)

    def test_timeline_net_bins(self):
        core.add_event(self.p, "Team A", "shots", 300_000)    # 5' -> bin 0-10
        core.add_event(self.p, "Team B", "shots", 360_000)    # 6' -> bin 0-10
        core.add_event(self.p, "Team B", "shots", 660_000)    # 11' -> bin 10-20
        s = core.timeline_series(self.p, ["shots"], 10)
        self.assertEqual(s["labels"][0], "0\u201310")
        self.assertEqual(s["net"][0], 0)    # 1 each in first bin
        self.assertEqual(s["net"][1], 1)    # +1 Team B in second bin

    def test_cumulative_running_totals(self):
        core.add_event(self.p, "Team A", "shots", 300_000)
        core.add_event(self.p, "Team A", "shots", 600_000)
        core.add_event(self.p, "Team B", "shots", 900_000)
        s = core.cumulative_series(self.p, ["shots"])
        self.assertEqual(s["a"][-1][1], 2)   # Team A ends on 2
        self.assertEqual(s["b"][-1][1], 1)   # Team B ends on 1

    def test_possession_by_window_minutes(self):
        core.set_possession(self.p, 0, 0)         # A from 0:00
        core.set_possession(self.p, 300_000, 1)   # B from 5:00
        core.set_possession(self.p, 720_000, 0)   # A from 12:00
        self.p["position_ms"] = 900_000           # frontier 15:00
        s = core.possession_by_window(self.p, "5")
        self.assertEqual(s["labels"], ["0\u20135", "5\u201310", "10\u201315"])
        self.assertEqual(s["team_a"], [300, 0, 180])   # seconds
        self.assertEqual(s["team_b"], [0, 300, 120])
        self.assertEqual(s["pct_a"][2], 60.0)          # 180 / 300

    def test_possession_by_half(self):
        core.set_possession(self.p, 0, 0)
        core.set_possession(self.p, 300_000, 1)
        self.p["position_ms"] = 600_000
        s = core.possession_by_window(self.p, "half")
        self.assertEqual(s["labels"], ["First half"])
        self.assertEqual(s["team_a"], [300]); self.assertEqual(s["team_b"], [300])


class PersistenceRobustnessTests(unittest.TestCase):
    def test_concurrent_saves_never_corrupt(self):
        # Reproduces the "Extra data" crash: many threads saving projects of very
        # different sizes to the same path at once. With a unique temp per save,
        # the file is always a complete, loadable object.
        import threading
        with tempfile.TemporaryDirectory() as d:
            def saver(n):
                p = core.new_project("race.mp4")
                core.add_stat(p, "corners")
                for i in range(n):
                    core.add_event(p, "Team A", "shots", i * 1000)
                for _ in range(25):
                    core.save_project(p, d)
            threads = [threading.Thread(target=saver, args=(n,)) for n in (1, 300, 5, 600, 50)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            loaded = core.load_project("race.mp4", d)   # must not raise
            self.assertIsInstance(loaded["events"], list)

    def test_load_recovers_from_extra_data(self):
        with tempfile.TemporaryDirectory() as d:
            good = json.dumps(core.new_project("c.mp4"))
            with open(core._project_path("c.mp4", d), "w", encoding="utf-8") as fh:
                fh.write(good + "\n}\n  garbage tail")   # simulate the corruption
            loaded = core.load_project("c.mp4", d)        # must recover, not crash
            self.assertEqual(loaded["video"], "c.mp4")
            self.assertEqual(loaded["events"], [])


class XgTest(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("m.mp4")
        # one half so shots get a match minute
        core.set_period_anchor(self.p, "first_half", "start", 0)

    def _shot(self, team, ms):
        ev = core.add_event(self.p, team, "shots", ms)
        return ev["id"]

    def test_model_loaded(self):
        self.assertIsNotNone(core.xg_for_shot(0.5, 0.2), "xg_coefficients.json should be present")

    def test_penalty_higher_than_open_play_same_spot(self):
        spot_y = 11 / 60
        op = core.xg_for_shot(0.5, spot_y, penalty=False)
        pen = core.xg_for_shot(0.5, spot_y, penalty=True)
        self.assertGreater(pen, op)
        self.assertGreater(pen, 0.6)

    def test_xg_falls_with_distance(self):
        near = core.xg_for_shot(0.5, 6 / 60)
        far = core.xg_for_shot(0.5, 30 / 60)
        self.assertGreater(near, far)

    def test_header_lowers_xg(self):
        foot = core.xg_for_shot(0.5, 6 / 60, header=False)
        head = core.xg_for_shot(0.5, 6 / 60, header=True)
        self.assertLess(head, foot)

    def test_set_shot_mark_stores_xg(self):
        eid = self._shot("Team A", 60000)
        rec = core.set_shot_mark(self.p, eid, {"x0": 0.5, "y0": 0.12, "result": "goal", "header": True})
        self.assertIn("xg", rec)
        self.assertTrue(rec["header"])
        self.assertAlmostEqual(rec["xg"], core.xg_for_shot(0.5, 0.12, header=True), places=4)

    def test_new_feature_directions(self):
        base = dict(x0=0.5, y0=0.2)
        b = core.xg_for_shot(**base)
        self.assertLess(core.xg_for_shot(**base, under_pressure=True), b)     # pressure lowers
        self.assertGreater(core.xg_for_shot(**base, free_kick=True), b)       # direct FK raises
        self.assertLess(core.xg_for_shot(**base, n_def=3), b)                 # defenders lower
        self.assertGreater(core.xg_for_shot(**base, gk_dist=12), b)           # keeper off line raises

    def test_gk_off_line_uses_picked_location(self):
        eid = self._shot("Team A", 60000)
        on_line = core.set_shot_mark(self.p, eid, {"x0": 0.5, "y0": 0.2, "result": "saved"})
        self.assertEqual(on_line["gk_dist"], core.DEFAULT_GK_DIST)
        # keeper marked well off the line -> larger gk_dist, higher xG
        off = core.set_shot_mark(self.p, eid, {"x0": 0.5, "y0": 0.2, "result": "saved",
                                               "gk_off": True, "gk_x0": 0.5, "gk_y0": 0.18})
        self.assertGreater(off["gk_dist"], on_line["gk_dist"])
        self.assertGreater(off["xg"], on_line["xg"])

    def test_team_xg_totals(self):
        a = self._shot("Team A", 60000); b = self._shot("Team B", 120000)
        core.set_shot_mark(self.p, a, {"x0": 0.5, "y0": 0.1, "result": "goal"})
        core.set_shot_mark(self.p, b, {"x0": 0.5, "y0": 0.5, "result": "miss"})
        tot = core.team_xg_totals(self.p)
        self.assertGreater(tot["Team A"], tot["Team B"])
        csv_out = core.to_csv(self.p)
        self.assertIn("xg", csv_out.splitlines()[0].split(","))

    def test_xg_timeline_and_cumulative(self):
        a = self._shot("Team A", 60000)
        core.set_shot_mark(self.p, a, {"x0": 0.5, "y0": 0.1, "result": "goal"})
        tl = core.timeline_series(self.p, [], 10, xg=True)
        self.assertTrue(any(v != 0 for v in tl["net"]))
        cu = core.cumulative_series(self.p, [], xg=True)
        self.assertGreater(cu["a"][-1][1], 0)


class BreaksTest(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("m.mp4")
        core.set_period_anchor(self.p, "first_half", "start", 0)

    def _break(self, s, e):
        b = core.add_break(self.p)
        core.set_break_anchor(self.p, b["id"], "start", s)
        core.set_break_anchor(self.p, b["id"], "end", e)
        return b

    def test_minute_subtracts_break(self):
        self._break(120_000, 300_000)             # 3-min weather break (2:00–5:00)
        breaks = self.p["breaks"]
        # 6:00 video → 3:00 match (3 of the 6 minutes were break)
        self.assertAlmostEqual(core.match_minute(360_000, self.p["periods"], breaks), 3.0, places=3)

    def test_minute_frozen_during_break(self):
        self._break(120_000, 300_000)
        breaks = self.p["breaks"]
        # 4:00 video is inside the break → frozen at 2:00 match
        self.assertAlmostEqual(core.match_minute(240_000, self.p["periods"], breaks), 2.0, places=3)

    def test_clock_phase_weather(self):
        self._break(120_000, 300_000)
        mc = core.match_clock(240_000, self.p["periods"], self.p["breaks"])
        self.assertEqual(mc["phase"], "break")
        self.assertEqual(mc["label"], "Weather break")
        mc2 = core.match_clock(360_000, self.p["periods"], self.p["breaks"])
        self.assertEqual(mc2["phase"], "play")
        self.assertEqual(mc2["display"], "03:00")

    def test_incomplete_break_has_no_effect(self):
        b = core.add_break(self.p)
        core.set_break_anchor(self.p, b["id"], "start", 120_000)   # no end
        self.assertAlmostEqual(core.match_minute(360_000, self.p["periods"], self.p["breaks"]), 6.0, places=3)
        self.assertIn("Weather break 1: set both start and end", core.validate_periods(self.p))

    def test_remove_break(self):
        b = self._break(120_000, 300_000)
        core.remove_break(self.p, b["id"])
        self.assertEqual(self.p["breaks"], [])
        self.assertAlmostEqual(core.match_minute(360_000, self.p["periods"], self.p["breaks"]), 6.0, places=3)


class CumulativeGoalsTest(unittest.TestCase):
    def test_cumulative_returns_goals_and_2dp(self):
        p = core.new_project("m.mp4")
        core.set_period_anchor(p, "first_half", "start", 0)
        core.add_stat(p, "goal")
        core.add_event(p, "Team A", "shots", 120_000)
        core.add_event(p, "Team A", "goal", 125_000)
        s = core.cumulative_series(p, ["shots"], goal_stat="goal")
        self.assertEqual(len(s["goals"]), 1)
        self.assertEqual(s["goals"][0]["team"], 0)
        # all y values rounded to <= 2 decimals
        for pt in s["a"] + s["b"]:
            self.assertEqual(round(pt[1], 2), pt[1])


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("m.mp4")

    def test_defaults_present(self):
        self.assertEqual(self.p["board"], {"tokens": [], "marks": []})

    def test_set_board_sanitizes(self):
        b = core.set_board(self.p, {
            "tokens": [
                {"kind": "player", "team": 0, "label": "10", "x": 0.3, "y": 1.4},   # y clamped
                {"kind": "ball", "team": 9, "x": -1, "y": 0.5},                       # team->None, x clamped
            ],
            "marks": [
                {"type": "arrow", "x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2, "color": "#00ff00", "dashed": True},
                {"type": "zone", "x0": 0.2, "y0": 0.2, "x1": 0.4, "y1": 0.5, "color": "not-a-color"},
                {"type": "text", "x": 0.5, "y": 0.5, "text": "press here"},
                {"type": "free", "points": [[0.1, 0.1], [0.2, 0.2], [0.3, 0.25]], "color": "#123abc"},
                {"type": "free", "points": [[0.1, 0.1]]},          # too few points -> dropped
            ],
        })
        self.assertEqual(b["tokens"][0]["y"], 1.0)
        self.assertIsNone(b["tokens"][1]["team"])
        self.assertEqual(b["tokens"][1]["x"], 0.0)
        self.assertTrue(all("id" in t for t in b["tokens"]))
        types = [m["type"] for m in b["marks"]]
        self.assertEqual(types, ["arrow", "zone", "text", "free"])   # short free dropped
        self.assertTrue(b["marks"][0]["dashed"])
        self.assertEqual(b["marks"][1]["color"], "#e6483d")          # bad color -> default
        self.assertEqual(len(b["marks"][3]["points"]), 3)

    def test_board_persists(self):
        with tempfile.TemporaryDirectory() as d:
            core.set_board(self.p, {"tokens": [{"kind": "player", "team": 1, "x": 0.5, "y": 0.5}], "marks": []})
            core.save_project(self.p, d)
            loaded = core.load_project("m.mp4", d)
            self.assertEqual(len(loaded["board"]["tokens"]), 1)


class BuildupTest(unittest.TestCase):
    def setUp(self):
        self.p = core.new_project("m.mp4")
        core.set_period_anchor(self.p, "first_half", "start", 0)

    def _poss(self, holder, ms):
        core.set_possession(self.p, ms, holder)

    def _shot(self, team, ms):
        return core.add_event(self.p, team, "shots", ms)["id"]

    def test_spell_durations_and_deadball_included(self):
        self._poss(0, 10_000)      # A wins ball at 10s
        self._poss(None, 25_000)   # dead ball at 25s
        self._poss(0, 30_000)      # A again at 30s
        self.p["position_ms"] = 40_000
        sp = core.possession_spells(self.p)
        holders = [s["holder_index"] for s in sp]
        self.assertEqual(holders, [0, None, 0])           # dead-ball stretch kept
        self.assertEqual(sp[0]["duration_sec"], 15.0)     # 10->25
        self.assertEqual(sp[1]["holder"], None)
        self.assertEqual(sp[0]["ended_by"], "dead_ball")

    def test_buildup_restarts_after_dead_ball(self):
        # A(10s) -> dead(25s) -> A(30s) -> shot at 38s  => build-up counts from 30s
        self._poss(0, 10_000); self._poss(None, 25_000); self._poss(0, 30_000)
        self._shot("Team A", 38_000); self.p["position_ms"] = 40_000
        b = core.shot_buildups(self.p)[0]
        self.assertFalse(b["from_dead_ball"])
        self.assertEqual(b["buildup_sec"], 8.0)           # 38 - 30
        self.assertEqual(b["possession_start_ms"], 30_000)

    def test_shot_from_dead_ball_is_set_piece(self):
        self._poss(0, 10_000); self._poss(None, 25_000)
        self._shot("Team A", 28_000); self.p["position_ms"] = 40_000
        b = core.shot_buildups(self.p)[0]
        self.assertTrue(b["from_dead_ball"])
        self.assertEqual(b["buildup_sec"], 0.0)

    def test_break_time_excluded_from_buildup(self):
        self._poss(0, 10_000)
        brk = core.add_break(self.p)
        core.set_break_anchor(self.p, brk["id"], "start", 15_000)
        core.set_break_anchor(self.p, brk["id"], "end", 45_000)   # 30s weather break
        self._shot("Team A", 50_000); self.p["position_ms"] = 60_000
        b = core.shot_buildups(self.p)[0]
        # raw 40s from 10s->50s, minus 30s break = 10s
        self.assertEqual(b["buildup_sec"], 10.0)


class XgModelSelectTest(unittest.TestCase):
    def test_default_is_logistic(self):
        self.assertEqual(core.new_project("m.mp4")["xg_model"], "logistic")

    def test_nn_falls_back_when_unavailable(self):
        # no xg_model/model.pt in the test env -> nn requests resolve to logistic
        self.assertFalse(core.xg_nn_available())
        self.assertEqual(core.effective_xg_model("nn"), "logistic")
        _, used = core.scored_xg({"x0": 0.5, "y0": 0.2}, "nn")
        self.assertEqual(used, "logistic")

    def test_set_model_recomputes_and_records(self):
        p = core.new_project("m.mp4")
        core.set_period_anchor(p, "first_half", "start", 0)
        e = core.add_event(p, "Team A", "shots", 60_000)["id"]
        core.set_shot_mark(p, e, {"x0": 0.5, "y0": 0.2, "result": "goal"})
        self.assertEqual(p["shot_marks"][e]["xg_model"], "logistic")
        core.set_xg_model(p, "nn")                       # preference stored...
        self.assertEqual(p["xg_model"], "nn")
        self.assertEqual(p["shot_marks"][e]["xg_model"], "logistic")   # ...scorer falls back

    def test_bad_model_rejected(self):
        with self.assertRaises(ValueError):
            core.set_xg_model(core.new_project("m.mp4"), "banana")


class DeleteEventTest(unittest.TestCase):
    def test_delete_event_and_mark(self):
        p = core.new_project("m.mp4")
        core.set_period_anchor(p, "first_half", "start", 0)
        a = core.add_event(p, "Team A", "shots", 60_000)["id"]
        b = core.add_event(p, "Team A", "shots", 90_000)["id"]
        core.set_shot_mark(p, a, {"x0": 0.5, "y0": 0.2, "result": "goal"})
        self.assertTrue(core.delete_event(p, a))
        self.assertEqual([e["id"] for e in p["events"]], [b])   # only a removed
        self.assertNotIn(str(a), p["shot_marks"])               # its mark gone too
        self.assertFalse(core.delete_event(p, "nope"))          # unknown id -> no-op


if __name__ == "__main__":
    unittest.main(verbosity=2)
