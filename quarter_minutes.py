"""
Derives per-quarter minutes from ESPN play-by-play substitution events.
Logic:
  - Each quarter is 10 minutes.
  - A player is ON the court from game-start / when they enter, until they exit / quarter ends.
  - Sub events tell us exactly when each player enters/exits.
  - We accumulate time-on-court per quarter per player across multiple recent games,
    then average to get typical quarter distributions.
"""

import re
import json
import requests
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timedelta

CACHE_DIR = Path(__file__).parent / "data"
CACHE_DIR.mkdir(exist_ok=True)
QUARTER_SECONDS = 600  # 10 min per quarter

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


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _load_cache(key: str, ttl_hours: float = 6):
    p = _cache_path(key)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    ts = datetime.fromisoformat(data.get("timestamp", "2000-01-01"))
    if datetime.now() - ts > timedelta(hours=ttl_hours):
        return None
    return data.get("payload")


def _save_cache(key: str, payload, ttl_hours: float = 6):
    _cache_path(key).write_text(
        json.dumps({"timestamp": datetime.now().isoformat(),
                    "payload": payload, "ttl_hours": ttl_hours}, indent=2),
        encoding="utf-8"
    )


def _get_json(url: str) -> dict:
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[quarter_minutes] {url} failed: {e}")
        return {}


# ---------------------------------------------------------------------------
# Clock parsing
# ---------------------------------------------------------------------------

def _clock_to_seconds(clock_str: str) -> float:
    """Convert '5:30' or '56.3' to seconds remaining in quarter."""
    clock_str = str(clock_str).strip()
    if ":" in clock_str:
        parts = clock_str.split(":")
        return float(parts[0]) * 60 + float(parts[1])
    try:
        return float(clock_str)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Play-by-play parser
# ---------------------------------------------------------------------------

def _parse_quarter_minutes_from_game(game_id: str, team_id: int) -> dict:
    """
    Parse play-by-play for one game.
    Returns {player_name: {1: secs, 2: secs, 3: secs, 4: secs}}
    Only includes players on the given team_id.
    """
    data = _get_json(
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary?event={game_id}"
    )
    if not data:
        return {}

    plays = data.get("plays", [])
    if not plays:
        return {}

    # Identify which team IDs are in this game
    boxscore = data.get("boxscore", {})
    team_ids_in_game = []
    player_to_team = {}  # player_name -> team_id_str

    for team_block in boxscore.get("players", []):
        tid = str(team_block.get("team", {}).get("id", ""))
        team_ids_in_game.append(tid)
        for stat_group in team_block.get("statistics", []):
            for athlete in stat_group.get("athletes", []):
                name = athlete.get("athlete", {}).get("displayName", "")
                if name:
                    player_to_team[name] = tid

    target_tid = str(team_id)

    # Determine starters from boxscore (starter=True)
    starters = set()
    for team_block in boxscore.get("players", []):
        if str(team_block.get("team", {}).get("id", "")) != target_tid:
            continue
        for stat_group in team_block.get("statistics", []):
            for athlete in stat_group.get("athletes", []):
                if athlete.get("starter") and not athlete.get("didNotPlay"):
                    name = athlete.get("athlete", {}).get("displayName", "")
                    if name:
                        starters.add(name)

    # Track who is on court per quarter: {quarter: set(player_names)}
    on_court = defaultdict(set)
    # Track time each player has been on court this quarter
    # on_since[player] = seconds_remaining when they came on (in current quarter)
    on_since: dict[str, float] = {}
    # Accumulated seconds per player per quarter
    quarter_secs: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))

    current_quarter = 0

    def flush_quarter(q: int, clock_at_end: float = 0.0):
        """Credit everyone currently on court from on_since → clock_at_end."""
        for player, entered_at in list(on_since.items()):
            played = entered_at - clock_at_end
            if played > 0:
                quarter_secs[player][q] += played

    def start_quarter(q: int):
        nonlocal current_quarter
        if current_quarter != q:
            flush_quarter(current_quarter)
            current_quarter = q
            # Reset on_since to 600 for starters, 0 for everyone else
            on_since.clear()
            if q == 1:
                for s in starters:
                    if player_to_team.get(s) == target_tid:
                        on_since[s] = QUARTER_SECONDS
            else:
                # Carry over who was on court at end of previous quarter
                # (they'll be re-seeded when the new quarter's first play arrives)
                pass

    # Sub text patterns
    ENTERS_RE = re.compile(r"^(.+?)\s+enters the game for\s+(.+)$", re.I)
    # Some ESPN games use "enters" differently — also handle period-start plays

    prev_quarter = None

    for play in plays:
        q = play.get("period", {}).get("number", 0)
        if q == 0 or q > 4:
            continue  # skip OT for now

        clock_str = play.get("clock", {}).get("displayValue", "10:00")
        clock_secs = _clock_to_seconds(clock_str)

        play_type = play.get("type", {}).get("text", "")
        play_text  = play.get("text", "")
        play_team  = str(play.get("team", {}).get("id", ""))

        # Quarter transition
        if q != prev_quarter:
            if prev_quarter is not None:
                flush_quarter(prev_quarter, 0.0)
            # Players still on court at end of previous quarter carry over
            carried = set(on_since.keys())
            on_since.clear()
            current_quarter = q
            if q == 1:
                for s in starters:
                    if player_to_team.get(s) == target_tid:
                        on_since[s] = QUARTER_SECONDS
            else:
                # Seed players who were on court when the previous quarter ended
                for player in carried:
                    if player_to_team.get(player) == target_tid:
                        on_since[player] = QUARTER_SECONDS
            prev_quarter = q

        # Only process subs for our target team
        if play_team != target_tid:
            continue

        if play_type.lower() == "substitution":
            m = ENTERS_RE.match(play_text)
            if m:
                entering = m.group(1).strip()
                exiting  = m.group(2).strip()

                # Credit exiting player up to this clock
                if exiting in on_since:
                    played = on_since[exiting] - clock_secs
                    if played > 0:
                        quarter_secs[exiting][q] += played
                    del on_since[exiting]

                # Entering player starts tracking now
                on_since[entering] = clock_secs
                # Make sure we know their team
                if entering not in player_to_team:
                    player_to_team[entering] = target_tid

    # Flush the final quarter
    if current_quarter > 0:
        flush_quarter(current_quarter, 0.0)

    # Convert seconds → minutes, only return players on our target team
    result = {}
    for player, q_secs in quarter_secs.items():
        if player_to_team.get(player) != target_tid:
            continue
        result[player] = {
            q: round(secs / 60, 2)
            for q, secs in q_secs.items()
            if secs > 0
        }

    return result


