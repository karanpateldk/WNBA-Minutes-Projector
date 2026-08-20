"""
Head-to-head evaluation: our model vs RotoWire, on a like-for-like basis.

Run this after ANY model change to confirm the change actually helped.

    python evaluate.py                 # headline table + per-tier + per-date
    python evaluate.py --baseline      # also score the old frozen-projection
                                       # method, to show what the fix was worth
    python evaluate.py --strict        # no target-game lineup info at all
    python evaluate.py --csv out.csv   # per-row dump for inspection

WHAT IS BEING COMPARED
----------------------
For every (date, player) row in data/accuracy_log.csv that has a RotoWire
projection and an actual, we score three predictors against the same actual:

  rotowire   RotoWire's published projection for that game
  replay     our model, rebuilt from games before that date (replay.py)
  frozen     our model as run today, applied to every historical date
             (the old accuracy_tracker behaviour — --baseline only)

Comparing on an identical row set matters. The previous code appended to
`our_errors` only when a RotoWire projection existed but appended to
`rw_errors` whenever RotoWire parsed, so a blank `our_projected` handed
RotoWire a free row. Here a row is scored for everyone or for no one.

WHY THE PAIRED CI
-----------------
Two independent MAEs invite reading a 0.1-minute difference as a win. The
quantity that actually carries a confidence interval is the per-row paired
difference |ours - actual| - |theirs - actual|. If its CI straddles zero the
comparison is a tie, however different the two MAEs look.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path

import replay

DATA_DIR = Path(__file__).parent / "data"
LOG_PATH = DATA_DIR / "accuracy_log.csv"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def _norm(n: str) -> str:
    return n.lower().replace(".", "").replace("'", "").replace("-", " ").strip()


def match_name(name: str, pool: dict[str, float]) -> str | None:
    """Exact, then normalised, then last-name + first-initial (only if unique)."""
    if name in pool:
        return name
    target = _norm(name)
    norm_pool: dict[str, list[str]] = defaultdict(list)
    for k in pool:
        norm_pool[_norm(k)].append(k)
    if target in norm_pool and len(norm_pool[target]) == 1:
        return norm_pool[target][0]
    parts = target.split()
    if len(parts) >= 2:
        last, init = parts[-1], parts[0][:1]
        cands = [k for k in pool
                 if _norm(k).split()[-1] == last and _norm(k).split()[0][:1] == init]
        if len(cands) == 1:
            return cands[0]
    return None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def summarise(errors: list[float]) -> dict:
    if not errors:
        return {"mae": None, "rmse": None, "within2": None, "within4": None, "n": 0}
    n = len(errors)
    return {
        "mae": sum(errors) / n,
        "rmse": math.sqrt(sum(e * e for e in errors) / n),
        "within2": 100 * sum(1 for e in errors if e <= 2) / n,
        "within4": 100 * sum(1 for e in errors if e <= 4) / n,
        "n": n,
    }


def paired(a: list[float], b: list[float]) -> dict:
    """
    Paired difference a - b with a 95% CI. `a` and `b` must be aligned
    row-for-row. Negative mean favours `a`.
    """
    d = [x - y for x, y in zip(a, b)]
    n = len(d)
    if n < 2:
        return {"mean": None, "ci": None, "n": n, "significant": False}
    m = sum(d) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in d) / (n - 1))
    se = sd / math.sqrt(n)
    half = 1.96 * se
    return {"mean": m, "ci": half, "n": n, "significant": abs(m) > half}


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------

def load_baseline(path: Path) -> dict[tuple[str, str], float]:
    """
    {(date, player): our_projected} from a pre-fix copy of the accuracy log.

    The baseline MUST come from a separate file. Reading `our_projected` out of
    the live log after running `accuracy_tracker.py --rebuild` would compare the
    replay against itself, which silently reports "no change" no matter how large
    the real effect was.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"no baseline log at {path} — pass --baseline-file, or skip --baseline")
    out: dict[tuple[str, str], float] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            v = _f(r.get("our_projected"))
            if v is not None:
                out[(r.get("date", ""), r.get("player", ""))] = v
    return out


