"""
Snowflake data source for WNBA statistics using Sportradar data.

Views used:
  SPORTRADAR.DBO.WNBA_SCHEDULE           → completed regular-season game list per team
  SPORTRADAR.DBO.WNBA_GAMESUMMARY        → final scores (blowout detection)
  SPORTRADAR.DBO.WNBA_GAMESUMMARY_PLAYERS → per-player minutes, fouls, starter, DNP,
                                             and per-period breakdown (PLAYER_STATISTICS_PERIODS)
  SPORTRADAR.DBO.WNBA_PLAYBYPLAY         → fallback only, if period breakdown is unavailable

Key schema facts:
  - GAME_ID is a Sportradar UUID (e.g. f9d3aad2-45ac-4f65-8e75-734ac3de27eb)
  - Team names use full display names matching app's ESPN_TEAM_IDS keys (e.g. "Indiana Fever")
    BUT team_market + team_name are separate in GAMESUMMARY_PLAYERS (e.g. "Indiana" + "Fever")
  - PLAYER_STATISTICS_MINUTES is "MM:SS" format (e.g. "32:14")
  - PLAYER_STATISTICS_PERSONAL_FOULS is a clean integer column
  - PLAYER_STARTER is a boolean column
  - PLAYER_NOT_PLAYING_REASON / PLAYER_NOT_PLAYING_DESCRIPTION indicate DNP
  - PLAYER_STATISTICS_PERIODS is a VARIANT with per-period stats including minutes

Credentials: add to .streamlit/secrets.toml:
    [snowflake]
    account   = "your-account.region"
    user      = "your_user"
    password  = "your_password"
    warehouse = "your_warehouse"
    database  = "SPORTRADAR"
    schema    = "DBO"

Or set environment variables: SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD,
SNOWFLAKE_WAREHOUSE, SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict

# Windows App Store Python sandbox fix:
# The snowflake connector calls platform.libc_ver() which tries to open
# sys.executable as a binary — but the App Store python.exe is a stub that
# raises [Errno 22]. On Windows, libc_ver() is meaningless (Linux-only),
# so patch it to return empty before the connector is imported.
import platform as _platform
import sys as _sys
if "WindowsApps" in _sys.executable:
    _platform.libc_ver = lambda executable=None, lib='', version='', chunksize=16384: ('', '')

QUARTER_SECONDS = 600  # 10 min per quarter in seconds

# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

_connection = None


def _get_credentials() -> dict:
    """
    Read credentials from environment variable or Streamlit secrets.
    Auth method: Programmatic Access Token (PAT) via SNOWFLAKE_PAT env var.
    PAT is passed as the token with authenticator='oauth'.
    Never store the token in a file.
    """
    pat = os.getenv("SNOWFLAKE_PAT", "") or os.getenv("SNOWFLAKE_TOKEN", "")

    # Read all config from Streamlit secrets first, then env vars as fallback.
    # Nothing is hardcoded here — credentials live only in secrets.toml or env.
    base = {
        "account":   "",
        "user":      "",
        "warehouse": "",
        "database":  "SPORTRADAR",
        "schema":    "DBO",
    }
    try:
        import streamlit as st
        sf = st.secrets.get("snowflake", {})
        for k in ("account", "user", "warehouse", "database", "schema"):
            if sf.get(k):
                base[k] = sf[k]
    except Exception:
        pass

    # Env vars override secrets (useful for CI/testing)
    base["account"]   = os.getenv("SNOWFLAKE_ACCOUNT",   base["account"])
    base["user"]      = os.getenv("SNOWFLAKE_USER",      base["user"])
    base["warehouse"] = os.getenv("SNOWFLAKE_WAREHOUSE", base["warehouse"])
    base["database"]  = os.getenv("SNOWFLAKE_DATABASE",  base["database"])
    base["schema"]    = os.getenv("SNOWFLAKE_SCHEMA",    base["schema"])

    base["pat"] = pat
    return base


def get_connection():
    """
    Return a Snowflake connection using PAT auth, or None if unavailable.
    Uses st.cache_resource when running inside Streamlit so the connection
    is shared across sessions (not recreated per user). Falls back to a
    module-level singleton when running outside Streamlit (backtest CLI, etc.).
    """
    global _connection

    creds = _get_credentials()
    if not creds.get("pat") or not creds.get("account"):
        return None

    # Try st.cache_resource path (Streamlit Cloud / app context)
    try:
        import streamlit as st

        @st.cache_resource
        def _cached_connection(account, user, warehouse, database, schema, pat):
            import snowflake.connector
            return snowflake.connector.connect(
                account=account,
                user=user,
                authenticator="programmatic_access_token",
                token=pat,
                warehouse=warehouse,
                database=database,
                schema=schema,
                client_session_keep_alive=True,
                insecure_mode=True,
            )

        return _cached_connection(
            creds["account"], creds["user"], creds["warehouse"],
            creds["database"], creds["schema"], creds["pat"],
        )
    except Exception:
        pass

    # Outside Streamlit (CLI tools like backtest.py) — module-level singleton
    if _connection is not None:
        try:
            _connection.cursor().execute("SELECT 1")
            return _connection
        except Exception:
            _connection = None

    try:
        import snowflake.connector
        _connection = snowflake.connector.connect(
            account=creds["account"],
            user=creds["user"],
            authenticator="programmatic_access_token",
            token=creds["pat"],
            warehouse=creds["warehouse"],
            database=creds["database"],
            schema=creds["schema"],
            client_session_keep_alive=True,
            insecure_mode=True,
        )
        return _connection
    except Exception as e:
        print(f"[snowflake] Connection failed: {e}")
        return None


def is_available() -> bool:
    """True if Snowflake credentials exist and connection succeeds."""
    return get_connection() is not None


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """Execute SQL and return rows as list of lowercase-keyed dicts."""
    conn = get_connection()
    if conn is None:
        return []
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        print(f"[snowflake] Query failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Team name helpers
# ---------------------------------------------------------------------------

# Sportradar splits team into market + name (e.g. "Indiana" + "Fever").
# We store the combined full name in our app. Build a lookup both ways.
_SR_TEAM_FULL_NAMES = {
    "Atlanta Dream":           ("Atlanta",       "Dream"),
    "Chicago Sky":             ("Chicago",       "Sky"),
    "Connecticut Sun":         ("Connecticut",   "Sun"),
    "Dallas Wings":            ("Dallas",        "Wings"),
    "Golden State Valkyries":  ("Golden State",  "Valkyries"),
    "Indiana Fever":           ("Indiana",       "Fever"),
    "Las Vegas Aces":          ("Las Vegas",     "Aces"),
    "Los Angeles Sparks":      ("Los Angeles",   "Sparks"),
    "Minnesota Lynx":          ("Minnesota",     "Lynx"),
    "New York Liberty":        ("New York",      "Liberty"),
    "Phoenix Mercury":         ("Phoenix",       "Mercury"),
    "Portland Fire":           ("Portland",      "Fire"),
    "Seattle Storm":           ("Seattle",       "Storm"),
    "Toronto Tempo":           ("Toronto",       "Tempo"),
    "Washington Mystics":      ("Washington",    "Mystics"),
}

# Reverse: (market, name) -> full_name
_SR_MARKET_NAME_TO_FULL = {v: k for k, v in _SR_TEAM_FULL_NAMES.items()}


def _sr_team_name_filter(team_name: str) -> str:
    """
    Return a SQL WHERE snippet value for matching team_name column in
    WNBA_GAMESUMMARY_PLAYERS. That column contains just the short name
    (e.g. "Fever"), so we extract the team short name.
    """
    parts = _SR_TEAM_FULL_NAMES.get(team_name)
    return parts[1] if parts else team_name.split()[-1]


def _full_team_name(market: str, name: str) -> str:
    """Reconstruct full team name from Sportradar market + name fields."""
    return _SR_MARKET_NAME_TO_FULL.get((market, name), f"{market} {name}")


# ---------------------------------------------------------------------------
# Injuries: from WNBA_ROSTER_CURRENT PLAYER variant (primary source)
# ---------------------------------------------------------------------------

def get_all_injuries() -> dict:
    """
    Return {player_full_name: {"status": str, "injury": str, "team": str, "dnp_type": str}}
    for all players with an active injury listed in WNBA_ROSTER_CURRENT.

    The PLAYER variant contains an "injuries" array with:
      - status: "Out", "Day To Day", "Questionable", "Probable", etc.
      - desc: injury description e.g. "Knee", "Back", "Coach Decision"
      - comment: free-text game note e.g. "Clark is Probable for Monday's game"
      - update_date: most recent update

    dnp_type: "coach" if desc contains "Coach" (healthy scratch),
              "injury" otherwise. Lets the model treat them differently.
    """
    rows = _query(
        """
        SELECT
            roster_market || ' ' || roster_name  AS team_full,
            player:full_name::varchar             AS player_name,
            player:injuries                       AS injuries_variant
        FROM SPORTRADAR.DBO.WNBA_ROSTER_CURRENT
        WHERE player:injuries IS NOT NULL
          AND ARRAY_SIZE(player:injuries) > 0
        """
    )

    result = {}
    for r in rows:
        name        = r["player_name"] or ""
        team        = r["team_full"] or ""
        inj_raw     = r["injuries_variant"]
        if not name or not inj_raw:
            continue
        try:
            injuries = inj_raw if isinstance(inj_raw, list) else json.loads(str(inj_raw))
            if not injuries:
                continue
            # Take the most recently updated injury
            inj = max(injuries, key=lambda x: x.get("update_date", ""))
            status_raw  = str(inj.get("status", "")).strip()
            desc        = str(inj.get("desc", "")).strip()
            comment     = str(inj.get("comment", "")).strip()

            status = _normalize_injury_status(status_raw)
            dnp_type = "coach" if "coach" in desc.lower() else "injury"

            result[name] = {
                "status":   status,
                "injury":   desc,
                "comment":  comment,
                "team":     team,
                "dnp_type": dnp_type,
            }
        except Exception:
            continue
    return result


def _normalize_injury_status(raw: str) -> str:
    """Map Sportradar injury status strings to app status values."""
    mapping = {
        "out":        "Out",
        "day to day": "Day-To-Day",
        "dtd":        "Day-To-Day",
        "questionable": "Questionable",
        "probable":   "Probable",
        "doubtful":   "Doubtful",
        "active":     "Active",
    }
    return mapping.get(raw.lower().strip(), "Questionable")


# ---------------------------------------------------------------------------
# Plus/minus: season averages per player from WNBA_GAMESUMMARY_PLAYERS
# ---------------------------------------------------------------------------

def get_player_plus_minus(team_name: str, season_year: int = 2026) -> dict[str, float]:
    """
    Return {player_full_name: avg_plus_minus} for all players on the team
    with at least 3 games played. Used to adjust confidence scores.
    """
    short_name = _sr_team_name_filter(team_name)
    rows = _query(
        """
        SELECT
            player_full_name,
            ROUND(AVG(player_statistics_pls_min), 2) AS avg_pm,
            COUNT(*) AS gp
        FROM SPORTRADAR.DBO.WNBA_GAMESUMMARY_PLAYERS
        WHERE team_name  = %s
          AND scheduled >= %s
          AND player_played = TRUE
          AND player_statistics_pls_min IS NOT NULL
        GROUP BY player_full_name
        HAVING COUNT(*) >= 3
        """,
        (short_name, f"{season_year}-05-01"),
    )
    return {r["player_full_name"]: float(r["avg_pm"]) for r in rows if r["player_full_name"]}


# ---------------------------------------------------------------------------
# Schedule: completed regular-season games per team
# ---------------------------------------------------------------------------

def _current_season_year() -> int:
    """WNBA season runs May-Oct. Jan-Apr = still in prior season's offseason."""
    from datetime import date
    today = date.today()
    return today.year if today.month >= 5 else today.year - 1

