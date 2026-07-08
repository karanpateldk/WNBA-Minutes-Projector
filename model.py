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

# Status → minutes scale factor.
# All non-Out statuses project full base minutes — injury status affects color
# and stat share only, not minutes. If a player suits up, she plays her normal
# role. Minutes reduction only applies when injury text explicitly signals a
# restriction (detected by _has_minutes_restriction).
STATUS_SCALE = {
    "Active":       1.00,
    "Probable":     1.00,
    "Questionable": 1.00,
    "Doubtful":     0.00,   # treat as DNP — minutes redistributed to teammates
    "Day-To-Day":   1.00,
    "Out":          0.00,
}

_RESTRICTION_KEYWORDS = (
    "minutes restriction", "minute restriction", "minutes limit",
    "minute limit", "load management", "limited minutes",
    "on a minutes", "minutes cap", "restricted minutes",
)

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
    confidence: int = 0
    reasons: list = field(default_factory=list)

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
    last1_min: float | None = None,
) -> float:
    """
    Sample-size-aware blend of season average and recent games.

    Backtesting shows season_avg outperforms recency-heavy blends early in
    the season (<20 games). Weights shift toward recent as sample grows:

      <5 games:  100% season avg (no recent signal yet)
      5-10:       70% season / 30% last3
      10-20:      50% season / 50% last3
      20-30:      30% season / 70% last3
      30+:        15% season / 85% last3

    When last3 diverges >=20% from season avg (role change / injury return),
    last3 weight gets an additional boost of up to 15%.

    When last1 (most recent game) is available, it gets a 15% slice carved
    from last3 weight — single-game signal is strong for starters in stable
    rotations.
    """
    ca  = clean_avg       if clean_avg       is not None else avg_min
    l3c = last3_clean_avg if last3_clean_avg is not None else last3_avg

    if ols_coeffs is not None:
        b0, b1, b2 = ols_coeffs
        return round(b0 + b1 * ca + b2 * l3c, 1)

    if games_played < 3:
        return round(ca * 0.80 + l3c * 0.20, 1)

    # Sample-size-aware base weights — backtested optimal across all 15 teams.
    # Slightly more season weight than prior version: divergence boost handles
    # mid-season role changes without raising the baseline recent weight.
    if games_played < 5:
        w_season, w_last3 = 1.00, 0.00
    elif games_played < 10:
        w_season, w_last3 = 0.70, 0.30
    elif games_played < 20:
        w_season, w_last3 = 0.55, 0.45
    elif games_played < 30:
        w_season, w_last3 = 0.40, 0.60
    else:
        w_season, w_last3 = 0.25, 0.75

    # Boost last3 weight when recent trend diverges significantly from season
    if ca > 0:
        divergence = abs(l3c - ca) / ca
        if divergence >= 0.20:
            boost = min(divergence - 0.20, 0.15)
            w_last3  = min(w_last3 + boost, 0.90)
            w_season = 1.0 - w_last3

    # Blend last1 into the recent component if available
    if last1_min is not None and w_last3 > 0:
        w_last1 = w_last3 * 0.40           # backtested optimal: 0.40 beats 0.25/0.50
        w_last3 = w_last3 * 0.60
        recent  = l3c * (w_last3 / (w_last3 + w_last1)) + last1_min * (w_last1 / (w_last3 + w_last1))
        return round(ca * w_season + recent * (w_last3 + w_last1), 1)

    return round(ca * w_season + l3c * w_last3, 1)


def _has_minutes_restriction(injury_text: str) -> bool:
    """Return True if injury note explicitly mentions a minutes limit/restriction."""
    text = (injury_text or "").lower()
    return any(kw in text for kw in _RESTRICTION_KEYWORDS)


def _apply_injury_scale(base_min: float, status: str, injury_text: str = "",
                        duration: str = "new") -> float:
    """Scale minutes based on injury status.

    For Questionable/Probable/Day-To-Day: only reduce minutes if the injury note
    explicitly mentions a minutes restriction. Otherwise they play normal minutes
    when active — play_prob already captures the uncertainty of whether they play.
    """
    if status == "Out":
        return 0.0
    scale = STATUS_SCALE.get(status, 1.0)
    # If a minutes restriction is explicitly noted, fall back to the old haircut
    # so the projection reflects genuinely reduced role while playing.
    if scale == 1.00 and _has_minutes_restriction(injury_text):
        scale = 0.75
    return round(base_min * scale, 1)


