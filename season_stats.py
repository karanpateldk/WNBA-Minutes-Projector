"""
Builds season-aggregate player stats from every completed game this season
using ESPN's boxscore + play-by-play APIs.

Produces per-team data:
  - avg_min, last3_avg, games_played
  - starter_pct (how often they start)
  - quarter minute averages (from play-by-play)
  - most recent starting lineup

All data cached to disk; call rebuild_team(team_name) to refresh.
"""

import json
import re
import requests
import sys
import os
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent))

try:
    import snowflake_connector as _sf
    _SF_AVAILABLE = _sf.is_available()
except Exception:
    _sf = None  # type: ignore
    _SF_AVAILABLE = False

CACHE_DIR = Path(__file__).parent / "data"
CACHE_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

ESPN_TEAM_IDS = {
    "Atlanta Dream":           20,
    "Chicago Sky":             19,
    "Connecticut Sun":         18,
    "Dallas Wings":            3,
    "Golden State Valkyries":  129689,
    "Indiana Fever":           5,
    "Las Vegas Aces":          17,
    "Los Angeles Sparks":      6,
    "Minnesota Lynx":          8,
    "New York Liberty":        9,
    "Phoenix Mercury":         11,
    "Portland Fire":           132052,
    "Seattle Storm":           14,
    "Toronto Tempo":           131935,
    "Washington Mystics":      16,
}

QUARTER_SECONDS = 600


# ---------------------------------------------------------------------------
# Outlier-resistant averaging
# ---------------------------------------------------------------------------

