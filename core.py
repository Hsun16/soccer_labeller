"""Core logic for the soccer stats labeler.

This module is intentionally free of any web/Flask dependency so the
interesting logic (add event, undo, derive counts, export, persistence)
can be unit-tested in isolation. The events list is the single source of
truth; the counts table is always derived from it.
"""

import csv
import io
import json
import os
import time
import uuid

DEFAULT_TEAMS = ["Team A", "Team B"]
DEFAULT_STATS = ["shots", "shots_on_target", "left_crosses", "right_crosses"]

# Each period: where its clock starts (base_min) and when stoppage begins (regulation_min).
PERIOD_SPECS = {
    "first_half":  {"label": "First half",   "base_min": 0,   "regulation_min": 45},
    "second_half": {"label": "Second half",  "base_min": 45,  "regulation_min": 45},
    "et_first":    {"label": "Extra time 1", "base_min": 90,  "regulation_min": 15},
    "et_second":   {"label": "Extra time 2", "base_min": 105, "regulation_min": 15},
}
PERIOD_ORDER = ["first_half", "second_half", "et_first", "et_second"]
EXTRA_TIME = ["et_first", "et_second"]


def make_period(period_type):
    spec = PERIOD_SPECS[period_type]
    return {
        "type": period_type,
        "base_min": spec["base_min"],
        "regulation_min": spec["regulation_min"],
        "video_start_ms": None,   # kick (set by the user)
        "video_end_ms": None,     # whistle (set by the user)
    }


def format_timestamp(timestamp_ms):
    """Convert milliseconds into an MM:SS display string."""
    total_seconds = int(timestamp_ms) // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def new_project(video):
    """Create a fresh project for a video filename."""
    return {
        "video": video,
        "teams": list(DEFAULT_TEAMS),
        "stats": list(DEFAULT_STATS),
        "archived_stats": [],   # deleted stats; their events are kept in `events`
        "events": [],
        "position_ms": 0,
        "periods": [make_period("first_half"), make_period("second_half")],
    }


def set_period_anchor(project, period_type, which, video_ms):
    """Set (or clear, with video_ms=None) a period's start/end video position."""
    if which not in ("start", "end"):
        raise ValueError("which must be 'start' or 'end'")
    key = "video_start_ms" if which == "start" else "video_end_ms"
    for period in project["periods"]:
        if period["type"] == period_type:
            period[key] = None if video_ms is None else max(0, int(video_ms))
            return period
    raise ValueError(f"unknown period: {period_type!r}")


def add_extra_time(project):
    """Add the two extra-time periods if they aren't already present."""
    have = {p["type"] for p in project["periods"]}
    for t in EXTRA_TIME:
        if t not in have:
            project["periods"].append(make_period(t))
    project["periods"].sort(key=lambda p: PERIOD_ORDER.index(p["type"]))
    return project["periods"]


def remove_extra_time(project):
    """Drop the extra-time periods. Events stay in `events` (kept as raw video time)."""
    project["periods"] = [p for p in project["periods"] if p["type"] not in EXTRA_TIME]
    return project["periods"]


