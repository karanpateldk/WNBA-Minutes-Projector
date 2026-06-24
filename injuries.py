"""
WNBA injury report scraper.

Source: https://www.wnba.com/api/injury-reports
Returns links to the official PDF reports published by the WNBA.
We fetch the most recent PDF and parse player name + status.

Status values: 'Out', 'Questionable', 'Probable', 'Doubtful'
Names in PDF are "Last, First" — we normalise to "First Last".
"""

import io
import logging
import re

import pandas as pd
import requests
from pathlib import Path
from datetime import datetime

RAW_DIR = Path(__file__).parent.parent / "data" / "raw"
logger = logging.getLogger(__name__)

_API_URL = "https://www.wnba.com/api/injury-reports"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.wnba.com/"}

STATUS_PROB = {
    "out":          0.0,
    "doubtful":     0.15,
    "questionable": 0.70,
    "probable":     0.90,
    "active":       1.0,
}


def _last_first_to_full(name: str) -> str:
    """Convert 'Last, First' → 'First Last'."""
    parts = [p.strip() for p in name.split(",", 1)]
    if len(parts) == 2:
        return f"{parts[1]} {parts[0]}"
    return name.strip()


def scrape_injuries() -> pd.DataFrame:
    """
    Fetch the most recent WNBA official injury report PDF and parse it.
    Returns df: player_name, team, game_date, status, reason, play_prob, scraped_at
    """
    try:
        import pypdf
    except ImportError:
        logger.error("pypdf not installed — run: pip install pypdf")
        return pd.DataFrame()

    try:
        r = requests.get(_API_URL, headers=_HEADERS, timeout=10)
        r.raise_for_status()
        links = r.json().get("links", [])
        if not links:
            logger.warning("No injury report links found")
            return pd.DataFrame()
        latest_url = links[-1]["href"]
        logger.info(f"Fetching injury PDF: {latest_url}")
    except Exception as e:
        logger.error(f"Failed to fetch injury report index: {e}")
        return pd.DataFrame()

    try:
        pdf_r = requests.get(latest_url, headers=_HEADERS, timeout=15)
        pdf_r.raise_for_status()
        reader = pypdf.PdfReader(io.BytesIO(pdf_r.content))
        full_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        logger.error(f"Failed to fetch/parse PDF: {e}")
        return pd.DataFrame()

    # Parse line by line — PDF uses \n between tokens.
    # Pattern: a "Last, First" line is followed immediately by a status line.
    # e.g. lines: ["Jones,", "Brionna", "Out", "Injury/Illness", ...]
    statuses = {"out", "questionable", "probable", "doubtful", "day-to-day"}
    lines = [l.strip() for l in full_text.splitlines() if l.strip()]

    # Rejoin lines split mid-hyphen: "Laney-" + "Hamilton," → "Laney-Hamilton,"
    merged: list[str] = []
    for line in lines:
        if merged and merged[-1].endswith("-") and re.match(r"^[A-Za-z'\-]+,?$", line):
            merged[-1] = merged[-1] + line
        else:
            merged.append(line)
    lines = merged

    rows = []
    i = 0
    while i < len(lines):
        # Look for "Lastname," pattern
        if re.match(r"^[A-Z][A-Za-z'\-]+,$", lines[i]):
            last = lines[i].rstrip(",")
            # Next line should be first name
            if i + 1 < len(lines) and re.match(r"^[A-Z][A-Za-z'\-]+$", lines[i + 1]):
                first = lines[i + 1]
                # Line after first name should be status
                if i + 2 < len(lines) and lines[i + 2].lower() in statuses:
                    status = lines[i + 2]
                    full_name = f"{first} {last}"
                    play_prob = STATUS_PROB.get(status.lower(), 1.0)
                    rows.append({
                        "player_name": full_name,
                        "status":      status,
                        "play_prob":   play_prob,
                        "scraped_at":  datetime.utcnow().isoformat(),
                    })
                    i += 3
                    continue
        i += 1

    if not rows:
        logger.warning("No injury rows parsed from PDF — format may have changed")
        return pd.DataFrame()

    df = pd.DataFrame(rows).drop_duplicates(subset=["player_name"], keep="last")
    logger.info(f"Parsed {len(df)} injury entries from {latest_url}")
    return df


