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
- **Delete a stat (non-destructive):** the × on a card hides it from the grid
  and CSV but keeps its events in the JSON; it appears under *Archived* and a
  tap restores it (counts included).
- **Team names:** edit the two boxes; existing events follow the rename.
- **Undo:** removes the most recent event (tap repeatedly to go back further).
- **Export:** *Export JSON* (every event with its raw video time and derived
  match-clock time, plus the periods config and archived stats) and
  *Export CSV* (one row per team, one column per active stat).
- **Resume:** reopen the same video — it returns to where you left off with
  events, periods, and stat config intact. Progress is keyed per video
  filename in `projects/`.

## How it's built

- `core.py` — all the logic (add/undo/derive counts/export/persistence),
  with **no web dependency** so it is unit-tested in isolation.
- `app.py` — Flask server. It is authoritative for state: every action
  persists to disk immediately and returns fresh state, so a crash or a
  closed tab never loses work. Video is streamed with HTTP Range support
  so seeking works.
- `templates/index.html` — the single-page UI.
- `test_core.py` — run with `python test_core.py` (14 tests).