def _confidence_score(gp: int, avg_min: float, last3_range: float,
                      status: str, start_pct: float,
                      plus_minus: float | None = None) -> int:
    """
    Confidence in tonight's projection. Calibrated so HIGH (~green) only fires
    when there are strong signals the minutes will be predictable:
      - Established role (clear starter or clear bench)
      - Consistent recent usage (low last3_range)
      - Healthy status
      - Sufficient sample size (10+ games)

    Thresholds: HIGH >= 70, MED 45-69, LOW < 45
    """
    # Base starts low — player must earn confidence through signals
    score = 30

    # Sample size — meaningful only after 10+ games
    if gp >= 20:
        score += 20
    elif gp >= 10:
        score += 12
    elif gp >= 5:
        score += 5
    # <5 games: no bonus — projection is essentially a guess

    # Role clarity — clear starter or clear bench is predictable
    if start_pct >= 0.85 or start_pct <= 0.10:
        score += 18   # locked role
    elif start_pct >= 0.70 or start_pct <= 0.25:
        score += 10   # mostly consistent
    elif 0.40 <= start_pct <= 0.60:
        score -= 10   # swing player — hard to predict

    # Minute consistency — low variance in recent games is the strongest signal
    if last3_range < 3:
        score += 15   # very stable
    elif last3_range < 6:
        score += 8
    elif last3_range > 10:
        score -= 10
    elif last3_range > 15:
        score -= 20   # highly volatile

    # Injury status penalties
    penalties = {
        "Probable":     -8,
        "Questionable": -20,
        "Day-To-Day":   -20,
        "Doubtful":     -35,
    }
    score += penalties.get(status, 0)

    # Low usage bench players are harder to project
    if avg_min < 8:
        score -= 12
    elif avg_min < 12:
        score -= 5

    # Plus/minus: coach trust signal
    if plus_minus is not None:
        if plus_minus >= 6:
            score += 6
        elif plus_minus >= 3:
            score += 3
        elif plus_minus <= -3:
            score -= 3
        elif plus_minus <= -6:
            score -= 6

    return max(0, min(100, score))


def _reason_codes(role: str, ewma_min: float, avg_min: float, status: str,
                  is_replacement: bool, gp: int, last3_range: float,
                  role_changed: bool = False) -> list:
    reasons = []
    if role == "starter":
        reasons.append("Projected starter")
    if role_changed:
        reasons.append("Role change detected")
    if avg_min > 0 and ewma_min > avg_min * 1.10:
        reasons.append("Trending up")
    elif avg_min > 0 and ewma_min < avg_min * 0.90:
        reasons.append("Trending down")
    if status not in ("Active", "Probable"):
        reasons.append("Injury adjustment")
    if is_replacement:
        reasons.append("Beneficiary of absence")
    if gp < 5:
        reasons.append("Limited sample")
    if last3_range >= 14:
        reasons.append("Volatile minutes")
    return reasons