def _stoppage(ms):
    """Stoppage portion: minutes un-padded, e.g. '+1:00', '+12:34'."""
    minutes, seconds = divmod(int(ms) // 1000, 60)
    return f"+{minutes}:{seconds:02d}"


def _break_text(prev_type):
    return {
        "first_half":  ("HT", "Half time"),
        "second_half": ("Break", "Before extra time"),
        "et_first":    ("ET break", "Extra-time break"),
    }.get(prev_type, ("Break", "Break"))


def match_clock(video_ms, periods):
    """Map a raw video position to the displayed match clock.

    Returns {phase, period_type, display, label}. phase is one of
    play / stoppage / break / pre / post / unset.
    """
    v = int(video_ms)
    active = sorted(
        (p for p in periods if p["video_start_ms"] is not None),
        key=lambda p: p["video_start_ms"],
    )
    if not active:
        return {"phase": "unset", "period_type": None, "display": "—", "label": "No kickoff set"}
    if v < active[0]["video_start_ms"]:
        return {"phase": "pre", "period_type": None, "display": "—", "label": "Pre-match"}

    inf = float("inf")
    for i, p in enumerate(active):
        start = p["video_start_ms"]
        nxt = active[i + 1]["video_start_ms"] if i + 1 < len(active) else None
        # If the whistle isn't marked yet, the period runs until the next kick (or on).
        play_end = p["video_end_ms"] if p["video_end_ms"] is not None else (nxt if nxt is not None else inf)
        if start <= v <= play_end:
            elapsed = v - start
            reg = p["regulation_min"] * 60_000
            base = p["base_min"] * 60_000
            if elapsed <= reg:
                return {"phase": "play", "period_type": p["type"],
                        "display": format_timestamp(base + elapsed),
                        "label": PERIOD_SPECS[p["type"]]["label"]}
            return {"phase": "stoppage", "period_type": p["type"],
                    "display": format_timestamp(base + reg) + " " + _stoppage(elapsed - reg),
                    "label": PERIOD_SPECS[p["type"]]["label"]}
        if nxt is not None and play_end != inf and play_end < v < nxt:
            short, label = _break_text(p["type"])
            return {"phase": "break", "period_type": None, "display": short, "label": label}

    return {"phase": "post", "period_type": None, "display": "—", "label": "Full time"}


def validate_periods(project):
    """Advisory warnings about anchor mistakes (non-blocking)."""
    warnings = []
    for p in project["periods"]:
        s, e = p["video_start_ms"], p["video_end_ms"]
        if s is not None and e is not None and e < s:
            warnings.append(f"{PERIOD_SPECS[p['type']]['label']}: whistle is before kickoff")
    ordered = [p for p in project["periods"] if p["video_start_ms"] is not None]
    ordered.sort(key=lambda p: PERIOD_ORDER.index(p["type"]))
    for a, b in zip(ordered, ordered[1:]):
        if a["video_start_ms"] >= b["video_start_ms"]:
            warnings.append(f"{PERIOD_SPECS[b['type']]['label']} starts before {PERIOD_SPECS[a['type']]['label']}")
        elif a["video_end_ms"] is not None and a["video_end_ms"] > b["video_start_ms"]:
            warnings.append(f"{PERIOD_SPECS[a['type']]['label']} overlaps {PERIOD_SPECS[b['type']]['label']}")
    return warnings


def add_event(project, team, stat, timestamp_ms):
    """Append one labelled event. Returns the created event.

    The timestamp_display is derived here (not trusted from the caller)
    so it can never disagree with timestamp_ms.
    """
    if team not in project["teams"]:
        raise ValueError(f"unknown team: {team!r}")
    if stat not in project["stats"]:
        raise ValueError(f"unknown stat: {stat!r}")

    event = {
        "id": uuid.uuid4().hex,
        "team": team,
        "stat": stat,
        "timestamp_ms": int(timestamp_ms),
        "timestamp_display": format_timestamp(timestamp_ms),
        "created_at": time.time(),
    }
    project["events"].append(event)
    return event


def undo(project):
    """Remove and return the most recent event, or None if there are none."""
    if not project["events"]:
        return None
    return project["events"].pop()


def rename_teams(project, new_teams):
    """Rename teams positionally, remapping existing events so none are orphaned.

    Must keep the same number of teams (the app is two-team by design).
    """
    if len(new_teams) != len(project["teams"]):
        raise ValueError("team count must not change")
    mapping = dict(zip(project["teams"], new_teams))
    for event in project["events"]:
        if event["team"] in mapping:
            event["team"] = mapping[event["team"]]
    project["teams"] = list(new_teams)
    return project["teams"]


def add_stat(project, stat):
    """Add a statistic column. Restores it if it was previously deleted."""
    stat = stat.strip()
    if not stat:
        raise ValueError("stat name is empty")
    if stat in project["archived_stats"]:
        project["archived_stats"].remove(stat)
    if stat not in project["stats"]:
        project["stats"].append(stat)
    return project["stats"]


def delete_stat(project, stat):
    """Hide a statistic from the grid/CSV. Events under it are kept (in `events`)
    and the name is remembered in `archived_stats` so it can be restored."""
    if stat in project["stats"]:
        project["stats"].remove(stat)
        if stat not in project["archived_stats"]:
            project["archived_stats"].append(stat)
    return project["stats"]


def restore_stat(project, stat):
    """Bring a previously deleted statistic back into the active grid."""
    if stat in project["archived_stats"]:
        project["archived_stats"].remove(stat)
        if stat not in project["stats"]:
            project["stats"].append(stat)
    return project["stats"]


def derive_counts(project):
    """Build the counts table from events: {team: {stat: count}}.

    Every (team, stat) pair is present and zero-filled so the UI/CSV
    always have a complete grid even before any events are logged.
    """
    counts = {team: {stat: 0 for stat in project["stats"]} for team in project["teams"]}
    for event in project["events"]:
        # Tolerate stats/teams that may have been renamed away.
        if event["team"] in counts and event["stat"] in counts[event["team"]]:
            counts[event["team"]][event["stat"]] += 1
    return counts


def to_csv(project):
    """Render the derived counts as CSV text: one row per team, one column per stat."""
    counts = derive_counts(project)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["team"] + project["stats"])
    for team in project["teams"]:
        writer.writerow([team] + [counts[team][stat] for stat in project["stats"]])
    return buffer.getvalue()


