WNBA MINUTES PROJECTOR
======================

REQUIREMENTS
------------
Python 3.10 or newer
Download from: https://www.python.org/downloads/

QUICK START
-----------
1. Open a terminal in this folder (wnba_minutes)
2. Run: setup.bat
   OR manually:
      python -m pip install -r requirements.txt
      streamlit run app.py
3. Browser opens at http://localhost:8501

FILES
-----
app.py                  Main Streamlit UI
model.py                Minutes projection + redistribution logic
season_stats.py         Per-player rolling stats from Snowflake CSVs
snowflake_connector.py  Snowflake connection + CSV fallback helpers
export_snowflake_data.py  Exports Snowflake data to CSV (run via GitHub Actions)
wnba_scraper.py         Live roster, lineup, and injury scraping (ESPN/RotoWire)
quarter_minutes.py      Per-quarter minute distribution logic
roster_data.py          Static fallback rosters
backtest.py             Accuracy backtesting tool


HOW IT WORKS
------------
1. Select a team in the sidebar.

2. Data is loaded from Snowflake-exported CSVs (refreshed automatically
   4x per day via GitHub Actions on a self-hosted runner):
     - snowflake_boxscores.csv     full game-by-game minutes for every player
     - snowflake_player_stats.csv  season + recent starter percentages
     - snowflake_team_averages.csv team-level starter/bench minute averages
     - snowflake_injuries.csv      current injury report

   If the CSVs are unavailable, the app falls back to ESPN and RotoWire
   scrapers for rosters and injuries. Projections may be less accurate
   in fallback mode since live scraping is less reliable than Snowflake data.

3. Each player's projected minutes use a sample-size-aware rolling average
   of their clean season average and last-3-game median:

     < 5 games:  100% season average (no recent signal yet)
     5-10 games:  70% season / 30% last-3-game average
     10-20 games: 55% season / 45% last-3-game average
     20-30 games: 40% season / 60% last-3-game average
     30+ games:   25% season / 75% last-3-game average

   When a player's last-3 average diverges 20%+ from their season average
   (e.g. a role change or injury return), the model boosts weight toward
   recent games by up to an additional 15% to capture the new trend.

   The most recent single game is blended in as an additional signal
   (40% of the last-3 weight) for players with enough history.

   Blowout games and foul-trouble games are excluded from the clean averages
   used in these blends to filter noise from unusual rotations.

   Bench players who DNP 40%+ of their games since joining this team have
   their projection scaled down by their DNP rate — they are spot-use players
   whose expected contribution per game is lower than their per-game average.

4. Normalization: all active player projections are trimmed proportionally
   — players projected furthest above their own season average give back
   the most minutes — so no single player crowds out teammates.
   Total always sums to exactly 200 minutes (5 players x 40 min).


INJURY STATUS EFFECTS
---------------------
   Active / Probable / Day-To-Day / Questionable
     → Full projected minutes (play probability is uncertain but if they
       play, they play normal minutes). Exception: if the injury note
       explicitly mentions a "minutes restriction," a 25% haircut applies.

   Doubtful
     → Treated as DNP — minutes set to 0 and redistributed to teammates.

   Out
     → Minutes set to 0 and redistributed to teammates.

There is no secondary duration dropdown. Doubtful and Out are the only
statuses that affect projected minutes.


WHEN A PLAYER IS OUT
--------------------
The model redistributes vacated minutes using the best available data:

  Primary (when available): Snowflake "without player" game logs.
    The model looks up historical games where this player did not play
    and uses the actual observed minutes each teammate got in those games.
    Projection = 60% observed without-player average + 40% current projection.

  Fallback (when historical data is insufficient):
    Vacated minutes are distributed proportionally across all active players
    based on their current projected share of total minutes.
    Starters are capped at 36 min, bench at 38 min.

Total always re-normalizes to 200 min after redistribution.


QUARTER MINUTES BREAKDOWN
--------------------------
Each player's per-quarter minutes are projected using their own historical
quarter-by-quarter averages from Snowflake play-by-play data (Q1-Q4 minutes
per game exported in snowflake_boxscores.csv).

Per-quarter averages use a 75% last-3-game median / 25% season average blend,
with foul-trouble and outlier games filtered out. This means a player whose
Q4 role has changed recently (e.g. being rested in blowouts) will reflect
that trend quickly. The historical shape is then scaled proportionally to
match the player's total projected minutes, capped at 10 min per quarter.


DATA FRESHNESS
--------------
Snowflake CSVs are automatically updated 4x per day (10am, 2pm, 6pm, 10pm ET)
by a GitHub Actions workflow running on a local self-hosted runner.
The app always reads the latest committed CSVs — no manual refresh needed.

Live roster and injury data (ESPN/RotoWire) is cached for 6 hours and
refreshed automatically on the next app load after expiry.

Note: if scraping from ESPN or RotoWire fails (site down or blocked),
the app falls back to Snowflake CSV injury data. A warning will appear
in the app when fallback mode is active.


UPDATE DATA
-----------
To manually trigger a data refresh:
  GitHub → Actions → "Update Snowflake Data" → Run workflow

This re-runs the export script on the self-hosted runner, pulling the latest
rosters, injury reports, boxscores (with per-quarter minutes), player stats,
and team averages from Snowflake, then commits the updated CSVs to the repo.
Streamlit Cloud picks up the new data automatically on next load.

PAT ROTATION: The Snowflake Programmatic Access Token expires monthly.
When it expires, update the SNOWFLAKE_PAT secret in:
  GitHub → repo → Settings → Secrets and variables → Actions