def _apply_pace_adjustment(projections: list, pace_factor: float) -> list:
    """
    Scale bench minutes based on opponent pace relative to league average.
    Faster pace (>163 possessions) = more subs = bench gets slightly more.
    Slower pace (<163 possessions) = starters stay in = bench gets less.
    Only applied to bench players; starters are less affected by pace.
    Capped at ±1.5 min per player so it doesn't override individual history.
    """
    LEAGUE_AVG_PACE = 163.0
    if abs(pace_factor - LEAGUE_AVG_PACE) < 2:
        return projections  # negligible difference

    # Each 10 possessions above/below average = ~0.5 min for bench players
    raw_adj = (pace_factor - LEAGUE_AVG_PACE) / 10.0 * 0.5
    adj = max(-1.5, min(1.5, raw_adj))

    for p in projections:
        if p.role == "bench" and p.projected_min > 0:
            p.projected_min = round(max(1.0, p.projected_min + adj), 1)
    return projections


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
    # Use Snowflake-derived team role averages as anchors when available.
    # These are actual observed averages from game logs this season, not guesses.
    # Fall back to in-season median if Snowflake hasn't populated them yet.
    sf_starter_avg = next(
        (v.get("role_avg_starter", 0.0) for v in team_data.values()
         if isinstance(v, dict) and v.get("role_avg_starter")), 0.0
    )
    sf_bench_avg = next(
        (v.get("role_avg_bench", 0.0) for v in team_data.values()
         if isinstance(v, dict) and v.get("role_avg_bench")), 0.0
    )
    typical_starter_min = sf_starter_avg or _median_min(starter_mins) or 27.0
    typical_bench_min   = sf_bench_avg   or _median_min(bench_mins)   or 13.0

    projections: list[PlayerProjection] = []
    out_players: list[tuple[str, dict]] = []

    for player, info in team_data.items():
        if not isinstance(info, dict):
            continue
        status = injury_overrides.get(player, info.get("status", "Active"))
        gp = info.get("games_played", 0)
        avg_min  = info.get("avg_min", 10.0)
        ewma_min = info.get("ewma_min", avg_min)

        # Season anchor: use raw avg_min (includes foul-out games) so the season
        # component isn't inflated by excluding bad games.
        # Last3 anchor: use clean avg (excludes foul-out games) since a single
        # foul-out in a 3-game window has outsized impact.
        clean_avg   = avg_min   # raw season average as anchor
        last3_clean = info.get("last3_clean_avg") or info.get("last3_avg", avg_min)

        last1 = info.get("last_game_min") or None
        if last1 is not None and last1 < 0.5:
            last1 = None  # DNP last game — don't use as signal

        # Suppress last1 if the player fouled out last game (foul rate signals curtailed mins).
        # The clean_avg and last3_clean already exclude foul-trouble games — last1 should too.
        _last1_fouls = info.get("last_game_fouls", 0) or 0
        if last1 is not None and _last1_fouls >= 5:
            last1 = None

        # For situational/low-minute players (avg < 18), suppress last1 when it is
        # more than 2x their season average AND their last3 (excluding last1) is also
        # low — meaning the high game is a one-off, not a genuine role expansion.
        # If last3_clean is already elevated (player trending up), keep last1.
        if (last1 is not None and avg_min < 18 and avg_min > 0
                and last1 > avg_min * 2.0
                and last3_clean < avg_min * 1.5   # last3 still near normal range
                and player not in injury_overrides):
            last1 = None

        base_min = _weighted_minutes(
            clean_avg,
            last3_clean,
            gp,
            clean_avg=clean_avg,
            last3_clean_avg=last3_clean,
            ols_coeffs=ols_coeffs,
            last1_min=last1,
        )
        if gp == 0:
            base_min = min(base_min, 3.0)
        elif gp <= 2:
            base_min = min(base_min, 18.0) if info.get("role") == "bench" else base_min

        # DNP-rate adjustment: scale base_min by the fraction of games the player
        # actually suited up. A player who DNPs 60% of games has an expected
        # contribution of 40% of their per-game average — their projection when
        # they do play is correct, but for lineup purposes we need the expected value.
        # Only applied to bench players with meaningful DNP rate (>= 40%) to avoid
        # touching consistent starters who occasionally rest.
        dnp_rate = info.get("dnp_rate", 0.0) or 0.0
        _is_starter = info.get("starter_pct", 0.0) >= 0.50
        # DNP adjustment for bench players only — starters with high DNP rates
        # are typically injured and should be set Out manually by the user.
        # A bench player DNPing 40%+ of games is a spot-use player, not rotation.
        if dnp_rate >= 0.40 and not _is_starter and player not in injury_overrides:
            base_min = round(base_min * (1.0 - dnp_rate), 1)

        role = role_overrides.get(player, info.get("role", "bench"))
        depth = info.get("depth", 2)
        orig_role = info.get("role", "bench")

        # Auto-detect role change: if recent starter_pct diverges significantly
        # from the season starter_pct it signals a mid-season role shift.
        recent_sp = info.get("recent_starter_pct", info.get("starter_pct", 0.5))
        season_sp = info.get("starter_pct", 0.5)
        auto_role_changed = (
            player not in role_overrides
            and gp >= 4
            and abs(recent_sp - season_sp) >= 0.40
        )

        role_changed = (player in role_overrides and role != orig_role) or auto_role_changed
        if role_changed and auto_role_changed:
            target = typical_starter_min if role == "starter" else typical_bench_min
            # Only blend toward role average when it pulls in the right direction.
            # If the player's personal projection already exceeds the team bench avg
            # (e.g. Marine Johannes at 20 min vs team bench avg 13), blending toward
            # the lower role average wrongly suppresses a high-usage player.
            # Skip the blend when the target would pull base_min DOWN for a bench
            # player — their actual minutes are above the team average by design.
            if not (role == "bench" and target < base_min):
                base_min = round(base_min * 0.60 + target * 0.40, 1)

        if player in role_overrides:
            depth = 1 if role == "starter" else 2
            if role != orig_role:
                target = typical_starter_min if role == "starter" else typical_bench_min
                base_min = round(base_min * 0.60 + target * 0.40, 1)

        # Soft anchor toward team's observed role average (from Snowflake game logs).
        # Only applied when:
        #   - Role average is meaningful (>5 min, derived from actual games)
        #   - Player has a stable role (starter_pct clearly above or below 50%)
        #   - Player has enough history (5+ games)
        # Blend is light (15%) so it corrects systemic drift without overriding
        # individual player history. Players with unusual minutes keep their signal.
        if gp >= 5 and not role_changed:
            sp = info.get("starter_pct", 0.5)
            role_anchor = (
                typical_starter_min if role == "starter" and sp >= 0.60 and typical_starter_min > 5
                else typical_bench_min if role == "bench" and sp <= 0.40 and typical_bench_min > 5
                else None
            )
            if role_anchor is not None:
                base_min = round(base_min * 0.85 + role_anchor * 0.15, 1)

        # Garbage-time adjustment: bench players who only play in blowouts should
        # be projected at their close-game average, not their season average.
        # Signal: crunch_time_poss < 15 (rarely trusted in tight games) AND
        #         blowout_dependent = True (minutes drop 3+ min in close games).
        # We blend 70% toward avg_min_close so the projection reflects what they
        # actually do in competitive games rather than garbage-time inflation.
        if (role == "bench"
                and info.get("blowout_dependent")
                and (info.get("crunch_time_poss") or 999) < 15
                and gp >= 4):
            close_avg = info.get("avg_min_close", 0.0) or 0.0
            if 0 < close_avg < base_min:
                base_min = round(base_min * 0.30 + close_avg * 0.70, 1)

        # Bias corrections (+0.72 starters / -0.87 bench) removed — these were
        # calibrated on ESPN data which had systematic under/over-projection.
        # Snowflake boxscore data is accurate so the corrections now overshoot.

        proj_min = _apply_injury_scale(base_min, status, info.get("injury", ""))

        conf = _confidence_score(
            gp, avg_min,
            info.get("last3_range", 0.0) or 0.0,
            status,
            info.get("starter_pct", 0.0),
            plus_minus=info.get("plus_minus"),
        )
        reasons = _reason_codes(
            role, ewma_min, avg_min, status,
            False,  # is_replacement set later
            gp, info.get("last3_range", 0.0) or 0.0,
            role_changed=role_changed,
        )

        # Status badge handles the visual — no note needed from the model.
        inj_note = ""

        p = PlayerProjection(
            name=player,
            pos=info.get("pos", "?"),
            role=role,
            depth=depth,
            base_min=round(base_min, 1),
            projected_min=proj_min,
            status=status,
            injury=info.get("injury", ""),
            note=inj_note,
            confidence=conf,
            reasons=reasons,
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
        out_positions = [info.get("pos", "?") for name, info in out_players
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

    # --- Rotation depth cap ---
    # Zero out fringe bench players who are below meaningful rotation minutes.
    # Uses two signals:
    #   1. last3_avg < 8 min — genuinely garbage-time / end-of-bench role
    #   2. rank beyond rotation_depth slots AND last3 < bench median — clearly not in rotation
    # Players manually set via injury_overrides are always kept in.
    rotation_depth = (
        next((v.get("rotation_depth", 0) for v in team_data.values()
              if isinstance(v, dict) and v.get("rotation_depth")), 0)
        or team_data.get("rotation_depth", 0)
        or 9  # WNBA default
    )

    bench_slots = max(rotation_depth - 5, 3)
    # Score each bench player: last3_avg penalised by games missed so absent players
    # rank below active ones even if their last3 (when healthy) was high.
    def _bench_score(p):
        pinfo = team_data.get(p.name, {})
        last3  = pinfo.get("last3_avg") or p.projected_min
        missed = pinfo.get("games_missed_streak", 0)
        return last3 * max(0.0, 1.0 - missed * 0.15)   # -15% per missed game

    active_bench = sorted(
        [p for p in projections if p.role == "bench" and p.projected_min > 0],
        key=lambda p: -_bench_score(p),
    )
    for i, p in enumerate(active_bench):
        if p.name in injury_overrides:
            continue
        pinfo = team_data.get(p.name, {})
        last3  = pinfo.get("last3_avg") or p.projected_min
        missed = pinfo.get("games_missed_streak", 0)
        # Zero if:
        # 1. Garbage-time player (avg < 6 min when active)
        # 2. Beyond slot cap by recency-penalized score
        # 3. True DNP candidate: DNP rate > 70% AND missed 2+ straight games
        #    (catches habitual non-dressers who are on the roster but rarely play,
        #    while preserving returning players like Vandersloot who have a real role)
        avg    = pinfo.get("avg_min", last3)
        dnp    = pinfo.get("dnp_rate", 0.0) or 0.0
        if avg < 6.0 or i >= bench_slots or (dnp > 0.70 and missed >= 2):
            p.projected_min = 0.0

    # Redistribute vacated minutes proportionally across all active players
    if out_players:
        projections = _redistribute_minutes(projections, out_players, team_data, injury_overrides)

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
    injury_overrides: dict | None = None,
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

    # Step 1: total pool of vacated minutes.
    # For players manually set Out via injury_overrides, use their avg_min
    # (what they actually play when active) rather than their DNP-discounted
    # base_min. A player averaging 18 min who is set Out vacates ~18 min,
    # not 12 min after DNP rate adjustment.
    _overrides = injury_overrides or {}
    def _vacated_mins(name, info):
        p = proj_map.get(name)
        if p is None:
            return 0.0
        # For manually-overridden Out players, use their actual playing average
        # (not DNP-discounted base). User explicitly says they won't play tonight
        # so their full expected minutes open up for redistribution.
        # For auto-Out players (injury report), use base_min which already
        # reflects their expected value including DNP probability.
        if name in _overrides:
            pdata = team_data.get(name, {})
            l3c = pdata.get("last3_clean_avg") or pdata.get("last3_avg") or 0
            avg = pdata.get("avg_min") or 0
            return max(l3c, avg, p.base_min)
        return p.base_min

    total_vacated = sum(_vacated_mins(name, info) for name, info in out_players)

    # Fetch Snowflake "without player" averages for ALL Out players (manual or auto).
    # These give the actual observed rotation when this player doesn't play.
    _without_targets: dict[str, float] = {}
    try:
        import snowflake_connector as _sf
        if _sf.is_available():
            _team_name = team_data.get("__team_name__", "")
            if _team_name:
                for out_name, _ in out_players:
                    wo = _sf.get_minutes_without_player(_team_name, out_name)
                    if wo:
                        _without_targets.update(wo)
    except Exception:
        pass

    if total_vacated <= 0:
        return projections

    # Step 2: distribute vacated minutes.
    # When Snowflake "without player" averages are available, use them as direct
    # base_min targets (blend 70% toward observed, 30% current projection).
    # This directly answers "what does this rotation look like without player X"
    # rather than trying to proportionally distribute vacated minutes.
    REDIST_STARTER_CAP = 36.0
    total_active_min = sum(p.projected_min for p in active)
    if total_active_min > 0:
        if _without_targets:
            # Use Snowflake without-player averages as direct targets.
            # These are the actual observed minutes when this player didn't play —
            # they already sum to ~200 so just set them directly and let
            # normalization handle minor rounding only.
            # Blend only slightly with current projection to anchor to recent form.
            wo_sum = sum(_without_targets.get(p.name, 0) for p in active)
            if wo_sum > 50:  # only use if we have meaningful coverage
                for p in active:
                    wo_target = _without_targets.get(p.name, 0)
                    if wo_target > 0:
                        # 60% observed without-player avg, 40% current base projection
                        p.projected_min = round(
                            wo_target * 0.60 + p.projected_min * 0.40, 1
                        )
                    # players not in without-targets keep their current projection
            else:
                # Insufficient without-player data — proportional fallback
                for p in active:
                    share = (p.projected_min / total_active_min) * total_vacated
                    cap = REDIST_STARTER_CAP if p.role == "starter" else 38.0
                    p.projected_min = round(min(p.projected_min + share, cap), 1)
        else:
            for p in active:
                share = (p.projected_min / total_active_min) * total_vacated
                cap = REDIST_STARTER_CAP if p.role == "starter" else 38.0
                p.projected_min = round(min(p.projected_min + share, cap), 1)

    # Step 3: mark positional replacement notes for meaningful absences only.
    # Only set coverage notes when the out player has a significant base_min
    # (>= 12 min) — fringe/bench players being zeroed don't need coverage notes
    # as it clutters the UI without adding useful information.
    for out_name, out_info in out_players:
        out_base = proj_map.get(out_name, None)
        out_base_min = out_base.base_min if out_base else 0
        if out_base_min < 12.0:
            continue  # fringe player — skip coverage note
        out_pos   = out_info.get("pos", "?")
        out_depth = out_info.get("depth", 2)
        replacement = _find_replacement(out_name, out_pos, out_depth, active, team_data)
        if replacement:
            if not proj_map[replacement.name].note:
                proj_map[replacement.name].note = f"Covers {out_name}"
        else:
            suggestion = _suggest_replacement(out_name, out_pos, team_data, proj_map)
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

    Strategy when over budget:
      1. Trim starters proportionally toward 36
      2. Trim all proportionally down to floors
    Strategy when under budget:
      Spread proportionally across all active players.
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
        # Trim proportionally based on how much each player is projected ABOVE
        # their own season average. Players furthest above their avg give back
        # the most — role-agnostic so a 30-min starter over-projected by 6 min
        # gives back more than a 15-min bench player over-projected by 1 min.
        # Floor: each player keeps at least 85% of their season avg (base_min),
        # with an absolute minimum of BENCH_FLOOR for low-minute players.
        needed = -diff
        floors = {p.name: max(p.base_min * 0.85, BENCH_FLOOR) for p in active}
        trimmable_total = sum(max(p.projected_min - floors[p.name], 0.0) for p in active)

        if trimmable_total > 0:
            to_trim = min(trimmable_total, needed)
            for p in active:
                trimmable = max(p.projected_min - floors[p.name], 0.0)
                p.projected_min = round(
                    p.projected_min - (trimmable / trimmable_total) * to_trim, 1
                )
            needed = max(0.0, sum(p.projected_min for p in active) - target)

        # If still over after proportional trim, scale everyone down uniformly
        if needed > 0.1:
            current = sum(p.projected_min for p in active)
            if current > 0:
                scale = target / current
                for p in active:
                    p.projected_min = round(p.projected_min * scale, 1)

    else:
        # Under budget — spread proportionally to all active players
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
    opp_pace: float = 0.0,
) -> TeamLineup:
    """
    High-level entry point. Accepts status and role overrides and returns a fully adjusted lineup.
    opp_pace: opponent's avg possessions per game — applies pace adjustment to bench.
    """
    lineup = build_projection(team_data, injury_overrides=player_statuses, role_overrides=role_overrides)
    if opp_pace > 0:
        lineup.players = _apply_pace_adjustment(lineup.players, opp_pace)
        lineup.total_minutes = sum(p.projected_min for p in lineup.players)
    return lineup


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
