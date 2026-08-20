"""
Point-in-time (walk-forward) replay of the minutes model.

WHY THIS EXISTS
---------------
`accuracy_tracker` used to score our model by running `build_projection` against
*today's* team data and writing that single number onto every historical date.
The result: 133 of 170 tracked players had exactly ONE `our_projected` value
across all logged dates, while RotoWire's moved every game. That comparison
measured a season-to-date constant against a genuine per-game forecast, so any
trend read out of it was an artifact of when the last backfill ran.

This module rebuilds the model's input (`team_data`) using ONLY games that
finished strictly before a given date, then runs the real `build_projection`.
That makes our number an honest per-game forecast, directly comparable to
RotoWire's.

INFORMATION SET
---------------
`snowflake_boxscores.csv` contains only players who actually played (there are
no DNP rows), so "who was available" has to come from the target game itself.
Two modes:

  use_actual_availability=True  (default)
      The set of players who appeared in the target game is treated as the
      announced active list, and that game's `starter` flags as the announced
      starting five. This mirrors the production path where a confirmed lineup
      is known (`use_lineup_roles` in wnba_scraper.get_team_data), and mirrors
      RotoWire, who publish after lineup news. It is a slightly STRONGER
      information set than RotoWire had, because a player who dressed but got
      0 minutes is invisible to us. Minutes are never read from the target
      game — only presence and the starter flag.

  use_actual_availability=False
      Nothing about the target game is used. Availability and roles are
      inferred from pre-cutoff history alone (recent_starter_pct). Strictly
      out-of-sample; use this for the conservative bound.

Every numeric feature (averages, EWMA, trends, foul rates, role anchors,
rotation depth) is computed from pre-cutoff games only in both modes.

FIELD FIDELITY
--------------
Feature math is imported from `season_stats` (`_trimmed_avg`, `_median`,
`_ewma`, `_context_filter`) rather than reimplemented, so the numbers match
production. Two fields are unavoidably approximate:

  dnp_rate   Production counts healthy-scratch DNPs only, excluding injury DNPs
             (season_stats.py:884). The boxscore export has a `dnp_reason`
             column, but its query ends `AND g.PLAYER_PLAYED = TRUE`, so no DNP
             row is ever emitted and the column is empty on every row. While
             that holds, replay falls back to counting every absence since a
             player's debut, which OVERSTATES dnp_rate; model.py:512 then
             shrinks a non-starter's minutes by (1 - dnp_rate) above 0.25, so
             replay under-projects bench players relative to production. This is
             left uncorrected on purpose — scaling it back would be a fudge
             factor with no data behind it. `dnp_reason_available()` reports
             which formula is in force, and once the export emits DNP rows
             replay switches to production's exact formula automatically.
  crunch_time_poss  Needs possession-level data that exists only in Snowflake
             (season_stats.py:951), so it stays None. model.py:572 reads it as
             `(... or 999) < 15`, meaning the garbage-time signal never fires in
             replay although it can in production.

`plus_minus` IS present on every boxscore row. Production feeds build_projection
a season average from Snowflake for players with 3+ games; replay averages the
pre-cutoff games under the same threshold, which is the point-in-time analogue.

Because two of these three gaps starve replay of signal production has, measured
replay accuracy is a floor on the production model, not a flattering estimate.

USAGE
-----
    import replay
    proj = replay.project_team("Indiana Fever", "2026-08-05")   # {player: minutes}
    proj = replay.project_date("2026-08-05")                    # all teams playing
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from season_stats import _context_filter, _ewma, _median, _trimmed_avg
from model import build_projection

DATA_DIR = Path(__file__).parent / "data"
BOXSCORES_PATH = DATA_DIR / "snowflake_boxscores.csv"
ROSTERS_PATH = DATA_DIR / "snowflake_current_rosters.csv"

# Regulation team minutes: 5 players x 40 minutes.
REGULATION_TEAM_MINUTES = 200.0

# Verbatim from season_stats.py:637-639. A DNP whose reason contains any of these
# is treated as injury-driven and excluded from dnp_rate, because the player would
# have played if healthy. Kept as a named constant so the two copies can be
# diffed; parity.py asserts they still agree.
_INJURY_DNP_KEYWORDS = ("injur", "illness", "sick", "surgery", "pain",
                        "fracture", "sprain", "strain", "rest")


# ---------------------------------------------------------------------------
# Boxscore loading
# ---------------------------------------------------------------------------

class _Game:
    """One team's side of one game, as played."""

    __slots__ = ("gid", "date", "team", "margin", "players", "dnps")

    def __init__(self, gid: str, date: str, team: str, margin: float):
        self.gid = gid
        self.date = date
        self.team = team
        self.margin = margin
        # name -> {"minutes", "starter", "fouls", "plus_minus"}, OT scaled out
        self.players: dict[str, dict] = {}
        # name -> dnp_reason (lowercased) for players who dressed but did not
        # play. Empty for every game the current export produces, because the
        # boxscore query filters PLAYER_PLAYED = TRUE; see `dnp_reason_available`.
        self.dnps: dict[str, str] = {}


