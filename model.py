"""
Minutes projection model with injury scenario adjustments.
Core logic: weighted average minutes → injury redistribution → display.
apply_scenario accepts: team_data, player_statuses, duration_map=None, role_overrides=None
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

from roster_data import ROSTERS, POSITION_COMPAT, GAME_MINUTES

# Weight recent games more heavily
WEIGHTS = {
    "season_avg":   0.15,
    "last3_avg":    0.85,
    "rest_factor":  0.00,
    "matchup_adj":  0.00,
}

# Status → minutes scale factor
STATUS_SCALE = {
    "Active":       1.00,
    "Probable":     0.95,   # -5%
    "Questionable": 0.75,   # -25%
    "Doubtful":     0.20,   # 80% reduction
    "Day-To-Day":   0.75,   # legacy — treated like Questionable
    "Out":          0.00,
}

INJURY_COLOR = {
    "Active":       "#28a745",
    "Probable":     "#90ee90",
    "Questionable": "#ffc107",
    "Doubtful":     "#fd7e14",
    "Day-To-Day":   "#ffc107",
    "Out":          "#dc3545",
}


@dataclass
class PlayerProjection:
    name: str
    pos: str
    role: str
    depth: int
    base_min: float          # weighted avg before injury adj
    projected_min: float     # after injury adj
    status: str
    injury: str
    injury_duration: str = "new"
    is_replacement: bool = False
    replaced_player: str = ""
    note: str = ""

    @property
    def status_color(self) -> str:
        return INJURY_COLOR.get(self.status, "#6c757d")

    @property
    def display_status(self) -> str:
        if self.is_replacement:
            return f"Replaces {self.replaced_player}"
        return self.status or "Active"


@dataclass
class TeamLineup:
    team_name: str
    total_minutes: float
    players: list[PlayerProjection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def starters(self) -> list[PlayerProjection]:
        return [p for p in self.players if p.role == "starter" and not p.is_replacement]

    @property
    def bench(self) -> list[PlayerProjection]:
        return [p for p in self.players if p.role == "bench"]

    @property
    def minutes_sum(self) -> float:
        return sum(p.projected_min for p in self.players)

    @property
    def minutes_ok(self) -> bool:
        return abs(self.minutes_sum - GAME_MINUTES) <= 2.0


# ---------------------------------------------------------------------------
# Core projection
# ---------------------------------------------------------------------------

def _fit_ols(season_data: dict) -> tuple[float, float, float] | None:
    """
    Fit OLS: projected_min = β0 + β1*clean_avg + β2*last3_clean_avg
    using every player's per-game history as training samples.

    Returns (β0, β1, β2) or None if fewer than 15 games exist (too noisy).

    We use foul-adjusted averages as inputs so foul-trouble outliers don't
    contaminate the coefficients.
    """
    xs, ys = [], []
    for info in season_data.values():
        if not isinstance(info, dict):
            continue
        gp       = info.get("games_played", 0)
        avg      = info.get("clean_avg_min",  info.get("avg_min", 0.0))
        l3       = info.get("last3_clean_avg", info.get("last3_avg", avg))
        act      = info.get("avg_min", 0.0)   # actual season avg as label proxy
        if gp < 3 or avg < 1.0:
            continue
        xs.append((avg, l3))
        ys.append(act)

    if len(xs) < 8:
        return None

    n   = len(xs)
    sx  = sum(x[0] for x in xs)
    sl  = sum(x[1] for x in xs)
    sy  = sum(ys)
    sxx = sum(x[0] ** 2 for x in xs)
    sll = sum(x[1] ** 2 for x in xs)
    sxy = sum(xs[i][0] * ys[i] for i in range(n))
    sly = sum(xs[i][1] * ys[i] for i in range(n))
    sxl = sum(xs[i][0] * xs[i][1] for i in range(n))

    # Solve 3x3 normal equations via direct inversion (small enough to do inline)
    # [n,   sx,  sl ] [β0]   [sy ]
    # [sx,  sxx, sxl] [β1] = [sxy]
    # [sl,  sxl, sll] [β2]   [sly]
    try:
        import numpy as np
        A = np.array([[n,   sx,  sl ],
                      [sx,  sxx, sxl],
                      [sl,  sxl, sll]], dtype=float)
        b = np.array([sy, sxy, sly], dtype=float)
        beta = np.linalg.solve(A, b)
        b0, b1, b2 = float(beta[0]), float(beta[1]), float(beta[2])
        # Sanity check: coefficients must be positive and sum near 1
        if b1 < 0 or b2 < 0 or b1 + b2 > 2.0:
            return None
        return b0, b1, b2
    except Exception:
        return None


def _weighted_minutes(
    avg_min: float,
    last3_avg: float,
    games_played: int = 0,
    clean_avg: float | None = None,
    last3_clean_avg: float | None = None,
    ols_coeffs: tuple[float, float, float] | None = None,
) -> float:
    """
    Project minutes using OLS when enough season data exists, otherwise
    fall back to weighted blend.

    OLS inputs use foul-adjusted averages (clean_avg, last3_clean_avg) so
    foul-trouble games don't suppress the projection.  If clean versions
    aren't available, raw averages are used.
    """
    ca  = clean_avg      if clean_avg      is not None else avg_min
    l3c = last3_clean_avg if last3_clean_avg is not None else last3_avg

    # OLS path — only when we have fitted coefficients
    if ols_coeffs is not None:
        b0, b1, b2 = ols_coeffs
        return round(b0 + b1 * ca + b2 * l3c, 1)

    # Fallback: weighted blend with adaptive last3 weighting
    if games_played < 3:
        return avg_min * 0.30 + last3_avg * 0.70

    w_season = WEIGHTS["season_avg"]   # 0.15 base
    w_last3  = WEIGHTS["last3_avg"]    # 0.85 base

    if avg_min > 0:
        divergence = abs(last3_avg - avg_min) / avg_min
        if divergence >= 0.20:
            extra_last3 = min(divergence - 0.20, 0.30) * 0.50
            w_last3  = min(w_last3 + extra_last3, 0.95)
            w_season = 1.0 - w_last3

    return (avg_min * w_season + last3_avg * w_last3) / (w_season + w_last3)


def _apply_injury_scale(base_min: float, status: str, duration: str = "new") -> float:
    """Scale minutes based on injury status."""
    if status == "Out":
        return 0.0
    return round(base_min * STATUS_SCALE.get(status, 1.0), 1)


def build_projection(team_data: dict, injury_overrides: dict[str, str] | None = None,
                     duration_map: dict[str, str] | None = None,
                     role_overrides: dict[str, str] | None = None) -> TeamLineup:
    """
    Build a team's projected lineup.

    team_data: {player: {pos, avg_min, last3_avg, clean_avg_min, last3_clean_avg,
                          role, depth, status, injury, games_played, foul_rate}}
    injury_overrides: {player: new_status}  — manual status override from UI
    role_overrides: {player: "starter"|"bench"}  — manual starter/bench swap from UI
    duration_map: ignored, kept for API compatibility
    """
    injury_overrides = injury_overrides or {}
    role_overrides = role_overrides or {}

    ols_coeffs = None  # OLS regresses players toward team mean — disabled in favour of weighted blend

    # Pre-compute role-typical minute targets for blending when role is overridden.
    # "What does a starter on this team typically play?" — median of current starters.
    # "What does a bench player on this team typically play?" — median of current bench.
    # We use team_data roles (before overrides) as the baseline population.
    def _median_min(vals: list[float]) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        mid = len(s) // 2
        return (s[mid - 1] + s[mid]) / 2 if len(s) % 2 == 0 else s[mid]

    starter_mins = [
        v.get("avg_min", 0.0) for v in team_data.values()
        if isinstance(v, dict) and v.get("role") == "starter"
        and v.get("avg_min", 0.0) > 5.0
        and v.get("status", "Active") not in ("Out", "Doubtful")
    ]
    bench_mins = [
        v.get("avg_min", 0.0) for v in team_data.values()
        if isinstance(v, dict) and v.get("role") == "bench"
        and v.get("avg_min", 0.0) > 3.0
        and v.get("status", "Active") not in ("Out", "Doubtful")
    ]
    typical_starter_min = _median_min(starter_mins) or 28.0
    typical_bench_min   = _median_min(bench_mins)   or 14.0

    projections: list[PlayerProjection] = []
    out_players: list[tuple[str, dict]] = []

    for player, info in team_data.items():
        if not isinstance(info, dict):
            continue
        status = injury_overrides.get(player, info.get("status", "Active"))
        gp = info.get("games_played", 0)
        avg_min = info.get("avg_min", 10.0)
        base_min = _weighted_minutes(
            avg_min,
            info.get("last3_avg", avg_min),
            gp,
            clean_avg=info.get("clean_avg_min"),
            last3_clean_avg=info.get("last3_clean_avg"),
            ols_coeffs=ols_coeffs,
        )
        if gp == 0:
            base_min = min(base_min, 3.0)   # no games played — cap at 3 min regardless of default
        elif gp <= 2:
            base_min = min(base_min, 18.0) if info.get("role") == "bench" else base_min

        role = role_overrides.get(player, info.get("role", "bench"))
        depth = info.get("depth", 2)
        if player in role_overrides:
            depth = 1 if role == "starter" else 2
            orig_role = info.get("role", "bench")
            if role != orig_role:
                # Blend personal history 40% toward the typical minutes for the new role.
                # This moves Pouye from ~14 toward starter range without fully discarding
                # her history — a 40/60 blend captures "role upgrade" realistically.
                target = typical_starter_min if role == "starter" else typical_bench_min
                base_min = round(base_min * 0.60 + target * 0.40, 1)

        proj_min = _apply_injury_scale(base_min, status)

        p = PlayerProjection(
            name=player,
            pos=info.get("pos", "?"),
            role=role,
            depth=depth,
            base_min=round(base_min, 1),
            projected_min=proj_min,
            status=status,
            injury=info.get("injury", ""),
        )
        projections.append(p)
        if proj_min == 0.0 and status in ("Out", "Doubtful"):
            out_players.append((player, info))

    # --- Starter slot promotion ---
    # If out-players reduce active starters below 5, promote the best available
    # bench players into starter slots so there are always 5 starters projected.
    # "Best available" = highest projected minutes among bench players at a
    # position that covers one of the vacated starter positions.
    active_starters = [p for p in projections if p.role == "starter" and p.projected_min > 0]
    starters_needed = 5 - len(active_starters)
    if starters_needed > 0:
        # Collect positions still missing a starter
        out_names = {name for name, _ in out_players}
        out_positions = [info["pos"] for name, info in out_players
                         if next((p for p in projections
                                  if p.name == name and p.projected_min == 0), None)]
        bench_active = sorted(
            [p for p in projections if p.role == "bench" and p.projected_min > 0],
            key=lambda p: -p.projected_min
        )
        promoted = 0
        for candidate in bench_active:
            if promoted >= starters_needed:
                break
            # Check if this player covers a vacated position
            candidate_compat = POSITION_COMPAT.get(candidate.pos, [candidate.pos])
            covers_gap = any(
                op in candidate_compat or candidate.pos == op
                for op in out_positions
            ) if out_positions else True
            if covers_gap or promoted < starters_needed:
                candidate.role = "starter"
                candidate.depth = 1
                candidate.note = (candidate.note + " (starting)" if candidate.note
                                  else "Starting (injury fill-in)")
                promoted += 1
                # Remove covered position so next promotion targets a different gap
                for op in list(out_positions):
                    if op in candidate_compat or candidate.pos == op:
                        out_positions.remove(op)
                        break

    # Redistribute vacated minutes proportionally across all active players
    if out_players:
        projections = _redistribute_minutes(projections, out_players, team_data)

    # Normalize to exactly GAME_MINUTES (handles rounding only)
    projections = _normalize_to_total(projections, GAME_MINUTES)

    # Sort: starters first by projected min, then bench by projected min, Out last.
    # Bench sorted by projected_min so a hot bench player naturally floats above
    # a veteran who's been getting fewer minutes recently.
    projections.sort(key=lambda p: (
        2 if p.projected_min == 0 else (0 if p.role == "starter" else 1),
        -p.projected_min,
    ))

    warnings = _check_lineup(projections)
    total = sum(p.projected_min for p in projections)
    return TeamLineup(team_name="", total_minutes=total, players=projections, warnings=warnings)


def _redistribute_minutes(
    projections: list[PlayerProjection],
    out_players: list[tuple[str, dict]],
    team_data: dict,
) -> list[PlayerProjection]:
    """
    Redistribute all vacated minutes in a single pass to prevent compounding.

    With multiple players out (e.g. 4 starters), iterating one-by-one causes
    the same replacement to absorb 60% from each iteration and balloon past 40
    minutes. Instead:
      1. Sum all vacated minutes into one pool.
      2. Distribute pool across active players weighted by their current
         projected minutes — higher-minute players (starters) naturally absorb
         more. No single player gets a disproportionate bonus.
      3. Cap each player at 38 min after distribution.
      4. Mark the best positional replacement for each out player (note only).
    """
    proj_map = {p.name: p for p in projections}
    active   = [p for p in projections if p.projected_min > 0]

    if not active or not out_players:
        return projections

    # Step 1: total pool of vacated minutes
    total_vacated = sum(proj_map[name].base_min for name, _ in out_players
                        if name in proj_map)

    if total_vacated <= 0:
        return projections

    # Step 2: distribute proportionally by current projected minutes.
    # Starters cap at 36 during redistribution so bench players keep a viable budget.
    # After redistribution, _normalize_to_total handles final rounding to 200.
    REDIST_STARTER_CAP = 36.0
    total_active_min = sum(p.projected_min for p in active)
    if total_active_min > 0:
        for p in active:
            share = (p.projected_min / total_active_min) * total_vacated
            cap = REDIST_STARTER_CAP if p.role == "starter" else 38.0
            p.projected_min = round(min(p.projected_min + share, cap), 1)

    # Step 3: mark positional replacement notes (informational only)
    for out_name, out_info in out_players:
        replacement = _find_replacement(
            out_name, out_info["pos"], out_info["depth"], active, team_data
        )
        if replacement:
            if not proj_map[replacement.name].note:
                proj_map[replacement.name].note = f"Covers {out_name}"
        else:
            suggestion = _suggest_replacement(out_name, out_info["pos"], team_data, proj_map)
            if suggestion and suggestion in proj_map and not proj_map[suggestion].note:
                proj_map[suggestion].note = f"Suggested to cover {out_name}"
                proj_map[suggestion].is_replacement = True
                proj_map[suggestion].replaced_player = out_name

    return list(proj_map.values())


def _find_replacement(
    out_name: str,
    out_pos: str,
    out_depth: int,
    active: list[PlayerProjection],
    team_data: dict,
) -> PlayerProjection | None:
    """
    Find the best active player to absorb vacated minutes.

    Sorting priority (lower = better):
      0. starter_pct bucket: players who start 40%+ of games rank ahead of pure bench
      1. depth proximity: next depth level preferred
      2. projected minutes: higher is better (more established role)

    This ensures Sophie Cunningham (starter_pct ~0.8) ranks above Raven Johnson
    (starter_pct ~0.0) when Caitlin Clark goes out, even if their depth values
    are similar.
    """
    compat_positions = POSITION_COMPAT.get(out_pos, [out_pos])
    candidates = [
        p for p in active
        if p.pos in compat_positions
        and p.name != out_name
        and p.projected_min > 0
    ]
    if not candidates:
        return None

    def _sort_key(p: PlayerProjection):
        info = team_data.get(p.name, {})
        start_pct = info.get("starter_pct", 0.0)
        # Bucket: 0 = has meaningful starting history (≥40%), 1 = bench-only
        starter_bucket = 0 if start_pct >= 0.40 else 1
        depth_dist = abs(p.depth - (out_depth + 1))
        return (starter_bucket, depth_dist, -p.projected_min)

    candidates.sort(key=_sort_key)
    return candidates[0]


def _suggest_replacement(
    out_name: str,
    out_pos: str,
    team_data: dict,
    proj_map: dict,
) -> str | None:
    """Suggest a player name to replace out_name even if not currently active."""
    compat_positions = POSITION_COMPAT.get(out_pos, [out_pos])
    candidates = [
        (name, info) for name, info in team_data.items()
        if isinstance(info, dict) and info.get("pos") in compat_positions and name != out_name
    ]
    # Same priority as _find_replacement: starter history first, then depth, then minutes
    candidates.sort(key=lambda x: (
        0 if x[1].get("starter_pct", 0.0) >= 0.40 else 1,
        x[1].get("depth", 2),
        -x[1].get("avg_min", 0.0),
    ))
    for name, _ in candidates:
        if name in proj_map:
            return name
    return None


STARTER_MAX = 38.0


BENCH_FLOOR = 4.0   # minimum projected minutes for any active bench player

def _normalize_to_total(projections: list[PlayerProjection], target: float) -> list[PlayerProjection]:
    """
    Scale all active players so they sum to exactly target (200 min).

    This runs after _redistribute_minutes and handles both large gaps (starters
    over-inflated) and small rounding errors.

    Strategy when over budget (need to trim):
      1. Trim starters proportionally down toward 36 min each.
      2. If still over, trim bench proportionally down to BENCH_FLOOR.
    Strategy when under budget (need to add):
      Spread proportionally across all active players.

    Caps: 38 min per starter, 38 min per bench player.
    Floor: BENCH_FLOOR for bench, 10 min for starters (extreme edge case only).
    """
    active = [p for p in projections if p.projected_min > 0]
    if not active:
        return projections

    current_total = sum(p.projected_min for p in active)
    if abs(current_total - target) < 0.2:
        return projections

    diff = target - current_total
    starters = [p for p in active if p.role == "starter"]
    bench    = [p for p in active if p.role != "starter"]

    if diff < 0:
        # Phase 1: trim starters proportionally toward 36
        starter_excess = sum(max(p.projected_min - 36.0, 0.0) for p in starters)
        if starter_excess > 0:
            to_trim = min(starter_excess, -diff)
            for p in starters:
                excess = max(p.projected_min - 36.0, 0.0)
                p.projected_min = round(p.projected_min - (excess / starter_excess) * to_trim, 1)

        # Phase 2: trim all proportionally down to floors
        to_trim = sum(p.projected_min for p in active) - target
        if to_trim > 0.1:
            total_trimmable = sum(
                max(p.projected_min - (BENCH_FLOOR if p.role != "starter" else 10.0), 0.0)
                for p in active
            )
            if total_trimmable > 0:
                for p in active:
                    floor = BENCH_FLOOR if p.role != "starter" else 10.0
                    trimmable = max(p.projected_min - floor, 0.0)
                    p.projected_min = round(
                        p.projected_min - (trimmable / total_trimmable) * to_trim, 1
                    )

        # Phase 3: guaranteed hard scale — always reaches exactly target
        current = sum(p.projected_min for p in active)
        if current > 0 and abs(current - target) > 0.05:
            scale = target / current
            for p in active:
                p.projected_min = round(p.projected_min * scale, 1)

    else:
        # Under budget — add proportionally, respecting 38 cap
        total = sum(p.projected_min for p in active)
        if total > 0:
            for p in active:
                share = (p.projected_min / total) * diff
                p.projected_min = round(min(p.projected_min + share, STARTER_MAX), 1)

    # Fix any rounding drift — adjust the highest-minute player by the remainder
    current = sum(p.projected_min for p in active)
    drift = round(target - current, 1)
    if drift != 0.0 and active:
        largest = max(active, key=lambda p: p.projected_min)
        largest.projected_min = round(largest.projected_min + drift, 1)

    return projections


def _check_lineup(projections: list[PlayerProjection]) -> list[str]:
    warnings = []
    total = sum(p.projected_min for p in projections)
    if abs(total - GAME_MINUTES) > 2:
        warnings.append(f"Total minutes ({total:.1f}) deviates from {GAME_MINUTES}. Check lineup.")
    players_over_38 = [p for p in projections if p.projected_min > 38]
    for p in players_over_38:
        warnings.append(f"{p.name} projected {p.projected_min} min — unusually high for WNBA.")
    return warnings


# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------

StatusType = Literal["Active", "Probable", "Questionable", "Doubtful", "Day-To-Day", "Out"]


def apply_scenario(
    team_data: dict,
    player_statuses: dict[str, StatusType],
    duration_map: dict[str, str] | None = None,
    role_overrides: dict[str, str] | None = None,
) -> TeamLineup:
    """
    High-level entry point. Accepts status and role overrides and returns a fully adjusted lineup.
    """
    return build_projection(team_data, injury_overrides=player_statuses, role_overrides=role_overrides)


def get_status_options() -> list[str]:
    return ["Active", "Probable", "Questionable", "Doubtful", "Out"]


def get_duration_options() -> list[str]:
    return ["light", "medium", "extended"]


def minutes_delta_summary(base: TeamLineup, adjusted: TeamLineup) -> dict[str, float]:
    """Returns {player: delta_minutes} comparing two lineups."""
    base_map = {p.name: p.projected_min for p in base.players}
    adj_map = {p.name: p.projected_min for p in adjusted.players}
    deltas = {}
    for name in set(list(base_map.keys()) + list(adj_map.keys())):
        delta = adj_map.get(name, 0) - base_map.get(name, 0)
        if abs(delta) >= 0.5:
            deltas[name] = round(delta, 1)
    return deltas
