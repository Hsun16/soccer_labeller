# Touchline — manual soccer stat labeler

A local web app for logging soccer statistics against match video. Play the
video, tap a cell when something happens, and the event is recorded with the
video timestamp. Export events as JSON and team totals as CSV. Progress is
saved automatically, so you can quit and resume.

## Run it

1. Put your `.mp4` match videos in the `match_samples/` folder.
2. Install the one dependency:
   ```
   pip install -r requirements.txt
   ```
3. Start the server:
   ```
   python app.py
   ```
4. Open <http://localhost:5000> and pick a video from the dropdown.

## Using it

- **Log an event:** tap a team counter on any stat card. The event is
  timestamped at the video's current position.
- **Match clock & periods:** in the Periods panel, mark each half's kick
  (⚑) and whistle (⏹) by pressing the button at that moment in the video.
  The clock then shows true match time: it counts up during play, freezes at
  45:00 and shows `+1:00`, `+2:30`… during stoppage, reads `HT` during the
  break, and restarts at 45:00 for the second half — so one minute into
  first-half stoppage reads `45:00 +1:00` while one minute into the second
  half reads `46:00`. Injury time is automatic (whatever play runs past the
  45/90/105-minute regulation mark); you never type it in. For knockout games,
  press *Add extra time* to get two 15-minute periods (clock bases 90 and 105).
  Each event stores its raw video position, so re-marking a kick/whistle
  recomputes every match time correctly — nothing is baked in wrong. Mis-marked
  anchors (whistle before kick, overlaps) raise a non-blocking warning.
- **Playback:** Play/Pause (`Space`); skip buttons or `←`/`→` (±5s), plus
  ±10s and ±1m; the seek bar jumps anywhere; speed chips (0.5×–4×).
- **Stats grid:** each statistic is a card with a counter for each team. Cards
  wrap into as many rows as needed and the area scrolls, so any number of
  stats fits.
- **Custom stats:** type a name and press *Add* to append a card.
- **Delete a stat:** the × on a card sends it to the *Recycling bin* (hidden
  from the grid and CSV, events kept). From the bin you can *restore* it
  (counts included) or *delete* it permanently — permanent delete also removes
  its logged events and asks for confirmation.
- **Team names & colours:** edit the two boxes; existing events follow a
  rename. Each team has a colour picker that recolours its counters, log
  entries, and the possession frame.
- **Possession:** tap a team's possession button to give them the ball at the
  current video time, or *Dead ball* during stoppages/breaks/injuries. A frame
  around the video shows who holds the ball at the playhead (so it's also
  correct when scrubbing back). Possession % (teamA / (teamA+teamB), dead-ball
  time excluded) is shown live and written into both exports. Tap *Dead ball*
  at the final whistle for the cleanest totals.
- **Undo:** removes the most recent action, whether a stat event or a
  possession change.
- **Export:** *Export JSON* (every event with its raw video time and derived
  match-clock time, plus the periods config and archived stats) and
  *Export CSV* (one row per team, one column per active stat).
- **Resume:** reopen the same video — it returns to where you left off with
  events, periods, possession, and your chart board intact. Progress is keyed
  per video filename in `projects/`.

## Charts

Switch to the *Charts* tab (top bar) to build visualizations from the match
you've labelled. Pick a chart type, configure it, and *Add to board*:

- **Net timeline** — diverging bars of one team minus the other for a chosen
  stat (or stats), binned into match-clock windows. Optional **goal markers**
  drop a dashed line, a team-coloured dot, and the minute at each goal.
- **Cumulative line** — running totals per team over match minutes.
- **Comparison bars** — team A vs team B across chosen stats; check stats and
  use ↑/↓ to set their order.
- **Head-to-head** — a "match stats" panel: pick any stats (including
  **possession**, shown as %) and order them with ↑/↓; each row shows the two
  teams' values as diverging bars (each row scaled to its own larger value),
  counts labelled in fixed side gutters (always visible, even on full bars). A
  *stat label size* control sizes the row labels — *auto* fits them to the
  plot, or pick small/medium/large.
