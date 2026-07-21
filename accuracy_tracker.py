"""
Accuracy tracker — compares our model's projected minutes vs RotoWire
vs actual minutes played.

Called automatically by export_snowflake_data.py when it detects a fresh
RotoWire CSV at: C:/Users/kar.patel/Downloads/wnba-daily-projections.csv

Output: data/accuracy_log.csv
Columns: date, player, rw_team, rw_projected, our_projected, actual_minutes

Actual minutes are filled in from snowflake_boxscores.csv on each run.
Players with rw_projected == 0 (team not playing) are skipped.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
LOG_PATH = DATA_DIR / "accuracy_log.csv"
BOXSCORES_PATH = DATA_DIR / "snowflake_boxscores.csv"
RW_DOWNLOADS_DIR = Path("C:/Users/kar.patel/Downloads")
RW_DOWNLOADS = [
    Path("C:/Users/kar.patel/Downloads/wnba-daily-projections.csv"),
]


def _find_all_rw_csvs() -> list[tuple[str, Path]]:
    """
    Find all RotoWire projection CSVs in Downloads and return (date_str, path) pairs.
    Uses file modification date as the game date.
    """
    import glob as _glob
    from datetime import datetime as _dt
    results = []
    pattern = str(RW_DOWNLOADS_DIR / "wnba-daily-projections*.csv")
    for filepath in _glob.glob(pattern):
        p = Path(filepath)
        try:
            mdate = _dt.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
            results.append((mdate, p))
        except Exception:
            continue
    # Dedupe by date — keep the most recently modified file per date
    by_date: dict[str, Path] = {}
    for mdate, p in results:
        if mdate not in by_date or p.stat().st_mtime > by_date[mdate].stat().st_mtime:
            by_date[mdate] = p
    return sorted(by_date.items())

LOG_COLS = ["date", "game_label", "player", "rw_team", "rw_projected", "our_projected", "actual_minutes"]

# RotoWire team abbreviation → full name used in season_stats
_RW_TEAM_MAP = {
    "ATL": "Atlanta Dream",
    "CHI": "Chicago Sky",
    "CON": "Connecticut Sun",
    "DAL": "Dallas Wings",
    "GSV": "Golden State Valkyries",
    "GSW": "Golden State Valkyries",
    "IND": "Indiana Fever",
    "LVA": "Las Vegas Aces",
    "LAS": "Las Vegas Aces",
    "LOS": "Los Angeles Sparks",
    "LAX": "Los Angeles Sparks",
    "MIN": "Minnesota Lynx",
    "NYL": "New York Liberty",
    "PHO": "Phoenix Mercury",
    "POR": "Portland Fire",
    "SEA": "Seattle Storm",
    "TOR": "Toronto Tempo",
    "WAS": "Washington Mystics",
}


def _read_rw_csv(path: Path) -> list[dict]:
    """Parse RotoWire projections CSV. Skips junk first row and zero-min players."""
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        # Row 0 is junk (",,,,Popular Stats,..."), row 1 has real headers
        # Find the header row (contains "Player")
        header_idx = 0
        for i, line in enumerate(lines):
            if line.strip().startswith("Player"):
                header_idx = i
                break
        reader = csv.DictReader(lines[header_idx:])
        for row in reader:
            try:
                mins = float(row.get("Min", 0) or 0)
            except (ValueError, TypeError):
                mins = 0.0
            if mins <= 0:
                continue  # team not playing today
            player = row.get("Player", "").strip()
            team = row.get("Team", "").strip()
            if not player:
                continue
            rows.append({
                "player": player,
                "rw_team": team,
                "rw_projected": round(mins, 1),
            })
    except Exception as e:
        print(f"[accuracy] Failed to read RotoWire CSV: {e}")
    return rows


def _load_our_projections() -> dict[str, float]:
    """
    Run the real model for every team and return {player_name: projected_min}.
    This is what the app actually shows — weighted blend of season avg + last3 +
    injury adjustments — not just a raw season average.
    Falls back to season avg if the model can't be loaded.
    """
    result = {}
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from wnba_scraper import get_team_data
        from model import build_projection
        from roster_data import TEAMS

        for team_name in TEAMS:
            try:
                team_data = dict(get_team_data(team_name))
                team_data.pop("__meta__", None)
                lineup = build_projection(team_data)
                for p in lineup.players:
                    if p.projected_min > 0 and p.name not in result:
                        result[p.name] = round(p.projected_min, 1)
            except Exception as e:
                print(f"[accuracy] Model failed for {team_name}: {e}")
                continue
        if result:
            print(f"[accuracy] Loaded real model projections for {len(result)} players")
            return result
    except Exception as e:
        print(f"[accuracy] Could not run model, falling back to season avg: {e}")

    # Fallback: raw season average from CSV
    path = DATA_DIR / "snowflake_player_stats.csv"
    if not path.exists():
        return result
    try:
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = row.get("player_full_name", "").strip()
                try:
                    avg = float(row.get("avg_minutes") or 0)
                except (ValueError, TypeError):
                    avg = 0.0
                if name and avg > 0:
                    result[name] = round(avg, 1)
    except Exception as e:
        print(f"[accuracy] Failed to load fallback projections: {e}")
    return result


def _load_actuals() -> dict[tuple[str, str], float]:
    """
    Load actual minutes from snowflake_boxscores.csv.
    Returns {(player_name, game_date): actual_minutes}.
    Uses the most recent game per player per date.
    """
    result: dict[tuple[str, str], float] = {}
    if not BOXSCORES_PATH.exists():
        return result
    try:
        with open(BOXSCORES_PATH, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = row.get("player_full_name", "").strip()
                gdate = row.get("game_date", "").strip()[:10]
                played = str(row.get("player_played", "")).strip().lower() in ("true", "1")
                if not name or not gdate or not played:
                    continue
                try:
                    mins = float(row.get("minutes") or 0)
                except (ValueError, TypeError):
                    mins = 0.0
                result[(name, gdate)] = round(mins, 1)
    except Exception as e:
        print(f"[accuracy] Failed to load actuals: {e}")
    return result


def _load_existing_log() -> list[dict]:
    """Load existing accuracy log rows."""
    if not LOG_PATH.exists():
        return []
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _teams_by_date() -> dict:
    """Return {date: set(team_name)} from boxscores CSV for validation."""
    result: dict[str, set] = {}
    if not BOXSCORES_PATH.exists():
        return result
    try:
        import csv as _csv
        with open(BOXSCORES_PATH, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                d = row.get("game_date", "")
                t = row.get("team_name", "").strip()
                if d and t:
                    result.setdefault(d, set()).add(t)
    except Exception:
        pass
    return result


_ABBREV_TO_FULL_SAVE = {
    "ATL": "Atlanta Dream", "CHI": "Chicago Sky", "CON": "Connecticut Sun",
    "DAL": "Dallas Wings", "GSV": "Golden State Valkyries", "IND": "Indiana Fever",
    "LAS": "Las Vegas Aces", "LVA": "Las Vegas Aces", "LOS": "Los Angeles Sparks",
    "MIN": "Minnesota Lynx", "NYL": "New York Liberty", "PHO": "Phoenix Mercury",
    "POR": "Portland Fire", "SEA": "Seattle Storm", "TOR": "Toronto Tempo",
    "WAS": "Washington Mystics",
}


def _save_log(rows: list[dict]) -> None:
    # Backfill game_label for rows that predate the column being added
    team_pairs: dict[str, str] = {}  # date -> teams seen
    for r in rows:
        d = r.get("date", "")
        t = r.get("rw_team", "")
        if d and t:
            team_pairs.setdefault(d, set()).add(t)  # type: ignore[arg-type]
    date_labels: dict[str, dict[str, str]] = {}
    for d, teams in team_pairs.items():
        sorted_teams = sorted(teams)
        lmap: dict[str, str] = {}
        for i in range(0, len(sorted_teams) - 1, 2):
            label = f"{sorted_teams[i]} vs {sorted_teams[i+1]}"
            lmap[sorted_teams[i]]   = label
            lmap[sorted_teams[i+1]] = label
        if len(sorted_teams) % 2 == 1:
            lmap[sorted_teams[-1]] = sorted_teams[-1]
        date_labels[d] = lmap

    for r in rows:
        if not r.get("game_label"):
            r["game_label"] = date_labels.get(r.get("date",""), {}).get(r.get("rw_team",""), "")

    # Validate: drop rows where the player's team didn't play on that date.
    # Also dedupe by (date, player). This prevents bad RotoWire date mismatches
    # from accumulating regardless of which code path adds the row.
    tbd = _teams_by_date()
    seen_keys: set = set()
    valid_rows = []
    for r in rows:
        d = r.get("date", "")
        player = r.get("player", "")
        key = (d, player)
        if key in seen_keys:
            continue
        if tbd:
            rw_team = r.get("rw_team", "").strip()
            full_team = _ABBREV_TO_FULL_SAVE.get(rw_team.upper(), rw_team)
            teams_on_date = tbd.get(d, set())
            if teams_on_date and full_team not in teams_on_date:
                continue  # team didn't play on this date
        seen_keys.add(key)
        valid_rows.append(r)

    with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(valid_rows)


def _fuzzy_match(rw_name: str, our_names: set[str]) -> str | None:
    """
    Match RotoWire player name to our player name.
    Tries exact match first, then last-name + first-initial match.
    """
    if rw_name in our_names:
        return rw_name
    # Normalize: lower, strip punctuation
    def _norm(n: str) -> str:
        return n.lower().replace(".", "").replace("'", "").replace("-", " ").strip()

    rw_norm = _norm(rw_name)
    for our_name in our_names:
        if _norm(our_name) == rw_norm:
            return our_name

    # Last name match as fallback
    rw_parts = rw_norm.split()
    if len(rw_parts) >= 2:
        rw_last = rw_parts[-1]
        rw_first_init = rw_parts[0][0] if rw_parts[0] else ""
        candidates = [
            n for n in our_names
            if _norm(n).split()[-1] == rw_last
            and _norm(n).split()[0][0:1] == rw_first_init
        ]
        if len(candidates) == 1:
            return candidates[0]
    return None


def snapshot_all_available(rw_path: Path | None = None) -> int:
    """
    Snapshot all available RotoWire CSVs in Downloads that haven't been logged yet.
    Returns total new rows added across all dates.
    """
    all_csvs = _find_all_rw_csvs()
    total = 0
    for mdate, path in all_csvs:
        total += snapshot_today(rw_path=path, force_date=mdate)
    return total


def snapshot_today(rw_path: Path | None = None, force_date: str | None = None) -> int:
    """
    Read a RotoWire CSV, snapshot our projections, and append to log.
    Returns the number of new rows added.
    Skips if that date already has entries in the log.
    """
    today = force_date or str(date.today())

    # Find RotoWire file
    path = rw_path
    if path is None:
        for p in RW_DOWNLOADS:
            if p.exists():
                path = p
                break
    if path is None or not path.exists():
        print("[accuracy] No RotoWire CSV found — skipping snapshot")
        return 0

    # Load existing log
    existing = _load_existing_log()

    # If today already has rows, refresh our_projected with current model values
    # (model may have been updated since the initial snapshot)
    today_rows = [r for r in existing if r["date"] == today]
    if today_rows:
        our_proj = _load_our_projections()
        our_names = set(our_proj.keys())
        refreshed = 0
        for r in existing:
            if r["date"] != today:
                continue
            matched = _fuzzy_match(r["player"], our_names)
            if matched and our_proj.get(matched):
                r["our_projected"] = our_proj[matched]
                refreshed += 1
        _save_log(existing)
        print(f"[accuracy] Refreshed our_projected for {refreshed}/{len(today_rows)} rows for {today}")
        return 0

    rw_rows = _read_rw_csv(path)
    if not rw_rows:
        print("[accuracy] No RotoWire rows parsed — skipping snapshot")
        return 0

    # Prevent duplicate snapshots: if all players in this CSV were already
    # snapshotted on a different date, the CSV hasn't changed — skip.
    rw_players = {r["player"] for r in rw_rows}
    for existing_row in existing:
        if existing_row["date"] != today and existing_row["player"] in rw_players:
            already_dates = {r["date"] for r in existing if r["player"] in rw_players}
            if len(rw_players & {r["player"] for r in existing}) >= len(rw_players) * 0.8:
                print(f"[accuracy] RotoWire CSV appears to be a duplicate of {already_dates} — skipping snapshot")
                return 0
            break

    our_proj = _load_our_projections()
    our_names = set(our_proj.keys())

    # Build game labels from unique teams in the CSV e.g. "NYL vs DAL"
    teams_in_csv = sorted({r["rw_team"] for r in rw_rows})
    # Pair teams into games (every 2 teams = 1 game)
    game_label_map: dict[str, str] = {}
    for i in range(0, len(teams_in_csv) - 1, 2):
        label = f"{teams_in_csv[i]} vs {teams_in_csv[i+1]}"
        game_label_map[teams_in_csv[i]]   = label
        game_label_map[teams_in_csv[i+1]] = label
    # If odd team count, last team gets its own label
    if len(teams_in_csv) % 2 == 1:
        game_label_map[teams_in_csv[-1]] = teams_in_csv[-1]

    new_rows = []
    unmatched = []
    for rw in rw_rows:
        matched = _fuzzy_match(rw["player"], our_names)
        our_min = our_proj.get(matched, "") if matched else ""
        new_rows.append({
            "date":           today,
            "game_label":     game_label_map.get(rw["rw_team"], rw["rw_team"]),
            "player":         rw["player"],
            "rw_team":        rw["rw_team"],
            "rw_projected":   rw["rw_projected"],
            "our_projected":  our_min,
            "actual_minutes": "",
        })
        if not matched:
            unmatched.append(rw["player"])

    _save_log(existing + new_rows)
    print(f"[accuracy] Snapshotted {len(new_rows)} players for {today} "
          f"({len(unmatched)} unmatched: {unmatched[:5]}{'...' if len(unmatched) > 5 else ''})")
    return len(new_rows)


def fill_actuals() -> int:
    """
    Fill in actual_minutes for any rows that have a game_date with actuals
    in snowflake_boxscores.csv. Updates rows in place.
    Returns the number of rows updated.
    """
    rows = _load_existing_log()
    if not rows:
        return 0

    actuals = _load_actuals()
    updated = 0
    for row in rows:
        if row.get("actual_minutes"):
            continue  # already filled
        gdate = row.get("date", "")
        player = row.get("player", "")
        # Try exact match first
        key = (player, gdate)
        if key in actuals:
            row["actual_minutes"] = actuals[key]
            updated += 1
            continue
        # Try fuzzy match against actuals keys
        actual_names = {k[0] for k in actuals if k[1] == gdate}
        matched = _fuzzy_match(player, actual_names)
        if matched:
            row["actual_minutes"] = actuals.get((matched, gdate), "")
            if row["actual_minutes"]:
                updated += 1

    _save_log(rows)
    print(f"[accuracy] Filled actuals for {updated} rows")
    return updated


def compute_stats() -> dict:
    """
    Compute accuracy stats from the log.
    Returns {
      'our': {'mae': float, 'within2': float, 'within4': float, 'n': int},
      'rw':  {'mae': float, 'within2': float, 'within4': float, 'n': int},
      'rows': list[dict],  # all rows with actuals filled
    }
    """
    rows = _load_existing_log()
    our_errors, rw_errors = [], []
    filled_rows = []

    for row in rows:
        actual = row.get("actual_minutes", "")
        if actual == "" or actual is None:
            continue
        try:
            actual_f = float(actual)
        except (ValueError, TypeError):
            continue

        filled_rows.append(row)

        try:
            our_f = float(row.get("our_projected", ""))
            our_errors.append(abs(our_f - actual_f))
        except (ValueError, TypeError):
            pass

        try:
            rw_f = float(row.get("rw_projected", ""))
            rw_errors.append(abs(rw_f - actual_f))
        except (ValueError, TypeError):
            pass

    # Count unique games — dedupe by (date, sorted teams) so abbreviation labels
    # and full-name labels for the same game aren't counted twice.
    # Two teams playing each other on DIFFERENT dates are correctly kept separate.
    _ABBREV_TO_FULL = {
        "ATL": "Atlanta Dream", "CHI": "Chicago Sky", "CON": "Connecticut Sun",
        "DAL": "Dallas Wings",  "GSV": "Golden State Valkyries", "IND": "Indiana Fever",
        "LAS": "Las Vegas Aces", "LVA": "Las Vegas Aces", "LOS": "Los Angeles Sparks",
        "MIN": "Minnesota Lynx", "NYL": "New York Liberty", "PHO": "Phoenix Mercury",
        "POR": "Portland Fire",  "SEA": "Seattle Storm", "TOR": "Toronto Tempo",
        "WAS": "Washington Mystics",
    }
    def _normalize_label(label: str) -> str:
        parts = [p.strip() for p in label.split("vs")]
        if len(parts) == 2:
            parts = [_ABBREV_TO_FULL.get(p.upper(), p) for p in parts]
            return " vs ".join(sorted(parts))
        return label

    seen_game_keys: set = set()
    canonical_games: list = []
    for r in filled_rows:
        d = r.get("date", "")
        lbl = r.get("game_label", r.get("rw_team", ""))
        key = (d, _normalize_label(lbl))
        if key not in seen_game_keys:
            seen_game_keys.add(key)
            canonical_games.append((d, lbl))

    game_list = sorted(
        {f"{d} — {g}" for d, g in canonical_games if g},
        reverse=True
    )

    def _stats(errors: list[float]) -> dict:
        if not errors:
            return {"mae": None, "within2": None, "within4": None, "n": 0}
        n = len(errors)
        return {
            "mae":     round(sum(errors) / n, 2),
            "within2": round(100 * sum(1 for e in errors if e <= 2) / n, 1),
            "within4": round(100 * sum(1 for e in errors if e <= 4) / n, 1),
            "n":       n,
        }

    return {
        "our":        _stats(our_errors),
        "rw":         _stats(rw_errors),
        "rows":       filled_rows,
        "game_count": len(seen_game_keys),
        "game_list":  game_list,
    }


def backfill_from_boxscores(since_date: str = "2026-07-16") -> int:
    """
    Backfill accuracy log with our model projections + actuals for all games
    in snowflake_boxscores.csv since `since_date` that aren't already logged.
    rw_projected is left empty for backfilled rows (no RotoWire data available).
    Returns number of new rows added.
    """
    existing = _load_existing_log()
    existing_keys = {(r["date"], r["player"]) for r in existing}

    # Load actuals from boxscores
    actuals_by_date: dict[str, list[dict]] = {}
    if not BOXSCORES_PATH.exists():
        return 0
    try:
        import csv as _csv
        with open(BOXSCORES_PATH, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                gdate = row.get("game_date", "")[:10]
                if gdate < since_date:
                    continue
                played = str(row.get("player_played","")).lower() in ("true","1")
                if not played:
                    continue
                try:
                    mins = float(row.get("minutes") or 0)
                except (ValueError, TypeError):
                    mins = 0.0
                actuals_by_date.setdefault(gdate, []).append({
                    "player":    row.get("player_full_name","").strip(),
                    "team":      row.get("team_name","").strip(),
                    "home_team": row.get("home_team_name","").strip(),
                    "minutes":   round(mins, 1),
                })
    except Exception as e:
        print(f"[accuracy] Backfill failed reading boxscores: {e}")
        return 0

    # Run model for each team/date combo not already in log
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from wnba_scraper import get_team_data
        from model import build_projection
    except Exception as e:
        print(f"[accuracy] Backfill failed loading model: {e}")
        return 0

    new_rows = []
    dates_to_fill = sorted(d for d in actuals_by_date if d >= since_date)

    for gdate in dates_to_fill:
        day_players = actuals_by_date[gdate]
        teams_that_day = {p["team"] for p in day_players}

        # Build game labels for this date
        sorted_teams = sorted(teams_that_day)
        lmap: dict[str, str] = {}
        for i in range(0, len(sorted_teams) - 1, 2):
            label = f"{sorted_teams[i]} vs {sorted_teams[i+1]}"
            lmap[sorted_teams[i]]   = label
            lmap[sorted_teams[i+1]] = label
        if len(sorted_teams) % 2 == 1:
            lmap[sorted_teams[-1]] = sorted_teams[-1]

        # Get model projections for each team
        team_proj: dict[str, float] = {}
        for team in teams_that_day:
            try:
                td = dict(get_team_data(team))
                td.pop("__meta__", None)
                lineup = build_projection(td)
                for p in lineup.players:
                    if p.projected_min > 0:
                        team_proj[p.name] = round(p.projected_min, 1)
            except Exception:
                continue

        for p in day_players:
            key = (gdate, p["player"])
            if key in existing_keys:
                continue
            our_min = team_proj.get(p["player"], "")
            if not our_min:
                # fuzzy match
                matched = _fuzzy_match(p["player"], set(team_proj.keys()))
                our_min = team_proj.get(matched, "") if matched else ""
            if p["minutes"] < 0.5 and not our_min:
                continue  # skip true DNPs we have no projection for
            new_rows.append({
                "date":           gdate,
                "game_label":     lmap.get(p["team"], p["team"]),
                "player":         p["player"],
                "rw_team":        p["team"][:3].upper(),
                "rw_projected":   "",  # no RotoWire data for backfilled games
                "our_projected":  our_min,
                "actual_minutes": p["minutes"],
            })
            existing_keys.add(key)

    if new_rows:
        _save_log(existing + new_rows)
        print(f"[accuracy] Backfilled {len(new_rows)} player-games across {len(dates_to_fill)} dates")
    else:
        print("[accuracy] Backfill: no new rows to add")
    return len(new_rows)


if __name__ == "__main__":
    print("Snapshotting today's projections...")
    snapshot_today()
    print("Filling actuals from boxscores...")
    fill_actuals()
    stats = compute_stats()
    print(f"\nAccuracy Summary ({stats['our']['n']} player-games tracked):")
    print(f"  Our model — MAE: {stats['our']['mae']} min | "
          f"Within 2: {stats['our']['within2']}% | Within 4: {stats['our']['within4']}%")
    print(f"  RotoWire  — MAE: {stats['rw']['mae']} min | "
          f"Within 2: {stats['rw']['within2']}% | Within 4: {stats['rw']['within4']}%")