_games_cache: dict[str, list[_Game]] | None = None
_positions_cache: dict[str, str] | None = None

# True once a loaded boxscore row carries a non-empty dnp_reason. While this is
# False, replay cannot separate healthy-scratch DNPs from injury DNPs and says
# so via dnp_reason_available(); see the FIELD FIDELITY note in the docstring.
_dnp_reason_seen: bool = False


def dnp_reason_available() -> bool:
    """
    Whether the loaded boxscores carry DNP reasons.

    False means `dnp_rate` pools injury and coach DNPs and is therefore an upper
    bound on production's coach-only rate. Callers that report accuracy should
    surface this rather than let the approximation pass unmentioned.
    """
    return _dnp_reason_seen


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def load_games(path: Path | None = None) -> dict[str, list[_Game]]:
    """
    Return {team_name: [_Game, ...]} sorted by date ascending.

    Overtime is scaled out the same way season_stats does: if a team's total
    minutes exceed 205, players over 40 minutes are scaled toward a 200-minute
    game. Players held under 40 already have regulation-shaped minutes.
    """
    global _games_cache, _dnp_reason_seen
    if _games_cache is not None and path is None:
        return _games_cache

    src = path or BOXSCORES_PATH
    by_key: dict[tuple[str, str], _Game] = {}
    points: dict[str, dict] = {}

    if not src.exists():
        raise FileNotFoundError(f"boxscores not found: {src}")

    with open(src, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Pass 1 — game-level scores, for margins.
    for r in rows:
        gid = (r.get("game_id") or "").strip()
        if not gid or gid in points:
            continue
        points[gid] = {
            "home": (r.get("home_team_name") or "").strip(),
            "home_pts": _to_float(r.get("home_points")),
            "away_pts": _to_float(r.get("away_points")),
        }

    # Pass 2 — player rows.
    for r in rows:
        gid = (r.get("game_id") or "").strip()
        team = (r.get("team_name") or "").strip()
        name = (r.get("player_full_name") or "").strip()
        gdate = (r.get("game_date") or "")[:10]
        if not (gid and team and name and gdate):
            continue
        played = str(r.get("player_played", "")).strip().lower() in ("true", "1")
        minutes = _to_float(r.get("minutes"))

        key = (gid, team)
        game = by_key.get(key)
        if game is None:
            p = points.get(gid, {})
            hp, ap = p.get("home_pts", 0.0), p.get("away_pts", 0.0)
            margin = (hp - ap) if p.get("home") == team else (ap - hp)
            game = _Game(gid, gdate, team, margin)
            by_key[key] = game

        if not played or minutes < 0.5:
            # Record the absence rather than dropping the row. Only rows the
            # export actually emits can be seen here, so this stays empty while
            # the boxscore query filters PLAYER_PLAYED = TRUE.
            reason = (r.get("dnp_reason") or "").strip().lower()
            if not played:
                game.dnps[name] = reason
                if reason:
                    _dnp_reason_seen = True
            continue

        game.players[name] = {
            "minutes": minutes,
            "starter": str(r.get("starter", "")).strip().lower() in ("true", "1"),
            "fouls": _to_int(r.get("personal_fouls")),
            # Per-game plus/minus. Production feeds build_projection a SEASON
            # average from Snowflake (season_stats.py:890); replay averages the
            # pre-cutoff games instead, which is the point-in-time analogue.
            "plus_minus": _to_float(r.get("plus_minus")),
        }

    # Scale out overtime per team-game.
    for game in by_key.values():
        total = sum(p["minutes"] for p in game.players.values())
        if total > 205.0:
            scale = REGULATION_TEAM_MINUTES / total
            for p in game.players.values():
                if p["minutes"] > 40.0:
                    p["minutes"] = round(p["minutes"] * scale, 1)

    result: dict[str, list[_Game]] = defaultdict(list)
    for game in by_key.values():
        result[game.team].append(game)
    for team in result:
        result[team].sort(key=lambda g: (g.date, g.gid))

    out = dict(result)
    if path is None:
        _games_cache = out
    return out


def load_positions() -> dict[str, str]:
    """
    {player_name: position} from the current roster export.

    Position is a static player attribute, so reading today's value for a
    historical date is not lookahead in any meaningful sense.
    """
    global _positions_cache
    if _positions_cache is not None:
        return _positions_cache
    result: dict[str, str] = {}
    if ROSTERS_PATH.exists():
        try:
            with open(ROSTERS_PATH, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    n = (r.get("player_name") or "").strip()
                    p = (r.get("position") or "").strip()
                    if n:
                        result[n] = p or "?"
        except Exception:
            pass
    _positions_cache = result
    return result


# ---------------------------------------------------------------------------
# Feature construction
# ---------------------------------------------------------------------------

def _blowout_split(mins: list[float], margins: list[float]) -> tuple[float, bool]:
    """
    (avg_min_close, blowout_dependent) matching snowflake_connector's
    get_bench_blowout_split: blowout margin >= 15, close margin < 10,
    dependent when blowout exceeds close by 3+ and close is under 8 minutes.
    Needs 4+ games, mirroring that query's HAVING clause.
    """
    if len(mins) < 4:
        return 0.0, False
    blow = [m for m, g in zip(mins, margins) if abs(g) >= 15]
    close = [m for m, g in zip(mins, margins) if abs(g) < 10]
    if not blow or not close:
        return (round(sum(close) / len(close), 2) if close else 0.0), False
    avg_blow = sum(blow) / len(blow)
    avg_close = sum(close) / len(close)
    return round(avg_close, 2), bool((avg_blow - avg_close) >= 3.0 and avg_close < 8.0)


def team_data_as_of(
    team_name: str,
    as_of_date: str,
    use_actual_availability: bool = True,
    games: dict[str, list[_Game]] | None = None,
) -> dict:
    """
    Build the `team_data` dict `build_projection` expects, using only games
    that finished strictly before `as_of_date`.

    Returns {} when the team has no pre-cutoff games (nothing to forecast from).
    """
    all_games = games if games is not None else load_games()
    team_games = all_games.get(team_name, [])

    history = [g for g in team_games if g.date < as_of_date]
    target = next((g for g in team_games if g.date == as_of_date), None)
    if not history:
        return {}

    # --- accumulate per-player history, in chronological order ---
    mins_by: dict[str, list[float]] = defaultdict(list)
    clean_by: dict[str, list[float]] = defaultdict(list)
    l3_clean_by: dict[str, list[float]] = defaultdict(list)
    fouls_by: dict[str, list[int]] = defaultdict(list)
    margins_by: dict[str, list[float]] = defaultdict(list)
    starts_by: dict[str, list[bool]] = defaultdict(list)
    foul_trouble: dict[str, int] = defaultdict(int)
    appeared_idx: dict[str, list[int]] = defaultdict(list)
    last_played: dict[str, str] = {}
    pm_by: dict[str, list[float]] = defaultdict(list)
    coach_dnp: dict[str, int] = defaultdict(int)

    rotation_counts: list[int] = []
    starter_pg_mins: list[float] = []   # per starter player-game
    bench_pg_mins: list[float] = []     # per bench player-game

    for idx, g in enumerate(history):
        rotation = 0
        for name, p in g.players.items():
            m, fouls = p["minutes"], p["fouls"]
            mins_by[name].append(m)
            fouls_by[name].append(fouls)
            margins_by[name].append(g.margin)
            starts_by[name].append(p["starter"])
            appeared_idx[name].append(idx)
            last_played[name] = g.date
            pm_by[name].append(p.get("plus_minus", 0.0))
            if m >= 5.0:
                rotation += 1
            if p["starter"]:
                starter_pg_mins.append(m)
            else:
                bench_pg_mins.append(m)

            # Foul-trouble exclusion, matching season_stats: 5+ fouls always
            # counts as curtailed; exactly 4 only when minutes fell >25% below
            # the running season average at that point in time.
            run = mins_by[name]
            cur_avg = sum(run) / len(run) if run else 0.0
            curtailed = fouls >= 5 or (fouls == 4 and cur_avg > 0 and m < cur_avg * 0.75)
            if curtailed:
                foul_trouble[name] += 1
            else:
                clean_by[name].append(m)
                l3_clean_by[name].append(m)
        # Healthy-scratch DNPs, mirroring season_stats.py:634-643: injury-flavoured
        # reasons never count, and a coach DNP only counts for a player already in
        # the rotation (someone who has appeared in an earlier pre-cutoff game).
        for name, reason in g.dnps.items():
            if any(kw in reason for kw in _INJURY_DNP_KEYWORDS):
                continue
            if appeared_idx.get(name):
                coach_dnp[name] += 1

        if rotation:
            rotation_counts.append(rotation)

    n_hist = len(history)
    recent5 = sorted(rotation_counts[-5:])
    rotation_depth = recent5[len(recent5) // 2] if recent5 else 8
    role_avg_starter = round(sum(starter_pg_mins) / len(starter_pg_mins), 2) if len(starter_pg_mins) >= 10 else 0.0
    role_avg_bench = round(sum(bench_pg_mins) / len(bench_pg_mins), 2) if len(bench_pg_mins) >= 10 else 0.0

    # --- availability + roles for the target game ---
    if use_actual_availability and target is not None:
        available = set(target.players)
        starters = {n for n, p in target.players.items() if p["starter"]}
    else:
        available = None
        starters = set()
    use_lineup_roles = len(starters) >= 4

    positions = load_positions()
    # Sorted, not set order. build_projection's minute redistribution walks
    # team_data in insertion order, and Python randomises string set iteration
    # per process, so an unsorted set made the same date replay to different
    # numbers on different runs (off by up to ~0.7 min per player).
    candidates = sorted(set(mins_by) | (available or set()))

    merged: dict = {}
    for name in candidates:
        hist = mins_by.get(name, [])
        gp = len(hist)

        # Never-played callups: mirror get_team_data's new-starter default.
        if gp == 0:
            if available is None or name not in available:
                continue
            merged[name] = {
                "pos": positions.get(name, "?"),
                "role": "starter" if name in starters else "bench",
                "depth": 1 if name in starters else 2,
                "avg_min": 15.0 if name in starters else 8.0,
                "last3_avg": 15.0 if name in starters else 8.0,
                "last_game_min": 0.0,
                "games_played": 0,
                "starter_pct": 1.0 if name in starters else 0.0,
                "status": "Active",
                "injury": "",
                "lineup_confirmed": use_lineup_roles,
                "zero_min_season": False,
                "rotation_depth": rotation_depth,
                "role_avg_starter": role_avg_starter,
                "role_avg_bench": role_avg_bench,
            }
            continue

        trimmed = _trimmed_avg(hist)
        # Untrimmed mean, matching season_stats.py:779. build_projection does not
        # read it; it is emitted so the two dicts carry the same fields.
        raw_avg = round(sum(hist) / len(hist), 1)
        clean = clean_by.get(name, [])
        clean_avg = _trimmed_avg(clean) if clean else trimmed

        l3 = hist[-3:]
        l3_clean = l3_clean_by.get(name, [])[-3:]
        last3_avg = _median(l3) if l3 else trimmed
        last3_clean_avg = _median(l3_clean) if l3_clean else last3_avg

        # Anomaly filter on last3_clean, matching season_stats: 8+ games AND a
        # meaningful role (trimmed >= 18). Without the 18-minute guard the filter
        # drops a low-minute player's normal games and leaves an exceptional
        # high-minute game as their baseline — season_stats warns about exactly
        # this, so omitting the guard silently projected a different model.
        if gp >= 8 and trimmed >= 18 and l3_clean:
            floor = trimmed * 0.60
            filt = [m for m in l3_clean if m >= floor]
            if filt and len(filt) < len(l3_clean):
                last3_clean_avg = _median(filt)

        stable = [m for m in l3_clean if m >= trimmed * 0.40]
        last3_range = round(max(stable) - min(stable), 1) if len(stable) >= 2 else 0.0

        ctx = _context_filter(hist, fouls_by.get(name, []), margins_by.get(name, []))
        ewma_min = _ewma(ctx) if len(ctx) >= 2 else trimmed

        starts = starts_by.get(name, [])
        starter_pct = round(sum(starts) / gp, 2) if gp else 0.0
        r_starts = starts[-5:]
        recent_starter_pct = round(sum(r_starts) / len(r_starts), 2) if r_starts else starter_pct

        # trend_3v6: mean of last 3 appearances minus mean of appearances 4-6.
        trend_3v6 = 0.0
        if gp >= 6:
            a, b = hist[-3:], hist[-6:-3]
            trend_3v6 = round(sum(a) / len(a) - sum(b) / len(b), 2)

        # Consecutive team games missed at the end of the pre-cutoff window.
        appearances = appeared_idx.get(name, [])
        games_missed_streak = (n_hist - 1 - appearances[-1]) if appearances else n_hist
        if _dnp_reason_seen:
            # Production's exact formula (season_stats.py:884): coach DNPs only,
            # over games played plus those coach DNPs.
            cd = coach_dnp.get(name, 0)
            dnp_rate = round(cd / max(gp + cd, 1), 3)
        else:
            # No DNP rows in the export, so injury and coach absences cannot be
            # separated and every absence since debut is counted. This OVERSTATES
            # dnp_rate, and model.py:512 shrinks a non-starter's minutes by
            # (1 - dnp_rate) once it reaches 0.25 — so replay under-projects
            # bench players relative to production. Left deliberately uncorrected:
            # a fudge factor here would move accuracy numbers without the data to
            # justify it. Fixed properly by exporting DNP rows.
            span = n_hist - appearances[0] if appearances else 0
            absences = max(0, span - gp)
            dnp_rate = round(absences / max(span, 1), 3)

        # Production feeds a season-average plus/minus from Snowflake for players
        # with 3+ games (snowflake_connector.get_player_plus_minus); replay
        # averages the pre-cutoff games under the same threshold. Below it,
        # production leaves the key None and model.py:325 skips the adjustment.
        pm_hist = pm_by.get(name, [])
        plus_minus = round(sum(pm_hist) / len(pm_hist), 2) if len(pm_hist) >= 3 else None

        avg_min_close, blowout_dependent = _blowout_split(hist, margins_by.get(name, []))

        # Status: with a known active list, absence from it means unavailable.
        if available is not None and name not in available:
            status = "Out"
        else:
            status = "Active"

        if use_lineup_roles:
            role = "starter" if name in starters else "bench"
            depth = 1 if role == "starter" else 2
        else:
            eff = (recent_starter_pct
                   if gp >= 5 and abs(recent_starter_pct - starter_pct) >= 0.40
                   else starter_pct)
            role = "starter" if eff >= 0.50 else "bench"
            depth = 1 if role == "starter" else (2 if eff >= 0.10 else 3)

        merged[name] = {
            "pos": positions.get(name, "?"),
            "role": role,
            "depth": depth,
            "avg_min": round(trimmed, 1),
            "ewma_min": ewma_min,
            "clean_avg_min": clean_avg,
            "last3_avg": round(last3_avg, 1),
            "last3_clean_avg": round(last3_clean_avg, 1),
            "last3_range": last3_range,
            "last_game_min": round(hist[-1], 1),
            "last_game_fouls": (fouls_by.get(name, []) or [0])[-1],
            "games_played": gp,
            "games_started": sum(starts),
            "foul_rate": round(foul_trouble.get(name, 0) / gp, 2) if gp else 0.0,
            "foul_trouble_games": foul_trouble.get(name, 0),
            "starter_pct": starter_pct,
            "recent_starter_pct": recent_starter_pct,
            "quarter_avgs": {},
            "status": status,
            "injury": "",
            "lineup_confirmed": use_lineup_roles,
            "zero_min_season": False,
            "recently_active": games_missed_streak <= 1,
            "last_played_date": last_played.get(name, ""),
            "games_total": n_hist,
            "dnp_rate": dnp_rate,
            "raw_avg_min": raw_avg,
            "games_missed_streak": games_missed_streak,
            "plus_minus": plus_minus,
            # Needs possession-level crunch-time data, which lives only in
            # Snowflake (season_stats.py:951) and is absent from the boxscore
            # export. None leaves model.py:572's `or 999` guard False, so the
            # garbage-time signal never fires in replay but can in production.
            "crunch_time_poss": None,
            "avg_min_close": avg_min_close,
            "blowout_dependent": blowout_dependent,
            "trend_3v6": trend_3v6,
            "role_avg_starter": role_avg_starter,
            "role_avg_bench": role_avg_bench,
            "rotation_depth": rotation_depth,
        }

    # Drop fringe players the production path would have filtered out, so the
    # 200-minute normalisation is spread over a realistic rotation.
    if available is None:
        for name in [
            n for n, p in merged.items()
            if p.get("games_played", 0) > 0
            and p.get("avg_min", 0) < 5.0
            and p.get("status") == "Active"
        ]:
            del merged[name]

    if merged:
        merged["__team_name__"] = team_name
        merged["rotation_depth"] = rotation_depth
    return merged


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def project_team(
    team_name: str,
    as_of_date: str,
    use_actual_availability: bool = True,
    games: dict[str, list[_Game]] | None = None,
    drop_zeros: bool = False,
) -> dict[str, float]:
    """
    {player: projected_minutes} for `team_name` on `as_of_date`, using only
    games that finished before that date. Empty dict when unforecastable.

    Players the model zeroes out (rotation cap, status Out) are returned with
    0.0 by default rather than omitted. That matters for scoring: "the model
    said this player would not play" is a prediction, and dropping it would
    silently remove the model's worst calls from the sample while RotoWire
    still gets charged for theirs. Pass drop_zeros=True for display use, where
    a 0-minute row is just noise.
    """
    td = team_data_as_of(team_name, as_of_date,
                         use_actual_availability=use_actual_availability,
                         games=games)
    if not td:
        return {}
    payload = {k: v for k, v in td.items() if isinstance(v, dict)}
    payload["__team_name__"] = team_name
    payload["rotation_depth"] = td.get("rotation_depth", 8)
    try:
        lineup = build_projection(payload)
    except Exception as e:
        print(f"[replay] build_projection failed for {team_name} @ {as_of_date}: {e}")
        return {}
    return {p.name: round(p.projected_min, 1)
            for p in lineup.players
            if not drop_zeros or p.projected_min > 0}


def teams_on(as_of_date: str, games: dict[str, list[_Game]] | None = None) -> list[str]:
    """Teams that played on `as_of_date`."""
    all_games = games if games is not None else load_games()
    return sorted(t for t, gl in all_games.items() if any(g.date == as_of_date for g in gl))


def project_date(
    as_of_date: str,
    use_actual_availability: bool = True,
    games: dict[str, list[_Game]] | None = None,
    drop_zeros: bool = False,
) -> dict[str, float]:
    """
    {player: projected_minutes} across every team that played on `as_of_date`.
    """
    all_games = games if games is not None else load_games()
    out: dict[str, float] = {}
    for team in teams_on(as_of_date, all_games):
        for name, mins in project_team(
            team, as_of_date,
            use_actual_availability=use_actual_availability,
            games=all_games,
            drop_zeros=drop_zeros,
        ).items():
            out.setdefault(name, mins)
    return out


def available_dates(games: dict[str, list[_Game]] | None = None) -> list[str]:
    """Every date with at least one game, ascending."""
    all_games = games if games is not None else load_games()
    return sorted({g.date for gl in all_games.values() for g in gl})


if __name__ == "__main__":
    import sys

    gms = load_games()
    dates = available_dates(gms)
    print(f"Loaded {sum(len(v) for v in gms.values())} team-games "
          f"across {len(dates)} dates ({dates[0]} to {dates[-1]})")

    target = sys.argv[1] if len(sys.argv) > 1 else dates[-1]
    proj = project_date(target, games=gms, drop_zeros=True)
    print(f"\n{target}: {len(proj)} players projected "
          f"({', '.join(teams_on(target, gms))})")
    for name, mins in sorted(proj.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {name:26} {mins:5.1f}")