def load_or_scrape_injuries(cache_minutes: int = 60) -> pd.DataFrame:
    """Return cached injuries if fresh, otherwise re-scrape. Caches to parquet."""
    cache_path = RAW_DIR / "injuries.parquet"
    if cache_path.exists():
        try:
            df = pd.read_parquet(cache_path)
            if not df.empty:
                scraped_at = pd.to_datetime(df["scraped_at"].iloc[0])
                age = (datetime.utcnow() - scraped_at.replace(tzinfo=None)).total_seconds() / 60
                if age < cache_minutes:
                    logger.info(f"Using cached injuries ({age:.0f}m old)")
                    return df
        except Exception:
            pass

    df = scrape_injuries()
    if not df.empty:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache_path, index=False)
    return df


def get_player_status(player_name: str, injuries: pd.DataFrame | None = None) -> dict:
    """Look up a player's injury status. Returns {status, play_prob}."""
    if injuries is None:
        injuries = load_or_scrape_injuries()
    if injuries is None or injuries.empty:
        return {"status": "active", "play_prob": 1.0}

    name_lower = player_name.strip().lower()
    inj_names  = injuries["player_name"].str.lower()

    # 1. Exact full-name match
    mask = inj_names == name_lower
    if not mask.any():
        parts = name_lower.split()
        if len(parts) >= 2:
            first      = parts[0]
            first_init = first[0]
            last       = parts[-1]
            # last-name parts for hyphenated names e.g. "laney-hamilton" → ["laney", "hamilton"]
            last_parts = re.split(r"[-]", last)

            def _name_matches(n: str) -> bool:
                n_parts = n.split()
                if not n_parts:
                    return False
                n_first = n_parts[0]
                n_last  = n_parts[-1] if len(n_parts) > 1 else ""
                # first name must match (exact or initial)
                first_ok = (n_first == first or n_first == first_init + "."
                            or n_first.startswith(first_init + "."))
                if not first_ok:
                    return False
                # last name: exact match OR any hyphen-part of roster name matches PDF last
                return (n_last == last
                        or any(n_last == lp for lp in last_parts)
                        or any(lp in n_last for lp in last_parts))

            mask = inj_names.apply(_name_matches)
    matches = injuries[mask]
    if matches.empty:
        return {"status": "active", "play_prob": 1.0}

    row = matches.iloc[0]
    return {"status": row["status"], "play_prob": float(row["play_prob"])}


def find_absent_injured(
    all_players_db: dict[int, str],
    injuries: pd.DataFrame | None = None,
) -> tuple[set[int], set[int], set[int], set[int]]:
    """
    Scan injury report for players and match them to player IDs.
    Returns (doubtful_ids, out_ids, questionable_ids, probable_ids) for players in all_players_db.
    all_players_db: {player_id: player_name}
    """
    if injuries is None:
        injuries = load_or_scrape_injuries()
    if injuries is None or injuries.empty:
        return set(), set(), set(), set()

    doubtful_ids:     set[int] = set()
    out_ids:          set[int] = set()
    questionable_ids: set[int] = set()
    probable_ids:     set[int] = set()

    for pid, name in all_players_db.items():
        info   = get_player_status(name, injuries)
        status = info["status"].lower()
        if status in ("out", "inactive", "did not play", "dnp"):
            out_ids.add(int(pid))
        elif status in ("doubtful",):
            doubtful_ids.add(int(pid))
        elif status in ("questionable", "day-to-day"):
            questionable_ids.add(int(pid))
        elif status in ("probable",):
            probable_ids.add(int(pid))

    return doubtful_ids, out_ids, questionable_ids, probable_ids


def classify_players(
    stints_data: list[dict],
    injuries: pd.DataFrame | None = None,
) -> tuple[set[int], set[int], set[int], set[int]]:
    """
    Match stints_data players against the injury report.
    Returns (probable_ids, questionable_ids, doubtful_ids, out_ids).
    """
    if injuries is None:
        injuries = load_or_scrape_injuries()

    if injuries is None or injuries.empty:
        return set(), set(), set(), set()

    probable:     set[int] = set()
    questionable: set[int] = set()
    doubtful:     set[int] = set()
    out_ids:      set[int] = set()

    for p in stints_data:
        pid  = int(p.get("player_id", 0))
        name = str(p.get("player_name", "")).strip()
        if not name:
            continue
        info   = get_player_status(name, injuries)
        status = info["status"].lower()
        if status in ("out", "inactive", "did not play", "dnp"):
            out_ids.add(pid)
        elif status in ("doubtful",):
            doubtful.add(pid)
        elif status in ("questionable", "day-to-day"):
            questionable.add(pid)
        elif status in ("probable",):
            probable.add(pid)

    return probable, questionable, doubtful, out_ids


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    df = scrape_injuries()
    if not df.empty:
        print(df[["player_name", "team", "status", "play_prob", "comment"]].to_string())
    else:
        print("No injuries found or scrape failed.")
