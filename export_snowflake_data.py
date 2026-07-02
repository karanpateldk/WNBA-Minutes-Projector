"""
Export Snowflake data to CSV files for Streamlit Cloud.

Run locally (where Snowflake is accessible):
    python export_snowflake_data.py

This generates 3 CSV files in the data/ folder:
  - snowflake_player_stats.csv  (per-player recent role + advanced stats)
  - snowflake_team_averages.csv (per-team role minute averages)
  - snowflake_injuries.csv      (current injury report with full context)

These CSVs are committed to GitHub and loaded by the app on Cloud when
Snowflake is not directly accessible (IP allowlisting prevents connection).

Run this script whenever you want to update the data:
    python export_snowflake_data.py
Then commit and push the generated CSVs.
"""

import csv
import json
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def run():
    import snowflake_connector as sf

    if not sf.is_available():
        print("ERROR: Snowflake not available. Run this script on your local machine.")
        return

    conn = sf.get_connection()
    cur = conn.cursor()
    print(f"Connected to Snowflake. Exporting data...")

    # ── 1. Player stats: recent_starter_pct + advanced signals ───────────────
    print("  Exporting player stats...")
    cur.execute("""
        WITH all_team_games AS (
            -- Get last 5 games per team
            SELECT s.game_id,
                   s.home_team_name AS team_name,
                   s.scheduled
            FROM SPORTRADAR.DBO.WNBA_SCHEDULE s
            WHERE s.season_type = 'REG' AND s.season_year = 2026
              AND s.game_status IN ('complete','closed')
            UNION ALL
            SELECT s.game_id,
                   s.away_team_name AS team_name,
                   s.scheduled
            FROM SPORTRADAR.DBO.WNBA_SCHEDULE s
            WHERE s.season_type = 'REG' AND s.season_year = 2026
              AND s.game_status IN ('complete','closed')
        ),
        team_recent AS (
            SELECT game_id, team_name,
                   ROW_NUMBER() OVER (PARTITION BY team_name ORDER BY scheduled DESC) AS rn
            FROM all_team_games
        ),
        recent_game_ids AS (
            SELECT DISTINCT game_id FROM team_recent WHERE rn <= 5
        ),
        recent_stats AS (
            SELECT
                g.PLAYER_FULL_NAME,
                g.TEAM_MARKET || ' ' || g.TEAM_NAME AS team_name,
                ROUND(AVG(CASE WHEN g.PLAYER_STARTER THEN 1.0 ELSE 0.0 END), 4)
                    AS recent_starter_pct,
                COUNT(*) AS recent_gp
            FROM SPORTRADAR.DBO.WNBA_GAMESUMMARY_PLAYERS g
            JOIN recent_game_ids rg ON g.game_id = rg.game_id
            WHERE g.PLAYER_PLAYED = TRUE
            GROUP BY 1, 2
            HAVING COUNT(*) >= 2
        ),
        season_stats AS (
            SELECT
                g.PLAYER_FULL_NAME,
                g.TEAM_MARKET || ' ' || g.TEAM_NAME AS team_name,
                ROUND(AVG(CASE WHEN g.PLAYER_STARTER THEN 1.0 ELSE 0.0 END), 4)
                    AS season_starter_pct,
                COUNT(*) AS season_gp,
                ROUND(AVG(
                    TRY_CAST(SPLIT_PART(g.PLAYER_STATISTICS_MINUTES,':',1) AS INT)*60 +
                    TRY_CAST(SPLIT_PART(g.PLAYER_STATISTICS_MINUTES,':',2) AS INT)
                )/60.0, 2) AS avg_minutes
            FROM SPORTRADAR.DBO.WNBA_GAMESUMMARY_PLAYERS g
            JOIN SPORTRADAR.DBO.WNBA_SCHEDULE s ON g.game_id = s.game_id
            WHERE s.season_type = 'REG' AND s.season_year = 2026
              AND s.game_status IN ('complete','closed')
              AND g.PLAYER_PLAYED = TRUE
              AND g.PLAYER_STATISTICS_MINUTES IS NOT NULL
            GROUP BY 1, 2
            HAVING COUNT(*) >= 3
        )
        SELECT
            s.PLAYER_FULL_NAME,
            s.team_name,
            s.season_starter_pct,
            s.season_gp,
            s.avg_minutes,
            COALESCE(r.recent_starter_pct, s.season_starter_pct) AS recent_starter_pct,
            COALESCE(r.recent_gp, 0) AS recent_gp
        FROM season_stats s
        LEFT JOIN recent_stats r
            ON s.PLAYER_FULL_NAME = r.PLAYER_FULL_NAME
            AND s.team_name = r.team_name
        ORDER BY s.team_name, s.PLAYER_FULL_NAME
    """)

    player_rows = cur.fetchall()
    player_cols = [d[0].lower() for d in cur.description]

    with open(DATA_DIR / "snowflake_player_stats.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(player_cols + ["exported_at"])
        ts = datetime.utcnow().isoformat()
        for row in player_rows:
            w.writerow(list(row) + [ts])
    print(f"    -> {len(player_rows)} players written to snowflake_player_stats.csv")

    # ── 2. Team role averages ─────────────────────────────────────────────────
    print("  Exporting team averages...")
    cur.execute("""
        SELECT
            g.TEAM_MARKET || ' ' || g.TEAM_NAME AS team_name,
            ROUND(AVG(CASE WHEN g.PLAYER_STARTER THEN
                TRY_CAST(SPLIT_PART(g.PLAYER_STATISTICS_MINUTES,':',1) AS INT)*60 +
                TRY_CAST(SPLIT_PART(g.PLAYER_STATISTICS_MINUTES,':',2) AS INT)
            END)/60.0, 2) AS avg_starter_mins,
            ROUND(AVG(CASE WHEN (g.PLAYER_STARTER IS NULL OR g.PLAYER_STARTER = FALSE) THEN
                TRY_CAST(SPLIT_PART(g.PLAYER_STATISTICS_MINUTES,':',1) AS INT)*60 +
                TRY_CAST(SPLIT_PART(g.PLAYER_STATISTICS_MINUTES,':',2) AS INT)
            END)/60.0, 2) AS avg_bench_mins,
            COUNT(DISTINCT g.game_id) AS games_played
        FROM SPORTRADAR.DBO.WNBA_GAMESUMMARY_PLAYERS g
        JOIN SPORTRADAR.DBO.WNBA_SCHEDULE s ON g.game_id = s.game_id
        WHERE s.season_type = 'REG' AND s.season_year = 2026
          AND s.game_status IN ('complete','closed')
          AND g.PLAYER_PLAYED = TRUE
          AND g.PLAYER_STATISTICS_MINUTES IS NOT NULL
        GROUP BY 1
        HAVING COUNT(DISTINCT g.game_id) >= 5
        ORDER BY 1
    """)

    team_rows = cur.fetchall()
    team_cols = [d[0].lower() for d in cur.description]

    with open(DATA_DIR / "snowflake_team_averages.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(team_cols + ["exported_at"])
        ts = datetime.utcnow().isoformat()
        for row in team_rows:
            w.writerow(list(row) + [ts])
    print(f"    -> {len(team_rows)} teams written to snowflake_team_averages.csv")

    # ── 3. Full boxscores — replaces ESPN boxscore API entirely ──────────────
    print("  Exporting boxscores...")
    cur.execute("""
        SELECT
            g.GAME_ID,
            g.SCHEDULED::DATE                                         AS game_date,
            g.TEAM_MARKET || ' ' || g.TEAM_NAME                      AS team_name,
            g.PLAYER_FULL_NAME,
            g.PLAYER_PLAYED,
            COALESCE(g.PLAYER_STARTER, FALSE)                        AS starter,
            COALESCE(
                TRY_CAST(SPLIT_PART(g.PLAYER_STATISTICS_MINUTES,':',1) AS INT)*60 +
                TRY_CAST(SPLIT_PART(g.PLAYER_STATISTICS_MINUTES,':',2) AS INT),
                0
            ) / 60.0                                                  AS minutes,
            COALESCE(g.PLAYER_STATISTICS_PERSONAL_FOULS, 0)          AS personal_fouls,
            COALESCE(g.PLAYER_STATISTICS_PLUS + g.PLAYER_STATISTICS_MINUS, 0)
                                                                      AS plus_minus
        FROM SPORTRADAR.DBO.WNBA_GAMESUMMARY_PLAYERS g
        JOIN SPORTRADAR.DBO.WNBA_SCHEDULE s ON g.GAME_ID = s.GAME_ID
        WHERE s.season_type = 'REG'
          AND s.season_year = 2026
          AND s.game_status IN ('complete', 'closed')
          AND g.PLAYER_PLAYED = TRUE
        ORDER BY g.SCHEDULED DESC, g.TEAM_MARKET, g.PLAYER_FULL_NAME
    """)

    box_rows = cur.fetchall()
    box_cols = [d[0].lower() for d in cur.description]

    with open(DATA_DIR / "snowflake_boxscores.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(box_cols + ["exported_at"])
        ts = datetime.utcnow().isoformat()
        for row in box_rows:
            w.writerow(list(row) + [ts])
    print(f"    -> {len(box_rows)} player-game rows written to snowflake_boxscores.csv")

    # ── 4. Injuries ───────────────────────────────────────────────────────────
    print("  Exporting injuries...")
    injuries = sf.get_all_injuries()

    with open(DATA_DIR / "snowflake_injuries.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["player_name", "status", "injury", "comment", "team",
                    "dnp_type", "exported_at"])
        ts = datetime.utcnow().isoformat()
        for name, info in sorted(injuries.items()):
            w.writerow([
                name,
                info.get("status", "Active"),
                info.get("injury", ""),
                info.get("comment", ""),
                info.get("team", ""),
                info.get("dnp_type", "injury"),
                ts,
            ])
    print(f"    -> {len(injuries)} injuries written to snowflake_injuries.csv")

    cur.close()
    print()
    print("Done. Now run:")
    print("  git add -f data/snowflake_*.csv")
    print("  git commit -m 'Update Snowflake data'")
    print("  git push origin main")
    print()
    print("Streamlit Cloud will pick up the new CSVs automatically.")
    print()
    print("Files exported:")
    print("  snowflake_player_stats.csv  - recent roles + season stats (197 players)")
    print("  snowflake_team_averages.csv - starter/bench minute averages (15 teams)")
    print("  snowflake_injuries.csv      - current injury report (with full comments)")
    print("  snowflake_boxscores.csv     - full game history replaces ESPN boxscore API")


if __name__ == "__main__":
    run()
