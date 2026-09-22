#!/usr/bin/env python3
"""Generate the standard match-report graphics and the first five slides.

Run from the labelling-app directory (the one containing app.py / core.py):

    python make_report.py --video ECFC_vs_Dutch_Lions.mp4 \
        --opponent "Chicago Dutch Lions" --date 06/27/2026

What it does:
  1. Starts the labelling app on a spare port (your normal one can stay open).
  2. Drives headless Chromium through the app's own chart rendering + PNG
     export, using the chart configs in report_config.json — so the graphics
     are pixel-for-pixel what you'd get clicking export yourself:
     stats overview (head-to-head), xG timeline, match momentum,
     attack sides, and the shot maps + legend.
     Your saved chart board is not touched: the report charts exist only in
     the browser page and are never saved to the project file.
  3. Renders the possession-spell boxplot with matplotlib (that one was never
     an app chart) from core.possession_spells().
  4. Fills report_template.pptx (slides 1-5 of the report) with the new
     graphics, title, and date -> Match_Report_vs_<Opponent>.pptx.

Requires: playwright (`pip install playwright && playwright install chromium`)
and matplotlib. Score shown on the graphics comes from the Score box on the
app's chart board (chart_score), so set it there before running.
"""

import argparse
import datetime
import io
import json
import os
import re
import sys
import threading
import time
import urllib.request
import zipfile
from xml.sax.saxutils import escape

import core

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- flask app