CURRENT_SEASON_YEAR = _current_season_year()


def get_games_for_team(team_name: str, season_year: int = CURRENT_SEASON_YEAR) -> list[tuple[str, str]]:
    """
    Return list of (sr_game_id, date_str) for completed regular-season games,
    oldest first. date_str is 'YYYY-MM-DD'.
    Uses SPORTRADAR.DBO.WNBA_SCHEDULE.
    """
    rows = _query(
        """
        SELECT
            game_id,
            TO_VARCHAR(scheduled::DATE, 'YYYY-MM-DD') AS game_date
        FROM SPORTRADAR.DBO.WNBA_SCHEDULE
        WHERE season_type  = 'REG'
          AND season_year  = %s
          AND game_status  IN ('complete', 'closed')
          AND (home_team_name = %s OR away_team_name = %s)
        ORDER BY scheduled ASC
        """,
        (season_year, team_name, team_name),
    )
    return [(r["game_id"], r["game_date"]) for r in rows]


def get_game_margin(sr_game_id: str, team_name: str) -> float:
    """
    Point differential (team - opponent) from final score.
    Uses SPORTRADAR.DBO.WNBA_SCHEDULE home/away points.
    """
    rows = _query(
        """
        SELECT home_team_name, away_team_name, home_team_points, away_team_points
        FROM SPORTRADAR.DBO.WNBA_SCHEDULE
        WHERE game_id = %s
        LIMIT 1
        """,
        (sr_game_id,),
    )
    if not rows:
        return 0.0
    r = rows[0]
    try:
        if r["home_team_name"] == team_name:
            return float(r["home_team_points"] or 0) - float(r["away_team_points"] or 0)
        else:
            return float(r["away_team_points"] or 0) - float(r["home_team_points"] or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Roster: player list with positions from WNBA_ROSTER_CURRENT
# ---------------------------------------------------------------------------

# Sportradar position codes → app position codes
_SR_POS_MAP = {
    "G":   "G",   "G-F": "G/F", "F-G": "G/F",
    "F":   "F",   "F-C": "F/C", "C-F": "F/C",
    "C":   "C",   "NA":  "?",   "":    "?",
}


def get_roster(team_name: str) -> dict[str, dict]:
    """
    Return {player_full_name: {"pos": str}} for all active roster members.
    Uses WNBA_ROSTER_CURRENT PLAYER variant — position, jersey, status.
    """
    parts = _SR_TEAM_FULL_NAMES.get(team_name)
    if not parts:
        return {}
    market, short = parts
    rows = _query(
        """
        SELECT
            player:full_name::varchar   AS full_name,
            player:position::varchar    AS position,
            player:status::varchar      AS status
        FROM SPORTRADAR.DBO.WNBA_ROSTER_CURRENT
        WHERE roster_market = %s
          AND roster_name   = %s
        """,
        (market, short),
    )
    result = {}
    for r in rows:
        name = r["full_name"] or ""
        if not name:
            continue
        pos_raw = str(r["position"] or "").upper()
        pos = _SR_POS_MAP.get(pos_raw, pos_raw or "?")
        result[name] = {"pos": pos}
    return result


def get_all_players_sf() -> list[str]:
    """
    Return sorted list of all active WNBA players across all teams.
    Used for the manual-add dropdown.
    """
    rows = _query(
        """
        SELECT DISTINCT player:full_name::varchar AS full_name
        FROM SPORTRADAR.DBO.WNBA_ROSTER_CURRENT
        WHERE player:status::varchar = 'ACT'
          AND player:full_name IS NOT NULL
        ORDER BY full_name
        """
    )
    return [r["full_name"] for r in rows if r["full_name"]]


# ---------------------------------------------------------------------------
# Today's schedule: find tonight's game for a team
# ---------------------------------------------------------------------------

def get_todays_game(team_name: str) -> tuple[str, str, str]:
    """
    Return (sr_game_id, opponent_full_name, scheduled_time_str) for today's game.
    Returns ("", "", "") if no game today.
    Uses WNBA_SCHEDULE — checks scheduled date = today (UTC).
    """
    rows = _query(
        """
        SELECT
            game_id,
            home_team_name,
            away_team_name,
            TO_VARCHAR(scheduled, 'HH24:MI') AS tip_time,
            game_status
        FROM SPORTRADAR.DBO.WNBA_SCHEDULE
        WHERE season_type = 'REG'
          AND scheduled::DATE = CURRENT_DATE
          AND (home_team_name = %s OR away_team_name = %s)
        LIMIT 1
        """,
        (team_name, team_name),
    )
    if not rows:
        return "", "", ""
    r = rows[0]
    opponent = r["away_team_name"] if r["home_team_name"] == team_name else r["home_team_name"]
    return r["game_id"], opponent, r["tip_time"] or ""


# ---------------------------------------------------------------------------
# Opponent margins: for blowout/pace profiling in matchup.py
# ---------------------------------------------------------------------------

def get_team_margins(team_name: str, season_year: int = CURRENT_SEASON_YEAR) -> list[float]:
    """
    Return list of point differentials (team - opponent) for all completed
    regular-season games. Used by matchup.py for blowout/pace profiling.
    """
    rows = _query(
        """
        SELECT
            home_team_name,
            away_team_name,
            home_team_points,
            away_team_points
        FROM SPORTRADAR.DBO.WNBA_SCHEDULE
        WHERE season_type  = 'REG'
          AND season_year  = %s
          AND game_status  IN ('complete', 'closed')
          AND (home_team_name = %s OR away_team_name = %s)
        ORDER BY scheduled ASC
        """,
        (season_year, team_name, team_name),
    )
    margins = []
    for r in rows:
        try:
            hp = float(r["home_team_points"] or 0)
            ap = float(r["away_team_points"] or 0)
            if r["home_team_name"] == team_name:
                margins.append(hp - ap)
            else:
                margins.append(ap - hp)
        except (TypeError, ValueError):
            continue
    return margins


# ---------------------------------------------------------------------------
# Boxscore: clean per-player stats from WNBA_GAMESUMMARY_PLAYERS
# ---------------------------------------------------------------------------

def _parse_minutes(minutes_str: str) -> float:
    """Convert Sportradar 'MM:SS' minutes string to float minutes."""
    s = str(minutes_str or "0").strip()
    if ":" in s:
        try:
            parts = s.split(":")
            return round(int(parts[0]) + int(parts[1]) / 60, 2)
        except (ValueError, IndexError):
            return 0.0
    try:
        return round(float(s), 2)
    except ValueError:
        return 0.0


def get_boxscore(sr_game_id: str, team_name: str) -> list[dict]:
    """
    Return per-player game stats for team_name in the given game.
    Uses SPORTRADAR.DBO.WNBA_GAMESUMMARY_PLAYERS directly — no derivation needed.

    Returns list of:
        {
          "name":    str,
          "minutes": float,
          "fouls":   int,
          "starter": bool,
          "dnp":     bool,
        }

    TEAM_NAME in GAMESUMMARY_PLAYERS is the short name (e.g. "Fever"), so we
    filter on that. TEAM_MARKET is the city (e.g. "Indiana").
    """
    short_name = _sr_team_name_filter(team_name)

    rows = _query(
        """
        SELECT
            player_full_name,
            player_statistics_minutes,
            player_statistics_personal_fouls,
            player_starter,
            player_played,
            player_not_playing_reason,
            player_not_playing_description
        FROM SPORTRADAR.DBO.WNBA_GAMESUMMARY_PLAYERS
        WHERE game_id   = %s
          AND team_name = %s
        """,
        (sr_game_id, short_name),
    )

    if not rows:
        return []

    results = []
    for r in rows:
        name    = r["player_full_name"] or ""
        if not name:
            continue
        minutes = _parse_minutes(r["player_statistics_minutes"])
        fouls   = int(r["player_statistics_personal_fouls"] or 0)
        starter = bool(r["player_starter"])
        # DNP: not played, or has a not_playing_reason, or 0 minutes with explicit reason
        played  = r["player_played"]
        dnp_reason = r["player_not_playing_reason"] or ""
        dnp     = (played is False) or bool(dnp_reason) or (minutes < 0.5 and bool(dnp_reason))

        results.append({
            "name":    name,
            "minutes": minutes,
            "fouls":   fouls,
            "starter": starter,
            "dnp":     dnp,
        })

    return results


# ---------------------------------------------------------------------------
# Quarter minutes: from PLAYER_STATISTICS_PERIODS variant
# ---------------------------------------------------------------------------

def get_quarter_minutes(sr_game_id: str, team_name: str) -> dict[str, dict[int, float]]:
    """
    Return {player_name: {1: mins, 2: mins, 3: mins, 4: mins}} for regulation quarters.

    Primary: parses PLAYER_STATISTICS_PERIODS variant from WNBA_GAMESUMMARY_PLAYERS.
    The periods variant is a list of objects like:
        [{"number": 1, "sequence": 1, "type": "quarter", "minutes": "9:45", ...}, ...]

    Fallback: if PLAYER_STATISTICS_PERIODS is null/empty, sums possession durations
    (GAME_CLOCK_START - GAME_CLOCK_END) per player per quarter from
    WNBA_PLAYBYPLAY_POSSESSIONS. This is more accurate than raw event tracking
    because the possessions view already excludes lineupchange/stoppage events.
    """
    short_name = _sr_team_name_filter(team_name)

    rows = _query(
        """
        SELECT
            player_full_name,
            player_statistics_periods,
            player_statistics_minutes,
            player_played
        FROM SPORTRADAR.DBO.WNBA_GAMESUMMARY_PLAYERS
        WHERE game_id   = %s
          AND team_name = %s
          AND player_played = TRUE
        """,
        (sr_game_id, short_name),
    )

    if not rows:
        return {}

    result: dict[str, dict[int, float]] = {}
    needs_pbp_fallback = []

    for r in rows:
        name = r["player_full_name"] or ""
        if not name:
            continue

        periods_raw = r["player_statistics_periods"]
        q_mins = _parse_periods_variant(periods_raw)

        if q_mins:
            result[name] = q_mins
        else:
            # No per-period breakdown — fall back to play-by-play for this game
            needs_pbp_fallback.append(name)

    # If any players are missing period data, fill from possessions view
    if needs_pbp_fallback:
        poss_data = _get_quarter_minutes_from_possessions(sr_game_id, team_name)
        for name in needs_pbp_fallback:
            if name in poss_data:
                result[name] = poss_data[name]
            elif name not in result:
                # Last resort: put all minutes in Q1 so they're not lost entirely
                total = _parse_minutes(
                    next((r["player_statistics_minutes"] for r in rows
                          if r["player_full_name"] == name), "0")
                )
                if total > 0:
                    result[name] = {1: total}

    return result


def _parse_periods_variant(periods_raw) -> dict[int, float]:
    """
    Parse PLAYER_STATISTICS_PERIODS variant into {quarter_number: float_minutes}.
    Only includes regulation quarters (type='quarter', number 1-4).
    Snowflake returns VARIANT as a Python dict/list or a JSON string.
    """
    if not periods_raw:
        return {}
    try:
        data = periods_raw if isinstance(periods_raw, list) else json.loads(str(periods_raw))
        if not isinstance(data, list):
            # Sometimes wrapped: {"period": [...]}
            if isinstance(data, dict):
                data = data.get("period", data.get("periods", []))
        q_mins: dict[int, float] = {}
        for period in data:
            if not isinstance(period, dict):
                continue
            ptype  = str(period.get("type", "")).lower()
            pnum   = int(period.get("number", 0))
            if ptype != "quarter" or pnum < 1 or pnum > 4:
                continue
            mins_str = period.get("minutes") or period.get("played_minutes") or "0"
            mins = _parse_minutes(str(mins_str))
            if mins > 0:
                q_mins[pnum] = mins
        return q_mins
    except Exception:
        return {}


def _get_quarter_minutes_from_possessions(sr_game_id: str, team_name: str) -> dict[str, dict[int, float]]:
    """
    Fallback: derive per-quarter on-court minutes from WNBA_PLAYBYPLAY_POSSESSIONS.

    Each possession row already has:
      - PERIOD_NUMBER (quarter 1-4)
      - PERIOD_SEQUENCE (>=5 means OT — skipped)
      - GAME_CLOCK_START / GAME_CLOCK_END (TIME values)
      - HOME_PLAYERS / AWAY_PLAYERS (variant: list of {full_name, id, ...})
      - HOME_TEAM_NAME / AWAY_TEAM_NAME (short names e.g. "Fever")

    On-court time for a possession = GAME_CLOCK_START - GAME_CLOCK_END (seconds).
    We sum that across all possessions in a quarter for each player who appears.

    This is cleaner and more accurate than event-level on-court tracking because
    the possession view already handles lineupchange/stoppage exclusions.
    """
    short_name = _sr_team_name_filter(team_name)

    rows = _query(
        """
        SELECT
            period_number,
            period_sequence,
            home_team_name,
            away_team_name,
            game_clock_start,
            game_clock_end,
            home_players,
            away_players
        FROM SPORTRADAR.DBO.WNBA_PLAYBYPLAY_POSSESSIONS
        WHERE game_id          = %s
          AND period_sequence  < 5          -- regulation only, skip OT
          AND period_number    BETWEEN 1 AND 4
        ORDER BY period_number ASC, event_sequence_start ASC
        """,
        (sr_game_id,),
    )

    if not rows:
        return {}

    # Determine home vs away for this team from the first row
    team_side = "home"
    if rows:
        first = rows[0]
        if str(first.get("away_team_name", "")).lower() == short_name.lower():
            team_side = "away"

    quarter_secs: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))

    for row in rows:
        q = int(row["period_number"] or 0)
        if q < 1 or q > 4:
            continue

        # Duration of this possession in seconds
        start = _time_to_seconds(row["game_clock_start"])
        end   = _time_to_seconds(row["game_clock_end"])
        duration = start - end
        if duration <= 0:
            continue

        players_raw = row["home_players"] if team_side == "home" else row["away_players"]
        for name in _parse_players_variant(players_raw):
            quarter_secs[name][q] += duration

    return {
        name: {q: round(s / 60, 2) for q, s in q_secs.items() if s > 0}
        for name, q_secs in quarter_secs.items()
    }


