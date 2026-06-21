"""
Scrapes WNBA lineups, minutes trends, and injury reports.
Priority chain for lineups:
  1. RotoWire confirmed lineups (game day, ~1h before tip)
  2. RotoWire projected lineups (1-2 days out)
  3. Lineups.com projected lineups (cross-check)
  4. Static depth chart fallback (roster_data.py)
"""

import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from roster_data import ROSTERS, TEAMS

CACHE_DIR = Path(__file__).parent / "data"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL_HOURS = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _load_cache(key: str):
    p = _cache_path(key)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    ts = datetime.fromisoformat(data.get("timestamp", "2000-01-01"))
    ttl = data.get("ttl_hours", CACHE_TTL_HOURS)
    if datetime.now() - ts > timedelta(hours=ttl):
        return None
    return data.get("payload")


def _save_cache(key: str, payload, ttl_hours: float = CACHE_TTL_HOURS):
    _cache_path(key).write_text(
        json.dumps({"timestamp": datetime.now().isoformat(),
                    "payload": payload, "ttl_hours": ttl_hours}, indent=2)
    )


def _get(url: str, timeout: int = 10) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        print(f"[scraper] GET {url} failed: {e}")
        return None


# ---------------------------------------------------------------------------
# RotoWire – team trends / minutes
# ---------------------------------------------------------------------------

def scrape_rotowire_team_trends(team_abbrev: str) -> dict:
    """
    Returns {player_name: {"avg_min": float, "last3_avg": float, "games": int}}
    scraped from RotoWire WNBA team page.
    Falls back to static roster data on failure.
    """
    cache_key = f"rotowire_trends_{team_abbrev}"
    cached = _load_cache(cache_key)
    if cached:
        return cached

    # Map abbrev → RotoWire slug
    rw_slugs = {
        "NYL": "new-york-liberty", "LVA": "las-vegas-aces",
        "CON": "connecticut-sun",  "SEA": "seattle-storm",
        "CHI": "chicago-sky",      "MIN": "minnesota-lynx",
        "LAS": "los-angeles-sparks", "PHX": "phoenix-mercury",
        "ATL": "atlanta-dream",    "WAS": "washington-mystics",
        "DAL": "dallas-wings",     "IND": "indiana-fever",
    }
    slug = rw_slugs.get(team_abbrev)
    if not slug:
        return _fallback_trends(team_abbrev)

    url = f"https://www.rotowire.com/basketball/team-stats.php?team={slug}&league=WNBA"
    soup = _get(url)
    if not soup:
        return _fallback_trends(team_abbrev)

    result = {}
    # RotoWire team stats table has player rows with minutes columns
    table = soup.find("table", {"class": re.compile(r"(player-stats|stats-table)", re.I)})
    if table:
        headers_row = table.find("thead")
        col_names = []
        if headers_row:
            col_names = [th.get_text(strip=True).lower() for th in headers_row.find_all("th")]

        for row in table.find("tbody", {}).find_all("tr") if table.find("tbody") else []:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            name_tag = cells[0].find("a") or cells[0]
            name = name_tag.get_text(strip=True)
            if not name:
                continue
            try:
                mins_idx = next((i for i, h in enumerate(col_names) if "min" in h), 2)
                avg_min = float(cells[mins_idx].get_text(strip=True) or 0)
            except (ValueError, IndexError):
                avg_min = 0
            result[name] = {"avg_min": avg_min, "last3_avg": avg_min, "games": 0}

    if not result:
        return _fallback_trends(team_abbrev)

    _save_cache(cache_key, result)
    return result


