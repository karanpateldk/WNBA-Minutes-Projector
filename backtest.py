"""
Walk-forward backtest for minutes projection methods.

Compares seven forecasting methods on held-out games:
  1. season_avg      — trimmed season average up to game N
  2. last3_median    — median of previous 3 games
  3. last5_median    — median of previous 5 games
  4. last10_median   — median of previous 10 games
  5. ewma            — EWMA (halflife=4) on all games up to game N
  6. ewma_context    — EWMA after context filter (foul, blowout, injury-return)
  7. weighted_blend  — adaptive 15/85 season/recent blend (mirrors production model)

Usage:
    python backtest.py --team "Indiana Fever"
    python backtest.py --team "Indiana Fever" --min-games 8
    python backtest.py --all-teams

Output:
    Per-method MAE, RMSE, MedAE, %within2, %within4, bias.
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


def _medae(errors: list[float]) -> float:
    """Median absolute error — robust to outliers."""
    if not errors:
        return 0.0
    abs_errors = sorted(abs(e) for e in errors)
    n = len(abs_errors)
    mid = n // 2
    if n % 2 == 1:
        return round(abs_errors[mid], 2)
    return round((abs_errors[mid - 1] + abs_errors[mid]) / 2, 2)


def _pct_within(errors: list[float], threshold: float) -> float:
    """Percentage of predictions within `threshold` minutes of actual."""
    if not errors:
        return 0.0
    return round(100.0 * sum(1 for e in errors if abs(e) <= threshold) / len(errors), 1)


def _bias(errors: list[float]) -> float:
    """Mean signed error — positive means over-projection."""
    if not errors:
        return 0.0
    return round(sum(errors) / len(errors), 2)


def _weighted_blend(hist: list[float]) -> float:
    """
    Mirror the production model's sample-size-aware blend.
    Weights shift from season-heavy (early season) toward recent-heavy (late season).
    """
    if not hist:
        return 0.0
    n = len(hist)
    season = _trimmed_avg(hist)
    last3_avg = _median(hist[-3:]) if n >= 3 else season
    last1 = hist[-1] if n >= 1 else None

    if n < 5:
        w_season, w_last3 = 1.00, 0.00
    elif n < 10:
        w_season, w_last3 = 0.70, 0.30
    elif n < 20:
        w_season, w_last3 = 0.50, 0.50
    elif n < 30:
        w_season, w_last3 = 0.30, 0.70
    else:
        w_season, w_last3 = 0.15, 0.85

    if season > 0:
        divergence = abs(last3_avg - season) / season
        if divergence >= 0.20:
            boost = min(divergence - 0.20, 0.15)
            w_last3  = min(w_last3 + boost, 0.90)
            w_season = 1.0 - w_last3

    # Blend last1 into recent component
    if last1 is not None and last1 >= 0.5 and w_last3 > 0:
        w_last1 = w_last3 * 0.25
        w_l3    = w_last3 * 0.75
        recent  = last3_avg * (w_l3 / (w_l3 + w_last1)) + last1 * (w_last1 / (w_l3 + w_last1))
        return round(season * w_season + recent * (w_l3 + w_last1), 1)

    return round(season * w_season + last3_avg * w_last3, 1)


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

    METHODS = [
        "season_avg",
        "last3_median",
        "last5_median",
        "last10_median",
        "ewma",
        "ewma_context",
        "weighted_blend",
    ]

    # Per-player per-method signed errors
    method_errors: dict[str, list[float]] = {m: [] for m in METHODS}

    # Per-player raw predictions for CSV output
    records = []

    for test_idx in range(min_games, len(games)):
        test_gid, test_date = games[test_idx]
        train_gids = [gid for gid, _ in games[:test_idx]]

        actual_box = boxscores.get(test_gid, [])

        # Build per-player training history
        player_history:      dict[str, list[float]] = defaultdict(list)
        player_fouls_hist:   dict[str, list[int]]   = defaultdict(list)
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
                continue

            fouls_hist   = player_fouls_hist.get(name, [])
            margins_hist = player_margins_hist.get(name, [])

            pred_season  = _trimmed_avg(hist)
            pred_last3   = _median(hist[-3:])
            pred_last5   = _median(hist[-5:])  if len(hist) >= 5  else pred_last3
            pred_last10  = _median(hist[-10:]) if len(hist) >= 10 else pred_last5
            pred_ewma    = _ewma(hist)
            ctx          = _context_filter(hist, fouls_hist, margins_hist)
            pred_ewma_ctx = _ewma(ctx) if len(ctx) >= 2 else _trimmed_avg(hist)
            pred_blend   = _weighted_blend(hist)

            preds = {
                "season_avg":    pred_season,
                "last3_median":  pred_last3,
                "last5_median":  pred_last5,
                "last10_median": pred_last10,
                "ewma":          pred_ewma,
                "ewma_context":  pred_ewma_ctx,
                "weighted_blend": pred_blend,
            }

            for method, pred in preds.items():
                method_errors[method].append(pred - actual)

            records.append({
                "game_idx":        test_idx,
                "game_date":       test_date,
                "player":          name,
                "actual":          round(actual, 1),
                "pred_season":     pred_season,
                "pred_last3":      pred_last3,
                "pred_last5":      pred_last5,
                "pred_last10":     pred_last10,
                "pred_ewma":       pred_ewma,
                "pred_ewma_ctx":   pred_ewma_ctx,
                "pred_blend":      pred_blend,
            })

    # Summary
    results = {}
    n_samples = len(method_errors["season_avg"])
    print(
        f"\n{'Method':<16} {'MAE':>6} {'RMSE':>7} {'MedAE':>7} "
        f"{'<=2min':>7} {'<=4min':>7} {'Bias':>7}  (n={n_samples})"
    )
    print("-" * 70)
    for method in METHODS:
        errors = method_errors[method]
        mae   = _mae(errors)
        rmse  = _rmse(errors)
        medae = _medae(errors)
        p2    = _pct_within(errors, 2.0)
        p4    = _pct_within(errors, 4.0)
        bias  = _bias(errors)
        results[method] = {
            "mae": mae, "rmse": rmse, "medae": medae,
            "pct_within_2": p2, "pct_within_4": p4,
            "bias": bias, "n": len(errors),
        }
        print(f"{method:<16} {mae:>6.2f} {rmse:>7.2f} {medae:>7.2f} "
              f"{p2:>6.1f}% {p4:>6.1f}% {bias:>7.2f}")

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

    ALL_METHODS = [
        "season_avg", "last3_median", "last5_median", "last10_median",
        "ewma", "ewma_context", "weighted_blend",
    ]

    if args.all_teams:
        # Accumulate raw signed errors across teams for aggregate stats
        agg_errors: dict[str, list[float]] = {m: [] for m in ALL_METHODS}
        for team in sorted(ESPN_TEAM_IDS.keys()):
            r = run_backtest(team, min_games=args.min_games)
            if not r:
                continue
            for method, stats in r["summary"].items():
                # Reconstruct approximate error list from MAE × n (sign unknown — use MAE proxy)
                agg_errors[method].extend([stats["mae"]] * stats["n"])
        n_total = len(agg_errors["season_avg"])
        print("\n=== AGGREGATE ACROSS ALL TEAMS ===")
        print(
            f"\n{'Method':<16} {'Avg MAE':>8} {'<=2min':>8} {'<=4min':>8}"
        )
        print("-" * 44)
        for method in ALL_METHODS:
            vals = agg_errors[method]
            avg = round(sum(vals) / len(vals), 2) if vals else 0.0
            # Aggregate %within not reconstructable without raw records; show "--"
            print(f"{method:<16} {avg:>8.2f} {'--':>8} {'--':>8}")
        print(f"\n(n≈{n_total} total player-game samples)")
    else:
        result = run_backtest(args.team, min_games=args.min_games)
        if result and args.csv and result.get("records"):
            out_path = Path(args.csv)
            fieldnames = [
                "game_idx", "game_date", "player", "actual",
                "pred_season", "pred_last3", "pred_last5", "pred_last10",
                "pred_ewma", "pred_ewma_ctx", "pred_blend",
            ]
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(result["records"])
            print(f"\nWrote {len(result['records'])} records to {out_path}")


if __name__ == "__main__":
    main()
