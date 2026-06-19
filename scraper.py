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
from datetime import datetime, timedelta
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
    if datetime.now() - ts > timedelta(hours=CACHE_TTL_HOURS):
        return None
    return data.get("payload")


def _save_cache(key: str, payload):
    _cache_path(key).write_text(
        json.dumps({"timestamp": datetime.now().isoformat(), "payload": payload}, indent=2)
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
    Scrapes the official WNBA injury report from wnba.com.
    Returns {player_name: {"status": str, "injury": str, "team": str}}
    Falls back to empty dict if unavailable.
    """
    cache_key = "wnba_injuries"
    cached = _load_cache(cache_key)
    if cached:
        return cached

    injuries = {}

    # Primary: official WNBA injury report page
    url = "https://www.wnba.com/injuries"
    soup = _get(url)

    if soup:
        # WNBA.com renders injury tables per team
        # Try multiple structural patterns the site uses
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            player = cells[0].get_text(strip=True)
            injury = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            status_raw = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            # Some table layouts have status in col 3
            if len(cells) > 3 and not status_raw:
                status_raw = cells[3].get_text(strip=True)
            if player and status_raw:
                injuries[player] = {
                    "status": _normalize_status(status_raw),
                    "injury": injury,
                    "team": "",
                }

        # Try JSON-LD or Next.js __NEXT_DATA__ embedded data
        if not injuries:
            script = soup.find("script", {"id": "__NEXT_DATA__"})
            if script and script.string:
                try:
                    data = json.loads(script.string)
                    # Walk the JSON tree looking for injury arrays
                    injuries.update(_extract_injuries_from_json(data))
                except (json.JSONDecodeError, KeyError):
                    pass

    # Secondary fallback: RotoWire WNBA injury page
    if not injuries:
        injuries = _scrape_rotowire_injuries()

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
# Lineup scraping — RotoWire + Lineups.com
# ---------------------------------------------------------------------------

# RotoWire team name → slug used in their lineup page
RW_LINEUP_SLUGS = {
    "New York Liberty":        "new-york-liberty",
    "Las Vegas Aces":          "las-vegas-aces",
    "Connecticut Sun":         "connecticut-sun",
    "Seattle Storm":           "seattle-storm",
    "Chicago Sky":             "chicago-sky",
    "Minnesota Lynx":          "minnesota-lynx",
    "Los Angeles Sparks":      "los-angeles-sparks",
    "Phoenix Mercury":         "phoenix-mercury",
    "Atlanta Dream":           "atlanta-dream",
    "Washington Mystics":      "washington-mystics",
    "Dallas Wings":            "dallas-wings",
    "Indiana Fever":           "indiana-fever",
    "Golden State Valkyries":  "golden-state-valkyries",
}

# Lineups.com uses slightly different slugs
LINEUPS_COM_SLUGS = {
    "New York Liberty":        "new-york-liberty",
    "Las Vegas Aces":          "las-vegas-aces",
    "Connecticut Sun":         "connecticut-sun",
    "Seattle Storm":           "seattle-storm",
    "Chicago Sky":             "chicago-sky",
    "Minnesota Lynx":          "minnesota-lynx",
    "Los Angeles Sparks":      "los-angeles-sparks",
    "Phoenix Mercury":         "phoenix-mercury",
    "Atlanta Dream":           "atlanta-dream",
    "Washington Mystics":      "washington-mystics",
    "Dallas Wings":            "dallas-wings",
    "Indiana Fever":           "indiana-fever",
    "Golden State Valkyries":  "golden-state-valkyries",
}


def scrape_rotowire_lineups(team_name: str) -> dict:
    """
    Scrapes RotoWire WNBA lineups page for today's projected/confirmed starters.
    Returns:
      {
        "starters": ["Player A", ...],   # up to 5
        "source":   "rotowire",
        "confirmed": bool,               # True if officially confirmed
        "game_time": str,                # e.g. "7:00 PM ET"
        "opponent":  str,
      }
    Returns empty dict if team has no game today or scraping fails.
    """
    cache_key = f"rw_lineup_{team_name.replace(' ', '_')}"
    cached = _load_cache(cache_key)
    if cached:
        return cached

    url = "https://www.rotowire.com/basketball/wnba-lineups.php"
    soup = _get(url)
    if not soup:
        return {}

    result = _parse_rotowire_lineup_page(soup, team_name)
    if result:
        # Lineups are time-sensitive — cache only 30 min
        _cache_path(cache_key).write_text(
            json.dumps({"timestamp": datetime.now().isoformat(), "payload": result}, indent=2)
        )
    return result


def _parse_rotowire_lineup_page(soup: BeautifulSoup, team_name: str) -> dict:
    """
    Parse RotoWire lineups page HTML.
    RotoWire renders each matchup in a card; starters are listed inside.
    """
    slug = RW_LINEUP_SLUGS.get(team_name, "").lower()
    target_keywords = [w.lower() for w in team_name.split()]

    # Each game card has a class like 'lineup__main' or 'lineup'
    game_cards = soup.find_all("div", class_=re.compile(r"lineup__box|lineup-card|lineups__box", re.I))
    if not game_cards:
        # Try broader selector
        game_cards = soup.find_all("div", class_=re.compile(r"lineup", re.I))

    for card in game_cards:
        card_text = card.get_text(" ", strip=True).lower()
        # Check if this card mentions our team
        if not any(kw in card_text for kw in target_keywords):
            continue

        # Determine if confirmed
        confirmed = bool(card.find(string=re.compile(r"confirm", re.I)))

        # Get game time
        time_tag = card.find(class_=re.compile(r"game.?time|time", re.I))
        game_time = time_tag.get_text(strip=True) if time_tag else ""

        # Get opponent
        team_tags = card.find_all(class_=re.compile(r"lineup__team|team.?name", re.I))
        opponent = ""
        for t in team_tags:
            t_text = t.get_text(strip=True)
            if not any(kw in t_text.lower() for kw in target_keywords):
                opponent = t_text
                break

        # Extract starters — RotoWire lists them in order PG/SG/SF/PF/C
        starters = []
        # Look for player links inside this card
        player_links = card.find_all("a", href=re.compile(r"/basketball/player", re.I))
        for link in player_links:
            name = link.get_text(strip=True)
            if name and len(name) > 3 and name not in starters:
                starters.append(name)
            if len(starters) >= 5:
                break

        # Fallback: find list items labeled as starters
        if not starters:
            for li in card.find_all("li", class_=re.compile(r"starter|player", re.I)):
                name = li.get_text(strip=True)
                if name and len(name) > 3:
                    starters.append(name)
                if len(starters) >= 5:
                    break

        if starters:
            return {
                "starters":  starters,
                "source":    "rotowire",
                "confirmed": confirmed,
                "game_time": game_time,
                "opponent":  opponent,
            }

    return {}


def scrape_lineups_com(team_name: str) -> dict:
    """
    Secondary lineup source: lineups.com
    Returns same structure as scrape_rotowire_lineups.
    """
    cache_key = f"lineups_com_{team_name.replace(' ', '_')}"
    cached = _load_cache(cache_key)
    if cached:
        return cached

    slug = LINEUPS_COM_SLUGS.get(team_name, "")
    if not slug:
        return {}

    url = f"https://www.lineups.com/wnba/lineups/{slug}"
    soup = _get(url)
    if not soup:
        return {}

    starters = []
    # lineups.com typically has a starting lineup table/list
    for section in soup.find_all(["div", "ul"], class_=re.compile(r"starter|lineup|starting", re.I)):
        for tag in section.find_all(["a", "span", "li"], class_=re.compile(r"player|name", re.I)):
            name = tag.get_text(strip=True)
            if name and len(name) > 3 and name not in starters:
                starters.append(name)
            if len(starters) >= 5:
                break
        if starters:
            break

    if not starters:
        return {}

    result = {
        "starters":  starters[:5],
        "source":    "lineups.com",
        "confirmed": False,
        "game_time": "",
        "opponent":  "",
    }
    _cache_path(cache_key).write_text(
        json.dumps({"timestamp": datetime.now().isoformat(), "payload": result}, indent=2)
    )
    return result


def get_lineup_for_team(team_name: str) -> dict:
    """
    Master lineup fetch with priority chain:
      1. RotoWire confirmed
      2. RotoWire projected
      3. Lineups.com
      4. Empty dict (caller falls back to depth chart)
    Returns {starters, source, confirmed, game_time, opponent}
    """
    rw = scrape_rotowire_lineups(team_name)
    if rw.get("starters"):
        return rw

    lc = scrape_lineups_com(team_name)
    if lc.get("starters"):
        return lc

    return {}


# ---------------------------------------------------------------------------
# Fallback helpers
# ---------------------------------------------------------------------------

def _fallback_trends(team_abbrev: str) -> dict:
    """Returns baseline trends from static roster data."""
    from roster_data import TEAM_ABBREV_TO_NAME
    team_name = TEAM_ABBREV_TO_NAME.get(team_abbrev, "")
    roster = ROSTERS.get(team_name, {})
    return {
        player: {
            "avg_min": info["avg_min"],
            "last3_avg": info["avg_min"],
            "games": 0,
            "source": "static",
        }
        for player, info in roster.items()
    }


# ---------------------------------------------------------------------------
# Combined data fetch
# ---------------------------------------------------------------------------

def get_team_data(team_name: str) -> dict:
    """
    Returns merged data for a team:
    {
      player_name: {
        pos, avg_min, last3_avg, role, depth, status, injury, games,
        lineup_confirmed  # True if from a confirmed lineup source
      }
    }
    Lineup source overrides depth-chart role assignments when available.
    """
    abbrev = TEAMS.get(team_name, "")
    base_roster = ROSTERS.get(team_name, {})

    # Minutes trends from RotoWire
    trends = scrape_rotowire_team_trends(abbrev)

    # Injury statuses from WNBA official site
    all_injuries = scrape_wnba_injuries()

    # Live lineup (starters confirmed or projected)
    lineup_data = get_lineup_for_team(team_name)
    scraped_starters = [_normalize_name(n) for n in lineup_data.get("starters", [])]
    lineup_confirmed = lineup_data.get("confirmed", False)

    merged = {}
    for player, info in base_roster.items():
        trend = trends.get(player, {})
        inj = all_injuries.get(player, {})

        avg_min = trend.get("avg_min") or info["avg_min"]
        last3 = trend.get("last3_avg") or avg_min

        # Determine role from lineup if available
        norm_player = _normalize_name(player)
        if scraped_starters:
            if norm_player in scraped_starters:
                role = "starter"
                depth = 1
            else:
                role = "bench"
                depth = info["depth"] if info["depth"] > 1 else 2
        else:
            role = info["role"]
            depth = info["depth"]

        merged[player] = {
            "pos":              info["pos"],
            "role":             role,
            "depth":            depth,
            "avg_min":          round(avg_min, 1),
            "last3_avg":        round(last3, 1),
            "games":            trend.get("games", 0),
            "status":           inj.get("status", "Active"),
            "injury":           inj.get("injury", ""),
            "lineup_confirmed": lineup_confirmed,
        }

    # Add scraped starters that aren't in our static roster
    for scraped_name in lineup_data.get("starters", []):
        norm = _normalize_name(scraped_name)
        already_in = any(_normalize_name(p) == norm for p in merged)
        if not already_in and scraped_name.strip():
            merged[scraped_name] = {
                "pos":              "?",
                "role":             "starter",
                "depth":            1,
                "avg_min":          25.0,
                "last3_avg":        25.0,
                "games":            0,
                "status":           "Active",
                "injury":           "",
                "lineup_confirmed": lineup_confirmed,
                "note":             "Added from scraped lineup — verify position",
            }

    return merged


def get_lineup_info(team_name: str) -> dict:
    """Returns raw lineup metadata for display in the UI."""
    return get_lineup_for_team(team_name)


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation for fuzzy matching."""
    return re.sub(r"[^a-z ]", "", name.lower().strip())


def get_all_injuries() -> dict:
    """Returns the full injury report dict from the official WNBA site."""
    return scrape_wnba_injuries()