def _time_to_seconds(t) -> float:
    """
    Convert a Snowflake TIME value or 'MM:SS' string to seconds.
    Snowflake TIME columns come back as datetime.time objects via the connector.
    """
    if t is None:
        return 0.0
    import datetime
    if isinstance(t, datetime.time):
        return t.minute * 60 + t.second + t.microsecond / 1_000_000
    s = str(t).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            if len(parts) == 3:
                # HH:MM:SS from full time representation
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            return int(parts[0]) * 60 + float(parts[1])
        except (ValueError, IndexError):
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_players_variant(raw) -> list[str]:
    """
    Extract player full_names from HOME_PLAYERS / AWAY_PLAYERS variant.
    Structure: array of {full_name, id, jersey_number, reference, sr_id}
    """
    if not raw:
        return []
    try:
        data = raw if isinstance(raw, list) else json.loads(str(raw))
        names = []
        for p in data:
            if not isinstance(p, dict):
                continue
            n = p.get("full_name") or p.get("name") or ""
            if n:
                names.append(n)
        return names
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clock_to_seconds(clock_str: str) -> float:
    """Convert '5:30' or '56.3' to seconds remaining in the quarter."""
    s = clock_str.strip()
    if ":" in s:
        parts = s.split(":")
        try:
            return float(parts[0]) * 60 + float(parts[1])
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0