def scrape_rotowire_news(team_abbrev: str) -> list[dict]:
    """
    Scrapes recent player news/trends from RotoWire WNBA news feed.
    Returns list of {player, headline, detail}.
    """
    cache_key = f"rotowire_news_{team_abbrev}"
    cached = _load_cache(cache_key)
    if cached:
        return cached

    url = "https://www.rotowire.com/basketball/wnba-news.php"
    soup = _get(url)
    if not soup:
        return []

    news = []
    for item in (soup.find_all("div", class_=re.compile(r"news-item|player-news", re.I)) or []):
        player_tag = item.find("a", class_=re.compile(r"player", re.I))
        headline = item.find(class_=re.compile(r"headline|title", re.I))
        detail = item.find(class_=re.compile(r"news-desc|detail|analysis", re.I))
        if player_tag:
            news.append({
                "player": player_tag.get_text(strip=True),
                "headline": headline.get_text(strip=True) if headline else "",
                "detail": detail.get_text(strip=True) if detail else "",
            })

    _save_cache(cache_key, news)
    return news


# ---------------------------------------------------------------------------
# Official WNBA injury report  (wnba.com)
# ---------------------------------------------------------------------------

# Map display statuses from WNBA site to our internal values
_WNBA_STATUS_MAP = {
    "out":           "Out",
    "doubtful":      "Doubtful",
    "questionable":  "Questionable",
    "probable":      "Probable",
    "day-to-day":    "Day-To-Day",
    "gtd":           "Day-To-Day",
    "game time decision": "Day-To-Day",
    "active":        "Active",
    "available":     "Active",
}


def _normalize_status(raw: str) -> str:
    return _WNBA_STATUS_MAP.get(raw.lower().strip(), raw.strip().title())


def scrape_wnba_injuries() -> dict:
    """
    Returns {player_name: {"status": str, "injury": str, "team": str}}
    Primary: ESPN injuries API (confirmed working).
    """
    cache_key = "wnba_injuries"
    cached = _load_cache(cache_key)
    if cached:
        return cached

    injuries = {}

    # ESPN injuries API — confirmed working
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/injuries",
            headers=HEADERS, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        for team_block in data.get("injuries", []):
            team_name = team_block.get("team", {}).get("displayName", "")
            for entry in team_block.get("injuries", []):
                name       = entry.get("athlete", {}).get("displayName", "")
                status_raw = entry.get("status", "")
                desc       = entry.get("type", {}).get("description", "")
                if name and status_raw:
                    injuries[name] = {
                        "status": _normalize_status(status_raw),
                        "injury": desc,
                        "team":   team_name,
                    }
    except Exception as e:
        print(f"[scraper] ESPN injuries API failed: {e}")

    if injuries:
        _save_cache(cache_key, injuries)
    return injuries


def _extract_injuries_from_json(data, depth: int = 0) -> dict:
    """Recursively search Next.js page props for injury data."""
    injuries = {}
    if depth > 8:
        return injuries
    if isinstance(data, dict):
        for key, val in data.items():
            if key in ("injuries", "injuryReport", "players") and isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        name = (item.get("playerName") or item.get("name") or
                                item.get("displayName") or "")
                        status = (item.get("status") or item.get("injuryStatus") or "")
                        injury = (item.get("comment") or item.get("injury") or
                                  item.get("description") or "")
                        team = (item.get("teamName") or item.get("team") or "")
                        if name and status:
                            injuries[name] = {
                                "status": _normalize_status(status),
                                "injury": injury,
                                "team": team,
                            }
            else:
                injuries.update(_extract_injuries_from_json(val, depth + 1))
    elif isinstance(data, list):
        for item in data:
            injuries.update(_extract_injuries_from_json(item, depth + 1))
    return injuries


def _scrape_rotowire_injuries() -> dict:
    """Secondary fallback: RotoWire WNBA injury report."""
    injuries = {}
    url = "https://www.rotowire.com/basketball/wnba-injuries.php"
    soup = _get(url)
    if not soup:
        return injuries

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        player_tag = cells[0].find("a") or cells[0]
        player = player_tag.get_text(strip=True)
        team = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        injury = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        status_raw = cells[4].get_text(strip=True) if len(cells) > 4 else ""
        if player and status_raw:
            injuries[player] = {
                "status": _normalize_status(status_raw),
                "injury": injury,
                "team": team,
            }
    return injuries


