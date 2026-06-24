"""
Explore what play-by-play signals are available for minute projection improvement.
Run: .\explore_snowflake.bat  (after updating explore_snowflake.bat to point here)
Or run directly.
"""
import os, winreg, sys, tempfile, platform
platform.libc_ver = lambda executable=None, lib='', version='', chunksize=16384: ('', '')

pat = os.getenv("SNOWFLAKE_PAT", "")
if not pat:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
        pat, _ = winreg.QueryValueEx(key, "SNOWFLAKE_PAT")
    except Exception:
        pass

import snowflake.connector
conn = snowflake.connector.connect(
    account="DRAFTKINGS-DRAFTKINGS", user="KAR.PATEL",
    authenticator="programmatic_access_token", token=pat,
    warehouse="QUERY_WH", database="SPORTRADAR", schema="DBO",
    insecure_mode=True,
)
cur = conn.cursor()

print("=" * 65)
print("1. OPPONENT PACE — avg possessions per game (2026 REG season)")
print("=" * 65)
cur.execute("""
    SELECT
        p.home_team_name AS team,
        COUNT(DISTINCT p.game_id) AS games,
        ROUND(COUNT(*) / COUNT(DISTINCT p.game_id), 1) AS avg_possessions_per_game
    FROM SPORTRADAR.DBO.WNBA_PLAYBYPLAY_POSSESSIONS p
    JOIN SPORTRADAR.DBO.WNBA_SCHEDULE s ON s.game_id = p.game_id
    WHERE s.season_type = 'REG'
      AND s.season_year = 2026
      AND p.period_sequence < 5
    GROUP BY 1
    ORDER BY 3 DESC
""")
rows = cur.fetchall()
print(f"  {'Team':<28} {'Games':>6} {'Avg Poss/Game':>14}")
print("  " + "-" * 52)
for r in rows:
    print(f"  {r[0]:<28} {r[1]:>6} {r[2]:>14.1f}")

print()
print("=" * 65)
print("2. CRUNCH TIME PLAYERS — on court when game within 5, last 5 min")
print("   (Indiana Fever example)")
print("=" * 65)
cur.execute("""
    SELECT
        p.value:full_name::varchar AS player_name,
        COUNT(*) AS crunch_possessions
    FROM SPORTRADAR.DBO.WNBA_PLAYBYPLAY_POSSESSIONS pos
    JOIN SPORTRADAR.DBO.WNBA_SCHEDULE s ON s.game_id = pos.game_id,
    LATERAL FLATTEN(pos.home_players) p
    WHERE s.season_type = 'REG'
      AND s.season_year = 2026
      AND s.home_team_name = 'Indiana Fever'
      AND pos.period_sequence = 4
      AND ABS(pos.home_points_end - pos.away_points_end) <= 5
      AND pos.game_clock_end <= '00:05:00'::TIME
      AND player_name IS NOT NULL
    GROUP BY 1
    ORDER BY 2 DESC
    LIMIT 10
""")
rows = cur.fetchall()
print(f"  {'Player':<28} {'Crunch Possessions':>20}")
print("  " + "-" * 50)
for r in rows:
    print(f"  {r[0]:<28} {r[1]:>20}")

print()
print("=" * 65)
print("3. PLAYER FOUL RATE VS SPECIFIC OPPONENT")
print("   (Indiana Fever players vs Minnesota Lynx)")
print("=" * 65)
cur.execute("""
    SELECT
        ps.player_full_name,
        COUNT(DISTINCT ps.game_id) AS games,
        SUM(CASE WHEN ps.type = 'personalfoul' THEN 1 ELSE 0 END) AS total_fouls,
        ROUND(SUM(CASE WHEN ps.type = 'personalfoul' THEN 1 ELSE 0 END)
              / NULLIF(COUNT(DISTINCT ps.game_id), 0), 2) AS fouls_per_game
    FROM SPORTRADAR.DBO.WNBA_PLAYBYPLAY_STATISTICS ps
    JOIN SPORTRADAR.DBO.WNBA_SCHEDULE s ON s.game_id = ps.game_id
    WHERE s.season_type = 'REG'
      AND s.season_year = 2026
      AND ps.team_name = 'Fever'
      AND (s.home_team_name = 'Minnesota Lynx' OR s.away_team_name = 'Minnesota Lynx')
    GROUP BY 1
    HAVING games >= 1
    ORDER BY fouls_per_game DESC
""")
rows = cur.fetchall()
print(f"  {'Player':<28} {'Games':>6} {'Fouls':>7} {'Per Game':>10}")
print("  " + "-" * 55)
for r in rows:
    print(f"  {r[0]:<28} {r[1]:>6} {r[2]:>7} {r[3]:>10.2f}")

print()
print("=" * 65)
print("4. BLOWOUT-DEPENDENT BENCH PLAYERS")
print("   (players whose minutes correlate with large margins)")
print("   Atlanta Dream bench players")
print("=" * 65)
cur.execute("""
    SELECT
        gp.player_full_name,
        ROUND(AVG(
            TRY_TO_NUMBER(SPLIT_PART(gp.player_statistics_minutes,':',1))
            + TRY_TO_NUMBER(SPLIT_PART(gp.player_statistics_minutes,':',2))/60.0
        ), 1) AS avg_min_all,
        ROUND(AVG(CASE
            WHEN ABS(s.home_team_points - s.away_team_points) >= 15
            THEN TRY_TO_NUMBER(SPLIT_PART(gp.player_statistics_minutes,':',1))
                 + TRY_TO_NUMBER(SPLIT_PART(gp.player_statistics_minutes,':',2))/60.0
        END), 1) AS avg_min_blowout,
        ROUND(AVG(CASE
            WHEN ABS(s.home_team_points - s.away_team_points) < 10
            THEN TRY_TO_NUMBER(SPLIT_PART(gp.player_statistics_minutes,':',1))
                 + TRY_TO_NUMBER(SPLIT_PART(gp.player_statistics_minutes,':',2))/60.0
        END), 1) AS avg_min_close,
        COUNT(*) AS games
    FROM SPORTRADAR.DBO.WNBA_GAMESUMMARY_PLAYERS gp
    JOIN SPORTRADAR.DBO.WNBA_SCHEDULE s ON s.game_id = gp.game_id
    WHERE s.season_type = 'REG'
      AND s.season_year = 2026
      AND gp.team_name = 'Dream'
      AND gp.player_starter = FALSE
      AND gp.player_played = TRUE
    GROUP BY 1
    HAVING COUNT(*) >= 5
    ORDER BY avg_min_all DESC
""")
rows = cur.fetchall()
print(f"  {'Player':<28} {'All':>5} {'Blowout':>8} {'Close':>7} {'Diff':>7} {'GP':>5}")
print("  " + "-" * 60)
for r in rows:
    avg_all = r[1] or 0
    blowout = r[2] or 0
    close   = r[3] or 0
    diff    = round((blowout or 0) - (close or 0), 1)
    print(f"  {r[0]:<28} {avg_all:>5.1f} {blowout:>8.1f} {close:>7.1f} {diff:>+7.1f} {r[4]:>5}")

conn.close()
input("\nPress Enter to exit...")
