"""
Opponent matchup adjustments for minutes projections.

Approach: team-level signals from game logs, not player-vs-team career stats.
Reasons:
  - WNBA teams play each other 2-4x per season → too few games for player-level
    opponent splits to be statistically meaningful.
  - Roster turnover is high year-to-year, so career splits against a team carry
    less weight than what that team has been doing THIS season.

Signals used (all derived from this season's game logs):
  1. Opponent pace: fast-paced opponents → more possessions → more sub opportunities
     → slightly wider rotation.
  2. Opponent foul rate: opponents who draw fouls force starters into foul trouble,
     increasing bench minutes.
  3. Blowout tendency: games that become blowouts early expand the rotation (more
     bench) or collapse it (playing out the clock with bench). Uses margin from
     each team's past meetings.
  4. Opponent size tendency: teams that play heavy big-lineup minutes may force
     the other team into more C/F minutes (starters) at the expense of G bench.

Outputs per player: a minutes adjustment in the range [-3, +3].
The adjustment is intentionally conservative — only 1-3 min — because these are
team tendencies, not certainties, and over-adjusting creates false precision.
"""

from __future__ import annotations
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

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


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _load_cache(key: str, ttl_hours: float = 6.0):
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


def _save_cache(key: str, payload, ttl_hours: float = 6.0):
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
        print(f"[matchup] {url}: {e}")
        return {}