# Keep old name as alias so nothing else breaks
def scrape_espn_injuries() -> dict:
    return scrape_wnba_injuries()


def scrape_espn_recent_games(team_name: str, n: int = 5) -> list[dict]:
    """
    Returns list of recent game box scores for a team (up to n games).
    Each item: {date, opponent, players: {name: minutes_played}}
    """
    abbrev = TEAMS.get(team_name, "")
    cache_key = f"espn_games_{abbrev}"
    cached = _load_cache(cache_key)
    if cached:
        return cached[:n]

    espn_ids = {
        "NYL": 17, "LVA": 20, "CON": 3, "SEA": 14, "CHI": 4, "MIN": 8,
        "LAS": 6, "PHX": 12, "ATL": 1, "WAS": 19, "DAL": 5, "IND": 16,
    }
    team_id = espn_ids.get(abbrev)
    if not team_id:
        return []

    url = f"https://www.espn.com/wnba/team/schedule/_/id/{team_id}"
    soup = _get(url)
    if not soup:
        return []

    game_links = []
    for a in soup.find_all("a", href=re.compile(r"/wnba/game/_/gameId/")):
        href = a.get("href", "")
        game_id = re.search(r"gameId/(\d+)", href)
        if game_id and href not in [g.get("url", "") for g in game_links]:
            game_links.append({"url": href, "game_id": game_id.group(1)})
        if len(game_links) >= n:
            break

    games = []
    for g in game_links:
        box_url = f"https://www.espn.com/wnba/boxscore/_/gameId/{g['game_id']}"
        box_soup = _get(box_url)
        if not box_soup:
            continue
        game_data = _parse_boxscore(box_soup, team_name)
        if game_data:
            games.append(game_data)
        time.sleep(0.5)

    _save_cache(cache_key, games)
    return games


def _parse_boxscore(soup: BeautifulSoup, team_name: str) -> dict | None:
    """Extract minutes per player from ESPN boxscore page."""
    players = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            name_cell = cells[0]
            name = name_cell.get_text(strip=True)
            # ESPN usually puts MIN in column index 2 for boxscores
            for i, cell in enumerate(cells):
                text = cell.get_text(strip=True)
                if re.match(r"^\d{1,2}:\d{2}$", text):  # MM:SS format
                    mins_played = int(text.split(":")[0])
                    if name:
                        players[name] = mins_played
                    break
    if not players:
        return None
    return {"team": team_name, "players": players, "date": ""}


# ---------------------------------------------------------------------------
# Live roster scraping from ESPN
# ---------------------------------------------------------------------------

# ESPN team IDs for WNBA
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

# ESPN position codes → our position codes
ESPN_POS_MAP = {
    "PG": "G", "SG": "G", "G": "G",
    "SF": "F", "PF": "F", "F": "F",
    "C":  "C",
    "G/F": "G/F", "F/C": "F/C",
}


def scrape_espn_roster(team_name: str) -> dict:
    """
    Fetches live roster from ESPN's public JSON API.
    Returns {player_name: {pos}} or {} on failure.
    Cached for 6 hours.
    """
    cache_key = f"espn_roster_{team_name.replace(' ', '_')}"
    cached = _load_cache(cache_key)
    if cached:
        return cached

    team_id = ESPN_TEAM_IDS.get(team_name)
    if not team_id:
        return {}

    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/roster"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[scraper] ESPN roster API failed for {team_name}: {e}")
        return {}

    roster = {}
    for player in data.get("athletes", []):
        name = player.get("displayName", "")
        if not name or len(name) < 3:
            continue
        pos_info = player.get("position", {})
        pos_raw  = pos_info.get("abbreviation", "") if isinstance(pos_info, dict) else ""
        pos      = ESPN_POS_MAP.get(pos_raw, pos_raw or "?")
        roster[name] = {"pos": pos}

    if roster:
        _cache_path(cache_key).write_text(
            json.dumps({"timestamp": datetime.now().isoformat(),
                        "payload": roster, "ttl_hours": 6}, indent=2)
        )
    return roster


