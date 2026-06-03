"""Flask server for the soccer stats labeler.

Run:  python app.py   then open http://localhost:5000

The server is authoritative for state. Every mutation (event, undo,
config change, playback position) is applied to the project, persisted
to disk immediately, and the fresh project is returned. So whenever the
user quits, the last saved state is already on disk -- resume just loads.
"""

import os

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
def api_project():
    video_name = request.args.get("video")
    if not video_name:
        abort(400, "missing video")
    project = core.load_project(video_name, PROJECTS_DIR)
    return jsonify(_view(project))


@app.route("/api/event", methods=["POST"])
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
def api_undo():
    data = request.get_json(force=True)
    project = core.load_project(data["video"], PROJECTS_DIR)
    core.undo(project)
    core.save_project(project, PROJECTS_DIR)
    return jsonify(_view(project))


@app.route("/api/config", methods=["POST"])
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
        if "set_anchor" in data:
            a = data["set_anchor"]
            core.set_period_anchor(project, a["period"], a["which"], a.get("ms"))
        if data.get("add_extra_time"):
            core.add_extra_time(project)
        if data.get("remove_extra_time"):
            core.remove_extra_time(project)
    except ValueError as exc:
        abort(400, str(exc))
    core.save_project(project, PROJECTS_DIR)
    return jsonify(_view(project))


@app.route("/api/position", methods=["POST"])
def api_position():
    """Lightweight save of the current playback position for resume."""
    data = request.get_json(force=True)
    project = core.load_project(data["video"], PROJECTS_DIR)
    project["position_ms"] = int(data["position_ms"])
    core.save_project(project, PROJECTS_DIR)
    return ("", 204)


@app.route("/api/export/json")
def export_json():
    project = core.load_project(request.args["video"], PROJECTS_DIR)
    return Response(
        core.to_events_json(project),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{_stem(project)}_events.json"'},
    )


@app.route("/api/export/csv")
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
            "warnings": core.validate_periods(project)}


if __name__ == "__main__":
    print("Soccer labeler running at http://localhost:5000")
    print(f"Drop .mp4 files into: {VIDEO_DIR}")
    app.run(debug=True, port=5000)