def _parse_score(raw) -> int | None:
    """
    ESPN returns score as a dict {"value": 82, "displayValue": "82"},
    a plain string "82", or occasionally a bare int.
    Returns int or None on failure.
    """
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("displayValue", None))
    try:
        return int(float(str(raw)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Opponent team profile
# ---------------------------------------------------------------------------

def _get_opponent_profile(opp_name: str) -> dict:
    """
    Returns a profile dict for the opponent team with stats that affect
    rotation decisions:
      pace_rank:        1=fastest, 15=slowest (relative to league this season)
      foul_draw_rate:   avg fouls drawn per game (high = foul trouble risk)
      blowout_rate:     fraction of games decided by 15+ points
      avg_players_used: avg distinct players who logged minutes per game
      rotation_depth:   their typical rotation size (from our season_stats cache)
    """
    cache_key = f"opp_profile_{opp_name.replace(' ', '_')}"
    cached = _load_cache(cache_key, ttl_hours=6.0)
    if cached:
        return cached

    # Try to read from our existing season stats cache for this team
    season_cache_key = f"season_{opp_name.replace(' ', '_')}"
    season_path = _cache_path(season_cache_key)
    if season_path.exists():
        try:
            raw = json.loads(season_path.read_text(encoding="utf-8"))
            payload = raw.get("payload", {})
            rotation_depth = payload.get("rotation_depth", 8)
            games_processed = payload.get("games_processed", 0)
        except Exception:
            rotation_depth = 8
            games_processed = 0
    else:
        rotation_depth = 8
        games_processed = 0

    team_id = ESPN_TEAM_IDS.get(opp_name)
    profile = {
        "rotation_depth":   rotation_depth,
        "games_processed":  games_processed,
        "pace_factor":      0.0,   # >0 = fast pace, <0 = slow
        "foul_pressure":    0.0,   # >0 = draws lots of fouls
        "blowout_rate":     0.0,   # 0.0-1.0
        "avg_margin":       0.0,   # positive = they win big, negative = close games
        "sample_games":     0,
    }

    if not team_id:
        _save_cache(cache_key, profile)
        return profile

    # Fetch completed game results to compute margin/blowout tendency
    data = _get_json(
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/schedule"
    )

    margins = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        if not comp.get("status", {}).get("type", {}).get("completed"):
            continue
        competitors = comp.get("competitors", [])
        scores = {}
        for c in competitors:
            tid = str(c.get("team", {}).get("id", ""))
            val = _parse_score(c.get("score"))
            if val is not None:
                scores[tid] = val
        if len(scores) == 2:
            vals = list(scores.values())
            margin = abs(vals[0] - vals[1])
            margins.append(margin)

    if margins:
        blowout_count = sum(1 for m in margins if m >= 15)
        profile["blowout_rate"]   = round(blowout_count / len(margins), 2)
        profile["avg_margin"]     = round(sum(margins) / len(margins), 1)
        profile["sample_games"]   = len(margins)

    # Pace proxy: use rotation_depth as a rough pace signal.
    # Teams with deeper rotations tend to play faster (more subs = more possessions).
    profile["pace_factor"] = round((rotation_depth - 8.0) * 0.25, 2)

    _save_cache(cache_key, profile)
    return profile


# ---------------------------------------------------------------------------
# Head-to-head history: team vs opponent this season
# ---------------------------------------------------------------------------

def _get_h2h_results(team_name: str, opp_name: str) -> list[dict]:
    """
    Returns list of {margin, team_score, opp_score, display} for each completed
    game between team_name and opp_name this season.
    margin > 0 means team_name won.
    display is a string like "W 84-71" or "L 68-79".
    """
    team_id = ESPN_TEAM_IDS.get(team_name)
    opp_id  = ESPN_TEAM_IDS.get(opp_name)
    if not team_id or not opp_id:
        return []

    cache_key = f"h2h_results_{team_name.replace(' ','_')}_{opp_name.replace(' ','_')}"
    cached = _load_cache(cache_key, ttl_hours=6.0)
    if cached:
        return cached

    data = _get_json(
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/schedule"
    )

    results = []
    opp_id_str  = str(opp_id)
    team_id_str = str(team_id)

    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        if not comp.get("status", {}).get("type", {}).get("completed"):
            continue
        competitors = comp.get("competitors", [])
        team_ids = [str(c.get("team", {}).get("id", "")) for c in competitors]
        if opp_id_str not in team_ids:
            continue
        scores = {}
        for c in competitors:
            tid = str(c.get("team", {}).get("id", ""))
            val = _parse_score(c.get("score"))
            if val is not None:
                scores[tid] = val
        if team_id_str in scores and opp_id_str in scores:
            ts = scores[team_id_str]
            os_ = scores[opp_id_str]
            margin = ts - os_
            win = margin > 0
            results.append({
                "margin":      margin,
                "team_score":  ts,
                "opp_score":   os_,
                "display":     f"{'W' if win else 'L'} {max(ts,os_)}-{min(ts,os_)}",
            })

    _save_cache(cache_key, results, ttl_hours=6.0)
    return results


def _get_h2h_margins(team_name: str, opp_name: str) -> list[int]:
    """Legacy wrapper — returns plain margin list for callers that only need that."""
    return [r["margin"] for r in _get_h2h_results(team_name, opp_name)]


# ---------------------------------------------------------------------------
# Per-player minutes vs opponent this season
# ---------------------------------------------------------------------------

def _get_player_vs_opp_minutes(
    team_name: str,
    opp_name: str,
) -> dict[str, list[float]]:
    """
    For each player on team_name, returns list of minutes played in completed
    games against opp_name this season. Empty list = no games played vs them.
    """
    from season_stats import _parse_boxscore, get_all_game_ids, ESPN_TEAM_IDS as SS_IDS

    team_id = SS_IDS.get(team_name)
    opp_id  = ESPN_TEAM_IDS.get(opp_name)
    if not team_id or not opp_id:
        return {}

    cache_key = f"h2h_mins_{team_name.replace(' ','_')}_{opp_name.replace(' ','_')}"
    cached = _load_cache(cache_key, ttl_hours=6.0)
    if cached:
        return cached

    # Get all game IDs for the team, filter to those vs opp_name
    data = _get_json(
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/schedule"
    )
    opp_id_str = str(opp_id)
    h2h_game_ids = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        if not comp.get("status", {}).get("type", {}).get("completed"):
            continue
        comp_ids = [str(c.get("team", {}).get("id", ""))
                    for c in comp.get("competitors", [])]
        raw_date = event.get("date", "")
        game_date = raw_date[:10] if raw_date else ""
        if opp_id_str in comp_ids and game_date >= "2026-05-16":
            h2h_game_ids.append(event["id"])

    result: dict[str, list[float]] = defaultdict(list)
    for gid in h2h_game_ids:
        box = _parse_boxscore(gid, team_id)
        for p in box:
            if not p["dnp"] and p["minutes"] >= 0.5:
                # Exclude foul-trouble games so Signal C isn't skewed by random foul outs
                if p.get("fouls", 0) < 4:
                    result[p["name"]].append(p["minutes"])

    out = dict(result)
    _save_cache(cache_key, out, ttl_hours=6.0)
    return out


def get_h2h_foul_notes(
    team_name: str,
    opp_name: str,
    team_data: dict,
) -> dict[str, str]:
    """
    Returns {player_name: note_string} for players who have a notable foul
    trouble pattern specifically against opp_name.

    Triggers when ALL of:
      - 2+ H2H games vs this opponent
      - Player averaged 4+ fouls in those H2H games  OR
        H2H minutes are 20%+ below their season avg (fouls curtailed their time)
    """
    from season_stats import _parse_boxscore as _ss_parse, ESPN_TEAM_IDS as SS_IDS

    team_id = SS_IDS.get(team_name)
    opp_id  = ESPN_TEAM_IDS.get(opp_name)
    if not team_id or not opp_id:
        return {}

    cache_key = f"h2h_foul_{team_name.replace(' ','_')}_{opp_name.replace(' ','_')}"
    cached = _load_cache(cache_key, ttl_hours=6.0)
    if cached is not None:
        return cached

    data = _get_json(
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/schedule"
    )
    opp_id_str = str(opp_id)
    h2h_game_ids = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        if not comp.get("status", {}).get("type", {}).get("completed"):
            continue
        comp_ids = [str(c.get("team", {}).get("id", ""))
                    for c in comp.get("competitors", [])]
        raw_date = event.get("date", "")
        game_date = raw_date[:10] if raw_date else ""
        if opp_id_str in comp_ids and game_date >= "2026-05-16":
            h2h_game_ids.append(event["id"])

    if not h2h_game_ids:
        _save_cache(cache_key, {}, ttl_hours=6.0)
        return {}

    # Accumulate per-player H2H fouls and minutes
    h2h_fouls:   dict[str, list[int]]   = defaultdict(list)
    h2h_minutes: dict[str, list[float]] = defaultdict(list)

    for gid in h2h_game_ids:
        box = _ss_parse(gid, team_id)
        for p in box:
            if not p["dnp"] and p["minutes"] >= 0.5:
                h2h_fouls[p["name"]].append(p.get("fouls", 0))
                h2h_minutes[p["name"]].append(p["minutes"])

    notes = {}
    for player, fouls_list in h2h_fouls.items():
        foul_out_games   = sum(1 for f in fouls_list if f >= 6)
        close_foul_games = sum(1 for f in fouls_list if f == 5)

        # Only flag if foul trouble happened in 2+ H2H games vs this opponent
        if foul_out_games >= 2:
            notes[player] = "Fouled out"
        elif close_foul_games >= 2:
            notes[player] = "Foul trouble"
        elif foul_out_games >= 1 and close_foul_games >= 1:
            notes[player] = "Foul trouble"

    _save_cache(cache_key, notes, ttl_hours=6.0)
    return notes


# ---------------------------------------------------------------------------
# Core adjustment calculation
# ---------------------------------------------------------------------------

def compute_matchup_adjustments(
    team_name: str,
    opp_name: str,
    team_data: dict,
) -> dict[str, float]:
    """
    Returns {player_name: minutes_adjustment} for each active player.
    Adjustments are in [-3.0, +3.0] range, rounded to 0.5.

    Three signal layers (all capped before combining):
      A. Opponent rotation/pace signal  → affects bench players most
      B. Blowout risk signal            → expands rotation if high
      C. Player's own H2H history       → small anchor vs. that team this season
    """
    opp_profile   = _get_opponent_profile(opp_name)
    h2h_mins      = _get_player_vs_opp_minutes(team_name, opp_name)
    h2h_margins   = _get_h2h_margins(team_name, opp_name)

    adjustments: dict[str, float] = {}

    # --- Signal A: opponent rotation depth vs team's own ---
    # If opponent runs a deep rotation, games tend to be more contested →
    # our team may also go deeper.  Signal is the delta in rotation sizes.
    opp_depth  = opp_profile.get("rotation_depth", 8)
    # We don't have team's own rotation_depth here, default to 8
    depth_delta = (opp_depth - 8) * 0.15   # ±0.15 per extra/fewer rotation player

    # --- Signal B: blowout risk ---
    # High blowout rate → bench gets garbage time (positive for bench, neutral/negative for starters)
    # We split this: bench gets +adj, starters get -adj (coaches rest them in blowouts)
    blowout_rate = opp_profile.get("blowout_rate", 0.0)
    # Only meaningful if we have enough sample
    blowout_signal = blowout_rate * 2.0 if opp_profile.get("sample_games", 0) >= 4 else 0.0

    # --- Signal C: player's own H2H minutes this season ---
    # If a player has 1-3 games vs this opponent already, their avg in those
    # games is a weak signal. Weight it at 15% — mostly informational.
    # We compare their H2H avg to their season avg to get a delta.

    for player, info in team_data.items():
        if info.get("status") in ("Out", "Doubtful"):
            adjustments[player] = 0.0
            continue

        role     = info.get("role", "bench")
        avg_min  = info.get("avg_min", 0.0)
        last3    = info.get("last3_avg", avg_min)

        # Base projection (same logic as model._weighted_minutes but simplified)
        base = avg_min * 0.25 + last3 * 0.75

        # Signal A: pace/depth — affects bench more than starters
        role_mult = 0.6 if role == "starter" else 1.0
        adj_a = depth_delta * role_mult

        # Signal B: blowout — bench gains in blowouts, starters lose
        if role == "bench":
            adj_b = blowout_signal * 0.8
        else:
            adj_b = -blowout_signal * 0.4

        # Signal C: H2H minutes delta this season
        h2h = h2h_mins.get(player, [])
        adj_c = 0.0
        if len(h2h) >= 1:
            h2h_avg = sum(h2h) / len(h2h)
            raw_delta = h2h_avg - base
            # Weight by sample size: 1 game = 10%, 2 games = 20%, 3+ = 30%
            h2h_weight = min(len(h2h) * 0.10, 0.30)
            adj_c = raw_delta * h2h_weight

        # Combine — cap total adjustment to ±3 min
        total = adj_a + adj_b + adj_c
        total = max(-3.0, min(3.0, total))

        # Round to nearest 0.5 — avoids false precision
        total = round(total * 2) / 2
        adjustments[player] = total

    return adjustments


def get_player_h2h_minutes(team_name: str, opp_name: str) -> dict[str, list[float]]:
    """
    Public wrapper for the H2H display table — returns raw minutes including
    foul-trouble games so the historical record is accurate.
    """
    from season_stats import _parse_boxscore, ESPN_TEAM_IDS as SS_IDS

    team_id = SS_IDS.get(team_name)
    opp_id  = ESPN_TEAM_IDS.get(opp_name)
    if not team_id or not opp_id:
        return {}

    cache_key = f"h2h_mins_raw_{team_name.replace(' ','_')}_{opp_name.replace(' ','_')}"
    cached = _load_cache(cache_key, ttl_hours=6.0)
    if cached:
        return cached

    data = _get_json(
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/schedule"
    )
    opp_id_str = str(opp_id)
    h2h_game_ids = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        if not comp.get("status", {}).get("type", {}).get("completed"):
            continue
        comp_ids = [str(c.get("team", {}).get("id", "")) for c in comp.get("competitors", [])]
        raw_date = event.get("date", "")
        game_date = raw_date[:10] if raw_date else ""
        if opp_id_str in comp_ids and game_date >= "2026-05-16":
            h2h_game_ids.append(event["id"])

    result: dict[str, list[float]] = defaultdict(list)
    for gid in h2h_game_ids:
        box = _parse_boxscore(gid, team_id)
        for p in box:
            if not p["dnp"] and p["minutes"] >= 0.5:
                result[p["name"]].append(p["minutes"])

    out = dict(result)
    _save_cache(cache_key, out, ttl_hours=6.0)
    return out


def get_matchup_summary(team_name: str, opp_name: str) -> dict:
    """
    Returns matchup context used by the UI banner.

    Confidence tiers:
      low    — 0 H2H games played yet this season
      medium — 1-2 H2H games
      high   — 3+ H2H games (enough sample to draw real conclusions)

    Blowout rate is always shown with the raw game count that backs it up,
    not as a bare percentage.
    """
    profile     = _get_opponent_profile(opp_name)
    h2h_results = _get_h2h_results(team_name, opp_name)
    h2h_margins = [r["margin"] for r in h2h_results]
    h2h_scores  = [r["display"] for r in h2h_results]   # e.g. ["W 84-71", "L 68-79"]

    n_h2h = len(h2h_margins)

    # Confidence: driven entirely by H2H sample size
    # < 3 games = low (not enough data to draw conclusions about this specific matchup)
    # 3+ games = high (seen each other enough times to spot real patterns)
    if n_h2h >= 3:
        confidence = "high"
    else:
        confidence = "low"

    notes = []
    sample         = profile.get("sample_games", 0)
    blowout_rate   = profile.get("blowout_rate", 0.0)
    blowout_count  = round(blowout_rate * sample)
    avg_margin_all = profile.get("avg_margin", 0.0)
    opp_depth      = profile.get("rotation_depth", 8)

    # H2H results this season
    if h2h_scores:
        scores_str = "  |  ".join(h2h_scores)
        avg_margin = sum(h2h_margins) / n_h2h
        sign = "+" if avg_margin >= 0 else ""
        notes.append(f"H2H results: {scores_str} &nbsp; (avg margin {sign}{avg_margin:.0f} pts)")
    else:
        notes.append("No H2H games played yet this season")

    # Blowout tendency
    if sample >= 4:
        if blowout_rate >= 0.40:
            notes.append(f"{blowout_count}/{sample} games decided by 15+ pts — bench gets extra run late")
        elif blowout_rate <= 0.15:
            notes.append(f"Only {blowout_count}/{sample} blowouts — close games, starters play full rotations")
        else:
            notes.append(f"{blowout_count}/{sample} blowouts &nbsp;|&nbsp; avg margin {avg_margin_all:+.0f} pts")

    # Rotation depth
    if opp_depth >= 10:
        notes.append(f"Runs a {opp_depth}-player rotation — expect deeper bench usage")
    elif opp_depth <= 7:
        notes.append(f"Tight {opp_depth}-player rotation — starters carry heavy minutes")

    return {
        "notes":        notes,
        "confidence":   confidence,
        "sample_games": sample,
        "h2h_games":    n_h2h,
        "h2h_scores":   h2h_scores,
        "opp_depth":    opp_depth,
        "blowout_rate": blowout_rate,
        "blowout_count": blowout_count,
        "blowout_sample": sample,
    }