def get_live_roster(team_name: str) -> dict:
    """
    Returns the live ESPN roster merged with static fallback.
    Uses fuzzy name matching so ESPN name variants (apostrophes, accents,
    suffixes) still map to static roster entries correctly.
    Result: {player_name: {pos, avg_min, role, depth}}
    """
    live = scrape_espn_roster(team_name)
    static = ROSTERS.get(team_name, {})

    if not live:
        return static  # full fallback

    static_names = list(static.keys())
    merged = {}

    # Start from live roster — every player ESPN lists is on the team.
    # Use the ESPN canonical name as the key (most up-to-date),
    # but pull avg_min/role/depth from the fuzzy-matched static entry.
    for player, info in live.items():
        static_info = static.get(player)
        if static_info is None:
            matched = _fuzzy_match_name(player, static_names)
            static_info = static.get(matched, {}) if matched else {}

        merged[player] = {
            "pos":       info.get("pos") or static_info.get("pos", "?"),
            "avg_min":   static_info.get("avg_min", 3.0),
            "last3_avg": static_info.get("avg_min", 3.0),
            "role":      static_info.get("role", "bench"),
            "depth":     static_info.get("depth", 2),
        }

    if not merged:
        return static

    return merged


# ---------------------------------------------------------------------------
# Lineup detection via ESPN APIs
# ---------------------------------------------------------------------------

def _get_todays_game_id(team_name: str) -> tuple[str, str, str]:
    """
    Returns (game_id, opponent_name, game_time_str) for today's game,
    or ("", "", "") if no game today.
    """
    team_id = ESPN_TEAM_IDS.get(team_name)
    if not team_id:
        return "", "", ""

    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
            headers=HEADERS, timeout=10
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return "", "", ""

    tid_str = str(team_id)
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        team_ids = [str(c.get("team", {}).get("id", "")) for c in competitors]
        if tid_str not in team_ids:
            continue

        game_id = event.get("id", "")
        game_time = event.get("status", {}).get("type", {}).get("shortDetail", "")

        # Opponent name
        opponent = ""
        for c in competitors:
            if str(c.get("team", {}).get("id", "")) != tid_str:
                opponent = c.get("team", {}).get("displayName", "")
                break

        return game_id, opponent, game_time

    return "", "", ""


