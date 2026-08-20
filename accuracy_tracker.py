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


# Single source of truth for team abbreviations.
#
# RotoWire uses LAS for Los Angeles Sparks and LVA for Las Vegas Aces. This
# module previously mapped LAS -> Las Vegas Aces in all four of its copies of
# this table, which silently attributed 11 Sparks rows per affected date to a
# team that had not played, corrupting date validation, game labels and the
# game count. Keep one table.
TEAM_ABBREV = {
    "ATL": "Atlanta Dream",
    "CHI": "Chicago Sky",
    "CON": "Connecticut Sun",
    "CONN": "Connecticut Sun",
    "DAL": "Dallas Wings",
    "GSV": "Golden State Valkyries",
    "GSW": "Golden State Valkyries",
    "IND": "Indiana Fever",
    "LAS": "Los Angeles Sparks",      # RotoWire's code for the Sparks
    "LAX": "Los Angeles Sparks",
    "LOS": "Los Angeles Sparks",
    "LVA": "Las Vegas Aces",
    "LV":  "Las Vegas Aces",
    "MIN": "Minnesota Lynx",
    "NYL": "New York Liberty",
    "NY":  "New York Liberty",
    "PHO": "Phoenix Mercury",
    "PHX": "Phoenix Mercury",
    "POR": "Portland Fire",
    "SEA": "Seattle Storm",
    "TOR": "Toronto Tempo",
    "WAS": "Washington Mystics",
    "WSH": "Washington Mystics",
}


