"""Core logic for the soccer stats labeler.

This module is intentionally free of any web/Flask dependency so the
interesting logic (add event, undo, derive counts, export, persistence)
can be unit-tested in isolation. The events list is the single source of
truth; the counts table is always derived from it.
"""

import csv
import io
import json
import math
import os
import re
import tempfile
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
        "team_colors": ["#ff9d3d", "#3db5ff"],
        "stats": list(DEFAULT_STATS),
        "archived_stats": [],   # deleted stats; their events are kept in `events`
        "events": [],
        "possession": [],       # change markers: {timestamp_ms, holder: 0|1|None}
        "position_ms": 0,
        "periods": [make_period("first_half"), make_period("second_half")],
        "charts": [],          # chart-board configs (see chart_* functions)
        "chart_score": "",      # manual score string for chart footers, e.g. "2 – 1"
        "combined_stats": [],   # [{name, parts:[stat,...]}] virtual stats for charts
        "match_label": "",      # groups part-videos of one match (e.g. weather-suspended)
        "shot_stat": "shots",   # which stat the shot map iterates
        "shot_marks": {},       # event_id -> {x0,y0,x1,y1,result} pitch positions
        "breaks": [],           # in-half stoppages (e.g. weather): {id, video_start_ms, video_end_ms}
        "board": {"tokens": [], "marks": []},   # tactical board (per match)
        "xg_model": "logistic",  # 'logistic' or 'nn' — which xG model scores shots
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


def add_break(project):
    """Add an empty in-half break (e.g. weather). Returns its id."""
    project.setdefault("breaks", [])
    b = {"id": uuid.uuid4().hex[:8], "video_start_ms": None, "video_end_ms": None}
    project["breaks"].append(b)
    return b


def set_break_anchor(project, break_id, which, video_ms):
    """Set (or clear, with video_ms=None) a break's start/end video position."""
    if which not in ("start", "end"):
        raise ValueError("which must be 'start' or 'end'")
    key = "video_start_ms" if which == "start" else "video_end_ms"
    for b in project.get("breaks", []):
        if b["id"] == break_id:
            b[key] = None if video_ms is None else max(0, int(video_ms))
            return b
    raise ValueError(f"unknown break: {break_id!r}")


def remove_break(project, break_id):
    project["breaks"] = [b for b in project.get("breaks", []) if b["id"] != break_id]
    return project["breaks"]


_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def _clamp01(v):
    try:
        return max(0.0, min(1.0, round(float(v), 4)))
    except (TypeError, ValueError):
        return 0.0


def _color(v, default="#e6483d"):
    return v if isinstance(v, str) and _HEX_COLOR.match(v) else default


def set_board(project, board):
    """Replace the tactical board with a sanitized copy of the posted one.
    tokens: {id, kind(player|ball), team(0|1|None), label, x, y}
    marks:  arrow/line {x0,y0,x1,y1,color,dashed} · zone {x0,y0,x1,y1,color}
            · text {x,y,text,color} · free {points:[[x,y]...],color}"""
    board = board or {}
    tokens = []
    for t in (board.get("tokens") or [])[:100]:
        tokens.append({
            "id": str(t.get("id") or uuid.uuid4().hex[:8]),
            "kind": "ball" if t.get("kind") == "ball" else "player",
            "team": t["team"] if t.get("team") in (0, 1) else None,
            "label": str(t.get("label", ""))[:16],
            "x": _clamp01(t.get("x")), "y": _clamp01(t.get("y")),
        })
    marks = []
    for m in (board.get("marks") or [])[:300]:
        mt = m.get("type")
        base = {"id": str(m.get("id") or uuid.uuid4().hex[:8]), "type": mt, "color": _color(m.get("color"))}
        if mt in ("arrow", "line", "zone"):
            base.update(x0=_clamp01(m.get("x0")), y0=_clamp01(m.get("y0")),
                        x1=_clamp01(m.get("x1")), y1=_clamp01(m.get("y1")))
            if mt != "zone":
                base["dashed"] = bool(m.get("dashed"))
            marks.append(base)
        elif mt == "text":
            base.update(x=_clamp01(m.get("x")), y=_clamp01(m.get("y")), text=str(m.get("text", ""))[:80])
            marks.append(base)
        elif mt == "free":
            pts = [[_clamp01(p[0]), _clamp01(p[1])] for p in (m.get("points") or [])[:1000]
                   if isinstance(p, (list, tuple)) and len(p) >= 2]
            if len(pts) >= 2:
                base["points"] = pts
                marks.append(base)
    project["board"] = {"tokens": tokens, "marks": marks}
    return project["board"]


def _active_breaks(project):
    """Breaks with both ends marked and end after start."""
    return [b for b in project.get("breaks", [])
            if b.get("video_start_ms") is not None and b.get("video_end_ms") is not None
            and b["video_end_ms"] > b["video_start_ms"]]


def _paused_ms(v, start, breaks):
    """Frozen break time within [start, v]; and whether v falls inside a break.
    Incomplete or invalid breaks (missing end, or end<=start) are ignored."""
    paused, inside = 0, False
    for b in breaks:
        bs, be = b.get("video_start_ms"), b.get("video_end_ms")
        if bs is None or be is None or be <= bs:
            continue
        lo, hi = max(bs, start), min(be, v)
        if hi > lo:
            paused += hi - lo
        if bs <= v < be:
            inside = True
    return paused, inside


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