def _get_confirmed_starters(game_id: str, team_id: int) -> list[str]:
    """
    Once a game is in progress or just tipped off, ESPN boxscore
    marks starter=True. Returns list of confirmed starter names.
    """
    try:
        r = requests.get(
            f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary?event={game_id}",
            headers=HEADERS, timeout=10
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    tid_str = str(team_id)
    starters = []
    for team_block in data.get("boxscore", {}).get("players", []):
        if str(team_block.get("team", {}).get("id", "")) != tid_str:
            continue
        for stat_group in team_block.get("statistics", []):
            for athlete in stat_group.get("athletes", []):
                if athlete.get("starter") and not athlete.get("didNotPlay"):
                    name = athlete.get("athlete", {}).get("displayName", "")
                    if name:
                        starters.append(name)
    return starters


def _get_game_injuries(game_id: str, team_id: int) -> dict:
    """
    Returns {player_name: {status, injury}} from ESPN game summary injury report.
    More accurate than the general injury page because it's game-specific.
    """
    try:
        r = requests.get(
            f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary?event={game_id}",
            headers=HEADERS, timeout=10
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return {}

    tid_str = str(team_id)
    injuries = {}
    for team_block in data.get("injuries", []):
        if str(team_block.get("team", {}).get("id", "")) != tid_str:
            continue
        for entry in team_block.get("injuries", []):
            name = entry.get("athlete", {}).get("displayName", "")
            raw_status = entry.get("status", "")
            injury_type = entry.get("type", {}).get("description", "")
            if name:
                injuries[name] = {
                    "status": _normalize_status(raw_status),
                    "injury": injury_type,
                }
    return injuries


def _infer_starters_from_history(team_name: str, injured_out: set) -> list[str]:
    """
    When no confirmed lineup exists, infer starters from the most recent
    game's opening lineup via play-by-play (starter=True in boxscore).
    Excludes players who are confirmed out today.
    """
    from quarter_minutes import get_recent_game_ids, ESPN_TEAM_IDS as QTR_IDS
    team_id = QTR_IDS.get(team_name)
    if not team_id:
        return []

    game_ids = get_recent_game_ids(team_id, n=3)
    for gid in game_ids:
        starters = _get_confirmed_starters(gid, team_id)
        if starters:
            # Filter out players confirmed out today
            filtered = [s for s in starters if s not in injured_out]
            if len(filtered) >= 4:
                return filtered[:5]
    return []


def get_lineup_for_team(team_name: str) -> dict:
    """
    Master lineup fetch — all via ESPN APIs.

    Priority:
      1. ESPN boxscore confirmed starters (game in progress / just tipped)
      2. ESPN most-recent-game historical starters (adjusted for today's injuries)
      3. Empty dict — UI falls back to depth chart

    Returns {starters, source, confirmed, game_time, opponent, game_injuries}
    """
    cache_key = f"lineup_{team_name.replace(' ', '_')}"
    cached = _load_cache(cache_key)
    if cached:
        return cached

    team_id = ESPN_TEAM_IDS.get(team_name)
    if not team_id:
        return {}

    game_id, opponent, game_time = _get_todays_game_id(team_name)

    # Get game-specific injuries (more accurate than general report)
    game_injuries = {}
    if game_id:
        game_injuries = _get_game_injuries(game_id, team_id)

    injured_out = {p for p, info in game_injuries.items() if info["status"] == "Out"}

    # Try confirmed starters from live boxscore first
    if game_id:
        confirmed_starters = _get_confirmed_starters(game_id, team_id)
        if confirmed_starters:
            result = {
                "starters":      confirmed_starters,
                "source":        "ESPN (confirmed)",
                "confirmed":     True,
                "game_time":     game_time,
                "opponent":      opponent,
                "game_injuries": game_injuries,
            }
            # Short cache — game is live
            _cache_path(cache_key).write_text(
                json.dumps({"timestamp": datetime.now().isoformat(),
                            "payload": result, "ttl_hours": 0.25}, indent=2)
            )
            return result

    # Fall back to historical starters adjusted for today's injuries
    historical_starters = _infer_starters_from_history(team_name, injured_out)
    if historical_starters:
        note = "based on most recent game lineup"
        if injured_out:
            note += f" (adjusted: {', '.join(injured_out)} out)"
        result = {
            "starters":      historical_starters,
            "source":        f"ESPN (projected — {note})",
            "confirmed":     False,
            "game_time":     game_time,
            "opponent":      opponent,
            "game_injuries": game_injuries,
        }
        _cache_path(cache_key).write_text(
            json.dumps({"timestamp": datetime.now().isoformat(),
                        "payload": result, "ttl_hours": 1}, indent=2)
        )
        return result

    # No game today or no data
    if game_id:
        return {
            "starters":      [],
            "source":        "ESPN",
            "confirmed":     False,
            "game_time":     game_time,
            "opponent":      opponent,
            "game_injuries": game_injuries,
        }
    return {}


# ---------------------------------------------------------------------------
# Combined data fetch — season stats as primary source
# ---------------------------------------------------------------------------

def get_team_data(team_name: str) -> dict:
    """
    Builds team data using:
      1. season_stats  — real avg_min, last3_avg, starter_pct from every game played
      2. ESPN roster   — live position data
      3. Lineup        — today's projected/confirmed starters + game injuries
      4. Static roster — absolute fallback for new/expansion teams with no games yet

    Returns {player: {pos, avg_min, last3_avg, role, depth, starter_pct,
                       games_played, status, injury, lineup_confirmed}}
    """
    from season_stats import get_team_season_stats

    # 1. Season stats (primary)
    season = get_team_season_stats(team_name)
    season_players = season.get("players", {})
    # How many players does this team actually rotate? Derived from game logs.
    # Bench cap = rotation_depth minus 5 starters, clamped to [2, 7].
    rotation_depth = season.get("rotation_depth", 8)
    bench_slots = max(2, min(rotation_depth - 5, 7))

    # 2. Live ESPN roster for positions
    live_roster = get_live_roster(team_name)

    # 3. Today's lineup + game-specific injuries
    lineup_data     = get_lineup_for_team(team_name)
    today_starters  = lineup_data.get("starters", [])
    lineup_confirmed = lineup_data.get("confirmed", False)
    game_injuries   = lineup_data.get("game_injuries", {})

    # 4. General injury report (for teams with no game today)
    general_injuries = scrape_wnba_injuries()

    # Merge injury sources — game-specific wins over general
    def get_injury(player: str) -> tuple[str, str]:
        if player in game_injuries:
            return game_injuries[player]["status"], game_injuries[player].get("injury", "")
        if player in general_injuries:
            return general_injuries[player]["status"], general_injuries[player].get("injury", "")
        return "Active", ""

    # Determine roles:
    # If we have today's starters → use that as ground truth
    # Otherwise → use starter_pct from season stats (>=50% = starter)
    today_starter_set = set(today_starters)
    use_lineup_roles  = len(today_starters) >= 4

    # Build player set: INTERSECTION of current ESPN roster and season stats.
    # live_roster is the authority — anyone ESPN no longer lists (waived, traded)
    # is excluded even if they have season stats. Season-stats-only players who
    # somehow slipped past the ESPN roster check are also dropped.
    # Falls back to union if the live roster fetch failed (empty dict).
    if live_roster:
        all_players = set(live_roster.keys())
    else:
        all_players = set(season_players.keys())
    total_games = season.get("games_processed", 1) or 1

    merged = {}
    for player in all_players:
        sp   = season_players.get(player, {})
        live = live_roster.get(player, {})

        avg_min         = sp.get("avg_min",         live.get("avg_min", 10.0))
        clean_avg_min   = sp.get("clean_avg_min",   avg_min)
        last3_avg       = sp.get("last3_avg",        avg_min)
        last3_clean_avg = sp.get("last3_clean_avg",  last3_avg)
        gp              = sp.get("games_played",     0)
        gs              = sp.get("games_started",    0)
        foul_rate       = sp.get("foul_rate",        0.0)
        foul_trouble_gm = sp.get("foul_trouble_games", 0)
        start_pct       = sp.get("starter_pct",     0.0)
        pos             = live.get("pos") or "?"

        status, injury = get_injury(player)

        # Auto-Out: never seen a minute this season AND not in today's confirmed lineup.
        # Flag with zero_min_season so the UI can label them clearly and users can override.
        zero_min_season = (gp == 0 and player not in today_starter_set)
        if zero_min_season and status == "Active":
            status = "Out"

        on_injury_report = status in ("Out", "Doubtful", "Questionable", "Day-To-Day", "Probable")

        # Skip entirely only if: never played AND not in today's lineup AND not injured
        if gp == 0 and player not in today_starter_set and not on_injury_report:
            continue

        # Keep players averaging 5+ min (down from 8) or in lineup or on injury report.
        # 5 min catches legitimate bench rotators who may be breakout candidates.
        if avg_min < 5.0 and player not in today_starter_set and not on_injury_report:
            continue

        # Role assignment
        if use_lineup_roles:
            role  = "starter" if player in today_starter_set else "bench"
            depth = 1 if role == "starter" else 2
        else:
            # For returning players (low gp but high start_pct or on ESPN roster),
            # check if they were a starter before injury by looking at their games
            role  = "starter" if start_pct >= 0.50 else "bench"
            depth = 1 if role == "starter" else (2 if start_pct >= 0.10 else 3)

        last_played = sp.get("last_played_date", "")
        recently_active = False
        if last_played:
            try:
                days_ago = (date.today() - date.fromisoformat(last_played)).days
                recently_active = days_ago <= 5
            except ValueError:
                pass

        merged[player] = {
            "pos":               pos,
            "role":              role,
            "depth":             depth,
            "avg_min":           round(avg_min, 1),
            "ewma_min":          sp.get("ewma_min", round(avg_min, 1)),
            "clean_avg_min":     round(clean_avg_min, 1),
            "last3_avg":         round(last3_avg, 1),
            "last3_clean_avg":   round(last3_clean_avg, 1),
            "last_game_min":     round(sp.get("last_game_min", 0.0), 1),
            "last3_range":       sp.get("last3_range", 0.0) or 0.0,
            "games_played":      gp,
            "games_started":     gs,
            "foul_rate":         foul_rate,
            "foul_trouble_games": foul_trouble_gm,
            "starter_pct":       start_pct,
            "quarter_avgs":      sp.get("quarter_avgs", {}),
            "status":            status,
            "injury":            injury,
            "lineup_confirmed":  lineup_confirmed,
            "zero_min_season":   zero_min_season,
            "recently_active":   recently_active,
            "last_played_date":  last_played,
        }

    # Add any today's starters not yet in merged (new callups etc.)
    for name in today_starters:
        if name not in merged and name.strip():
            status, injury = get_injury(name)
            merged[name] = {
                "pos":              "?",
                "role":             "starter",
                "depth":            1,
                "avg_min":          15.0,
                "last3_avg":        15.0,
                "last_game_min":    0.0,
                "games_played":     0,
                "starter_pct":      1.0,
                "status":           status,
                "injury":           injury,
                "lineup_confirmed": lineup_confirmed,
                "zero_min_season":  False,
            }

    # ---------------------------------------------------------------------------
    # Bench trimming: remove deep bench players that exceed the minutes budget.
    # Recently-active players (played in last 5 days) are always kept.
    # Players on the injury report keep their status and are always kept.
    # Everyone else beyond the bench_cap is simply removed from merged so they
    # don't appear in the UI at all — no DNP label needed.
    # ---------------------------------------------------------------------------
    active_starters = [
        p for name, p in merged.items()
        if p["role"] == "starter" and p["status"] not in ("Out", "Doubtful")
    ]
    active_bench = [
        (name, p) for name, p in merged.items()
        if p["status"] == "Active"
        and p["role"] == "bench"
        and name not in today_starter_set
    ]

    def _bench_sort_score(p: dict) -> float:
        return 0.75 * p["last3_avg"] + 0.25 * p["avg_min"]

    active_bench.sort(key=lambda x: -_bench_sort_score(x[1]))

    starter_min_sum = sum(p.get("last3_avg", p["avg_min"]) for p in active_starters)
    remaining_budget = max(200.0 - starter_min_sum + 10.0, 30.0)
    bench_cap = 0
    running = 0.0
    for _, p in active_bench:
        if running >= remaining_budget:
            break
        running += _bench_sort_score(p)
        bench_cap += 1
    bench_cap = max(2, min(bench_cap, bench_slots))

    to_remove = []
    for i, (name, p) in enumerate(active_bench):
        if p.get("recently_active"):
            continue
        if i >= bench_cap:
            to_remove.append(name)
    for name in to_remove:
        del merged[name]

    # Attach metadata under a reserved key so the UI can surface it.
    # Player keys are never "__meta__" so this won't collide.
    merged["__meta__"] = {
        "rotation_depth": rotation_depth,
        "bench_slots":    bench_slots,
        "last_updated":   season.get("last_updated", ""),
        "games_processed": season.get("games_processed", 0),
    }

    return merged


def _fallback_trends(team_abbrev: str) -> dict:
    """Returns baseline from static roster — only used if ESPN is unreachable."""
    from roster_data import TEAM_ABBREV_TO_NAME
    team_name = TEAM_ABBREV_TO_NAME.get(team_abbrev, "")
    roster = ROSTERS.get(team_name, {})
    return {
        player: {"avg_min": info["avg_min"], "last3_avg": info["avg_min"],
                 "games": 0, "source": "static"}
        for player, info in roster.items()
    }


def get_lineup_info(team_name: str) -> dict:
    """Returns raw lineup metadata for display in the UI."""
    return get_lineup_for_team(team_name)


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation for fuzzy matching."""
    return re.sub(r"[^a-z ]", "", name.lower().strip())


def _fuzzy_match_name(query: str, candidates: list[str]) -> str | None:
    """
    Find best match for query in candidates using normalized comparison.
    Handles apostrophe variants, accents, suffixes (Jr., Sr., II, III).
    Returns matched candidate name or None.
    """
    def normalize(n: str) -> str:
        # Lowercase, remove accents via ASCII approximation, strip suffixes + punctuation
        n = n.lower()
        # Common accent substitutions
        for src, dst in [("é","e"),("è","e"),("ê","e"),("à","a"),("â","a"),("î","i"),
                         ("ô","o"),("û","u"),("ç","c"),("ñ","n"),("ü","u"),("ö","o")]:
            n = n.replace(src, dst)
        # Strip name suffixes
        n = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", n)
        # Strip all non-alpha chars
        n = re.sub(r"[^a-z ]", "", n).strip()
        # Collapse whitespace
        return re.sub(r"\s+", " ", n)

    q_norm = normalize(query)
    q_tokens = set(q_norm.split())

    best_name = None
    best_score = 0

    for cand in candidates:
        c_norm = normalize(cand)
        if q_norm == c_norm:
            return cand  # exact normalized match

        c_tokens = set(c_norm.split())
        # Score = number of shared tokens; prefer longer matches
        shared = q_tokens & c_tokens
        score = len(shared)
        if score > best_score and score >= 2:  # need at least first+last name match
            best_score = score
            best_name = cand

    return best_name


def get_all_injuries() -> dict:
    """Returns the full injury report dict from the official WNBA site."""
    return scrape_wnba_injuries()


def get_all_players() -> list[str]:
    """
    Returns a sorted list of every known WNBA player for the manual-add dropdown.

    Sources (fast — no season stats scraping):
      1. Current ESPN rosters for all 15 teams (one lightweight API call per team)
      2. Static fallback from roster_data.py (catches anyone ESPN misses)

    Deliberately excludes season stats to keep this fast after a cache wipe.
    Season stats build lazily as individual teams are selected in the app.
    Cached for 6 hours.
    """
    from roster_data import ROSTERS

    cache_key = "all_players_combined"
    cached = _load_cache(cache_key)
    if cached:
        return cached

    all_names: set[str] = set()

    # Source 1: current ESPN rosters (roster endpoint is fast — no boxscores)
    for team_name in TEAMS:
        roster = scrape_espn_roster(team_name)
        all_names.update(roster.keys())

    # Source 2: static fallback
    for roster in ROSTERS.values():
        all_names.update(roster.keys())

    result = sorted(all_names)
    _cache_path(cache_key).write_text(
        json.dumps({"timestamp": datetime.now().isoformat(),
                    "payload": result, "ttl_hours": 6}, indent=2)
    )
    return result