def _as_float(v) -> float | None:
    """Parse a CSV cell to float, returning None for blanks and junk."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def full_team(abbrev_or_name: str) -> str:
    """Resolve a RotoWire abbreviation to a full team name; pass through names."""
    s = (abbrev_or_name or "").strip()
    return TEAM_ABBREV.get(s.upper(), s)


# Kept as aliases so any external callers keep working.
_RW_ABBREV = TEAM_ABBREV


def _find_all_rw_csvs() -> list[tuple[str, Path]]:
    """
    Map each RotoWire projection CSV to the ET game date it describes.

    Resolution order:
      1. the snapshot manifest, if this exact file content was recorded before
      2. which date the projected teams actually played (team-overlap scoring)
      3. file modification time

    Anything resolved by 2 or 3 is written back to the manifest with that
    provenance, so the guess is made once and stays put instead of drifting
    when a file's mtime changes. See the manifest section below.
    """
    import glob as _glob
    from datetime import datetime as _dt

    manifest = _load_rw_manifest()

    # Build teams-by-date from ET-corrected boxscores
    tbd: dict[str, set] = {}
    if BOXSCORES_PATH.exists():
        try:
            import csv as _csv
            with open(BOXSCORES_PATH, encoding="utf-8") as f:
                for row in _csv.DictReader(f):
                    d = row.get("game_date", "")
                    t = row.get("team_name", "").strip()
                    if d and t:
                        tbd.setdefault(d, set()).add(t)
        except Exception:
            pass

    results = []
    pattern = str(RW_DOWNLOADS_DIR / "wnba-daily-projections*.csv")
    for filepath in _glob.glob(pattern):
        p = Path(filepath)
        try:
            # 1. Already recorded? Trust that over any inference.
            fp = _rw_fingerprint(p)
            recorded = manifest.get(fp)
            if recorded and recorded.get("game_date"):
                results.append((recorded["game_date"], p))
                continue

            rw_rows = _read_rw_csv(p)
            # Get teams with meaningful minutes (starters) — high-minute teams are the signal
            starter_teams = {_RW_ABBREV.get(r["rw_team"].upper(), r["rw_team"])
                             for r in rw_rows if float(r.get("rw_projected", 0) or 0) >= 20}

            best_date = None
            best_score = 0
            mdate_fallback = _dt.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")

            if tbd and starter_teams:
                for d, box_teams in tbd.items():
                    # Score: how many starter-level teams from CSV played on this date
                    overlap = len(starter_teams & box_teams)
                    # Penalise if there are teams in CSV that didn't play this date
                    missing = len(starter_teams - box_teams)
                    score = overlap - missing * 2  # heavy penalty for mismatches
                    if score > best_score and overlap >= 2:
                        best_score = score
                        best_date = d

            # 2/3. Record the guess so it is auditable and stops moving.
            game_date = best_date if best_date else mdate_fallback
            record_rw_snapshot(
                p, game_date,
                DATE_INFERRED if best_date else DATE_MTIME)
            results.append((game_date, p))
        except Exception:
            continue

    # Dedupe by date
    by_date: dict[str, Path] = {}
    for gdate, p in results:
        if gdate not in by_date:
            by_date[gdate] = p
    return sorted(by_date.items())


# ---------------------------------------------------------------------------
# RotoWire snapshot provenance
# ---------------------------------------------------------------------------
# The RotoWire CSVs live in the browser's Downloads folder as
# "wnba-daily-projections (N).csv". Nothing in the file records which slate it
# describes: there is no date column, and the (N) suffix is browser dedup
# numbering that shifts when a file is re-downloaded or deleted. So the only
# retroactive signals are file mtime and guessing the date from which teams
# appear -- and mtime is wrong whenever a file was downloaded late, copied, or
# touched. A projection filed under the wrong date is scored against the wrong
# game, which inflates our error, RotoWire's, or both.
#
# The fix is to stop inferring. `record_rw_snapshot` writes the date down when
# the file is ingested (when "today" is known for certain) and archives the file
# under a content hash so it survives Downloads being cleaned. Inferences are
# still allowed for the historical files, but they are written to the manifest
# too, tagged with how they were derived, so that:
#   * the inference runs once and stops drifting between runs, and
#   * anyone can see which dates rest on a guess, and correct one by editing
#     data/rw_snapshots.json.
#
# Correcting a date there is fixing a provenance error, NOT tuning a metric: it
# changes which game a projection is compared against, not the projection.

RW_ARCHIVE_DIR = DATA_DIR / "rw_archive"
RW_MANIFEST_PATH = DATA_DIR / "rw_snapshots.json"

# How a snapshot's game_date was established, best first.
DATE_EXPLICIT = "explicit"   # recorded at ingest, when the date was known
DATE_INFERRED = "inferred"   # matched to a slate by which teams appear
DATE_MTIME = "mtime"         # file modification time only — least trustworthy


def _rw_fingerprint(path: Path) -> str:
    """Content hash, so a file keeps its identity through browser renumbering."""
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _load_rw_manifest() -> dict:
    """{fingerprint: {game_date, date_source, ingested_at, archived_as}}"""
    if not RW_MANIFEST_PATH.exists():
        return {}
    try:
        import json
        with open(RW_MANIFEST_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_rw_manifest(manifest: dict) -> None:
    import json
    RW_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RW_MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)


def record_rw_snapshot(path: Path, game_date: str,
                       date_source: str = DATE_EXPLICIT) -> str:
    """
    Archive a RotoWire CSV and record which slate it describes.

    An existing EXPLICIT entry is never downgraded by a later inference; that is
    the whole point of recording it. Returns the fingerprint.
    """
    from datetime import datetime as _dt
    fp = _rw_fingerprint(path)
    manifest = _load_rw_manifest()
    prior = manifest.get(fp)
    if prior and prior.get("date_source") == DATE_EXPLICIT and date_source != DATE_EXPLICIT:
        return fp

    archived = prior.get("archived_as") if prior else None
    if not archived:
        try:
            RW_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            dest = RW_ARCHIVE_DIR / f"rw-{game_date}-{fp}.csv"
            if not dest.exists():
                dest.write_bytes(path.read_bytes())
            archived = dest.name
        except Exception:
            archived = None

    manifest[fp] = {
        "game_date": game_date,
        "date_source": date_source,
        "ingested_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_file": path.name,
        "archived_as": archived,
    }
    _save_rw_manifest(manifest)
    return fp


def rw_date_provenance() -> dict[str, str]:
    """
    {game_date: date_source}. Dates absent from this map were never recorded,
    which means they came from the pre-manifest inference path.
    """
    out: dict[str, str] = {}
    rank = {DATE_EXPLICIT: 3, DATE_INFERRED: 2, DATE_MTIME: 1}
    for entry in _load_rw_manifest().values():
        d, s = entry.get("game_date"), entry.get("date_source", DATE_MTIME)
        if d and rank.get(s, 0) > rank.get(out.get(d, ""), 0):
            out[d] = s
    return out


LOG_COLS = ["date", "game_label", "player", "rw_team", "rw_projected",
            "our_projected", "actual_minutes", "proj_source", "rw_date_source"]

# proj_source values:
#   "live"   — snapshotted from the running model before that day's games, so it
#              is a genuine forecast. Never overwritten once actuals land.
#   "replay" — reconstructed by replay.py from games before that date. Used for
#              dates we never snapshotted live.
#   ""       — legacy row written by the old code, where our_projected was the
#              model as run on the day of the backfill, applied to every
#              historical date. Not point-in-time; treat as unscoreable.
LIVE = "live"
REPLAY = "replay"

# RotoWire name → Sportradar name when they differ
# Add entries here whenever a player can't be matched automatically
RW_TO_SR_NAMES: dict[str, str] = {
    # Same name in both systems — listed here to force inclusion despite team mismatch
    "Monique Akoa Makani": "Monique Akoa Makani",
    "Kayla Alexander":     "Kayla Alexander",
    "Anneli Maley":        "Anneli Maley",
    "Angela Dugalic":      "Angela Dugalic",
    "Tima Pouye":          "Tima Pouye",
}

# RotoWire team abbreviation → full name used in season_stats
_RW_TEAM_MAP = TEAM_ABBREV


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
            # Supplement with season avg only for active players who have
            # meaningful games on their CURRENT team (not traded-away season avg).
            # Uses season_gp from snowflake_player_stats which reflects current team games.
            _roster_path = DATA_DIR / "snowflake_current_rosters.csv"
            _stats_path  = DATA_DIR / "snowflake_player_stats.csv"
            if _roster_path.exists() and _stats_path.exists():
                # Build current-team stats lookup (team_name matches current roster)
                _current_stats: dict[str, tuple[str, float, int]] = {}  # name -> (team, avg, gp)
                with open(_stats_path, encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        n = row.get("player_full_name", "").strip()
                        t = row.get("team_name", "").strip()
                        try:
                            avg = float(row.get("avg_minutes") or 0)
                            gp  = int(row.get("season_gp") or 0)
                            _current_stats[n] = (t, avg, gp)
                        except (ValueError, TypeError):
                            pass
                # Build current roster team lookup
                _current_team: dict[str, str] = {}
                with open(_roster_path, encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        n = row.get("player_name", "").strip()
                        t = row.get("team_name", "").strip()
                        if n and t:
                            _current_team[n] = t
                # Only supplement if stats team matches current roster team AND 5+ games
                added = 0
                for n, curr_team in _current_team.items():
                    if n not in result and n in _current_stats:
                        stats_team, avg, gp = _current_stats[n]
                        if stats_team == curr_team and avg >= 5.0 and gp >= 5:
                            result[n] = round(avg, 1)
                            added += 1
            # Force-include players in alias map that still aren't matched
            for rw_name, sr_name in RW_TO_SR_NAMES.items():
                if sr_name not in result:
                    avg = _current_stats.get(sr_name, ('', 0.0, 0))[1]
                    if avg >= 5.0:
                        result[sr_name] = round(avg, 1)
            print(f"[accuracy] Total players after roster supplement: {len(result)}")
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
                d = row.get("game_date", "")[:10]
                t = row.get("team_name", "").strip()
                if d and t:
                    result.setdefault(d, set()).add(t)
    except Exception:
        pass
    return result


def _team_by_player_date() -> dict[tuple[str, str], str]:
    """
    {(player, date): full_team_name} from the boxscores.

    `rw_team` cannot be trusted to identify a team. RotoWire uses LAS for the
    Los Angeles Sparks, while the old backfill wrote `team_name[:3].upper()`,
    which turns "Las Vegas Aces" into the same "LAS". The log therefore contains
    both meanings and no abbreviation table can separate them. Who a player
    actually played for on a given date is unambiguous, so resolve from that.
    """
    result: dict[tuple[str, str], str] = {}
    if not BOXSCORES_PATH.exists():
        return result
    try:
        import csv as _csv
        with open(BOXSCORES_PATH, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                n = (row.get("player_full_name") or "").strip()
                d = (row.get("game_date") or "")[:10]
                t = (row.get("team_name") or "").strip()
                if n and d and t:
                    result[(n, d)] = t
    except Exception:
        pass
    return result


def _real_matchups() -> dict[tuple[str, str], str]:
    """
    Return {(date, full_team_name): "Team A vs Team B"} built from the two teams
    that share a game_id in the boxscores.

    Labels used to be manufactured by sorting the day's teams and pairing them
    two at a time, which is only correct on single-game days. On 2026-07-17 that
    produced "ATL vs CHI / CON vs IND / SEA vs TOR" when the real games were
    ATL-TOR, CHI-LAS, CON-PHO and IND-SEA — so "Games tracked" and the game list
    in the app were both wrong.
    """
    if not BOXSCORES_PATH.exists():
        return {}
    teams_by_game: dict[str, set] = {}
    date_by_game: dict[str, str] = {}
    try:
        import csv as _csv
        with open(BOXSCORES_PATH, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                gid = (row.get("game_id") or "").strip()
                t = (row.get("team_name") or "").strip()
                d = (row.get("game_date") or "")[:10]
                if gid and t and d:
                    teams_by_game.setdefault(gid, set()).add(t)
                    date_by_game[gid] = d
    except Exception:
        return {}

    out: dict[tuple[str, str], str] = {}
    for gid, teams in teams_by_game.items():
        if len(teams) != 2:
            continue
        label = " vs ".join(sorted(teams))
        d = date_by_game[gid]
        for t in teams:
            out[(d, t)] = label
    return out


_ABBREV_TO_FULL_SAVE = TEAM_ABBREV


def _save_log(rows: list[dict]) -> None:
    # Normalise rw_team to a full team name resolved from the boxscores, then set
    # game_label from the real matchup. Both are self-healing: stale
    # alphabetically-paired labels and ambiguous abbreviations get corrected on
    # every save.
    matchups = _real_matchups()
    team_lookup = _team_by_player_date()
    # How each date's RotoWire projections got their date. Stamped per row so a
    # reader can tell a recorded date from a guessed one without opening the
    # manifest. Blank means the row predates the manifest.
    provenance = rw_date_provenance()
    for r in rows:
        d = r.get("date", "")
        team = team_lookup.get((r.get("player", ""), d)) or full_team(r.get("rw_team", ""))
        if team:
            r["rw_team"] = team
        real = matchups.get((d, team))
        if real:
            r["game_label"] = real
        elif not r.get("game_label"):
            r["game_label"] = team
        if str(r.get("rw_projected") or "").strip():
            src = provenance.get(d)
            if src:
                r["rw_date_source"] = src

    # Dedupe by (date, player). For backfilled rows (no rw_projected), also
    # validate the team played on that date. RotoWire rows are trusted as-is
    # since RotoWire only projects teams playing that day.
    tbd = _teams_by_date()
    seen_keys: set = set()
    valid_rows = []
    for r in rows:
        d = r.get("date", "")
        player = r.get("player", "")
        key = (d, player)
        if key in seen_keys:
            continue
        # Only validate backfilled rows (no RotoWire projection)
        is_backfill = not str(r.get("rw_projected") or "").strip()
        if is_backfill and tbd:
            resolved = full_team(r.get("rw_team", ""))
            teams_on_date = tbd.get(d, set())
            if teams_on_date and resolved not in teams_on_date:
                continue  # backfill team didn't play on this date
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


def rebuild_replay_projections(overwrite_live: bool = False) -> int:
    """
    Recompute `our_projected` for every logged date as a point-in-time forecast,
    using only games that finished before that date (see replay.py).

    This is the fix for the core measurement bug: `backfill_from_boxscores` used
    to call `_load_our_projections()` once and write that single number onto
    every historical date, so 133 of 170 tracked players had exactly ONE
    `our_projected` across the whole log while RotoWire's moved every game. That
    compared a season-to-date constant against a real per-game forecast.

    Rows marked `proj_source == "live"` are left alone by default — a projection
    actually made before tip-off is better evidence than any reconstruction.

    Returns the number of rows updated.
    """
    try:
        import replay
    except Exception as e:
        print(f"[accuracy] replay unavailable, cannot rebuild: {e}")
        return 0

    rows = _load_existing_log()
    if not rows:
        return 0

    try:
        games = replay.load_games()
    except Exception as e:
        print(f"[accuracy] could not load boxscores: {e}")
        return 0

    dates = sorted({r.get("date", "") for r in rows if r.get("date")})
    updated = 0
    skipped_live = 0

    for d in dates:
        day_rows = [r for r in rows if r.get("date") == d]
        if not day_rows:
            continue
        try:
            proj = replay.project_date(d, games=games)
        except Exception as e:
            print(f"[accuracy] replay failed for {d}: {e}")
            continue
        if not proj:
            continue
        names = set(proj)
        for r in day_rows:
            if r.get("proj_source") == LIVE and not overwrite_live:
                skipped_live += 1
                continue
            matched = _fuzzy_match(r.get("player", ""), names)
            if matched is None:
                continue
            r["our_projected"] = proj[matched]
            r["proj_source"] = REPLAY
            updated += 1

    _save_log(rows)
    print(f"[accuracy] Rebuilt {updated} point-in-time projections across "
          f"{len(dates)} dates ({skipped_live} live rows preserved)")
    return updated


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


def _load_actuals_by_player() -> dict[str, tuple[str, str, float]]:
    """
    Return {player_name: (game_date, matchup_label, actual_minutes)}
    for the most recent game each player played, from boxscores.
    Used to match RotoWire projections to the actual game played.
    """
    if not BOXSCORES_PATH.exists():
        return {}
    result: dict[str, tuple[str, str, float]] = {}
    teams_by_game: dict[str, list[str]] = {}
    try:
        import csv as _csv
        # First pass: build game -> teams lookup for matchup labels
        with open(BOXSCORES_PATH, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                gid = row.get("game_id", "")
                t = row.get("team_name", "").strip()
                d = row.get("game_date", "")
                if gid and t:
                    teams_by_game.setdefault(gid, []).append(t)
        # Second pass: per player, find latest game
        game_dates: dict[str, str] = {}
        with open(BOXSCORES_PATH, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                gid = row.get("game_id", "")
                d = row.get("game_date", "")
                if gid and d:
                    game_dates[gid] = d
        with open(BOXSCORES_PATH, encoding="utf-8") as f:
            rows_sorted = sorted(_csv.DictReader(f),
                                 key=lambda r: r.get("game_date", ""), reverse=True)
        seen: set = set()
        for row in rows_sorted:
            name = row.get("player_full_name", "").strip()
            gid = row.get("game_id", "")
            d = row.get("game_date", "")
            if not name or not d or name in seen:
                continue
            try:
                mins = float(row.get("minutes") or 0)
            except (ValueError, TypeError):
                mins = 0.0
            teams = sorted(teams_by_game.get(gid, []))
            label = " vs ".join(teams[:2]) if len(teams) >= 2 else d
            result[name] = (d, label, mins)
            seen.add(name)
    except Exception:
        pass
    return result


def _merge_rw_projections(rw_rows: list[dict], gdate: str,
                          existing: list[dict]) -> int:
    """
    Attach RotoWire projections to a historical date already present in the log.

    Only `rw_projected` and `rw_team` are written. `our_projected` is left for
    `rebuild_replay_projections` to fill from pre-cutoff games — writing the live
    model here is what made the old log unscoreable.

    An existing rw_projected is never overwritten: the first snapshot of a slate
    is the one published closest to tip-off that we hold, and silently replacing
    it with another file's number would make the comparison depend on file order.

    Returns the number of rows given a RotoWire projection.
    """
    by_name = {}
    for rw in rw_rows:
        nm = RW_TO_SR_NAMES.get(rw["player"], rw["player"])
        by_name[nm] = rw
    rw_names = set(by_name)

    date_rows = [r for r in existing if r["date"] == gdate]
    filled = 0
    for r in date_rows:
        if str(r.get("rw_projected") or "").strip():
            continue
        matched = r["player"] if r["player"] in by_name else _fuzzy_match(r["player"], rw_names)
        if not matched:
            continue
        rw = by_name[matched]
        val = rw.get("rw_projected")
        if val in (None, ""):
            continue
        r["rw_projected"] = val
        if not str(r.get("rw_team") or "").strip():
            r["rw_team"] = full_team(rw.get("rw_team", "")) or rw.get("rw_team", "")
        filled += 1

    # RotoWire players with no row on this date at all. These are players who
    # were projected but did not record minutes (DNP, or did not dress). They
    # are added with no actual, so compute_stats will not score them until an
    # actual exists — but recording them keeps the RotoWire side complete.
    have = {r["player"] for r in date_rows}
    added = 0
    for nm, rw in by_name.items():
        if nm in have or _fuzzy_match(nm, have):
            continue
        val = rw.get("rw_projected")
        if val in (None, ""):
            continue
        existing.append({
            "date": gdate,
            "game_label": "",
            "player": nm,
            "rw_team": full_team(rw.get("rw_team", "")) or rw.get("rw_team", ""),
            "rw_projected": val,
            "our_projected": "",     # rebuild_replay_projections fills this
            "actual_minutes": "",
            "proj_source": "",
        })
        added += 1

    if filled or added:
        _save_log(existing)
    print(f"[accuracy] {gdate}: attached RotoWire projections to {filled} existing "
          f"rows, added {added} RotoWire-only rows")
    return filled + added


def snapshot_today(rw_path: Path | None = None, force_date: str | None = None) -> int:
    """
    Read a RotoWire CSV, snapshot our projections, and append to log.
    Matches each player to their actual game using boxscores — no date guessing.
    Returns the number of new rows added.
    Skips if entries for the same players+actuals already exist in the log.
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

    # Write the date down now, while it is known for certain, and archive the
    # file. Everything downstream then reads a recorded date instead of guessing
    # one from mtime or team overlap.
    #
    # Only when force_date is absent is the date genuinely known: `today` is then
    # the real calendar date. With force_date set, the caller is replaying an old
    # file whose date came from inference (snapshot_all_available), so recording
    # it as EXPLICIT would launder a guess into a certainty and defeat the point
    # of tracking provenance. Leave the existing manifest entry alone.
    if force_date is None:
        try:
            record_rw_snapshot(path, today, DATE_EXPLICIT)
        except Exception as _pe:
            print(f"[accuracy] Could not record RotoWire provenance: {_pe}")

    # Load existing log
    existing = _load_existing_log()

    rw_rows = _read_rw_csv(path)
    if not rw_rows:
        print("[accuracy] No RotoWire rows parsed — skipping snapshot")
        return 0

    # A historical date that is already in the log (from the boxscore backfill)
    # takes a different path. The live-refresh below must not run: it writes
    # TODAY's model onto that date, which is precisely the frozen-constant bug
    # this module was rewritten to remove. RotoWire's number, by contrast, was
    # published before the game, so filling it in now is transcription, not
    # lookahead — and it is what turns a backfilled date into a scoreable one.
    if force_date and any(r["date"] == force_date for r in existing):
        return _merge_rw_projections(rw_rows, force_date, existing)

    # If today already has rows, refresh our_projected with current model values
    # (lineup news firms up during the day, so a later snapshot is a better
    # forecast). Rows that already have actuals are frozen — rewriting a
    # projection after the game has been played is not a forecast, and doing
    # that indiscriminately is what made the old log unscoreable.
    today_rows = [r for r in existing if r["date"] == today]
    if today_rows:
        refreshable = [r for r in today_rows if not str(r.get("actual_minutes") or "").strip()]
        if not refreshable:
            print(f"[accuracy] {today} already has actuals — leaving projections frozen")
            return 0
        our_proj = _load_our_projections()
        our_names = set(our_proj.keys())
        refreshed = 0
        for r in refreshable:
            matched = _fuzzy_match(r["player"], our_names)
            if matched and our_proj.get(matched):
                r["our_projected"] = our_proj[matched]
                r["proj_source"] = LIVE
                refreshed += 1
        _save_log(existing)
        print(f"[accuracy] Refreshed our_projected for {refreshed}/{len(refreshable)} "
              f"unplayed rows for {today}")
        return 0

    # Prevent duplicate snapshots only when date is auto-detected (not force_date).
    # When force_date is set we're explicitly mapping this CSV to a specific date.
    if not force_date:
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

    # `our_proj` is the model AS OF NOW. That is a genuine pre-game forecast only
    # when this row's date is actually today. For a historical date (force_date,
    # i.e. an old CSV being ingested late) it is a projection built from games
    # that had not been played yet on that date — lookahead, and lookahead that
    # flatters our MAE.
    #
    # It cannot simply be written and left for rebuild_replay_projections to fix,
    # because that function deliberately PRESERVES proj_source == LIVE: a real
    # pre-tip projection is better evidence than any reconstruction. Stamping a
    # backfilled row LIVE would therefore pin today's model onto a past game
    # permanently. So: no projection is claimed here at all, and the rebuild that
    # runs next fills in an honest point-in-time one.
    backfilling = force_date is not None and force_date != str(date.today())
    new_rows = []
    unmatched = []
    for rw in rw_rows:
        # Check alias map first, then fuzzy match
        sr_name = RW_TO_SR_NAMES.get(rw["player"], rw["player"])
        matched = sr_name if sr_name in our_proj else _fuzzy_match(rw["player"], our_names)
        our_min = "" if backfilling else (our_proj.get(matched, "") if matched else "")
        new_rows.append({
            "date":           today,
            "game_label":     game_label_map.get(rw["rw_team"], rw["rw_team"]),
            "player":         rw["player"],
            "rw_team":        rw["rw_team"],
            "rw_projected":   rw["rw_projected"],
            "our_projected":  our_min,
            "actual_minutes": "",
            "proj_source":    LIVE if our_min != "" else "",
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
        key = (player, gdate)
        if key in actuals:
            row["actual_minutes"] = actuals[key]
            updated += 1
            continue
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
    import math

    rows = _load_existing_log()

    # A row is scored for BOTH predictors or for NEITHER. The old code appended
    # to our_errors only when a RotoWire projection existed but appended to
    # rw_errors whenever RotoWire parsed, so a blank our_projected handed
    # RotoWire a free row and the two MAEs were computed over different samples.
    #
    # Legacy rows (proj_source == "") carry the old non-point-in-time projection
    # and are excluded from scoring; run rebuild_replay_projections() to convert
    # them. They still appear in `rows` for display.
    scored_rows: list[dict] = []
    our_errors, rw_errors = [], []
    our_pct_errors, rw_pct_errors = [], []  # MAPE for rotation players
    legacy_skipped = 0

    for row in rows:
        actual_f = _as_float(row.get("actual_minutes"))
        our_f    = _as_float(row.get("our_projected"))
        rw_f     = _as_float(row.get("rw_projected"))
        if actual_f is None or our_f is None or rw_f is None:
            continue
        if row.get("proj_source", "") not in (LIVE, REPLAY):
            legacy_skipped += 1
            continue

        scored_rows.append(row)
        our_errors.append(abs(our_f - actual_f))
        rw_errors.append(abs(rw_f - actual_f))
        if our_f >= 10 and actual_f >= 5:
            our_pct_errors.append(abs(our_f - actual_f) / actual_f * 100)
        if rw_f >= 10 and actual_f >= 5:
            rw_pct_errors.append(abs(rw_f - actual_f) / actual_f * 100)

    # Game count from real matchups keyed on (date, resolved team), so a team
    # can never be double-counted via an abbreviation/full-name label pair.
    matchups = _real_matchups()
    seen_game_keys: set = set()
    for r in scored_rows:
        d = r.get("date", "")
        team = full_team(r.get("rw_team", ""))
        seen_game_keys.add((d, matchups.get((d, team), team)))

    game_list = sorted({f"{d} — {lbl}" for d, lbl in seen_game_keys if lbl},
                       reverse=True)

    def _stats(errors: list[float], pct_errors: list[float] | None = None) -> dict:
        if not errors:
            return {"mae": None, "rmse": None, "within2": None, "within4": None,
                    "mape": None, "n": 0}
        n = len(errors)
        mape = round(sum(pct_errors) / len(pct_errors), 1) if pct_errors else None
        return {
            "mae":     round(sum(errors) / n, 2),
            "rmse":    round(math.sqrt(sum(e * e for e in errors) / n), 2),
            "within2": round(100 * sum(1 for e in errors if e <= 2) / n, 1),
            "within4": round(100 * sum(1 for e in errors if e <= 4) / n, 1),
            "mape":    mape,   # % error on rotation players (≥10 min projected, ≥5 actual)
            "n":       n,
        }

    # Paired difference is the only figure that supports a verdict. Two
    # independent MAEs invite reading a 0.1-minute difference as a win; on this
    # sample the CI half-width is around 0.25 min, so anything smaller is noise.
    diffs = [o - w for o, w in zip(our_errors, rw_errors)]
    paired: dict = {"mean": None, "ci": None, "n": len(diffs), "significant": False}
    if len(diffs) >= 2:
        n = len(diffs)
        m = sum(diffs) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in diffs) / (n - 1))
        half = 1.96 * sd / math.sqrt(n)
        paired = {
            "mean": round(m, 3),
            "ci": round(half, 3),
            "n": n,
            "significant": abs(m) > half,
        }

    return {
        "our":            _stats(our_errors, our_pct_errors),
        "rw":             _stats(rw_errors, rw_pct_errors),
        "rows":           scored_rows,
        "game_count":     len(seen_game_keys),
        "game_list":      game_list,
        "paired":         paired,
        "legacy_skipped": legacy_skipped,
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

    # Point-in-time projections, rebuilt per date from games before that date.
    # This function used to call _load_our_projections() once and write that
    # single number onto every historical date — the frozen-constant bug.
    try:
        import replay as _replay
        _games = _replay.load_games()
    except Exception as e:
        print(f"[accuracy] Backfill needs replay.py: {e}")
        return 0

    matchups = _real_matchups()
    new_rows = []
    dates_to_fill = sorted(d for d in actuals_by_date if d >= since_date)

    for gdate in dates_to_fill:
        day_players = actuals_by_date[gdate]
        try:
            team_proj = _replay.project_date(gdate, games=_games)
        except Exception as e:
            print(f"[accuracy] replay failed for {gdate}: {e}")
            continue
        if not team_proj:
            continue
        proj_names = set(team_proj)

        for p in day_players:
            key = (gdate, p["player"])
            if key in existing_keys:
                continue
            our_min = team_proj.get(p["player"], "")
            if our_min == "":
                matched = _fuzzy_match(p["player"], proj_names)
                our_min = team_proj.get(matched, "") if matched else ""
            if p["minutes"] < 0.5 and our_min == "":
                continue  # skip true DNPs we have no projection for
            new_rows.append({
                "date":           gdate,
                "game_label":     matchups.get((gdate, p["team"]), p["team"]),
                "player":         p["player"],
                "rw_team":        p["team"],
                "rw_projected":   "",  # no RotoWire data for backfilled games
                "our_projected":  our_min,
                "actual_minutes": p["minutes"],
                "proj_source":    REPLAY if our_min != "" else "",
            })
            existing_keys.add(key)

    if new_rows:
        _save_log(existing + new_rows)
        print(f"[accuracy] Backfilled {len(new_rows)} player-games across {len(dates_to_fill)} dates")
    else:
        print("[accuracy] Backfill: no new rows to add")
    return len(new_rows)


def _print_summary() -> None:
    stats = compute_stats()
    our, rw, pr = stats["our"], stats["rw"], stats["paired"]
    print(f"\nAccuracy Summary — {our['n']} player-games, "
          f"{stats['game_count']} games, identical row set")
    if stats["legacy_skipped"]:
        print(f"  ({stats['legacy_skipped']} legacy rows excluded — run "
              f"rebuild_replay_projections() to convert them)")
    if not our["n"]:
        print("  No scoreable rows yet.")
        return
    print(f"  Our model — MAE: {our['mae']} min | RMSE: {our['rmse']} | "
          f"Within 2: {our['within2']}% | Within 4: {our['within4']}%")
    print(f"  RotoWire  — MAE: {rw['mae']} min | RMSE: {rw['rmse']} | "
          f"Within 2: {rw['within2']}% | Within 4: {rw['within4']}%")
    if pr["mean"] is not None:
        lo, hi = pr["mean"] - pr["ci"], pr["mean"] + pr["ci"]
        verdict = "significant" if pr["significant"] else "tie (CI includes 0)"
        side = "we are better" if pr["mean"] < 0 else "RotoWire is better"
        print(f"  Paired diff (ours - RotoWire): {pr['mean']:+.3f} min "
              f"95% CI [{lo:+.3f}, {hi:+.3f}] — {verdict}"
              f"{', ' + side if pr['significant'] else ''}")


if __name__ == "__main__":
    import sys

    args = set(sys.argv[1:])
    if "--rebuild" in args:
        print("Rebuilding point-in-time projections from replay...")
        rebuild_replay_projections(overwrite_live="--force" in args)
        fill_actuals()
        _print_summary()
    else:
        print("Snapshotting today's projections...")
        snapshot_today()
        print("Filling actuals from boxscores...")
        fill_actuals()
        _print_summary()