def events_for_view(project):
    """Events with match-clock fields added, for the UI log and JSON export.

    `timestamp_ms`/`timestamp_display` stay as the raw video position;
    match-clock fields are derived from the current periods.
    """
    periods = project["periods"]
    out = []
    for ev in project["events"]:
        mc = match_clock(ev["timestamp_ms"], periods)
        display = mc["display"] if mc["display"] != "—" else ev["timestamp_display"]
        out.append({**ev, "match_display": display, "phase": mc["phase"],
                    "period_type": mc["period_type"], "period_label": mc["label"]})
    return out


def to_events_json(project):
    """Render the events export as a JSON string."""
    payload = {
        "video": project["video"],
        "teams": project["teams"],
        "stats": project["stats"],
        "archived_stats": project.get("archived_stats", []),
        "periods": project["periods"],
        "events": events_for_view(project),
    }
    return json.dumps(payload, indent=2)


def save_project(project, projects_dir):
    """Persist the full project to <projects_dir>/<video>.json (atomic write)."""
    os.makedirs(projects_dir, exist_ok=True)
    path = _project_path(project["video"], projects_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(project, fh, indent=2)
    os.replace(tmp, path)
    return path


def load_project(video, projects_dir):
    """Load a saved project, or return a fresh one if none exists yet.

    Migrates older files: backfill archived_stats, and convert a single
    kickoff_ms into a two-period structure (first-half kick = old kickoff).
    """
    path = _project_path(video, projects_dir)
    if not os.path.exists(path):
        return new_project(video)
    with open(path, encoding="utf-8") as fh:
        project = json.load(fh)
    project.setdefault("archived_stats", [])
    if "periods" not in project:
        periods = [make_period("first_half"), make_period("second_half")]
        periods[0]["video_start_ms"] = project.get("kickoff_ms", 0) or 0
        project["periods"] = periods
    project.pop("kickoff_ms", None)
    return project


def _project_path(video, projects_dir):
    # Keep the saved filename tied to the video but filesystem-safe.
    safe = video.replace(os.sep, "_").replace("/", "_")
    return os.path.join(projects_dir, safe + ".json")
