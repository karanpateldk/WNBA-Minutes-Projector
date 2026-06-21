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
app.py          Main Streamlit UI
model.py        Minutes projection + redistribution logic
scraper.py      RotoWire & ESPN data scrapers
roster_data.py  Static fallback rosters (2025 season)
test_model.py   Sanity checks (run before first launch)
data/           Auto-created cache folder (refreshes every 2h)

HOW IT WORKS
------------
1. Select a team in the sidebar
2. The app scrapes RotoWire for recent minutes trends
   and ESPN for the current injury report
3. Each player shows their projected minutes based on
   a weighted blend: 45% last-3-game avg + 35% season avg
4. To adjust a player's status, use the dropdown under their name:
   Active → full minutes
   Probable → -3%
   Day-To-Day → -20%
   Questionable → shows a second "duration" dropdown:
     Just listed  → -20%
     < 1 week     → -28%
     1-3 weeks    → -45%
     Chronic      → -60%
   Doubtful → -70%
   Out → 0 min — lineup auto-redistributes

WHEN A PLAYER IS OUT
--------------------
- The model finds the most similar position backup
- Gives them ~60% of the vacated minutes
- Spreads the remaining 40% proportionally to all active players
- If no good replacement exists, the UI shows a "Suggested replacement"
  you can promote with the dropdown
- Total always re-normalizes to 200 min (5 players × 40 min)

QUARTER ROTATION CHART
-----------------------
Estimates minutes per quarter using:
  Starters: 30% / 20% / 30% / 20%  (heavier Q1 & Q3)
  Bench:    15% / 30% / 15% / 40%  (heavier Q2 & Q4)
These are approximations; WNBA rotations vary more than NBA.

DATA FRESHNESS
--------------
Scraped data is cached for 2 hours.
Press "Refresh Data" in the sidebar to force a reload.
If scraping fails (ESPN/RotoWire down or blocked), the app
automatically falls back to the static 2025 season averages in roster_data.py.

UPDATE ROSTERS
--------------
Open roster_data.py and edit the ROSTERS dictionary.
Each player needs: pos, avg_min, role (starter/bench), depth (1/2/3)
