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
RW_DOWNLOADS = [
    Path("C:/Users/kar.patel/Downloads/wnba-daily-projections.csv"),
]

LOG_COLS = ["date", "player", "rw_team", "rw_projected", "our_projected", "actual_minutes"]

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
    Load our model's season avg as the projection snapshot.
    Uses snowflake_player_stats.csv avg_minutes as the proxy —
    this is what the model anchors to before last3/injury adjustments.
    Returns {player_name: avg_minutes}.
    """
    path = DATA_DIR / "snowflake_player_stats.csv"
    result = {}
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
        print(f"[accuracy] Failed to load our projections: {e}")
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


def _save_log(rows: list[dict]) -> None:
    with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS)
        w.writeheader()
        w.writerows(rows)


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


def snapshot_today(rw_path: Path | None = None) -> int:
    """
    Read today's RotoWire CSV, snapshot our projections, and append to log.
    Returns the number of new rows added.
    Skips if today's date already has entries in the log.
    """
    today = str(date.today())

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
    existing_keys = {(r["date"], r["player"]) for r in existing}

    # Skip if already snapshotted today
    today_count = sum(1 for r in existing if r["date"] == today)
    if today_count > 0:
        print(f"[accuracy] Already have {today_count} rows for {today} — skipping snapshot")
        return 0

    rw_rows = _read_rw_csv(path)
    if not rw_rows:
        print("[accuracy] No RotoWire rows parsed — skipping snapshot")
        return 0

    our_proj = _load_our_projections()
    our_names = set(our_proj.keys())

    new_rows = []
    unmatched = []
    for rw in rw_rows:
        matched = _fuzzy_match(rw["player"], our_names)
        our_min = our_proj.get(matched, "") if matched else ""
        new_rows.append({
            "date":          today,
            "player":        rw["player"],
            "rw_team":       rw["rw_team"],
            "rw_projected":  rw["rw_projected"],
            "our_projected": our_min,
            "actual_minutes": "",  # filled in later by fill_actuals()
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
        "our":  _stats(our_errors),
        "rw":   _stats(rw_errors),
        "rows": filled_rows,
    }


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