def start_app(port):
    import app as appmod
    threading.Thread(
        target=lambda: appmod.app.run(port=port, debug=False, use_reloader=False),
        daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/videos", timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    sys.exit(f"The labelling app did not come up on port {port}.")


# ------------------------------------------------------- app-chart exports

def export_app_graphics(port, video, cfg, outdir):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is not installed: pip install playwright && playwright install chromium")

    charts = cfg["charts"]
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            sys.exit(f"Could not launch Chromium ({exc}).\nRun: playwright install chromium")
        page = browser.new_context(accept_downloads=True,
                                   viewport={"width": 1600, "height": 1000}).new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        page.wait_for_function("document.querySelectorAll('#videoSelect option').length > 1")
        page.select_option("#videoSelect", video)
        page.wait_for_function("typeof project !== 'undefined' && project && project.video")
        # Deterministic exports: no animation to race against.
        page.evaluate("Chart.defaults.animation = false")

        # Report charts live only in the page; nothing here calls saveBoard(),
        # so the board saved in the project file is left exactly as it was.
        page.evaluate("cfgs => { project.charts = cfgs; }", [c["cfg"] for c in charts])
        page.evaluate("showView('charts')")
        page.wait_for_function(
            f"document.querySelectorAll('#board canvas').length === {len(charts)}")
        page.wait_for_timeout(500)

        # Match the template's export aspect: widen/narrow the window until a
        # full-width chart canvas is canvas_target_px wide (chart heights are
        # fixed by CSS, so width alone sets the aspect ratio).
        target = cfg.get("canvas_target_px", 1376)
        width = page.evaluate(
            "document.querySelector('#board .chart-card canvas').getBoundingClientRect().width")
        page.set_viewport_size({"width": int(1600 + target - width), "height": 1000})
        page.wait_for_timeout(500)

        for i, c in enumerate(charts):
            with page.expect_download() as dl:
                page.click(f'#board .chart-card[data-i="{i}"] .cact[data-act="png"]')
            dl.value.save_as(os.path.join(outdir, c["file"]))
            print(f"  exported {c['file']}")

        page.evaluate("showView('shots')")
        page.wait_for_timeout(400)
        with page.expect_download() as dl:
            page.click("#exportShots")
        dl.value.save_as(os.path.join(outdir, cfg["shot_maps"]["file"]))
        print(f"  exported {cfg['shot_maps']['file']}")
        # The legend bar stretches to the page; shrink it to its content so the
        # screenshot matches the template strip's proportions.
        page.eval_on_selector("#shotLegend", "el => el.style.width = 'fit-content'")
        page.locator("#shotLegend").screenshot(
            path=os.path.join(outdir, cfg["shot_legend"]["file"]))
        print(f"  exported {cfg['shot_legend']['file']}")
        browser.close()


# ---------------------------------------------------- possession boxplot

def possession_boxplot(project, path, min_sec):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    teams, colors = project["teams"], project["team_colors"]
    spells = [s for s in core.possession_spells(project) if s["holder"] is not None]
    durs = {t: [s["duration_excl_break_sec"] for s in spells if s["holder"] == t]
            for t in teams}
    kept = {t: [d for d in durs[t] if d >= min_sec] for t in teams}
    removed = {t: len(durs[t]) - len(kept[t]) for t in teams}

    score = (project.get("chart_score") or "").strip()
    m = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", score)
    head = (f"{teams[0]} {m.group(1)} \u2013 {m.group(2)} {teams[1]}" if m
            else f"{teams[0]} vs {teams[1]}")

    fig, ax = plt.subplots(figsize=(8.9, 5.86), dpi=150)
    bp = ax.boxplot([kept[t] for t in teams], positions=[0, 1], widths=0.55,
                    patch_artist=True, showfliers=False,
                    medianprops=dict(color="white", linewidth=2),
                    whiskerprops=dict(color="#888", linestyle="--"),
                    capprops=dict(color="#888"))
    rng = np.random.default_rng(42)
    for i, t in enumerate(teams):
        box = bp["boxes"][i]
        box.set_facecolor(colors[i]); box.set_alpha(0.32); box.set_edgecolor("none")
        y = np.array(kept[t])
        ax.scatter(i + rng.uniform(-0.12, 0.12, len(y)), y,
                   s=18, color=colors[i], alpha=0.75, zorder=3, linewidths=0)

    ax.set_xticks([0, 1])
    ax.set_xticklabels(teams, fontsize=12, fontweight="bold")
    for lab, c in zip(ax.get_xticklabels(), colors):
        lab.set_color(c)
    ax.set_ylabel("Possession duration (seconds)", fontsize=11)
    ax.set_title(f"{head}\nPossession spell duration (\u2265 {min_sec}s spells only)",
                 fontsize=12, fontweight="bold", color="#2c2c2a", pad=14)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e6e6e2", linewidth=0.8)
    ax.set_axisbelow(True)
    total_removed = sum(removed.values())
    ax.annotate(f"Removed {total_removed} spell(s) < {min_sec}s  "
                f"{teams[0]}: {removed[teams[0]]}  |  {teams[1]}: {removed[teams[1]]}",
                xy=(0.5, -0.13), xycoords="axes fraction", ha="center",
                fontsize=9, style="italic", color="#777")
    fig.subplots_adjust(bottom=0.16)
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    print(f"  rendered {os.path.basename(path)}")


# --------------------------------------------------------- deck assembly

def build_deck(template_path, out_path, media_files, title, date):
    """Copy the 5-slide template, swapping in the new graphics and title."""
    src = zipfile.ZipFile(template_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename in media_files:
                with open(media_files[item.filename], "rb") as fh:
                    data = fh.read()
            elif item.filename == "ppt/slides/slide1.xml":
                data = (data.decode("utf-8")
                        .replace("{{MATCH_TITLE}}", escape(title))
                        .replace("{{MATCH_DATE}}", escape(date))
                        .encode("utf-8"))
            out.writestr(item, data)
    src.close()


def pad_to_template_aspect(template_path, media_name, path):
    """Pad an image (with its own background color) to the aspect ratio of the
    template graphic it replaces, so the slide placement doesn't stretch it.
    Used for the shot legend strip, whose width varies with fonts."""
    try:
        from PIL import Image
    except ImportError:
        return
    with zipfile.ZipFile(template_path) as src:
        ow, oh = Image.open(io.BytesIO(src.read(media_name))).size
    im = Image.open(path)
    w, h = im.size
    target_w = round(h * ow / oh)
    if target_w <= w:
        return
    out = Image.new("RGBA", (target_w, h), im.convert("RGBA").getpixel((1, 1)))
    out.paste(im, ((target_w - w) // 2, 0))
    out.save(path)


def check_aspects(template_path, media_files):
    """Warn if a new graphic's aspect ratio drifted from the template's
    (the slide placements stretch images to fixed boxes)."""
    try:
        from PIL import Image
    except ImportError:
        return
    src = zipfile.ZipFile(template_path)
    for name, path in media_files.items():
        old = Image.open(io.BytesIO(src.read(name))).size
        new = Image.open(path).size
        drift = abs(new[0] / new[1] - old[0] / old[1]) / (old[0] / old[1])
        if drift > 0.03:
            print(f"  WARNING: {os.path.basename(path)} aspect {new} differs from "
                  f"template {old} by {drift:.0%}; it will look stretched on the slide.")
    src.close()


def resolve_chart_stats(cfg, project):
    """Combined-stat names vary between projects (e.g. 'Attacks' vs 'attacks').
    Match each chart's 'combo:NAME' reference to the project's actual combined
    stat case-insensitively, and warn about stats the project doesn't have."""
    combos = {c["name"].lower(): c["name"] for c in project.get("combined_stats", [])}
    known = set(project["stats"]) | {"__xg__", "__possession__"}
    for chart in cfg["charts"]:
        stats = chart["cfg"].get("stats")
        if not stats:
            continue
        fixed = []
        for s in stats:
            if s.startswith("combo:"):
                name = combos.get(s[6:].lower())
                if name is None:
                    print(f"  WARNING: no combined stat like '{s[6:]}' in this project; "
                          f"'{chart['file']}' will show 0 for it.")
                    fixed.append(s)
                else:
                    fixed.append("combo:" + name)
            else:
                if s not in known:
                    print(f"  WARNING: stat '{s}' not in this project; "
                          f"'{chart['file']}' will show 0 for it.")
                fixed.append(s)
        chart["cfg"]["stats"] = fixed


# ----------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--video", required=True, help="video filename, e.g. ECFC_vs_Dutch_Lions.mp4")
    ap.add_argument("--opponent", help="opponent name for the title slide (default: team B)")
    ap.add_argument("--date", help="match date for the title slide (default: today)")
    ap.add_argument("--out", help="output directory (default: reports/<video stem>/)")
    ap.add_argument("--config", default=os.path.join(HERE, "report_config.json"))
    ap.add_argument("--port", type=int, default=5050)
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    project = core.load_project(args.video, os.path.join(HERE, "projects"))
    if not project.get("events"):
        sys.exit(f"No events found for {args.video} — is the video name right?")
    if not (project.get("chart_score") or "").strip():
        print("NOTE: no score set on the chart board (Score box); graphics will omit it.")

    opponent = args.opponent or project["teams"][1]
    date = args.date or datetime.date.today().strftime("%m/%d/%Y")
    stem = os.path.splitext(args.video)[0]
    outdir = args.out or os.path.join(HERE, "reports", stem)
    os.makedirs(outdir, exist_ok=True)

    print(f"Match: {project['teams'][0]} vs {project['teams'][1]}  "
          f"(score: {project.get('chart_score') or '?'})")
    print("Starting the labelling app + headless browser...")
    resolve_chart_stats(cfg, project)
    start_app(args.port)
    export_app_graphics(args.port, args.video, cfg, outdir)

    pb = cfg["possession_boxplot"]
    possession_boxplot(project, os.path.join(outdir, pb["file"]), pb["min_spell_sec"])

    media_files = {c["media"]: os.path.join(outdir, c["file"]) for c in cfg["charts"]}
    for key in ("shot_maps", "shot_legend", "possession_boxplot"):
        media_files[cfg[key]["media"]] = os.path.join(outdir, cfg[key]["file"])
    template = os.path.join(HERE, cfg["template"])
    pad_to_template_aspect(template, cfg["shot_legend"]["media"],
                           os.path.join(outdir, cfg["shot_legend"]["file"]))
    check_aspects(template, media_files)

    safe_opp = re.sub(r"[^\w-]+", "_", opponent).strip("_")
    deck = os.path.join(outdir, f"Match_Report_vs_{safe_opp}.pptx")
    build_deck(template, deck, media_files,
               f"{project['teams'][0]} vs. {opponent}", date)
    print(f"\nDone. Graphics + deck in {outdir}")
    print(f"  {os.path.basename(deck)}  (slides 1-5 filled; add your analysis slides)")


if __name__ == "__main__":
    main()