- **Possession by period** — stacked possession share per match-clock window;
  choose the window (5 / 10 / 15 minutes, or per half). Supports **goal
  markers** too.
- **Doughnut** — possession (A vs B), a stat's share between teams, or a
  success rate. Success rate takes **one or more** "made" stats and one or more
  "attempts" stats and sums them, so you can chart a joint rate (e.g. all
  crosses) even when you track left/right separately.

**Combined stats**: in the charts panel you can define a virtual stat that sums
several real ones (e.g. *attack* = attack left + right + center). Combined stats
then appear in the comparison, head-to-head, and doughnut-share pickers.

In the labelling grid you can **drag stat cards by their grip (⠿) to reorder**
them, and **click any event's timestamp** in the log to jump the video there.

**Templates**: once you've set up your stat list, click *Save current…* to store
it as a named template. On a new video, pick it and *Apply* to load that whole
stat set at once (your existing stats with logged events move to the recycle bin
rather than being lost). Templates are shared across all videos.

### Across matches

In the Charts area, the **Across matches** tab compares your own team's numbers
across games. Set your **tracked team** name once (matched in each match by team
name). Pick any subset of saved matches, choose a metric — a **stat total** (pick
one or several stats to sum), **possession %**, or a **success rate**
(made / attempts) — and plot one bar per match.

If a match was suspended and finished on another day across two videos, give both
videos the same **match label** (on the recording page, under Teams); they're
then aggregated into a single match in the comparison (counts, possession
seconds, and made/attempts all add up before any percentage is taken). Values
are raw match totals. Matches where the tracked team name isn't found are listed
and skipped.

Set a per-chart **legend size** (small/medium/large) so legends stay tidy on
small plots. Type a **score** for the footer. Reorder cards, cycle their size
(**half → wide → tall**, where tall is double height), remove them, or rename
titles inline. *Export* any single card to PNG, or *Export board* to composite
the whole board into one PNG. The board is saved with the project, so it's
there when you reopen the video.

Chart.js is bundled locally (`static/chart.umd.min.js`), so charting works
fully offline.

## Shot map

The **Shot map** view (top bar) shows a half-pitch per team (goal at top, each
team attacking upward) with every placed shot, colour-coded by result — saved,
blocked, hit post, missed — and goals drawn as a small ball, with a start→end
arrow.

Click **Mark shots** (or any row in the shot list) to open the marking view: the
video sits beside a clickable pitch. Scrub to each shot (−/+ 1/5/10 s buttons or
the arrow keys), click the pitch once for where the shot was taken and again for
where it ended, pick the result, and *Save & next* steps to the next unplaced
shot. *Prev/Next* move through the queue, *Clear shot* removes a placement, and
*Done* returns to the maps. Shots come from the `shots` events, so log every
attempt (including goals) with the shots counter; the outcome is chosen here.

Place shots **attacking-direction-normalized** — always toward the top goal —
regardless of which way the team played on video, so each team's shots gather at
one end. *Export both* saves the two pitches as one PNG. Marks are stored with
the project.

## How it's built

- `core.py` — all the logic (add/undo/derive counts/export/persistence),
  with **no web dependency** so it is unit-tested in isolation.
- `app.py` — Flask server. It is authoritative for state: every action
  persists to disk immediately and returns fresh state, so a crash or a
  closed tab never loses work. Video is streamed with HTTP Range support
  so seeking works.
- `templates/index.html` — the single-page UI.
- `test_core.py` — run with `python test_core.py` (14 tests).

## Deliberately left out (per the simplicity guideline — ask if you want them)

- Editing/deleting an arbitrary event (only last-event undo is supported).
- Redo.
- More than two teams (the app is two-team by design).

The event log panel and the keyboard shortcuts were small additions on top of
what was specified; both are easy to remove.
