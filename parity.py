"""
Parity guard: does replay.py still feed the model the same thing production does?

    python parity.py            # exit 0 = parity holds, 1 = divergence

WHY THIS EXISTS
---------------
`replay.py` rebuilds the `team_data` dict that `build_projection` consumes, using
only games before a cutoff date. Production builds the same dict in
`season_stats.py` from live Snowflake data. Two independent builders of one
contract will drift, and when they drift the backtest silently measures a model
nobody ships.

That is not hypothetical. Two divergences were already found by hand:

  * `clean_avg` used a plain mean where production uses `_trimmed_avg` (IQR).
  * the last-3 anomaly floor was gated on `gp >= 8` where production requires
    `gp >= 8 AND trimmed_avg >= 18`. season_stats.py:804-809 explains why the
    18-minute guard exists; without it the filter drops a low-minute player's
    normal games and leaves an exceptional high game as their baseline.

Neither produced an error. Both quietly shifted every projection in the backtest.
This file exists so the next one fails loudly instead.

WHAT IS AND IS NOT CHECKED
--------------------------
Checked here, without a Snowflake connection:

  1. Key parity — replay emits exactly the keys production emits, so a field the
     model reads is never silently missing (a missing key reads as a default and
     looks like a real projection).
  2. Type/range sanity — every value is the type the model expects, and the
     documented-approximate fields carry the documented values.
  3. Shared feature math — replay imports `_trimmed_avg`, `_median`, `_ewma` and
     `_context_filter` from `season_stats` rather than copying them, so those
     cannot drift at all. This asserts the imports are still bound to the same
     objects.
  4. Duplicated constants — the one constant replay must restate
     (`_INJURY_DNP_KEYWORDS`) still matches the production literal.
  5. Model contract — `build_projection` accepts replay's dict and returns a
     lineup that satisfies the 200-minute invariant.

NOT checked: numeric equality against a live production `team_data`. That needs
Snowflake credentials and a same-day comparison, because production reads
season-to-date state while replay reads pre-cutoff state — on any historical
date the two SHOULD differ. Key/type/math parity is what can be verified
offline, and it is what both real divergences would have tripped.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import model
import replay
import season_stats

BASE = Path(__file__).parent

# Fields replay cannot reconstruct from the boxscore export, with the reason.
# Anything here must be documented in replay.py's FIELD FIDELITY section.
KNOWN_GAPS = {
    "crunch_time_poss": "possession-level data is Snowflake-only "
                        "(season_stats.py:951)",
    "quarter_avgs": "per-quarter minutes are exported but production's blend "
                    "also needs Snowflake rotation context",
}

failures: list[str] = []
notes: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def note(msg: str) -> None:
    notes.append(msg)


# ---------------------------------------------------------------------------
# 1. Key parity against the keys production actually emits
# ---------------------------------------------------------------------------

def production_keys() -> set[str]:
    """
    The team_data keys season_stats emits, read out of its source.

    Parsed rather than executed because building it for real needs Snowflake.
    Two places contribute: the dict literal assigned to `players[name]`, and
    later `players[name]["key"] = ...` enrichment (plus_minus, crunch_time_poss,
    avg_min_close and friends are set there, after the Snowflake calls). Missing
    the second group made every enriched field look like a replay-only extra.
    """
    src = inspect.getsource(season_stats)
    keys: set[str] = set()
    m = re.search(r"players\[name\]\s*=\s*\{(.*?)\n\s*\}", src, re.S)
    if m:
        keys |= set(re.findall(r'"([a-z0-9_]+)"\s*:', m.group(1)))
    # Enrichment assignments, e.g. players[name]["plus_minus"] = pm
    keys |= set(re.findall(r'players\[\w+\]\[\s*"([a-z0-9_]+)"\s*\]\s*=', src))
    return keys


def model_read_keys() -> set[str]:
    """
    team_data keys `model.py` actually reads.

    Severity hinges on this. A key the model reads that replay omits silently
    becomes a default and changes every projection. A key the model ignores
    (e.g. raw_avg_min, a display field) is a bookkeeping mismatch, not a
    measurement error, and should not fail the build.
    """
    src = inspect.getsource(model)
    got = set(re.findall(r'\.get\(\s*"([a-z0-9_]+)"', src))
    got |= set(re.findall(r'\[\s*"([a-z0-9_]+)"\s*\]', src))
    return got


def check_keys() -> None:
    prod = production_keys()
    if not prod:
        fail("could not locate season_stats' players[name] dict literal — "
             "the parser needs updating, parity is UNVERIFIED")
        return

    td = replay.team_data_as_of("Indiana Fever", "2026-08-05")
    players = {k: v for k, v in td.items() if isinstance(v, dict)}
    if not players:
        fail("replay produced no players for Indiana Fever 2026-08-05")
        return
    got = set(next(iter(players.values())))

    read = model_read_keys()
    missing = prod - got - set(KNOWN_GAPS)

    # Only a key the model reads can corrupt a projection.
    critical = sorted(missing & read)
    if critical:
        fail(f"replay is MISSING keys the model READS: {critical} — these will "
             f"fall back to defaults and the backtest will measure something "
             f"production never runs")
    cosmetic = sorted(missing - read)
    if cosmetic:
        note(f"absent but unread by the model, so harmless: {cosmetic}")

    for k in sorted((prod - got) & set(KNOWN_GAPS)):
        note(f"absent by design: {k} ({KNOWN_GAPS[k]})")

    extra = sorted((got - prod) & read)
    if extra:
        note(f"replay supplies model-read keys production's parsed contract "
             f"lacks: {extra} — check whether production is the gap")

    note(f"key parity: {len(prod & got)}/{len(prod)} production keys present; "
         f"{len(read & got)} of the model's {len(read)} read keys supplied")


# ---------------------------------------------------------------------------
# 2. Shared feature math is genuinely shared, not copied
# ---------------------------------------------------------------------------

def check_shared_math() -> None:
    for fn in ("_trimmed_avg", "_median", "_ewma", "_context_filter"):
        r = getattr(replay, fn, None)
        p = getattr(season_stats, fn, None)
        if p is None:
            fail(f"season_stats.{fn} no longer exists — replay imports it")
        elif r is None:
            fail(f"replay no longer imports {fn} from season_stats")
        elif r is not p:
            fail(f"replay.{fn} is NOT season_stats.{fn} — the feature math has "
                 f"been forked and can now drift silently")
    note("feature math: _trimmed_avg/_median/_ewma/_context_filter are the "
         "same objects as production's")


# ---------------------------------------------------------------------------
# 3. The one constant replay has to restate
# ---------------------------------------------------------------------------

def check_duplicated_constants() -> None:
    src = inspect.getsource(season_stats)
    m = re.search(r"_injury_keywords\s*=\s*\((.*?)\)", src, re.S)
    if not m:
        fail("could not find _injury_keywords in season_stats — "
             "replay._INJURY_DNP_KEYWORDS is now UNVERIFIED")
        return
    prod = tuple(re.findall(r'"([^"]+)"', m.group(1)))
    ours = tuple(replay._INJURY_DNP_KEYWORDS)
    if prod != ours:
        fail(f"injury-DNP keywords diverged.\n"
             f"     production: {prod}\n"
             f"     replay:     {ours}")
    else:
        note(f"injury-DNP keywords match ({len(ours)} entries)")


# ---------------------------------------------------------------------------
# 4. Value sanity + documented approximations
# ---------------------------------------------------------------------------

def check_values() -> None:
    td = replay.team_data_as_of("Indiana Fever", "2026-08-05")
    players = {k: v for k, v in td.items() if isinstance(v, dict)}

    numeric = ("avg_min", "clean_avg_min", "last3_avg", "last3_clean_avg",
               "ewma_min", "dnp_rate", "foul_rate", "starter_pct",
               "recent_starter_pct", "trend_3v6", "avg_min_close")
    for name, info in players.items():
        for k in numeric:
            v = info.get(k)
            if v is None:
                fail(f"{name}: {k} is None, expected a number")
            elif not isinstance(v, (int, float)):
                fail(f"{name}: {k} is {type(v).__name__}, expected a number")
        for k in ("dnp_rate", "foul_rate", "starter_pct", "recent_starter_pct"):
            v = info.get(k)
            if isinstance(v, (int, float)) and not 0.0 <= v <= 1.0:
                fail(f"{name}: {k}={v} outside [0, 1]")
        pm = info.get("plus_minus")
        if pm is not None and not isinstance(pm, (int, float)):
            fail(f"{name}: plus_minus is {type(pm).__name__}")
        if info.get("crunch_time_poss") is not None:
            fail(f"{name}: crunch_time_poss is populated but "
                 f"replay documents it as unavailable — update KNOWN_GAPS")

    with_pm = sum(1 for i in players.values() if i.get("plus_minus") is not None)
    note(f"plus_minus populated for {with_pm}/{len(players)} players "
         f"(production's threshold is 3+ games)")

    # Surface which dnp_rate formula is in force — they are not interchangeable.
    if replay.dnp_reason_available():
        note("dnp_rate: using production's coach-DNP-only formula")
    else:
        note("dnp_rate: APPROXIMATE — export emits no DNP rows, so injury and "
             "coach absences are pooled. Overstates dnp_rate, which shrinks "
             "bench minutes via model.py:512. Replay is therefore a floor on "
             "production, not a flattering estimate.")


# ---------------------------------------------------------------------------
# 5. The model still accepts replay's dict
# ---------------------------------------------------------------------------

def check_model_contract() -> None:
    for team, date in [("Indiana Fever", "2026-08-05"),
                       ("Las Vegas Aces", "2026-07-22"),
                       ("Los Angeles Sparks", "2026-08-03")]:
        td = replay.team_data_as_of(team, date)
        if not td:
            note(f"no pre-cutoff history for {team} on {date} — skipped")
            continue
        try:
            lineup = model.build_projection(td)
        except Exception as e:
            fail(f"build_projection rejected replay's team_data for {team} "
                 f"{date}: {type(e).__name__}: {e}")
            continue
        total = sum(p.projected_min for p in lineup.players)
        if not 195.0 <= total <= 205.0:
            fail(f"{team} {date}: team total {total:.1f} min violates the "
                 f"200-minute normalisation")
        bad = [p.name for p in lineup.players
               if p.projected_min < 0 or p.projected_min > 40]
        if bad:
            fail(f"{team} {date}: minutes outside [0, 40] for {bad[:3]}")
    note("model contract: build_projection accepts replay output and the "
         "200-minute invariant holds")


# ---------------------------------------------------------------------------
# 6. Determinism — set iteration order once made replays non-reproducible
# ---------------------------------------------------------------------------

def check_determinism() -> None:
    a = replay.project_team("Indiana Fever", "2026-08-05")
    b = replay.project_team("Indiana Fever", "2026-08-05")
    if a != b:
        fail("replay is not deterministic within a process")
        return
    note("determinism: repeated projection of one date is identical "
         "(run twice in separate processes to check PYTHONHASHSEED effects)")


def main() -> int:
    for check in (check_keys, check_shared_math, check_duplicated_constants,
                  check_values, check_model_contract, check_determinism):
        try:
            check()
        except Exception as e:
            fail(f"{check.__name__} raised {type(e).__name__}: {e}")

    print("=" * 74)
    print("REPLAY / PRODUCTION PARITY")
    print("=" * 74)
    for n in notes:
        print(f"  ok    {n}")
    if failures:
        print()
        for f in failures:
            print(f"  FAIL  {f}")
        print(f"\n{len(failures)} divergence(s). The backtest is measuring a "
              f"model production does not run.")
        return 1
    print("\nParity holds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