def build_rows(strict: bool = False,
               baseline_path: Path | None = None) -> list[dict]:
    """
    One dict per scored player-game, with every predictor's number attached.
    Only rows where RotoWire, the replay, and an actual all exist are kept.
    When `baseline_path` is given, rows also require a baseline projection.
    """
    if not LOG_PATH.exists():
        raise FileNotFoundError(f"no accuracy log at {LOG_PATH}")
    with open(LOG_PATH, encoding="utf-8") as f:
        log = list(csv.DictReader(f))

    baseline = load_baseline(baseline_path) if baseline_path else None

    games = replay.load_games()
    dates = sorted({r["date"] for r in log if _f(r.get("rw_projected")) is not None})

    # Point-in-time projections, one pass per date.
    replayed: dict[str, dict[str, float]] = {}
    for d in dates:
        replayed[d] = replay.project_date(
            d, use_actual_availability=not strict, games=games)

    # Actual minutes, keyed by (player, date).
    actuals: dict[tuple[str, str], float] = {}
    for team_games in games.values():
        for g in team_games:
            for name, p in g.players.items():
                actuals[(name, g.date)] = p["minutes"]

    rows = []
    unmatched: dict[str, list[str]] = defaultdict(list)
    for r in log:
        d = r.get("date", "")
        rw = _f(r.get("rw_projected"))
        if rw is None or d not in replayed:
            continue
        player = r.get("player", "")

        pool = replayed[d]
        m = match_name(player, pool)
        if m is None:
            unmatched[d].append(player)
            continue

        actual = actuals.get((m, d))
        if actual is None:
            # Matched the projection pool but did not play — the tracker only
            # scores players with actual minutes, so there is nothing to score.
            continue

        row = {
            "date": d,
            "player": player,
            "team": r.get("rw_team", ""),
            "actual": actual,
            "rotowire": rw,
            "replay": pool[m],
        }
        if baseline is not None:
            fz = baseline.get((d, player))
            if fz is None:
                continue
            row["frozen"] = fz
        rows.append(row)

    n_unmatched = sum(len(v) for v in unmatched.values())
    if n_unmatched:
        sample = sorted({p for v in unmatched.values() for p in v})[:6]
        print(f"[evaluate] {n_unmatched} RotoWire rows had no model projection "
              f"(not on a projected roster that date): {sample}"
              f"{'...' if n_unmatched > 6 else ''}")
    return rows


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def provenance(rows: list[dict]) -> None:
    """
    State how current and how trustworthy the scored sample is, before any
    metric is shown.

    A stale log is indistinguishable from a fresh one by MAE alone — this was
    misdiagnosed once already, when a rebuilt log carried a fresh mtime while
    the clone behind it was 28 commits old. Two things are surfaced:

      * the window actually scored, and the last date with boxscore actuals, so
        a number that stopped moving days ago is visible as such;
      * how many scored rows rest on a RotoWire game date that was INFERRED
        from the file rather than recorded at download time. Inferred dates are
        usable but not certain, and a wrong date would attribute a projection
        to the wrong game.
    """
    try:
        import accuracy_tracker
        prov = accuracy_tracker.rw_date_provenance()
    except Exception as e:                    # tracker is optional here
        print(f"[evaluate] date provenance unavailable ({type(e).__name__})")
        prov = {}

    dates = sorted({r["date"] for r in rows})
    if not dates:
        return

    games = replay.load_games()
    all_dates = sorted({g.date for gs in games.values() for g in gs})

    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[prov.get(r["date"], "unrecorded")] += 1

    print(f"[evaluate] scored window {dates[0]} .. {dates[-1]} "
          f"({len(dates)} dates); boxscore data runs to {all_dates[-1]}")
    detail = ", ".join(f"{n} {src}" for src, n in sorted(counts.items()))
    print(f"[evaluate] RotoWire game dates behind these rows: {detail}")
    if counts.get("inferred") or counts.get("unrecorded"):
        print("[evaluate]   'inferred'/'unrecorded' = date read off the file, "
              "not recorded at download; treat as probable, not certain")


def _errs(rows: list[dict], key: str) -> list[float]:
    return [abs(r[key] - r["actual"]) for r in rows]


def _bias(rows: list[dict], key: str) -> float:
    return sum(r[key] - r["actual"] for r in rows) / len(rows)