def _trimmed_avg(minutes_list: list[float]) -> float:
    """
    Season average with low outliers removed using IQR method.

    Only removes games that are anomalously *low* (blowout garbage time,
    load management DNP-lite, early foul-out) — not high games, since a
    40-min game is a real usage signal.

    Rules:
      - Need at least 5 games to attempt trimming (3-4 games: just return mean).
      - Outlier threshold: any game below Q1 − 1.5 × IQR.
      - After removing outliers, keep at least 60% of original games so we
        don't over-trim a player with genuinely variable usage.
    """
    n = len(minutes_list)
    if n < 5:
        return round(sum(minutes_list) / n, 1) if n > 0 else 0.0

    s = sorted(minutes_list)
    q1 = s[n // 4]
    q3 = s[(3 * n) // 4]
    iqr = q3 - q1
    # Only trim when there's meaningful spread (IQR > 3 min)
    if iqr <= 3.0:
        return round(sum(minutes_list) / n, 1)

    lower_fence = q1 - 1.5 * iqr
    kept = [m for m in minutes_list if m >= lower_fence]

    # Don't over-trim: keep at least 60% of games
    min_keep = max(3, int(n * 0.60))
    if len(kept) < min_keep:
        kept = sorted(minutes_list)[n - min_keep:]  # keep the top min_keep values

    return round(sum(kept) / len(kept), 1)


def _median(values: list[float]) -> float:
    """Median of a list — immune to a single outlier in small windows."""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return round((s[mid - 1] + s[mid]) / 2 if n % 2 == 0 else s[mid], 1)


def _iqr_trim(values: list[float]) -> list[float]:
    """
    Remove low outliers using IQR method — same logic as _trimmed_avg but
    returns the cleaned list rather than the average. Only trims low values
    (anomalously short quarter stints). Requires at least 4 values to attempt
    trimming; returns original list otherwise.
    """
    n = len(values)
    if n < 4:
        return values
    s = sorted(values)
    q1 = s[n // 4]
    q3 = s[(3 * n) // 4]
    iqr = q3 - q1
    if iqr <= 1.5:
        return values
    lower_fence = q1 - 1.5 * iqr
    kept = [v for v in values if v >= lower_fence]
    # Always keep at least 60% of values
    min_keep = max(3, int(n * 0.60))
    if len(kept) < min_keep:
        kept = sorted(values)[n - min_keep:]
    return kept if kept else values


def _ewma(values: list[float], halflife: float = 4.0) -> float:
    """
    Exponentially weighted moving average. halflife=4 means a game 4 games
    ago has half the weight of the most recent game. Uses full history so
    it's strictly better than last-3 median for trend detection.
    """
    if not values:
        return 0.0
    alpha = 1.0 - 0.5 ** (1.0 / halflife)
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1.0 - alpha) * result
    return round(result, 1)


def _context_filter(minutes_list: list[float], fouls_list: list[int],
                    margins_list: list[float]) -> list[float]:
    """
    Remove atypical games before computing EWMA:
    - Foul trouble (4+ fouls)
    - Blowouts (margin > 15 pts)
    - Injury-return games (first game back — minutes < 40% of trimmed avg)
    Always keeps at least 60% of games.
    """
    n = len(minutes_list)
    if n < 3:
        return minutes_list

    trimmed = _trimmed_avg(minutes_list)
    injury_return = set()
    for i in range(n):
        if minutes_list[i] < trimmed * 0.40:
            # Only flag as injury return if NOT two consecutive low games
            if i == 0 or minutes_list[i - 1] >= trimmed * 0.40:
                injury_return.add(i)

    kept = []
    for i, m in enumerate(minutes_list):
        foul_game = fouls_list[i] >= 4 if i < len(fouls_list) else False
        blowout   = abs(margins_list[i]) >= 15 if i < len(margins_list) else False
        ret_game  = i in injury_return
        if not (foul_game or blowout or ret_game):
            kept.append(m)

    min_keep = max(3, int(n * 0.60))
    if len(kept) < min_keep:
        kept = minutes_list[-min_keep:]
    return kept if kept else minutes_list


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _load_cache(key: str, ttl_hours: float = 4.0):
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data.get("timestamp", "2000-01-01"))
        if datetime.now() - ts > timedelta(hours=ttl_hours):
            return None
        return data.get("payload")
    except Exception:
        return None


def _save_cache(key: str, payload, ttl_hours: float = 4.0):
    _cache_path(key).write_text(
        json.dumps({"timestamp": datetime.now().isoformat(),
                    "payload": payload, "ttl_hours": ttl_hours}, indent=2),
        encoding="utf-8"
    )


def _get(url: str) -> dict:
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[season_stats] {url}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Game ID fetching
# ---------------------------------------------------------------------------

def get_all_game_ids(team_name: str) -> list[str]:
    """Return all completed game IDs for a team this season, oldest first."""
    return [gid for gid, _ in get_all_games_with_dates(team_name)]


def get_all_games_with_dates(team_name: str) -> list[tuple[str, str]]:
    """
    Return list of (game_id, date_str) for all completed REGULAR-SEASON games, oldest first.
    date_str is ISO format 'YYYY-MM-DD'.

    Primary source: Sportradar Snowflake (WNBA_SCHEDULE, SEASON_TYPE='REG').
    Fallback: ESPN schedule API (used when Snowflake is unavailable).
    """
    if _SF_AVAILABLE:
        games = _sf.get_games_for_team(team_name)
        if games:
            return games

    # ESPN fallback
    from datetime import date as _date
    _yr = _date.today().year if _date.today().month >= 5 else _date.today().year - 1
    REGULAR_SEASON_START = f"{_yr}-05-01"

    team_id = ESPN_TEAM_IDS.get(team_name)
    if not team_id:
        return []
    data = _get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/schedule")
    results = []
    for e in data.get("events", []):
        comp = e.get("competitions", [{}])[0]
        if not comp.get("status", {}).get("type", {}).get("completed"):
            continue

        season_type = e.get("season", {}).get("type", None)
        raw_date = e.get("date", "")
        date_str = raw_date[:10] if raw_date else ""

        if season_type is not None:
            if season_type != 2:
                continue
        else:
            if date_str and date_str < REGULAR_SEASON_START:
                continue

        results.append((e["id"], date_str))
    return results


# ---------------------------------------------------------------------------
# Boxscore parsing — minutes + starter flag
# ---------------------------------------------------------------------------

def _is_sr_uuid(game_id: str) -> bool:
    """True if game_id looks like a Sportradar UUID (8-4-4-4-12 hex)."""
    import re as _re
    return bool(_re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        str(game_id), _re.I
    ))


def _team_name_for_id(team_id: int) -> str:
    """Reverse-lookup team name from ESPN team ID."""
    for name, tid in ESPN_TEAM_IDS.items():
        if tid == team_id:
            return name
    return ""


def _parse_boxscore(game_id: str, team_id: int) -> list[dict]:
    """
    Returns list of {name, minutes, fouls, starter, dnp} for players on team_id.

    Primary source: Sportradar Snowflake (when game_id is a SR UUID or Snowflake is available).
    Fallback: ESPN summary API.
    """
    if _SF_AVAILABLE and _is_sr_uuid(game_id):
        team_name = _team_name_for_id(team_id)
        if team_name:
            rows = _sf.get_boxscore(game_id, team_name)
            if rows:
                return rows

    # ESPN fallback (numeric game IDs only)
    if _is_sr_uuid(game_id):
        return []  # can't query ESPN with a SR UUID

    data = _get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary?event={game_id}")
    if not data:
        return []

    tid_str = str(team_id)
    results = []

    for tb in data.get("boxscore", {}).get("players", []):
        if str(tb.get("team", {}).get("id", "")) != tid_str:
            continue
        for sg in tb.get("statistics", []):
            labels = sg.get("labels", [])
            min_idx = labels.index("MIN") if "MIN" in labels else 0
            pf_idx  = labels.index("PF")  if "PF"  in labels else None
            for a in sg.get("athletes", []):
                name = a.get("athlete", {}).get("displayName", "")
                dnp  = a.get("didNotPlay", False)
                starter = a.get("starter", False)
                stats = a.get("stats", [])
                mins_raw = stats[min_idx] if stats and min_idx < len(stats) else "0"
                try:
                    mins = float(str(mins_raw).replace(":", ".")) if ":" not in str(mins_raw) else (
                        int(str(mins_raw).split(":")[0]) + int(str(mins_raw).split(":")[1]) / 60
                    )
                except (ValueError, IndexError):
                    mins = 0.0
                fouls = 0
                if pf_idx is not None and stats and pf_idx < len(stats):
                    try:
                        fouls = int(stats[pf_idx])
                    except (ValueError, TypeError):
                        fouls = 0
                if name:
                    results.append({
                        "name":    name,
                        "minutes": round(mins, 1),
                        "fouls":   fouls,
                        "starter": starter,
                        "dnp":     dnp,
                    })
    return results


# ---------------------------------------------------------------------------
# Play-by-play quarter minute parsing
# ---------------------------------------------------------------------------

def _clock_to_secs(clock_str: str) -> float:
    s = str(clock_str).strip()
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


def _parse_quarter_minutes(game_id: str, team_id: int,
                            starters: set[str]) -> dict[str, dict[int, float]]:
    """
    Derive per-quarter minutes from play-by-play substitution events.
    Returns {player: {1: mins, 2: mins, 3: mins, 4: mins}}
    """
    data = _get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary?event={game_id}")
    plays = data.get("plays", [])
    if not plays:
        return {}

    tid_str = str(team_id)

    # Map player -> team from boxscore
    player_team: dict[str, str] = {}
    for tb in data.get("boxscore", {}).get("players", []):
        t = str(tb.get("team", {}).get("id", ""))
        for sg in tb.get("statistics", []):
            for a in sg.get("athletes", []):
                n = a.get("athlete", {}).get("displayName", "")
                if n:
                    player_team[n] = t

    ENTERS_RE = re.compile(r"^(.+?)\s+enters the game for\s+(.+)$", re.I)

    # on_since[player] = clock_seconds when they came on in this quarter
    on_since: dict[str, float] = {}
    quarter_secs: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    prev_q = None

    def flush(q: int, end_secs: float = 0.0):
        for player, entered in list(on_since.items()):
            played = entered - end_secs
            if played > 0:
                quarter_secs[player][q] += played

    for play in plays:
        q = play.get("period", {}).get("number", 0)
        if q == 0 or q > 4:
            continue

        clock_secs = _clock_to_secs(
            play.get("clock", {}).get("displayValue", "10:00")
        )
        play_type = play.get("type", {}).get("text", "")
        play_text  = play.get("text", "")
        play_team  = str(play.get("team", {}).get("id", ""))

        # Quarter change
        if q != prev_q:
            if prev_q is not None:
                flush(prev_q, 0.0)
            # Players still on court at end of previous quarter carry over
            carried = set(on_since.keys())
            on_since.clear()
            prev_q = q
            if q == 1:
                # Seed starters at start of Q1
                for s in starters:
                    if player_team.get(s) == tid_str:
                        on_since[s] = QUARTER_SECONDS
            else:
                # Seed players who were still on court when the previous quarter ended
                for player in carried:
                    if player_team.get(player) == tid_str:
                        on_since[player] = QUARTER_SECONDS

        if play_team != tid_str:
            continue

        if play_type.lower() == "substitution":
            m = ENTERS_RE.match(play_text)
            if m:
                entering = m.group(1).strip()
                exiting  = m.group(2).strip()

                if exiting in on_since:
                    played = on_since[exiting] - clock_secs
                    if played > 0:
                        quarter_secs[exiting][q] += played
                    del on_since[exiting]

                on_since[entering] = clock_secs
                if entering not in player_team:
                    player_team[entering] = tid_str

    if prev_q:
        flush(prev_q, 0.0)

    result: dict[str, dict[int, float]] = {}
    for player, q_secs in quarter_secs.items():
        if player_team.get(player) != tid_str:
            continue
        result[player] = {q: round(s / 60, 2) for q, s in q_secs.items() if s > 0}

    return result


# ---------------------------------------------------------------------------
# Season aggregation
# ---------------------------------------------------------------------------

def rebuild_team(team_name: str, force: bool = False) -> dict:
    """
    Pulls ALL completed games for a team, aggregates:
      - avg_min, last3_avg, games_played, starter_pct
      - quarter minute averages
      - most_recent_starters list

    Saves to cache and returns the result dict.
    Result shape:
    {
      "players": {
        player_name: {
          "avg_min": float,
          "last3_avg": float,
          "games_played": int,
          "starter_pct": float,       # 0.0-1.0
          "quarter_avgs": {1: float, 2: float, 3: float, 4: float},
        }
      },
      "most_recent_starters": [str, ...],  # up to 5
      "games_processed": int,
      "last_updated": str,
    }
    """
    cache_key = f"season_{team_name.replace(' ', '_')}"
    if not force:
        cached = _load_cache(cache_key, ttl_hours=4.0)
        if cached:
            return cached

    team_id = ESPN_TEAM_IDS.get(team_name)
    if not team_id:
        return {}

    games_with_dates = get_all_games_with_dates(team_name)
    if not games_with_dates:
        return {}

    # Per-player accumulators
    all_minutes:        dict[str, list[float]] = defaultdict(list)
    clean_minutes:      dict[str, list[float]] = defaultdict(list)  # foul-trouble games excluded
    foul_trouble_games: dict[str, int]         = defaultdict(int)   # games with 4+ fouls
    starter_games:      dict[str, int]         = defaultdict(int)
    games_played:       dict[str, int]         = defaultdict(int)
    last_played_date:   dict[str, str]         = {}
    quarter_acc:        dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))

    most_recent_starters: list[str] = []
    rotation_counts_per_game: list[int] = []
    boxscore_cache: dict[str, list[dict]] = {}
    game_margins: dict[str, float] = {}   # game_id -> point differential (team - opp)
    player_fouls_by_game: dict[str, list[int]] = defaultdict(list)
    player_margins_by_game: dict[str, list[float]] = defaultdict(list)

    for i, (gid, game_date) in enumerate(games_with_dates):
        box = _parse_boxscore(gid, team_id)
        if not box:
            continue
        boxscore_cache[gid] = box

        # Get game margin — from Snowflake if available, else ESPN summary
        if _SF_AVAILABLE and _is_sr_uuid(gid):
            game_margins[gid] = _sf.get_game_margin(gid, team_name)
        else:
            try:
                summary = _get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary?event={gid}")
                competitors = summary.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
                team_score = opp_score = None
                for c in competitors:
                    score_val = c.get("score", {})
                    val = float(score_val.get("value", score_val)) if isinstance(score_val, dict) else float(score_val or 0)
                    if str(c.get("team", {}).get("id", "")) == str(team_id):
                        team_score = val
                    else:
                        opp_score = val
                if team_score is not None and opp_score is not None:
                    game_margins[gid] = team_score - opp_score
                else:
                    game_margins[gid] = 0.0
            except Exception:
                game_margins[gid] = 0.0

        game_starters = {p["name"] for p in box if p["starter"] and not p["dnp"]}

        rotation_count = 0
        for p in box:
            name = p["name"]
            if p["dnp"] or p["minutes"] < 0.5:
                continue
            all_minutes[name].append(p["minutes"])
            games_played[name] += 1
            if p["starter"]:
                starter_games[name] += 1
            if p["minutes"] >= 5.0:
                rotation_count += 1
            if game_date:
                last_played_date[name] = game_date
            # Track foul trouble: 4+ fouls = curtailed minutes, exclude from clean avg
            if p.get("fouls", 0) >= 4:
                foul_trouble_games[name] += 1
            else:
                clean_minutes[name].append(p["minutes"])
            player_fouls_by_game[name].append(p.get("fouls", 0))
            player_margins_by_game[name].append(game_margins.get(gid, 0.0))

        if rotation_count > 0:
            rotation_counts_per_game.append(rotation_count)

        # Quarter minutes from play-by-play
        q_mins = _parse_quarter_minutes(gid, team_id, game_starters)
        for player, q_data in q_mins.items():
            for q, m in q_data.items():
                quarter_acc[player][q].append(m)

        # Track most recent game starters
        if i == len(games_with_dates) - 1:
            most_recent_starters = [p["name"] for p in box if p["starter"] and not p["dnp"]]

    if not all_minutes:
        return {}

    # Build last-3 averages, last-game minutes, and recent starter rate (last 5 games)
    last3_game_ids  = [gid for gid, _ in games_with_dates[-3:]]
    last5_game_ids  = [gid for gid, _ in games_with_dates[-5:]]
    last3_minutes:       dict[str, list[float]] = defaultdict(list)
    last3_clean_minutes: dict[str, list[float]] = defaultdict(list)
    last_game_minutes:   dict[str, float]       = {}
    recent_starter_games: dict[str, int]        = defaultdict(int)
    recent_games_played:  dict[str, int]        = defaultdict(int)

    for idx, gid in enumerate(last3_game_ids):
        box = boxscore_cache.get(gid) or _parse_boxscore(gid, team_id)
        for p in box:
            if not p["dnp"] and p["minutes"] >= 0.5:
                last3_minutes[p["name"]].append(p["minutes"])
                if p.get("fouls", 0) < 4:
                    last3_clean_minutes[p["name"]].append(p["minutes"])
                if idx == len(last3_game_ids) - 1:
                    last_game_minutes[p["name"]] = p["minutes"]

    for gid in last5_game_ids:
        box = boxscore_cache.get(gid) or _parse_boxscore(gid, team_id)
        for p in box:
            if not p["dnp"] and p["minutes"] >= 0.5:
                recent_games_played[p["name"]] += 1
                if p.get("starter"):
                    recent_starter_games[p["name"]] += 1

    # Rotation depth: median of players with 5+ min across the last 5 games.
    # Uses recent games so coaching changes mid-season get reflected quickly.
    if rotation_counts_per_game:
        recent = sorted(rotation_counts_per_game[-5:])
        rotation_depth = recent[len(recent) // 2]
    else:
        rotation_depth = 8  # WNBA default

    # Compile result
    players = {}
    for name in all_minutes:
        mins_list  = all_minutes[name]
        clean_list = clean_minutes.get(name, [])
        avg          = round(sum(mins_list)  / len(mins_list),  1)
        trimmed_avg  = _trimmed_avg(mins_list)
        clean_avg    = _trimmed_avg(clean_list) if clean_list else trimmed_avg

        l3       = last3_minutes.get(name, [])
        l3_clean = last3_clean_minutes.get(name, [])
        # Use median for last-3 so a single bad game doesn't skew the window
        last3_avg       = _median(l3)       if l3       else trimmed_avg
        last3_clean_avg = _median(l3_clean) if l3_clean else last3_avg
        # Range of last-3 game minutes — used to flag unstable usage.
        # Use clean list (foul-trouble games excluded) and also drop any game
        # where minutes were <40% of season avg (injury exit mid-game).
        injury_exit_threshold = trimmed_avg * 0.40
        l3_stable = [m for m in l3_clean if m >= injury_exit_threshold]
        last3_range = round(max(l3_stable) - min(l3_stable), 1) if len(l3_stable) >= 2 else 0.0

        gp = games_played[name]
        ft = foul_trouble_games[name]
        sp = round(starter_games[name] / gp, 2) if gp > 0 else 0.0
        foul_rate = round(ft / gp, 2) if gp > 0 else 0.0

        # EWMA with context filter
        fouls_seq   = player_fouls_by_game.get(name, [])
        margins_seq = player_margins_by_game.get(name, [])
        ctx_mins    = _context_filter(mins_list, fouls_seq, margins_seq)
        ewma_min    = _ewma(ctx_mins) if len(ctx_mins) >= 2 else trimmed_avg

        # Quarter averages — per-quarter trimmed median, weighted toward recent games.
        #
        # Three-step clean before averaging each quarter:
        #   1. Drop games where the player had 4+ fouls AND that quarter's minutes
        #      were more than 35% below their per-quarter average — foul-trouble
        #      games distort the quarter distribution without reflecting real rotation.
        #   2. IQR outlier removal (same as _trimmed_avg) — drops anomalously low
        #      quarter values (garbage time, early sit, tactical rest).
        #   3. 75/25 blend: 75% median of last-3 clean games, 25% trimmed season avg.
        #      Recent games weighted heavily because coaches adjust rotations week-to-week.
        #
        # Goal: project each quarter within ~0.5 min of actual for starters.
        q_avgs = {}
        for q in [1, 2, 3, 4]:
            all_vals = quarter_acc[name].get(q, [])
            if not all_vals:
                q_avgs[q] = 0.0
                continue

            # Step 1: foul-trouble filter — drop games where foul trouble curtailed
            # this quarter specifically. Proxy: any value >35% below the raw mean.
            raw_mean = sum(all_vals) / len(all_vals)
            foul_threshold = raw_mean * 0.65  # more than 35% below avg = foul-curtailed
            clean_vals = [v for v in all_vals if v >= foul_threshold] if raw_mean > 1.0 else all_vals
            if not clean_vals:
                clean_vals = all_vals

            # Step 2: IQR outlier removal on the cleaned list
            clean_vals = _iqr_trim(clean_vals)

            # Step 3: 75/25 blend of last-3 median vs trimmed season avg
            last3_vals  = clean_vals[-3:] if len(clean_vals) >= 3 else clean_vals
            season_q    = sum(clean_vals) / len(clean_vals)
            last3_q     = _median(last3_vals)
            q_avgs[q]   = round(last3_q * 0.75 + season_q * 0.25, 1)

        rgp = recent_games_played.get(name, 0)
        recent_sp = round(recent_starter_games.get(name, 0) / rgp, 2) if rgp > 0 else sp

        players[name] = {
            "avg_min":            trimmed_avg,
            "ewma_min":           ewma_min,
            "raw_avg_min":        avg,
            "clean_avg_min":      clean_avg,
            "last3_avg":          last3_avg,
            "last3_range":        last3_range,
            "last3_clean_avg":    last3_clean_avg,
            "last_game_min":      last_game_minutes.get(name, 0.0),
            "games_played":       gp,
            "games_started":      starter_games[name],
            "foul_trouble_games": ft,
            "foul_rate":          foul_rate,
            "starter_pct":        sp,
            "recent_starter_pct": recent_sp,
            "plus_minus":         None,   # populated below from Snowflake
            "quarter_avgs":       q_avgs,
            "last_played_date":   last_played_date.get(name, ""),
        }

    # Enrich with plus/minus from Snowflake
    if _SF_AVAILABLE:
        try:
            pm_map = _sf.get_player_plus_minus(team_name)
            for name, pm in pm_map.items():
                if name in players:
                    players[name]["plus_minus"] = pm
        except Exception:
            pass

    # Fetch team-specific role minute averages and rotation stats from Snowflake
    role_avgs = {}
    rotation_stats = {}
    if _SF_AVAILABLE:
        try:
            role_avgs = _sf.get_role_minute_averages(team_name)
        except Exception:
            pass
        try:
            rotation_stats = _sf.get_rotation_stats(team_name)
        except Exception:
            pass

    result = {
        "players":               players,
        "most_recent_starters":  most_recent_starters,
        "games_processed":       len(games_with_dates),
        "rotation_depth":        rotation_depth,
        "role_avg_starter":      role_avgs.get("starter", 0.0),
        "role_avg_bench":        role_avgs.get("bench", 0.0),
        "avg_bench_count":       rotation_stats.get("avg_bench_count", 0.0),
        "avg_bench_8plus":       rotation_stats.get("avg_bench_8plus", 0.0),
        "avg_starter_mins":      rotation_stats.get("avg_starter_mins", 0.0),
        "avg_bench_mins":        rotation_stats.get("avg_bench_mins", 0.0),
        "last_updated":          datetime.now().isoformat(),
    }

    _save_cache(cache_key, result, ttl_hours=4.0)
    return result


def get_team_season_stats(team_name: str) -> dict:
    """Public entry point — returns cached stats or rebuilds if stale."""
    return rebuild_team(team_name)