def match_clock(video_ms, periods, breaks=()):
    """Map a raw video position to the displayed match clock.

    Returns {phase, period_type, display, label}. phase is one of
    play / stoppage / break / pre / post / unset. In-half breaks (e.g. weather)
    freeze the clock while active and shift it back afterwards.
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
            paused, in_break = _paused_ms(v, start, breaks)
            elapsed = v - start - paused
            reg = p["regulation_min"] * 60_000
            base = p["base_min"] * 60_000
            if in_break:
                return {"phase": "break", "period_type": p["type"],
                        "display": format_timestamp(base + elapsed) + " \u26c5",
                        "label": "Weather break"}
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
    for i, bk in enumerate(project.get("breaks", []), 1):
        s, e = bk.get("video_start_ms"), bk.get("video_end_ms")
        if (s is None) != (e is None):
            warnings.append(f"Weather break {i}: set both start and end")
        elif s is not None and e is not None and e <= s:
            warnings.append(f"Weather break {i}: end is before start")
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
    """Remove and return the most recent action (stat event OR possession change)."""
    last_event = project["events"][-1] if project["events"] else None
    last_poss = project["possession"][-1] if project.get("possession") else None
    if last_event is None and last_poss is None:
        return None
    if last_poss is None or (last_event is not None and last_event["created_at"] >= last_poss["created_at"]):
        return project["events"].pop()
    return project["possession"].pop()


def delete_event(project, event_id):
    """Delete a single event by id (and its shot mark, if any). Returns True if removed."""
    before = len(project["events"])
    project["events"] = [e for e in project["events"] if e["id"] != event_id]
    project.get("shot_marks", {}).pop(str(event_id), None)
    return len(project["events"]) != before


def set_team_colors(project, colors):
    """Set the two team colours (hex strings)."""
    if len(colors) != 2:
        raise ValueError("need exactly two colours")
    project["team_colors"] = [str(c) for c in colors]
    return project["team_colors"]


def purge_stat(project, stat):
    """Permanently delete an archived stat AND every event logged under it."""
    if stat in project["archived_stats"]:
        project["archived_stats"].remove(stat)
    project["events"] = [e for e in project["events"] if e["stat"] != stat]
    return project["archived_stats"]


def set_possession(project, timestamp_ms, holder):
    """Record a possession change at a video position. holder is 0, 1, or None (dead ball)."""
    if holder not in (0, 1, None):
        raise ValueError("holder must be 0, 1, or None")
    marker = {
        "id": uuid.uuid4().hex,
        "timestamp_ms": int(timestamp_ms),
        "holder": holder,
        "created_at": time.time(),
    }
    project["possession"].append(marker)   # kept in insertion order; sorted when derived
    return marker


def _frontier(project):
    """Furthest video point the app knows the user reached (closes the final segment)."""
    f = project.get("position_ms", 0) or 0
    for ev in project["events"]:
        f = max(f, ev["timestamp_ms"])
    for m in project.get("possession", []):
        f = max(f, m["timestamp_ms"])
    return f


def possession_totals(project):
    """Derive per-team possession time and percentage from the change markers.

    Percentage is team / (teamA + teamB); dead-ball time is excluded from %.
    The final open segment is closed at the frontier (furthest point reached).
    """
    markers = sorted(project.get("possession", []), key=lambda m: m["timestamp_ms"])
    dedup = []
    for m in markers:
        if dedup and dedup[-1]["holder"] == m["holder"]:
            continue
        dedup.append(m)
    frontier = _frontier(project)
    held = [0, 0]
    dead_ms = 0
    for i, m in enumerate(dedup):
        end = dedup[i + 1]["timestamp_ms"] if i + 1 < len(dedup) else frontier
        dur = max(0, end - m["timestamp_ms"])
        if m["holder"] in (0, 1):
            held[m["holder"]] += dur
        else:
            dead_ms += dur
    total = held[0] + held[1]
    pct = [round(held[i] / total * 100, 1) if total else 0.0 for i in (0, 1)]
    teams = project["teams"]
    return {
        "teams": [
            {"team": teams[0], "seconds": round(held[0] / 1000), "pct": pct[0]},
            {"team": teams[1], "seconds": round(held[1] / 1000), "pct": pct[1]},
        ],
        "dead_ball_seconds": round(dead_ms / 1000),
        "frontier_ms": frontier,
    }


def match_minute(video_ms, periods, breaks=()):
    """Numeric match-clock minute (base + elapsed) for a video position, or None
    if it falls in a break / before kickoff / after the match. In-half breaks
    (e.g. weather) are subtracted so the clock resumes where it paused."""
    v = int(video_ms)
    active = sorted((p for p in periods if p["video_start_ms"] is not None),
                    key=lambda p: p["video_start_ms"])
    if not active or v < active[0]["video_start_ms"]:
        return None
    inf = float("inf")
    for i, p in enumerate(active):
        start = p["video_start_ms"]
        nxt = active[i + 1]["video_start_ms"] if i + 1 < len(active) else None
        play_end = p["video_end_ms"] if p["video_end_ms"] is not None else (nxt if nxt is not None else inf)
        if start <= v <= play_end:
            paused, _ = _paused_ms(v, start, breaks)
            return p["base_min"] + (v - start - paused) / 60_000.0
    return None


def _team_index(project, team):
    teams = project["teams"]
    return 0 if team == teams[0] else (1 if team == teams[1] else None)


def _period_at(project, video_ms):
    """The period whose play range contains video_ms, else None."""
    v = int(video_ms)
    active = sorted((p for p in project["periods"] if p["video_start_ms"] is not None),
                    key=lambda p: p["video_start_ms"])
    inf = float("inf")
    for i, p in enumerate(active):
        start = p["video_start_ms"]
        nxt = active[i + 1]["video_start_ms"] if i + 1 < len(active) else None
        end = p["video_end_ms"] if p["video_end_ms"] is not None else (nxt if nxt is not None else inf)
        if start <= v <= end:
            return p
    return None


def goal_markers(project, goal_stat, mode):
    """Events of `goal_stat`, each tied to a chart window label plus a `frac`
    (0..1) giving its position within that window, so the chart can place
    several goals in the same window without overlap.
    mode is ("bin", minutes) for minute windows or ("half",) for per-period."""
    if not goal_stat:
        return []
    out = []
    for ev in project["events"]:
        if ev["stat"] != goal_stat:
            continue
        m = match_minute(ev["timestamp_ms"], project["periods"], project.get("breaks", []))
        ti = _team_index(project, ev["team"])
        if m is None or ti is None:
            continue
        if mode[0] == "bin":
            w = max(1, int(mode[1])); lo = int(m // w) * w
            label = f"{lo}\u2013{lo + w}"
            frac = (m - lo) / w
        else:
            p = _period_at(project, ev["timestamp_ms"])
            if p is None:
                continue
            label = PERIOD_SPECS[p["type"]]["label"]
            span = max(1, p["regulation_min"])
            frac = min(1.0, max(0.0, (m - p["base_min"]) / span))
        out.append({"label": label, "minute": round(m, 1), "team": ti, "frac": round(frac, 4)})
    return out


def timeline_series(project, stats, bin_minutes, goal_stat=None, xg=False):
    """Net (teamB - teamA) of the given stats per match-minute window.
    When xg=True, sums each shot's xG (using the shot stat) instead of counts."""
    bin_minutes = max(1, int(bin_minutes))
    statset = {project.get("shot_stat", "shots")} if xg else set(stats)
    marks = project.get("shot_marks", {})
    bins = {}
    for ev in project["events"]:
        if ev["stat"] not in statset:
            continue
        m = match_minute(ev["timestamp_ms"], project["periods"], project.get("breaks", []))
        if m is None:
            continue
        ti = _team_index(project, ev["team"])
        if ti is None:
            continue
        w = (mark_xg(marks.get(str(ev["id"]))) or 0) if xg else 1
        bins.setdefault(int(m // bin_minutes), [0, 0])[ti] += w
    teams = project["teams"]
    goals = goal_markers(project, goal_stat, ("bin", bin_minutes))
    maxbin = max(bins) if bins else -1
    for g in goals:
        maxbin = max(maxbin, int(g["minute"] // bin_minutes))
    if maxbin < 0:
        return {"labels": [], "net": [], "team_a": [], "team_b": [], "teams": teams, "goals": goals}
    labels, net, a, b = [], [], [], []
    rnd = (lambda v: round(v, 2)) if xg else (lambda v: v)
    for i in range(maxbin + 1):
        lo = i * bin_minutes
        labels.append(f"{lo}\u2013{lo + bin_minutes}")
        va, vb = bins.get(i, [0, 0])
        a.append(rnd(va)); b.append(rnd(vb)); net.append(rnd(vb - va))
    return {"labels": labels, "net": net, "team_a": a, "team_b": b, "teams": teams, "goals": goals}


def cumulative_series(project, stats, xg=False, goal_stat=None):
    """Running totals of the given stats for each team over match minutes.
    When xg=True, accumulates each shot's xG (using the shot stat)."""
    statset = {project.get("shot_stat", "shots")} if xg else set(stats)
    marks = project.get("shot_marks", {})
    pts = []
    for ev in project["events"]:
        if ev["stat"] not in statset:
            continue
        m = match_minute(ev["timestamp_ms"], project["periods"], project.get("breaks", []))
        if m is None:
            continue
        ti = _team_index(project, ev["team"])
        if ti is not None:
            w = (mark_xg(marks.get(str(ev["id"]))) or 0) if xg else 1
            pts.append((m, ti, w))
    pts.sort()
    ca = cb = 0
    a, b = [[0, 0]], [[0, 0]]
    for m, ti, w in pts:
        if ti == 0:
            ca += w
        else:
            cb += w
        a.append([round(m, 2), round(ca, 2)]); b.append([round(m, 2), round(cb, 2)])
    goals = [{"minute": round(g["minute"], 2), "team": g["team"]}
             for g in goal_markers(project, goal_stat, ("bin", 1))]
    return {"a": a, "b": b, "teams": project["teams"], "goals": goals}


def possession_by_window(project, mode, goal_stat=None):
    """Possession per match-clock window. mode is "5"/"10"/"15" (minute bins) or
    "half" (per period). Time outside any period's play is ignored."""
    if mode != "half":
        try:
            int(mode)
        except (TypeError, ValueError):
            mode = "10"   # tolerate a missing/garbled window rather than erroring
    teams = project["teams"]
    markers = sorted(project.get("possession", []), key=lambda m: m["timestamp_ms"])
    empty = {"labels": [], "team_a": [], "team_b": [], "pct_a": [], "pct_b": [], "teams": teams}
    if not markers:
        return empty
    frontier = _frontier(project)
    segs = []
    for i, m in enumerate(markers):
        end = markers[i + 1]["timestamp_ms"] if i + 1 < len(markers) else frontier
        if m["holder"] in (0, 1) and end > m["timestamp_ms"]:
            segs.append((m["timestamp_ms"], end, m["holder"]))
    active = sorted((p for p in project["periods"] if p["video_start_ms"] is not None),
                    key=lambda p: p["video_start_ms"])
    buckets, order = {}, []

    def add(key, label, holder, ms):
        if key not in buckets:
            buckets[key] = [0, 0]; order.append((key, label))
        buckets[key][holder] += ms

    inf = float("inf")
    for s, e, holder in segs:
        for idx, p in enumerate(active):
            pstart = p["video_start_ms"]
            nxt = active[idx + 1]["video_start_ms"] if idx + 1 < len(active) else None
            pend = p["video_end_ms"] if p["video_end_ms"] is not None else (nxt if nxt is not None else inf)
            os_, oe = max(s, pstart), min(e, pend)
            if oe <= os_:
                continue
            if mode == "half":
                add(p["type"], PERIOD_SPECS[p["type"]]["label"], holder, oe - os_)
            else:
                width = int(mode) * 60_000
                base = p["base_min"] * 60_000
                m0, m1 = base + (os_ - pstart), base + (oe - pstart)
                w = m0 // width
                while w * width < m1:
                    lo, hi = w * width, (w + 1) * width
                    overlap = min(m1, hi) - max(m0, lo)
                    if overlap > 0:
                        add(w, f"{lo // 60_000}\u2013{hi // 60_000}", holder, overlap)
                    w += 1
    order.sort(key=lambda kl: PERIOD_ORDER.index(kl[0]) if mode == "half" else kl[0])
    out = {"labels": [], "team_a": [], "team_b": [], "pct_a": [], "pct_b": [], "teams": teams}
    for key, label in order:
        am, bm = buckets[key]; tot = am + bm
        out["labels"].append(label)
        out["team_a"].append(round(am / 1000)); out["team_b"].append(round(bm / 1000))
        out["pct_a"].append(round(am / tot * 100, 1) if tot else 0)
        out["pct_b"].append(round(bm / tot * 100, 1) if tot else 0)
    out["goals"] = goal_markers(project, goal_stat, ("half",) if mode == "half" else ("bin", int(mode)))
    return out


def possession_spells(project):
    """Possession spells derived from the change markers: one entry per stretch
    of a single holder, clipped to each half's play window and split at period
    boundaries. Dead-ball stretches are kept as holder=null spells. Weather-break
    time inside a spell is measured (break_ms) and also removed in *_excl_break."""
    teams = project["teams"]
    breaks = _active_breaks(project)
    markers = sorted(project.get("possession", []), key=lambda m: m["timestamp_ms"])
    dedup = []
    for m in markers:
        if dedup and dedup[-1]["holder"] == m["holder"]:
            continue
        dedup.append(m)
    if not dedup:
        return []
    frontier = _frontier(project)
    raw = []   # (start, end, holder, next_holder)  next_holder == "END" for the last
    for i, m in enumerate(dedup):
        end = dedup[i + 1]["timestamp_ms"] if i + 1 < len(dedup) else frontier
        nxt = dedup[i + 1]["holder"] if i + 1 < len(dedup) else "END"
        if end > m["timestamp_ms"]:
            raw.append((m["timestamp_ms"], end, m["holder"], nxt))
    active = sorted((p for p in project["periods"] if p["video_start_ms"] is not None),
                    key=lambda p: p["video_start_ms"])
    inf = float("inf")
    spells = []
    for s, e, holder, nxt in raw:
        for idx, p in enumerate(active):
            pstart = p["video_start_ms"]
            after = active[idx + 1]["video_start_ms"] if idx + 1 < len(active) else None
            pend = p["video_end_ms"] if p["video_end_ms"] is not None else (after if after is not None else inf)
            os_, oe = max(s, pstart), min(e, pend)
            if oe <= os_:
                continue
            brk, _ = _paused_ms(oe, os_, breaks)
            if oe < e:
                ended = "period_end"
            elif nxt == "END":
                ended = "match_end"
            elif nxt is None:
                ended = "dead_ball"
            else:
                ended = "team_switch"
            dur = oe - os_
            spells.append({
                "holder": teams[holder] if holder in (0, 1) else None,
                "holder_index": holder if holder in (0, 1) else None,
                "start_ms": os_, "end_ms": oe,
                "duration_ms": dur, "break_ms": brk,
                "duration_sec": round(dur / 1000, 1),
                "duration_excl_break_sec": round(max(0, dur - brk) / 1000, 1),
                "period_type": p["type"], "ended_by": ended,
            })
    spells.sort(key=lambda s: s["start_ms"])
    return spells


def shot_buildups(project):
    """For every shot (the shot stat), the build-up time = shot time minus the
    start of the possession spell that contains it, with weather-break time
    removed. A shot inside a dead-ball stretch is flagged from_dead_ball (a shot
    straight off a set piece) and given a build-up of 0."""
    shot_stat = project.get("shot_stat", "shots")
    breaks = _active_breaks(project)
    spells = possession_spells(project)
    marks = project.get("shot_marks", {})
    out = []
    for ev in sorted(project["events"], key=lambda e: e["timestamp_ms"]):
        if ev["stat"] != shot_stat:
            continue
        ts = ev["timestamp_ms"]
        sp = next((s for s in spells if s["start_ms"] <= ts < s["end_ms"]), None)
        mc = match_clock(ts, project["periods"], project.get("breaks", []))
        rec = {
            "event_id": ev["id"], "team": ev["team"], "timestamp_ms": ts,
            "match_display": mc["display"] if mc["display"] != "\u2014" else ev["timestamp_display"],
            "from_dead_ball": False, "buildup_sec": None,
            "possession_start_ms": None, "possession_holder": None,
        }
        mk = marks.get(str(ev["id"]))
        if mk:
            rec["result"] = mk.get("result")
            rec["xg"] = mk.get("xg")
            rec["xg_model"] = mk.get("xg_model", project.get("xg_model", "logistic"))
        if sp is None:
            pass   # no possession logged around this shot -> build-up unknown
        elif sp["holder"] is None:
            rec["from_dead_ball"] = True
            rec["buildup_sec"] = 0.0
            rec["possession_start_ms"] = ts
        else:
            start = sp["start_ms"]
            brk, _ = _paused_ms(ts, start, breaks)
            rec["possession_start_ms"] = start
            rec["possession_holder"] = sp["holder"]
            rec["buildup_sec"] = round(max(0, (ts - start) - brk) / 1000, 1)
        out.append(rec)
    return out


def reorder_stats(project, stats):
    """Set a new order for the active stats. Must be a permutation of them."""
    if sorted(stats) != sorted(project["stats"]):
        raise ValueError("reorder_stats must be a permutation of current stats")
    project["stats"] = list(stats)
    return project["stats"]


def set_combined_stats(project, combos):
    """Define virtual stats that sum several real stats (used by charts)."""
    clean = []
    for c in combos or []:
        name = (c.get("name") or "").strip()
        parts = [p for p in c.get("parts", []) if p in project["stats"]]
        if name and parts:
            clean.append({"name": name, "parts": parts})
    project["combined_stats"] = clean
    return clean


# ---- shot map ---------------------------------------------------------------

SHOT_RESULTS = ("goal", "saved", "blocked", "post", "miss")


# --- expected goals (xG) ---------------------------------------------------
# Pure-Python application of the logistic model in xg_coefficients.json (fit
# separately on StatsBomb open data). No ML dependency; offline. Geometry is the
# StatsBomb 120x80 frame the model was trained in; we map the labeller's
# normalized half-pitch (x0,y0 in 0..1, y0=0 at the goal line) into it.
_XG_MODEL = None
_XG_GOAL = (120.0, 40.0)
_XG_POST_L = (120.0, 36.0)
_XG_POST_R = (120.0, 44.0)
# Keeper distance to goal (model units = StatsBomb yards) used when "GK off line"
# isn't marked, i.e. the keeper is assumed on/near the line.
DEFAULT_GK_DIST = 0.5


def _load_xg_model():
    global _XG_MODEL
    if _XG_MODEL is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xg_coefficients.json")
        try:
            with open(path) as f:
                _XG_MODEL = json.load(f)
        except (OSError, ValueError):
            _XG_MODEL = {}
    return _XG_MODEL


def _sb_xy(x0, y0):
    """Normalized half-pitch (0..1, y0=0 at goal line) -> StatsBomb 120x80 frame."""
    return 120.0 - y0 * 60.0, x0 * 80.0


def _dist_to_goal(x0, y0):
    x, y = _sb_xy(x0, y0)
    return math.hypot(_XG_GOAL[0] - x, _XG_GOAL[1] - y)


XG_MODELS = ("logistic", "nn")
_NN = None   # lazy cache: {"ok", "model", "mean", "scale", "fill", "features"} or {"ok": False}


def _xg_features(x0, y0, header=False, penalty=False, under_pressure=False,
                 free_kick=False, n_def=0, gk_dist=None):
    """The model's raw feature dict for a shot (distance/angle computed here)."""
    x, y = _sb_xy(x0, y0)
    dist = math.hypot(_XG_GOAL[0] - x, _XG_GOAL[1] - y)
    v1 = (_XG_POST_L[0] - x, _XG_POST_L[1] - y)
    v2 = (_XG_POST_R[0] - x, _XG_POST_R[1] - y)
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    n = math.hypot(*v1) * math.hypot(*v2) + 1e-9
    angle = math.acos(max(-1.0, min(1.0, dot / n)))
    return {
        "distance": dist, "angle": angle,
        "header": 1 if header else 0, "penalty": 1 if penalty else 0,
        "free_kick": 1 if free_kick else 0, "under_pressure": 1 if under_pressure else 0,
        "n_def_in_cone": int(n_def or 0),
        "gk_dist_to_goal": DEFAULT_GK_DIST if gk_dist is None else float(gk_dist),
    }


def _xg_logistic(feats):
    model = _load_xg_model()
    if not model:
        return None
    missing = [f for f in model["features"] if f not in feats]
    if missing:
        raise ValueError(f"model needs unknown features: {missing}")
    z = model["intercept"] + sum(model["coef"][f] * feats[f] for f in model["features"])
    return round(1.0 / (1.0 + math.exp(-z)), 4)


def xg_nn_available():
    """Cheap check (file present) so the UI can enable the NN toggle."""
    return os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "xg_model", "model.pt"))


def _load_nn():
    """Lazily load the PyTorch xG checkpoint (xg_model/model.pt). Cached.
    Needs torch installed; returns {'ok': False} (fall back to logistic) otherwise."""
    global _NN
    if _NN is not None:
        return _NN
    _NN = {"ok": False}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xg_model", "model.pt")
    if not os.path.exists(path):
        return _NN
    try:
        import torch
        import torch.nn as nn

        class _XGNet(nn.Module):   # must mirror xgnet.py exactly (state_dict key parity)
            def __init__(self, n_features, hidden_layers, dropout_rates, use_batchnorm):
                super().__init__()
                layers, in_dim = [], n_features
                for i, out_dim in enumerate(hidden_layers):
                    layers.append(nn.Linear(in_dim, out_dim))
                    if use_batchnorm:
                        layers.append(nn.BatchNorm1d(out_dim))
                    layers.append(nn.ReLU())
                    if dropout_rates[i] > 0:
                        layers.append(nn.Dropout(dropout_rates[i]))
                    in_dim = out_dim
                layers.append(nn.Linear(in_dim, 1))
                self.network = nn.Sequential(*layers)

            def forward(self, x):
                return self.network(x)

        ckpt = torch.load(path, map_location="cpu", weights_only=False)
        cfg = ckpt["config"]
        net = _XGNet(len(ckpt["features"]), cfg["hidden_layers"], cfg["dropout_rates"], cfg["use_batchnorm"])
        net.load_state_dict(ckpt["model_state_dict"])
        net.eval()
        _NN = {"ok": True, "model": net, "torch": torch,
               "mean": ckpt["scaler_mean"], "scale": ckpt["scaler_scale"],
               "fill": ckpt["imputer_fill"], "features": ckpt["features"]}
    except Exception as exc:
        _NN = {"ok": False, "error": str(exc)}
    return _NN


def _xg_nn(feats):
    nn_ = _load_nn()
    if not nn_.get("ok"):
        return None
    torch = nn_["torch"]
    f = dict(feats)
    if f.get("penalty"):          # match training's penalty freeze-frame imputation
        f["n_def_in_cone"] = 0
        f["gk_dist_to_goal"] = 0.7
    row = []
    for i, name in enumerate(nn_["features"]):
        v = f.get(name, float("nan"))
        if v != v:                # NaN -> imputer median
            v = nn_["fill"][i]
        row.append((v - nn_["mean"][i]) / nn_["scale"][i])
    with torch.no_grad():
        p = torch.sigmoid(nn_["model"](torch.tensor([row], dtype=torch.float32))).item()
    return round(p, 4)


def effective_xg_model(requested):
    """The model actually usable now — 'nn' only if the checkpoint loads, else 'logistic'."""
    return "nn" if (requested == "nn" and _load_nn().get("ok")) else "logistic"


def scored_xg(mark, model="logistic"):
    """(xg, effective_model) for a mark under the requested model, with fallback."""
    gk = DEFAULT_GK_DIST
    if mark.get("gk_off") and mark.get("gk_x0") is not None and mark.get("gk_y0") is not None:
        gk = _dist_to_goal(float(mark["gk_x0"]), float(mark["gk_y0"]))
    feats = _xg_features(float(mark["x0"]), float(mark["y0"]),
                         header=mark.get("header"), penalty=mark.get("penalty"),
                         under_pressure=mark.get("under_pressure"), free_kick=mark.get("free_kick"),
                         n_def=mark.get("n_def") or 0, gk_dist=gk)
    if model == "nn":
        v = _xg_nn(feats)
        if v is not None:
            return v, "nn"
    return _xg_logistic(feats), "logistic"


def xg_for_shot(x0, y0, header=False, penalty=False, under_pressure=False,
                free_kick=False, n_def=0, gk_dist=None, model="logistic"):
    """xG for a shot at normalized half-pitch coords (logistic by default)."""
    feats = _xg_features(x0, y0, header, penalty, under_pressure, free_kick, n_def, gk_dist)
    if model == "nn":
        v = _xg_nn(feats)
        if v is not None:
            return v
    return _xg_logistic(feats)


def xg_for_mark(mark, model="logistic"):
    return scored_xg(mark, model)[0]


def mark_xg(mark):
    """xG stored on a mark, recomputed (with the mark's own model) if absent."""
    if not mark:
        return None
    if mark.get("xg") is not None:
        return mark["xg"]
    return xg_for_mark(mark, mark.get("xg_model", "logistic"))


def recompute_all_xg(project):
    """Re-score every mark under the project's current xG model (used on toggle)."""
    model = project.get("xg_model", "logistic")
    for m in project.get("shot_marks", {}).values():
        m["xg"], m["xg_model"] = scored_xg(m, model)
    return model


def set_xg_model(project, model):
    if model not in XG_MODELS:
        raise ValueError("xg_model must be 'logistic' or 'nn'")
    project["xg_model"] = model
    recompute_all_xg(project)
    return model


def shot_queue(project, shot_stat=None):
    """Shots to mark: every event of the shot stat, in chronological order,
    each with its current mark (or None)."""
    stat = shot_stat or project.get("shot_stat", "shots")
    marks = project.get("shot_marks", {})
    out = []
    for ev in sorted(project["events"], key=lambda e: e["timestamp_ms"]):
        if ev["stat"] != stat:
            continue
        out.append({
            "event_id": ev["id"],
            "team": ev["team"],
            "team_index": _team_index(project, ev["team"]),
            "timestamp_ms": ev["timestamp_ms"],
            "minute": match_minute(ev["timestamp_ms"], project["periods"], project.get("breaks", [])),
            "mark": marks.get(str(ev["id"])),
        })
    return out


def set_shot_mark(project, event_id, mark):
    """Store a shot's pitch positions + result. Coordinates are normalized 0..1
    (attacking-direction-normalized half pitch). End point may be omitted."""
    if mark.get("result") not in SHOT_RESULTS:
        raise ValueError("invalid shot result")

    def coord(v):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0.0 <= v <= 1.0):
            raise ValueError("coordinates must be numbers within 0..1")
        return float(v)

    rec = {"x0": coord(mark["x0"]), "y0": coord(mark["y0"]), "result": mark["result"]}
    x1, y1 = mark.get("x1"), mark.get("y1")
    if x1 is None or y1 is None:
        rec["x1"] = rec["y1"] = None
    else:
        rec["x1"], rec["y1"] = coord(x1), coord(y1)
    rec["header"] = bool(mark.get("header", False))
    rec["penalty"] = bool(mark.get("penalty", False))
    rec["under_pressure"] = bool(mark.get("under_pressure", False))
    rec["free_kick"] = bool(mark.get("free_kick", False))
    rec["n_def"] = max(0, min(5, int(mark.get("n_def") or 0)))
    rec["gk_off"] = bool(mark.get("gk_off", False))
    if rec["gk_off"] and mark.get("gk_x0") is not None and mark.get("gk_y0") is not None:
        rec["gk_x0"], rec["gk_y0"] = coord(mark["gk_x0"]), coord(mark["gk_y0"])
        rec["gk_dist"] = round(_dist_to_goal(rec["gk_x0"], rec["gk_y0"]), 2)
    else:
        rec["gk_x0"] = rec["gk_y0"] = None
        rec["gk_dist"] = DEFAULT_GK_DIST
    rec["xg"], rec["xg_model"] = scored_xg(rec, project.get("xg_model", "logistic"))
    project["shot_marks"][str(event_id)] = rec
    return rec


def clear_shot_mark(project, event_id):
    project["shot_marks"].pop(str(event_id), None)
    return project["shot_marks"]


def shots_for_team(project, team_index, shot_stat=None):
    """Marked shots for one team, for the output pitch plot."""
    stat = shot_stat or project.get("shot_stat", "shots")
    team = project["teams"][team_index]
    marks = project.get("shot_marks", {})
    out = []
    for ev in sorted(project["events"], key=lambda e: e["timestamp_ms"]):
        if ev["stat"] != stat or ev["team"] != team:
            continue
        m = marks.get(str(ev["id"]))
        if m:
            out.append({**m, "event_id": ev["id"],
                        "minute": match_minute(ev["timestamp_ms"], project["periods"], project.get("breaks", []))})
    return out


def team_xg_totals(project):
    """Summed xG of each team's marked shots. Empty if no model/marks."""
    stat = project.get("shot_stat", "shots")
    marks = project.get("shot_marks", {})
    totals = {t: 0.0 for t in project["teams"]}
    for ev in project["events"]:
        if ev["stat"] != stat or ev["team"] not in totals:
            continue
        xg = mark_xg(marks.get(str(ev["id"])))
        if xg is not None:
            totals[ev["team"]] += xg
    return {t: round(v, 2) for t, v in totals.items()}


def apply_template(project, stats, combined_stats=None):
    """Replace the active stat set with `stats` (e.g. from a saved template).

    Dropped stats are archived (events kept); any template stat that was
    archived is restored. Optionally also sets combined stats.
    """
    stats = list(dict.fromkeys(s for s in stats if s))   # dedup, keep order
    for s in project["stats"]:
        if s not in stats and s not in project["archived_stats"]:
            project["archived_stats"].append(s)
    project["archived_stats"] = [s for s in project["archived_stats"] if s not in stats]
    project["stats"] = stats
    if combined_stats is not None:
        set_combined_stats(project, combined_stats)
    return project["stats"]


def _templates_path(projects_dir):
    return os.path.join(projects_dir, "_templates.json")


def load_templates(projects_dir):
    path = _templates_path(projects_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_templates(projects_dir, templates):
    os.makedirs(projects_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=projects_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(templates, fh, indent=2)
        os.replace(tmp, _templates_path(projects_dir))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return templates


def upsert_template(projects_dir, name, stats, combined_stats=None):
    """Save (or overwrite) a named template of stats + combined stats."""
    name = (name or "").strip()
    if not name:
        raise ValueError("template name required")
    if not stats:
        raise ValueError("template needs at least one stat")
    templates = [t for t in load_templates(projects_dir) if t.get("name") != name]
    templates.append({"name": name, "stats": list(stats),
                      "combined_stats": list(combined_stats or [])})
    templates.sort(key=lambda t: t["name"].lower())
    return save_templates(projects_dir, templates)


def delete_template(projects_dir, name):
    templates = [t for t in load_templates(projects_dir) if t.get("name") != name]
    return save_templates(projects_dir, templates)


# ---- cross-match comparison -------------------------------------------------

def match_label(project):
    """Logical match name: explicit label, else the video filename stem.
    Part-videos of one match (e.g. a weather suspension) share a label."""
    lbl = (project.get("match_label") or "").strip()
    if lbl:
        return lbl
    video = project.get("video", "")
    return os.path.splitext(video)[0] or video


def _resolve_team_index(project, tracked_name):
    """Which side (0/1) is the tracked team in this project, matched by name."""
    if not tracked_name:
        return None
    want = tracked_name.strip().lower()
    for i, name in enumerate(project["teams"]):
        if (name or "").strip().lower() == want:
            return i
    return None


def match_components(project, team_index, spec):
    """Raw (numerator, denominator) pieces for one project, so part-videos can
    be summed before a ratio is taken. den is None for plain counts."""
    counts = derive_counts(project)
    c = counts[project["teams"][team_index]]
    kind = spec.get("kind")
    if kind == "success":
        made = sum(c.get(s, 0) for s in spec.get("made", []))
        att = sum(c.get(s, 0) for s in spec.get("attempts", []))
        return (made, att)
    if kind == "possession":
        secs = possession_totals(project)["teams"]
        return (secs[team_index]["seconds"], secs[0]["seconds"] + secs[1]["seconds"])
    return (sum(c.get(s, 0) for s in spec.get("stats", [])), None)


def cross_match_series(projects, tracked_name, spec):
    """One value per logical match for the tracked team. Projects sharing a
    match label are aggregated (counts/seconds/made/att summed) before any %."""
    ratio = spec.get("kind") in ("possession", "success")
    groups, order, missing = {}, [], []
    for project in projects:
        ti = _resolve_team_index(project, tracked_name)
        if ti is None:
            missing.append(project.get("video"))
            continue
        label = match_label(project)
        if label not in groups:
            groups[label] = {"num": 0, "den": 0, "videos": []}
            order.append(label)
        num, den = match_components(project, ti, spec)
        groups[label]["num"] += num
        groups[label]["den"] += (den or 0)
        groups[label]["videos"].append(project.get("video"))
    series = []
    for label in order:
        g = groups[label]
        value = (round(100 * g["num"] / g["den"], 1) if g["den"] else 0) if ratio else g["num"]
        series.append({"label": label, "value": value, "videos": g["videos"]})
    return {"series": series, "missing_team": missing, "tracked_team": tracked_name, "ratio": ratio}


def _settings_path(projects_dir):
    return os.path.join(projects_dir, "_settings.json")


def load_settings(projects_dir):
    path = _settings_path(projects_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_settings(projects_dir, settings):
    os.makedirs(projects_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=projects_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
        os.replace(tmp, _settings_path(projects_dir))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return settings


def set_charts(project, charts):
    project["charts"] = list(charts)
    return project["charts"]


def set_chart_score(project, score):
    project["chart_score"] = str(score)
    return project["chart_score"]


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
    """Render the derived counts as CSV: one row per team, one column per stat,
    plus possession seconds and percentage."""
    counts = derive_counts(project)
    poss = {p["team"]: p for p in possession_totals(project)["teams"]}
    xg = team_xg_totals(project)
    has_xg = any(v for v in xg.values())
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    header = ["team"] + project["stats"]
    if has_xg:
        header += ["xg"]
    header += ["possession_seconds", "possession_percent"]
    writer.writerow(header)
    for team in project["teams"]:
        p = poss.get(team, {"seconds": 0, "pct": 0.0})
        row = [team] + [counts[team][stat] for stat in project["stats"]]
        if has_xg:
            row += [xg.get(team, 0.0)]
        row += [p["seconds"], p["pct"]]
        writer.writerow(row)
    return buffer.getvalue()


def events_for_view(project):
    """Events with match-clock fields added, for the UI log and JSON export.

    `timestamp_ms`/`timestamp_display` stay as the raw video position;
    match-clock fields are derived from the current periods.
    """
    periods = project["periods"]
    breaks = project.get("breaks", [])
    shot_stat = project.get("shot_stat", "shots")
    marks = project.get("shot_marks", {})
    out = []
    for ev in project["events"]:
        mc = match_clock(ev["timestamp_ms"], periods, breaks)
        display = mc["display"] if mc["display"] != "—" else ev["timestamp_display"]
        entry = {**ev, "match_display": display, "phase": mc["phase"],
                 "period_type": mc["period_type"], "period_label": mc["label"]}
        if ev["stat"] == shot_stat:
            m = marks.get(str(ev["id"]))
            if m:
                entry["xg"] = mark_xg(m)
                entry["xg_model"] = m.get("xg_model", project.get("xg_model", "logistic"))
        out.append(entry)
    return out


def to_events_json(project):
    """Render the events export as a JSON string."""
    teams = project["teams"]
    payload = {
        "video": project["video"],
        "teams": teams,
        "team_colors": project.get("team_colors", []),
        "stats": project["stats"],
        "archived_stats": project.get("archived_stats", []),
        "xg_model": project.get("xg_model", "logistic"),
        "periods": project["periods"],
        "events": events_for_view(project),
        "possession": {
            "changes": [
                {"timestamp_ms": m["timestamp_ms"],
                 "timestamp_display": format_timestamp(m["timestamp_ms"]),
                 "holder": teams[m["holder"]] if m["holder"] in (0, 1) else None}
                for m in sorted(project.get("possession", []), key=lambda m: m["timestamp_ms"])
            ],
            "totals": possession_totals(project),
        },
        "possession_spells": possession_spells(project),
        "shots_buildup": shot_buildups(project),
        "breaks": [
            {"video_start_ms": b.get("video_start_ms"), "video_end_ms": b.get("video_end_ms")}
            for b in project.get("breaks", [])
        ],
    }
    return json.dumps(payload, indent=2)


def save_project(project, projects_dir):
    """Persist the full project atomically.

    Writes to a UNIQUE temp file (not a shared one), then atomically renames.
    A unique temp is essential: under the threaded dev server two saves can run
    at once, and a shared temp would let them clobber each other and leave a
    corrupt file. Each writer now lays down its own complete file.
    """
    os.makedirs(projects_dir, exist_ok=True)
    path = _project_path(project["video"], projects_dir)
    fd, tmp = tempfile.mkstemp(dir=projects_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(project, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def load_project(video, projects_dir):
    """Load a saved project, or return a fresh one if none exists yet.

    Migrates older files: backfill archived_stats, and convert a single
    kickoff_ms into a two-period structure (first-half kick = old kickoff).
    Tolerates a legacy 'Extra data' corruption (valid object + leftover tail)
    by recovering the leading object.
    """
    path = _project_path(video, projects_dir)
    if not os.path.exists(path):
        return new_project(video)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        project = json.loads(text)
    except json.JSONDecodeError:
        project, _ = json.JSONDecoder().raw_decode(text.lstrip())
    project.setdefault("archived_stats", [])
    project.setdefault("team_colors", ["#ff9d3d", "#3db5ff"])
    project.setdefault("possession", [])
    project.setdefault("charts", [])
    project.setdefault("chart_score", "")
    project.setdefault("combined_stats", [])
    project.setdefault("match_label", "")
    project.setdefault("shot_stat", "shots")
    project.setdefault("shot_marks", {})
    project.setdefault("breaks", [])
    project.setdefault("board", {"tokens": [], "marks": []})
    project.setdefault("xg_model", "logistic")
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
