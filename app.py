"""Flask server for the soccer stats labeler.

Run:  python app.py   then open http://localhost:5000

The server is authoritative for state. Every mutation (event, undo,
config change, playback position) is applied to the project, persisted
to disk immediately, and the fresh project is returned. So whenever the
user quits, the last saved state is already on disk -- resume just loads.
"""

import functools
import os
import threading

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)

import core

HERE = os.path.dirname(os.path.abspath(__file__))
VIDEO_DIR = os.path.join(HERE, "match_samples")
PROJECTS_DIR = os.path.join(HERE, "projects")

app = Flask(__name__)

# Serializes all project-file access. The dev server is threaded (so the video
# stream doesn't block other requests), which means project read-modify-write
# could otherwise interleave and corrupt the file / lose updates. The video
# route is intentionally NOT locked so streaming stays concurrent.
_project_lock = threading.Lock()


def locked(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        with _project_lock:
            return view(*args, **kwargs)
    return wrapper


def list_videos():
    if not os.path.isdir(VIDEO_DIR):
        return []
    return sorted(f for f in os.listdir(VIDEO_DIR) if f.lower().endswith(".mp4"))


def resolve_video(name):
    """Return an absolute path inside VIDEO_DIR, or abort 404. Blocks traversal."""
    if name not in list_videos():
        abort(404)
    return os.path.join(VIDEO_DIR, name)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/videos")
def api_videos():
    return jsonify({"videos": list_videos()})


@app.route("/video/<path:name>")
def video(name):
    path = resolve_video(name)
    # conditional=True makes send_file honour HTTP Range requests (206),
    # which the browser needs in order to seek within the video.
    return send_file(path, mimetype="video/mp4", conditional=True)


@app.route("/api/project")
@locked
def api_project():
    video_name = request.args.get("video")
    if not video_name:
        abort(400, "missing video")
    project = core.load_project(video_name, PROJECTS_DIR)
    return jsonify(_view(project))


@app.route("/api/event", methods=["POST"])
@locked
def api_event():
    data = request.get_json(force=True)
    project = core.load_project(data["video"], PROJECTS_DIR)
    try:
        core.add_event(project, data["team"], data["stat"], data["timestamp_ms"])
    except ValueError as exc:
        abort(400, str(exc))
    core.save_project(project, PROJECTS_DIR)
    return jsonify(_view(project))


@app.route("/api/undo", methods=["POST"])
@locked
def api_undo():
    data = request.get_json(force=True)
    project = core.load_project(data["video"], PROJECTS_DIR)
    core.undo(project)
    core.save_project(project, PROJECTS_DIR)
    return jsonify(_view(project))


@app.route("/api/possession", methods=["POST"])
@locked
def api_possession():
    data = request.get_json(force=True)
    project = core.load_project(data["video"], PROJECTS_DIR)
    try:
        core.set_possession(project, data["timestamp_ms"], data.get("holder"))
    except ValueError as exc:
        abort(400, str(exc))
    core.save_project(project, PROJECTS_DIR)
    return jsonify(_view(project))


@app.route("/api/config", methods=["POST"])
@locked
def api_config():
    data = request.get_json(force=True)
    project = core.load_project(data["video"], PROJECTS_DIR)
    try:
        if "teams" in data:
            core.rename_teams(project, data["teams"])
        if "add_stat" in data:
            core.add_stat(project, data["add_stat"])
        if "delete_stat" in data:
            core.delete_stat(project, data["delete_stat"])
        if "restore_stat" in data:
            core.restore_stat(project, data["restore_stat"])
        if "purge_stat" in data:
            core.purge_stat(project, data["purge_stat"])
        if "team_colors" in data:
            core.set_team_colors(project, data["team_colors"])
        if "charts" in data:
            core.set_charts(project, data["charts"])
        if "reorder_stats" in data:
            core.reorder_stats(project, data["reorder_stats"])
        if "combined_stats" in data:
            core.set_combined_stats(project, data["combined_stats"])
        if "match_label" in data:
            project["match_label"] = (data["match_label"] or "").strip()
        if "shot_stat" in data:
            project["shot_stat"] = data["shot_stat"]
        if "set_shot_mark" in data:
            m = data["set_shot_mark"]
            core.set_shot_mark(project, m["event_id"], m["mark"])
        if "clear_shot_mark" in data:
            core.clear_shot_mark(project, data["clear_shot_mark"])
        if "chart_score" in data:
            core.set_chart_score(project, data["chart_score"])
        if "set_anchor" in data:
            a = data["set_anchor"]
            core.set_period_anchor(project, a["period"], a["which"], a.get("ms"))
        if data.get("add_extra_time"):
            core.add_extra_time(project)
        if data.get("remove_extra_time"):
            core.remove_extra_time(project)
        if data.get("add_break"):
            core.add_break(project)
        if "set_break_anchor" in data:
            a = data["set_break_anchor"]
            core.set_break_anchor(project, a["id"], a["which"], a.get("ms"))
        if "remove_break" in data:
            core.remove_break(project, data["remove_break"])
        if "set_board" in data:
            core.set_board(project, data["set_board"])
        if "set_xg_model" in data:
            core.set_xg_model(project, data["set_xg_model"])
        if "delete_event" in data:
            core.delete_event(project, data["delete_event"])
    except ValueError as exc:
        abort(400, str(exc))
    core.save_project(project, PROJECTS_DIR)
    return jsonify(_view(project))


@app.route("/api/templates")
@locked
def api_templates():
    return jsonify({"templates": core.load_templates(PROJECTS_DIR)})


@app.route("/api/templates", methods=["POST"])
@locked
def api_templates_post():
    data = request.get_json(force=True)
    action = data.get("action")
    try:
        if action == "save":
            core.upsert_template(PROJECTS_DIR, data["name"], data.get("stats", []),
                                 data.get("combined_stats", []))
        elif action == "delete":
            core.delete_template(PROJECTS_DIR, data["name"])
        elif action == "apply":
            project = core.load_project(data["video"], PROJECTS_DIR)
            core.apply_template(project, data.get("stats", []), data.get("combined_stats"))
            core.save_project(project, PROJECTS_DIR)
            return jsonify(_view(project))
        else:
            abort(400, "unknown action")
    except ValueError as exc:
        abort(400, str(exc))
    return jsonify({"templates": core.load_templates(PROJECTS_DIR)})


def _list_projects():
    """Load every saved project (skips internal _templates/_settings files)."""
    out = []
    if not os.path.isdir(PROJECTS_DIR):
        return out
    for fn in sorted(os.listdir(PROJECTS_DIR)):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        try:
            out.append(core.load_project(fn[:-5], PROJECTS_DIR))
        except (OSError, ValueError):
            continue
    return out


@app.route("/api/matches")
@locked
def api_matches():
    projects = _list_projects()
    tracked = core.load_settings(PROJECTS_DIR).get("tracked_team", "")
    matches = [{"video": p["video"], "label": core.match_label(p), "teams": p["teams"],
                "stats": p["stats"],
                "resolves": core._resolve_team_index(p, tracked) is not None} for p in projects]
    return jsonify({"matches": matches, "tracked_team": tracked})


@app.route("/api/settings", methods=["POST"])
@locked
def api_settings():
    data = request.get_json(force=True)
    settings = core.load_settings(PROJECTS_DIR)
    if "tracked_team" in data:
        settings["tracked_team"] = (data["tracked_team"] or "").strip()
    core.save_settings(PROJECTS_DIR, settings)
    return jsonify({"tracked_team": settings.get("tracked_team", "")})


@app.route("/api/crossmatch")
@locked
def api_crossmatch():
    videos = [v for v in request.args.get("videos", "").split(",") if v]
    kind = request.args.get("kind", "count")
    spec = {"kind": kind,
            "stats": [s for s in request.args.get("stats", "").split(",") if s],
            "made": [s for s in request.args.get("made", "").split(",") if s],
            "attempts": [s for s in request.args.get("attempts", "").split(",") if s]}
    tracked = core.load_settings(PROJECTS_DIR).get("tracked_team", "")
    projects = [core.load_project(v, PROJECTS_DIR) for v in videos]
    return jsonify(core.cross_match_series(projects, tracked, spec))


@app.route("/api/position", methods=["POST"])
@locked
def api_position():
    """Lightweight save of the current playback position for resume."""
    data = request.get_json(force=True)
    project = core.load_project(data["video"], PROJECTS_DIR)
    project["position_ms"] = int(data["position_ms"])
    core.save_project(project, PROJECTS_DIR)
    return ("", 204)


@app.route("/api/chartdata")
@locked
def api_chartdata():
    """Time-based chart series (binning uses the tested core)."""
    project = core.load_project(request.args["video"], PROJECTS_DIR)
    kind = request.args.get("kind")
    stats = [s for s in request.args.get("stats", "").split(",") if s]
    goal = request.args.get("goals") or None
    xg = request.args.get("xg") == "1"
    if kind == "timeline":
        return jsonify(core.timeline_series(project, stats, request.args.get("bin", 10), goal_stat=goal, xg=xg))
    if kind == "cumulative":
        return jsonify(core.cumulative_series(project, stats, xg=xg, goal_stat=goal))
    if kind == "possession":
        return jsonify(core.possession_by_window(project, request.args.get("window", "10"), goal_stat=goal))
    abort(400, "unknown kind")


@app.route("/api/xg")
def api_xg():
    """Live xG for a candidate shot, for the marking panel readout."""
    a = request.args
    try:
        m = {"x0": float(a["x0"]), "y0": float(a["y0"])}
    except (KeyError, ValueError):
        abort(400, "x0,y0 required")
    m["header"] = a.get("header") == "1"
    m["penalty"] = a.get("penalty") == "1"
    m["under_pressure"] = a.get("under_pressure") == "1"
    m["free_kick"] = a.get("free_kick") == "1"
    m["n_def"] = a.get("n_def", 0)
    if a.get("gk_off") == "1" and a.get("gk_x0") and a.get("gk_y0"):
        m["gk_off"] = True
        try:
            m["gk_x0"] = float(a["gk_x0"]); m["gk_y0"] = float(a["gk_y0"])
        except ValueError:
            abort(400, "bad gk coords")
    model = "nn" if a.get("model") == "nn" else "logistic"
    xg, used = core.scored_xg(m, model)
    return jsonify({"xg": xg, "xg_model": used})


@app.route("/api/export/json")
@locked
def export_json():
    project = core.load_project(request.args["video"], PROJECTS_DIR)
    return Response(
        core.to_events_json(project),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{_stem(project)}_events.json"'},
    )


@app.route("/api/export/csv")
@locked
def export_csv():
    project = core.load_project(request.args["video"], PROJECTS_DIR)
    return Response(
        core.to_csv(project),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{_stem(project)}_stats.csv"'},
    )


def _stem(project):
    return os.path.splitext(project["video"])[0]


def _view(project):
    """Project plus its derived counts, the shape the frontend renders from."""
    return {**project,
            "events": core.events_for_view(project),
            "counts": core.derive_counts(project),
            "xg_nn_available": core.xg_nn_available(),
            "warnings": core.validate_periods(project)}


if __name__ == "__main__":
    print("Soccer labeler running at http://localhost:5000")
    print(f"Drop .mp4 files into: {VIDEO_DIR}")
    app.run(debug=True, port=5000)
