"""
Walk-forward backtest for minutes projection methods.

Compares four forecasting methods on held-out games:
  1. season_avg      — trimmed season average up to game N
  2. last3_median    — median of previous 3 games
  3. ewma            — EWMA (halflife=4) on all games up to game N
  4. ewma_context    — EWMA after context filter (foul, blowout, injury-return)

Usage:
    python backtest.py --team "Indiana Fever"
    python backtest.py --team "Indiana Fever" --min-games 8

Output:
    Per-method MAE, RMSE, and bias printed to stdout.
    Optional CSV output with --csv results.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from season_stats import (
    get_all_games_with_dates,
    _parse_boxscore,
    _trimmed_avg,
    _median,
    _iqr_trim,
    _ewma,
    _context_filter,
    ESPN_TEAM_IDS,
    _get,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_game_margin(gid: str, team_id: int) -> float:
    """Fetch point differential for team in given game."""
    try:
        summary = _get(
            f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary?event={gid}"
        )
        competitors = (
            summary.get("header", {})
            .get("competitions", [{}])[0]
            .get("competitors", [])
        )
        team_score = opp_score = None
        for c in competitors:
            score_val = c.get("score", {})
            val = (
                float(score_val.get("value", score_val))
                if isinstance(score_val, dict)
                else float(score_val or 0)
            )
            if str(c.get("team", {}).get("id", "")) == str(team_id):
                team_score = val
            else:
                opp_score = val
        if team_score is not None and opp_score is not None:
            return team_score - opp_score
    except Exception:
        pass
    return 0.0


def _rmse(errors: list[float]) -> float:
    if not errors:
        return 0.0
    return round((sum(e ** 2 for e in errors) / len(errors)) ** 0.5, 2)


def _mae(errors: list[float]) -> float:
    if not errors:
        return 0.0
    return round(sum(abs(e) for e in errors) / len(errors), 2)


def _bias(errors: list[float]) -> float:
    """Mean signed error — positive means over-projection."""
    if not errors:
        return 0.0
    return round(sum(errors) / len(errors), 2)


# ---------------------------------------------------------------------------
# Main backtest
# ---------------------------------------------------------------------------

def run_backtest(team_name: str, min_games: int = 5) -> dict:
    """
    Walk-forward backtest.

    For each game G (starting from game min_games+1):
      - Training set: all games before G
      - Test: actual minutes in game G
      - Predict using each method on training set only

    Returns dict with per-method error lists and summary stats.
    """
    team_id = ESPN_TEAM_IDS.get(team_name)
    if not team_id:
        print(f"Unknown team: {team_name}")
        return {}

    games = get_all_games_with_dates(team_name)
    if len(games) < min_games + 1:
        print(f"Not enough games for {team_name}: {len(games)} (need {min_games + 1})")
        return {}

    print(f"Backtesting {team_name} over {len(games)} games (warmup: {min_games})...")

    # Pre-load all boxscores + margins
    boxscores: dict[str, list[dict]] = {}
    margins:   dict[str, float]      = {}

    for gid, _ in games:
        box = _parse_boxscore(gid, team_id)
        boxscores[gid] = box
        margins[gid]   = _get_game_margin(gid, team_id)

    # Per-player per-method errors: {method: [abs_error, ...]}
    method_errors: dict[str, list[float]] = {
        "season_avg":   [],
        "last3_median": [],
        "ewma":         [],
        "ewma_context": [],
    }

    # Per-player raw predictions for CSV output
    records = []

    for test_idx in range(min_games, len(games)):
        test_gid, test_date = games[test_idx]
        train_gids = [gid for gid, _ in games[:test_idx]]

        # Actual minutes in the test game
        actual_box = boxscores.get(test_gid, [])

        # Build per-player training history
        player_history:     dict[str, list[float]] = defaultdict(list)
        player_fouls_hist:  dict[str, list[int]]   = defaultdict(list)
        player_margins_hist: dict[str, list[float]] = defaultdict(list)

        for gid in train_gids:
            box = boxscores.get(gid, [])
            for p in box:
                if p["dnp"] or p["minutes"] < 0.5:
                    continue
                name = p["name"]
                player_history[name].append(p["minutes"])
                player_fouls_hist[name].append(p.get("fouls", 0))
                player_margins_hist[name].append(margins.get(gid, 0.0))

        for p in actual_box:
            if p["dnp"] or p["minutes"] < 0.5:
                continue
            name   = p["name"]
            actual = p["minutes"]

            hist = player_history.get(name, [])
            if len(hist) < 3:
                continue  # not enough history to predict

            fouls_hist   = player_fouls_hist.get(name, [])
            margins_hist = player_margins_hist.get(name, [])

            # Method 1: trimmed season average
            pred_season = _trimmed_avg(hist)

            # Method 2: last-3 median
            l3 = hist[-3:]
            pred_last3 = _median(l3)

            # Method 3: EWMA on full history
            pred_ewma = _ewma(hist)

            # Method 4: EWMA after context filter
            ctx = _context_filter(hist, fouls_hist, margins_hist)
            pred_ewma_ctx = _ewma(ctx) if len(ctx) >= 2 else _trimmed_avg(hist)

            for method, pred in [
                ("season_avg",   pred_season),
                ("last3_median", pred_last3),
                ("ewma",         pred_ewma),
                ("ewma_context", pred_ewma_ctx),
            ]:
                err = pred - actual
                method_errors[method].append(err)

            records.append({
                "game_idx":      test_idx,
                "game_date":     test_date,
                "player":        name,
                "actual":        round(actual, 1),
                "pred_season":   pred_season,
                "pred_last3":    pred_last3,
                "pred_ewma":     pred_ewma,
                "pred_ewma_ctx": pred_ewma_ctx,
            })

    # Summary
    results = {}
    print(f"\n{'Method':<16} {'MAE':>6} {'RMSE':>7} {'Bias':>7}  (n={len(method_errors['season_avg'])} samples)")
    print("-" * 44)
    for method, errors in method_errors.items():
        mae  = _mae(errors)
        rmse = _rmse(errors)
        bias = _bias(errors)
        results[method] = {"mae": mae, "rmse": rmse, "bias": bias, "n": len(errors)}
        print(f"{method:<16} {mae:>6.2f} {rmse:>7.2f} {bias:>7.2f}")

    return {"summary": results, "records": records}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="WNBA minutes backtest")
    parser.add_argument("--team", default="Indiana Fever", help="Team name")
    parser.add_argument("--min-games", type=int, default=5,
                        help="Minimum games of history before first prediction (default: 5)")
    parser.add_argument("--csv", metavar="FILE",
                        help="Write per-prediction CSV to this file")
    parser.add_argument("--all-teams", action="store_true",
                        help="Run backtest for every team and print aggregate results")
    args = parser.parse_args()

    if args.all_teams:
        agg: dict[str, list[float]] = {
            "season_avg": [], "last3_median": [], "ewma": [], "ewma_context": []
        }
        for team in sorted(ESPN_TEAM_IDS.keys()):
            r = run_backtest(team, min_games=args.min_games)
            if r:
                for method, stats in r["summary"].items():
                    # Re-compute weighted contribution: n * mae (we'll average later)
                    agg[method].extend([stats["mae"]] * stats["n"])
        print("\n=== AGGREGATE ACROSS ALL TEAMS ===")
        print(f"{'Method':<16} {'Avg MAE':>8}")
        print("-" * 28)
        for method, vals in agg.items():
            avg = round(sum(vals) / len(vals), 2) if vals else 0.0
            print(f"{method:<16} {avg:>8.2f}")
    else:
        result = run_backtest(args.team, min_games=args.min_games)
        if result and args.csv and result.get("records"):
            out_path = Path(args.csv)
            fieldnames = [
                "game_idx", "game_date", "player", "actual",
                "pred_season", "pred_last3", "pred_ewma", "pred_ewma_ctx",
            ]
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(result["records"])
            print(f"\nWrote {len(result['records'])} records to {out_path}")


if __name__ == "__main__":
    main()