# ---------------------------------------------------------------------------
# Multi-game average
# ---------------------------------------------------------------------------

def get_recent_game_ids(team_id: int, n: int = 5) -> list[str]:
    """Return up to n completed game IDs for a team, most recent first."""
    data = _get_json(
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/schedule"
    )
    game_ids = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        if comp.get("status", {}).get("type", {}).get("completed"):
            game_ids.append(event["id"])
    # Schedule is chronological — reverse for most-recent-first
    return list(reversed(game_ids))[:n]


def get_quarter_minute_averages(team_name: str, n_games: int = 5) -> dict:
    """
    Returns {player_name: {1: avg_min, 2: avg_min, 3: avg_min, 4: avg_min}}
    averaged across up to n_games recent games.
    Returns {} if no data available.
    """
    cache_key = f"qtr_avgs_{team_name.replace(' ', '_')}"
    cached = _load_cache(cache_key, ttl_hours=3)
    if cached:
        return cached

    team_id = ESPN_TEAM_IDS.get(team_name)
    if not team_id:
        return {}

    game_ids = get_recent_game_ids(team_id, n_games)
    if not game_ids:
        return {}

    # Accumulate minutes per player per quarter across games
    totals: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))

    for gid in game_ids:
        game_data = _parse_quarter_minutes_from_game(gid, team_id)
        for player, q_mins in game_data.items():
            for q, mins in q_mins.items():
                totals[player][q].append(mins)

    if not totals:
        return {}

    # Average
    averages = {}
    for player, q_data in totals.items():
        total_min = sum(sum(v) / len(v) for v in q_data.values())
        if total_min < 1:
            continue
        averages[player] = {
            q: round(sum(v) / len(v), 1)
            for q, v in q_data.items()
        }

    _save_cache(cache_key, averages, ttl_hours=3)
    return averages


def distribute_quarters(player_name: str, projected_total: float,
                        historical: dict) -> dict[int, float]:
    """
    Scale historical quarter distribution to match projected_total, capped at 10 min/quarter.

    When scaling pushes any quarter over 10.0 (the hard WNBA quarter length), the overflow
    is redistributed to the quarters with the most remaining headroom, iteratively until
    all quarters are within cap and the sum equals projected_total.
    """
    Q_CAP = 10.0

    hist = historical.get(player_name, {})
    if not hist:
        per_q = round(min(projected_total / 4, Q_CAP), 1)
        return {1: per_q, 2: per_q, 3: per_q, 4: per_q}

    hist_total = sum(hist.values())
    if hist_total == 0:
        per_q = round(min(projected_total / 4, Q_CAP), 1)
        return {1: per_q, 2: per_q, 3: per_q, 4: per_q}

    # Initial proportional scale
    scale = projected_total / hist_total
    result = {q: hist.get(q, 0.0) * scale for q in [1, 2, 3, 4]}

    # Iteratively cap at Q_CAP and push overflow to uncapped quarters
    for _ in range(10):
        overflow = sum(max(v - Q_CAP, 0.0) for v in result.values())
        if overflow < 0.01:
            break
        # Clamp capped quarters
        result = {q: min(v, Q_CAP) for q, v in result.items()}
        # Distribute overflow proportionally across quarters still under cap
        uncapped = {q: v for q, v in result.items() if v < Q_CAP}
        if not uncapped:
            break
        unc_total = sum(uncapped.values())
        for q in uncapped:
            share = (result[q] / unc_total) * overflow if unc_total > 0 else overflow / len(uncapped)
            result[q] = min(result[q] + share, Q_CAP)

    # Round and fix rounding drift so sum exactly matches projected_total
    result = {q: round(v, 1) for q, v in result.items()}
    diff = round(projected_total - sum(result.values()), 1)
    if diff != 0:
        # Apply remainder to the quarter furthest from its cap
        headroom = {q: Q_CAP - v for q, v in result.items()}
        target_q = max(headroom, key=headroom.get)
        result[target_q] = round(result[target_q] + diff, 1)

    return result