def report(rows: list[dict], methods: list[str]) -> None:
    if not rows:
        print("No scoreable rows.")
        return

    print(f"\n{'=' * 74}")
    print(f"HEAD-TO-HEAD  —  {len(rows)} player-games, "
          f"{len({r['date'] for r in rows})} dates, identical row set")
    print("=" * 74)
    print(f"{'method':10} {'MAE':>7} {'RMSE':>7} {'w/in 2':>8} {'w/in 4':>8} {'bias':>7}")
    for m in methods:
        s = summarise(_errs(rows, m))
        print(f"{m:10} {s['mae']:7.3f} {s['rmse']:7.3f} "
              f"{s['within2']:7.1f}% {s['within4']:7.1f}% {_bias(rows, m):+7.2f}")

    print(f"\n{'-' * 74}")
    print("PAIRED DIFFERENCE vs RotoWire   (negative = we win; CI must exclude 0)")
    print("-" * 74)
    for m in methods:
        if m == "rotowire":
            continue
        p = paired(_errs(rows, m), _errs(rows, "rotowire"))
        verdict = "SIGNIFICANT" if p["significant"] else "tie (not significant)"
        print(f"  {m:10} {p['mean']:+.3f} min  95% CI "
              f"[{p['mean'] - p['ci']:+.3f}, {p['mean'] + p['ci']:+.3f}]   {verdict}")

    if "frozen" in methods:
        p = paired(_errs(rows, "replay"), _errs(rows, "frozen"))
        verdict = "SIGNIFICANT" if p["significant"] else "tie (not significant)"
        print(f"\n  replay vs frozen (did the fix help?): {p['mean']:+.3f} min  "
              f"95% CI [{p['mean'] - p['ci']:+.3f}, {p['mean'] + p['ci']:+.3f}]   {verdict}")

    # Per-tier — bench noise otherwise masks starter performance.
    print(f"\n{'-' * 74}")
    print("BY ROTOWIRE PROJECTED MINUTES")
    print("-" * 74)
    hdr = f"{'tier':>10} {'n':>5} " + " ".join(f"{m:>9}" for m in methods)
    print(hdr)
    for lo, hi in [(0, 5), (5, 10), (10, 20), (20, 28), (28, 50)]:
        sub = [r for r in rows if lo <= r["rotowire"] < hi]
        if not sub:
            continue
        cells = " ".join(f"{summarise(_errs(sub, m))['mae']:9.2f}" for m in methods)
        print(f"{lo:4}-{hi:<5} {len(sub):5} {cells}")

    # Per-date — shows how noisy any single day is.
    print(f"\n{'-' * 74}")
    print("BY DATE  (gap = our MAE - RotoWire MAE, with its own 95% CI)")
    print("-" * 74)
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r)
    print(f"{'date':12} {'n':>5} {'replay':>8} {'rotowire':>9} {'gap':>8} {'+/-':>7}")
    for d in sorted(by_date):
        sub = by_date[d]
        rp = summarise(_errs(sub, "replay"))["mae"]
        rw = summarise(_errs(sub, "rotowire"))["mae"]
        p = paired(_errs(sub, "replay"), _errs(sub, "rotowire"))
        ci = f"{p['ci']:7.2f}" if p["ci"] is not None else "      -"
        print(f"{d:12} {len(sub):5} {rp:8.2f} {rw:9.2f} {rp - rw:+8.2f} {ci}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", action="store_true",
                    help="also score the pre-fix frozen projections, read from "
                         "--baseline-file (NOT from the live log)")
    ap.add_argument("--baseline-file", metavar="PATH",
                    default=str(DATA_DIR / "accuracy_log.csv.bak"),
                    help="pre-fix copy of accuracy_log.csv (default: "
                         "data/accuracy_log.csv.bak)")
    ap.add_argument("--strict", action="store_true",
                    help="use no lineup information from the target game")
    ap.add_argument("--csv", metavar="PATH", help="write per-row detail to PATH")
    args = ap.parse_args()

    mode = "strict (no target-game lineup info)" if args.strict else \
           "announced-lineup (active list + starters known, as RotoWire had)"
    print(f"[evaluate] information set: {mode}")

    baseline_path = Path(args.baseline_file) if args.baseline else None
    if baseline_path:
        print(f"[evaluate] baseline from {baseline_path}")

    rows = build_rows(strict=args.strict, baseline_path=baseline_path)
    provenance(rows)
    methods = ["rotowire", "replay"] + (["frozen"] if args.baseline else [])
    report(rows, methods)

    if args.csv:
        out = Path(args.csv)
        cols = ["date", "player", "team", "actual"] + methods
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
