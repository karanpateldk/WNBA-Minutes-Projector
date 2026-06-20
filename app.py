"""
WNBA Minutes Projection Tool
Run: streamlit run app.py
"""

import sys
import os
from pathlib import Path

# Always resolve imports relative to this file's directory
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(_HERE)

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
from io import BytesIO

from roster_data import TEAMS, GAME_MINUTES
from wnba_scraper import get_team_data, get_all_injuries, get_lineup_info, get_all_players
from season_stats import get_team_season_stats
from model import (
    apply_scenario,
    get_status_options,
    minutes_delta_summary,
    INJURY_COLOR,
    TeamLineup,
    PlayerProjection,
)
from matchup import compute_matchup_adjustments, get_matchup_summary, get_player_h2h_minutes, get_h2h_foul_notes
from quarter_minutes import distribute_quarters

POSITIONS   = ["G", "G/F", "F", "F/C", "C"]
ALL_PLAYERS = get_all_players()

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="WNBA Minutes Projector",
    page_icon="🏀",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    /* ── works in both light and dark mode via currentColor / opacity ── */
    .status-badge {
        display: inline-block; padding: 2px 10px; border-radius: 12px;
        font-size: 0.78rem; font-weight: 600; color: #fff;
        letter-spacing: 0.02em;
    }
    .mins-big { font-size: 1.4rem; font-weight: 700; }
    .delta-pos { color: #28a745; font-weight: 600; }
    .delta-neg { color: #dc3545; font-weight: 600; }

    /* warning box — semi-transparent so it reads in both modes */
    .warning-box {
        background: rgba(255, 193, 7, 0.15);
        border-left: 4px solid #ffc107;
        padding: 10px; border-radius: 4px;
    }

    /* section header uses currentColor so it adapts to dark mode */
    .section-header {
        font-size: 1.1rem; font-weight: 700; margin: 12px 0 6px 0;
        border-bottom: 2px solid rgba(128,128,128,0.3);
        padding-bottom: 4px;
    }

    /* player card in status grid */
    .player-card {
        border: 1px solid rgba(128,128,128,0.25);
        border-radius: 8px;
        padding: 10px 12px 8px 12px;
        margin-bottom: 6px;
        background: rgba(128,128,128,0.04);
    }
    .player-card-name {
        font-weight: 700;
        font-size: 0.95rem;
        margin-bottom: 2px;
    }
    .player-card-meta {
        font-size: 0.75rem;
        opacity: 0.6;
        margin-bottom: 6px;
    }

    /* info banners — use rgba so they work in dark mode */
    .banner-confirmed {
        background: rgba(40,167,69,0.12);
        border-left: 4px solid #28a745;
        padding: 10px 14px; border-radius: 4px; margin-bottom: 12px;
    }
    .banner-projected {
        background: rgba(255,193,7,0.12);
        border-left: 4px solid #ffc107;
        padding: 10px 14px; border-radius: 4px; margin-bottom: 12px;
    }
    .banner-stats {
        background: rgba(74,144,226,0.1);
        border-left: 3px solid #4a90e2;
        padding: 8px 12px; border-radius: 4px; margin-bottom: 8px; font-size: 0.85rem;
    }
    .matchup-banner {
        background: rgba(128,128,128,0.08);
        border-left: 3px solid rgba(128,128,128,0.4);
        padding: 10px 14px; border-radius: 4px; margin-bottom: 8px; font-size: 0.83rem;
    }
</style>
""", unsafe_allow_html=True)

# One-backspace clears selectbox search text.
# st.markdown strips <script> tags, so we use components.html() which runs
# inside a same-origin iframe and can reach the parent document directly.
components.html("""
<script>
(function() {
    var doc = window.parent.document;
    function attach() {
        doc.querySelectorAll('[data-baseweb="select"] input').forEach(function(input) {
            if (input.dataset.clearBound) return;
            input.dataset.clearBound = "1";
            input.addEventListener("keydown", function(e) {
                if (e.key === "Backspace" && input.value.length > 0) {
                    e.stopPropagation();
                    input.value = "";
                    input.dispatchEvent(new Event("input", {bubbles: true}));
                    e.preventDefault();
                }
            }, true);
        });
    }
    new MutationObserver(attach).observe(doc.body, {childList: true, subtree: true});
    attach();
})();
</script>
""", height=0)


# ---------------------------------------------------------------------------
# Session state init
# ---------------------------------------------------------------------------

if "team_data_cache" not in st.session_state:
    st.session_state.team_data_cache = {}

if "selected_team" not in st.session_state:
    st.session_state.selected_team = "Atlanta Dream"

if "player_statuses" not in st.session_state:
    st.session_state.player_statuses = {}

if "duration_map" not in st.session_state:
    st.session_state.duration_map = {}

if "role_overrides" not in st.session_state:
    st.session_state.role_overrides = {}

if "selected_opponent" not in st.session_state:
    st.session_state.selected_opponent = None

# Manual add rows: list of unique integer IDs, one per visible row
if "manual_row_ids" not in st.session_state:
    st.session_state.manual_row_ids = [0]
if "manual_next_id" not in st.session_state:
    st.session_state.manual_next_id = 1
if "manual_last_team" not in st.session_state:
    st.session_state.manual_last_team = ""


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=240)
def load_team(team_name: str) -> dict:
    return get_team_data(team_name)

@st.cache_data(ttl=240)
def load_lineup_info(team_name: str) -> dict:
    return get_lineup_info(team_name)

@st.cache_data(ttl=240)
def load_injuries() -> dict:
    return get_all_injuries()

@st.cache_data(ttl=240)
def load_season_stats(team_name: str) -> dict:
    return get_team_season_stats(team_name)


# ---------------------------------------------------------------------------
# Helper: render a single player row
# ---------------------------------------------------------------------------

def render_player_row(
    p: PlayerProjection,
    base_min: float,
    last_game_min: float,
    col_name, col_pos, col_status, col_last, col_base, col_proj, col_adj, col_note,
    starters_set: set,
    foul_notes: dict | None = None,
):
    """Render all columns except col_adj — caller fills that externally."""
    color = INJURY_COLOR.get(p.status, "#6c757d")

    with col_name:
        marker = " ★" if p.role == "starter" else ""
        st.markdown(f"**{p.name}**{marker}")

    with col_pos:
        st.markdown(p.pos)

    with col_status:
        st.markdown(
            f'<span class="status-badge" style="background:{color}">'
            f'{p.display_status}</span>',
            unsafe_allow_html=True,
        )

    with col_last:
        if last_game_min > 0:
            st.markdown(f"{last_game_min:.0f}")
        else:
            st.markdown("—")

    with col_base:
        st.markdown(f"{base_min:.1f}")

    with col_proj:
        if p.projected_min == 0:
            st.markdown("**OUT**")
        else:
            st.markdown(f'<span class="mins-big">{p.projected_min:.1f}</span>', unsafe_allow_html=True)

    # col_adj is intentionally left empty here — filled by _render_adj_cell() after the call

    with col_note:
        foul_note = (foul_notes or {}).get(p.name)
        dnp_last  = last_game_min == 0 and p.status == "Active" and p.projected_min > 0
        if foul_note:
            st.markdown(
                f'<span style="font-size:0.75rem;color:#fd7e14;font-weight:600">⚠ {foul_note}</span>',
                unsafe_allow_html=True,
            )
        elif p.note and p.replaced_player and p.replaced_player in starters_set:
            st.caption(p.note)
        elif p.injury:
            st.markdown(
                f'<span style="font-size:0.75rem;color:{color};font-weight:600">{p.injury}</span>',
                unsafe_allow_html=True,
            )
        elif dnp_last:
            st.markdown(
                '<span style="font-size:0.75rem;color:#6c757d;font-weight:600">DNP last game</span>',
                unsafe_allow_html=True,
            )
        else:
            # Flag unstable minutes: last-3 range > 12 min (top ~10% most volatile in WNBA)
            last3_range = 0.0
            if p.name in team_data and isinstance(team_data[p.name], dict):
                last3_range = team_data[p.name].get("last3_range", 0.0)
            if last3_range >= 12.0:
                st.markdown(
                    '<span style="font-size:0.75rem;color:#6c757d;font-weight:600">Volatile mins</span>',
                    unsafe_allow_html=True,
                )


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown(
        '<p style="font-size:1.5rem;font-weight:700;margin-bottom:0">WNBA Minutes Projector</p>'
        '<p style="font-size:1rem;color:#888;margin-top:2px">Karan Patel</p>',
        unsafe_allow_html=True,
    )
    st.markdown("---")

    team_list = sorted(TEAMS.keys())
    default_idx = team_list.index("Atlanta Dream") if "Atlanta Dream" in team_list else 0
    selected_team = st.selectbox("Select Team", team_list, index=default_idx)
    if selected_team != st.session_state.manual_last_team and st.session_state.manual_last_team != "":
        # Team switched — wipe all manual rows so added players don't bleed across teams
        st.session_state.manual_row_ids = [0]
        st.session_state.manual_next_id = 1
        st.session_state.manual_added_players = {}
        st.session_state.role_overrides = {}
    st.session_state.manual_last_team = selected_team
    st.session_state.selected_team = selected_team

    # Opponent selector — filters out the selected team itself
    opp_options = ["— No opponent selected —"] + [t for t in team_list if t != selected_team]
    prev_opp = st.session_state.selected_opponent
    prev_idx = opp_options.index(prev_opp) if prev_opp in opp_options else 0
    selected_opponent_raw = st.selectbox(
        "Opponent",
        opp_options,
        index=prev_idx,
    )
    selected_opponent = None if selected_opponent_raw.startswith("—") else selected_opponent_raw
    st.session_state.selected_opponent = selected_opponent

    st.markdown("---")
    st.markdown("**Data Sources**")
    st.caption("Rosters, minutes & injuries: ESPN API")
    st.caption("Data refreshes every 4 hours.")

    if st.button("Update Rosters & Stats", use_container_width=True, type="primary"):
        # Wipe only the currently selected team's caches so the refresh is fast.
        # Other teams stay cached until selected. Also clears the combined player
        # list and Streamlit's in-memory cache so the UI reflects fresh data.
        from pathlib import Path
        import re
        cache_dir = Path(__file__).resolve().parent / "data"
        team_slug = selected_team.replace(" ", "_")
        patterns = [
            f"season_{team_slug}.json",
            f"espn_roster_{team_slug}.json",
            f"lineup_{team_slug}.json",
            f"all_players_combined.json",
            "wnba_injuries.json",
        ]
        for fname in patterns:
            p = cache_dir / fname
            if p.exists():
                p.unlink()
        # Also wipe any h2h / opp_profile files for this team
        for p in cache_dir.glob(f"*{team_slug}*"):
            p.unlink()
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("**Quick Reference**")
    st.caption("Active = full minutes")
    st.caption("Probable = -5%")
    st.caption("Questionable = -25%")
    st.caption("Doubtful = -80%")
    st.caption("Out = 0 min, auto-redistribute")
    st.markdown("---")
    show_charts = st.checkbox("Show quarter breakdown", value=True)
    show_delta = st.checkbox("Show delta vs baseline", value=True)
    export_excel = st.button("Export to Excel", use_container_width=True)


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------

st.header(f"{selected_team} — Minutes Projections")

with st.spinner("Loading team data..."):
    team_data = dict(load_team(selected_team))  # copy so mutations don't affect the cache
    injuries = load_injuries()
    lineup_info = load_lineup_info(selected_team)

# Pull and strip metadata before passing team_data to the model.
# The model only expects player-keyed dicts; __meta__ would cause a KeyError.
_meta = team_data.pop("__meta__", {})
_rotation_depth  = _meta.get("rotation_depth", 8)
_bench_slots     = _meta.get("bench_slots", 3)
_last_updated    = _meta.get("last_updated", "")[:10]
_games_processed = _meta.get("games_processed", 0)

# Inject live injury statuses — always overrides cached team data.
# The injury report is fresher than the season stats cache so it always wins.
for player, inj_info in injuries.items():
    if player in team_data:
        team_data[player]["status"] = inj_info.get("status", "Active")
        team_data[player]["injury"] = inj_info.get("injury", "")

# ---------------------------------------------------------------------------
# Lineup source banner
# ---------------------------------------------------------------------------

if lineup_info.get("starters"):
    confirmed = lineup_info.get("confirmed", False)
    source    = lineup_info.get("source", "")
    game_time = lineup_info.get("game_time", "")
    opponent  = lineup_info.get("opponent", "")

    icon  = "✅" if confirmed else "📋"
    label = "CONFIRMED LINEUP" if confirmed else "PROJECTED LINEUP"
    css   = "banner-confirmed" if confirmed else "banner-projected"
    opp_str      = f" vs {opponent}" if opponent else ""
    time_str     = f" — {game_time}" if game_time else ""
    starters_str = ",  ".join(lineup_info["starters"])

    st.markdown(
        f'<div class="{css}">'
        f'<strong>{icon} {label}</strong> (via {source}){opp_str}{time_str}<br>'
        f'<span style="font-size:0.9rem">{starters_str}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
else:
    st.info(
        "No lineup found for today — using season depth chart as baseline. "
        "Lineups typically appear on RotoWire 1-2 days before tip-off.",
        icon="ℹ️",
    )

# Data freshness info bar
if _games_processed > 0:
    updated_str = f"Updated {_last_updated}" if _last_updated else "Cache age unknown"
    st.markdown(
        f'<div class="banner-stats">'
        f'<strong>Stats from:</strong> {_games_processed} games this season &nbsp;|&nbsp; '
        f'{updated_str} — press <em>Update Rosters &amp; Stats</em> to refresh'
        f'</div>',
        unsafe_allow_html=True,
    )
else:
    st.caption("No completed games yet — using position estimates. Stats will populate once games are played.")

# Build baseline lineup (no overrides)
baseline_lineup = apply_scenario(team_data, {}, {}, {})

# ---------------------------------------------------------------------------
# Injury Status Controls
# ---------------------------------------------------------------------------

st.markdown('<div class="section-header">Player Status Adjustments</div>', unsafe_allow_html=True)
st.caption("To swap starters, set the incoming player to Starter and the outgoing player to Bench.")

status_options = get_status_options()

player_statuses: dict[str, str] = {}
role_overrides: dict[str, str] = {}

# Separate players by category so the UI is scannable:
#  1. Active / injured players (relevant to tonight)
#  2. Zero-minute-season players (never played this year — auto-Out)
player_names = list(team_data.keys())

zero_min_players = [p for p in player_names if team_data[p].get("zero_min_season")]
relevant_players = [p for p in player_names if p not in zero_min_players]

# Sort by season avg minutes descending so the highest-usage players appear first
relevant_players.sort(key=lambda p: -team_data[p].get("avg_min", 0.0))
zero_min_players.sort(key=lambda p: team_data[p].get("pos", "Z"))

n_cols = 3

def _render_status_grid(names: list[str]):
    rows = [names[i:i+n_cols] for i in range(0, len(names), n_cols)]
    for row_players in rows:
        cols = st.columns(n_cols)
        for i, player in enumerate(row_players):
            info = team_data[player]
            default_status = info.get("status", "Active")
            if default_status not in status_options:
                default_status = "Active"
            default_idx  = status_options.index(default_status)
            default_role = info.get("role", "bench")

            with cols[i]:
                color  = INJURY_COLOR.get(default_status, "#6c757d")
                gp     = info.get("games_played", 0)
                gs     = info.get("games_started", 0)
                avg    = info.get("avg_min", 0.0)
                gp_str = f"{gp} GP" if gp > 0 else ""
                gs_str = f" / {gs} GS" if gs > 0 else ""

                with st.container(border=True):
                    st.markdown(
                        f'<div style="font-weight:600;margin-bottom:2px">{player} '
                        f'<span style="font-size:0.75rem;color:{color}">({info.get("pos","?")})</span>'
                        f'</div>'
                        f'<div style="font-size:0.75rem;opacity:0.75;margin-bottom:4px">'
                        f'{avg:.0f} mpg &nbsp;·&nbsp; {gp_str}{gs_str}'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                    # Role on top
                    role_options  = ["Starter", "Bench"]
                    saved_role    = st.session_state.role_overrides.get(player, default_role)
                    role_idx      = 0 if saved_role == "starter" else 1
                    selected_role = st.selectbox(
                        "Role", role_options, index=role_idx,
                        key=f"role_{player}", label_visibility="collapsed",
                    )
                    new_role = "starter" if selected_role == "Starter" else "bench"
                    if new_role != default_role:
                        role_overrides[player] = new_role
                        st.session_state.role_overrides[player] = new_role
                    else:
                        st.session_state.role_overrides.pop(player, None)

                    # Status below
                    status = st.selectbox(
                        "Status", status_options, index=default_idx,
                        key=f"status_{player}", label_visibility="collapsed",
                    )
                    player_statuses[player] = status

_render_status_grid(relevant_players)

if zero_min_players:
    with st.expander(f"No minutes this season ({len(zero_min_players)} players — auto set to Out)", expanded=False):
        st.caption("These players are on the roster but haven't played a minute yet in 2026. Auto-set to Out. Override if they're expected to play tonight.")
        _render_status_grid(zero_min_players)

st.markdown("---")

# ---------------------------------------------------------------------------
# Manual Player Add
# ---------------------------------------------------------------------------

st.markdown('<div class="section-header">Add a Player Not Listed</div>', unsafe_allow_html=True)
st.caption("Use this if a player is missing from the roster (e.g. a late signing, callup, or roster correction).")

if "manual_added_players" not in st.session_state:
    st.session_state.manual_added_players = {}

with st.expander("+ Add / override players manually"):
    manual_options = ["— select or type below —"] + ALL_PLAYERS

    if st.button("+ Add another player", key="add_row_btn"):
        st.session_state.manual_row_ids.append(st.session_state.manual_next_id)
        st.session_state.manual_next_id += 1
        st.rerun()

    rows_to_delete = []

    for row_i, rid in enumerate(st.session_state.manual_row_ids):
        min_key     = f"manual_min_val_{rid}"
        min_txt_key = f"manual_min_text_{rid}"
        pending_key = f"manual_min_pending_{rid}"

        # Apply button update BEFORE any widget on this row renders
        if pending_key in st.session_state:
            st.session_state[min_key]     = st.session_state.pop(pending_key)
            st.session_state[min_txt_key] = str(st.session_state[min_key])

        if min_key not in st.session_state:
            st.session_state[min_key]     = 10
            st.session_state[min_txt_key] = "10"

        mc1, mc2, mc3, mc4, mc5, mc_del = st.columns([3, 1.2, 1.5, 1.5, 1.5, 0.7])
        with mc1:
            manual_pick = st.selectbox("Player name", manual_options, key=f"manual_pick_{rid}")
            manual_name = st.text_input("Or type name manually", key=f"manual_name_{rid}",
                                        placeholder="e.g. Sophie Cunningham")
            effective_name = manual_name.strip() if manual_name.strip() else (
                manual_pick if manual_pick != "— select or type below —" else ""
            )
        with mc2:
            manual_pos = st.selectbox("Pos", POSITIONS, key=f"manual_pos_{rid}")
        with mc3:
            manual_role = st.selectbox("Role", ["starter", "bench"], key=f"manual_role_{rid}")
        with mc4:
            typed = st.text_input("Mins", key=min_txt_key)
            try:
                st.session_state[min_key] = max(0, min(40, int(typed)))
            except ValueError:
                pass
            btn_minus, btn_plus = st.columns(2)
            with btn_minus:
                if st.button("−", key=f"min_minus_{rid}", use_container_width=True):
                    st.session_state[pending_key] = max(0, st.session_state[min_key] - 1)
                    st.rerun()
            with btn_plus:
                if st.button("+", key=f"min_plus_{rid}", use_container_width=True):
                    st.session_state[pending_key] = min(40, st.session_state[min_key] + 1)
                    st.rerun()
            manual_min = st.session_state[min_key]
        with mc5:
            manual_status = st.selectbox("Status", get_status_options(), key=f"manual_status_{rid}")
        with mc_del:
            st.write("")
            if st.button("✕", key=f"del_row_{rid}", help="Remove this row", use_container_width=True):
                rows_to_delete.append(rid)

        if effective_name and rid not in rows_to_delete:
            st.session_state.manual_added_players[rid] = {
                "name":   effective_name,
                "pos":    manual_pos,
                "role":   manual_role,
                "min":    manual_min,
                "status": manual_status,
            }
            st.success(f"{effective_name} — {manual_pos}, {manual_role}, {manual_min} min")
        elif not effective_name:
            st.session_state.manual_added_players.pop(rid, None)

        if row_i < len(st.session_state.manual_row_ids) - 1:
            st.markdown("---")

    if rows_to_delete:
        for r in rows_to_delete:
            st.session_state.pop(f"manual_min_val_{r}", None)
            st.session_state.pop(f"manual_min_text_{r}", None)
            st.session_state.pop(f"manual_min_pending_{r}", None)
            st.session_state.manual_added_players.pop(r, None)
        st.session_state.manual_row_ids = [r for r in st.session_state.manual_row_ids if r not in rows_to_delete]
        if not st.session_state.manual_row_ids:
            st.session_state.manual_row_ids = [st.session_state.manual_next_id]
            st.session_state.manual_next_id += 1
        st.rerun()

# Inject added players into team_data for projection
for _rid, _entry in st.session_state.manual_added_players.items():
    _name = _entry["name"]
    if _name not in team_data:
        team_data[_name] = {
            "pos":              _entry["pos"],
            "role":             _entry["role"],
            "depth":            1 if _entry["role"] == "starter" else 2,
            "avg_min":          _entry["min"],
            "last3_avg":        _entry["min"],
            "clean_avg_min":    _entry["min"],
            "last3_clean_avg":  _entry["min"],
            "games_played":     1,
            "games_started":    1 if _entry["role"] == "starter" else 0,
            "status":           _entry["status"],
            "injury":           "",
            "zero_min_season":  False,
            "recently_active":  True,
        }
    else:
        team_data[_name]["avg_min"]          = _entry["min"]
        team_data[_name]["last3_avg"]        = _entry["min"]
        team_data[_name]["clean_avg_min"]    = _entry["min"]
        team_data[_name]["last3_clean_avg"]  = _entry["min"]
        team_data[_name]["pos"]              = _entry["pos"]
        team_data[_name]["role"]             = _entry["role"]
        team_data[_name]["status"]           = _entry["status"]
        team_data[_name]["zero_min_season"]  = False
    player_statuses[_name] = _entry["status"]

# Rebuild baseline with any added players included
baseline_lineup = apply_scenario(team_data, {}, {}, {})

st.markdown("---")

# ---------------------------------------------------------------------------
# Adjusted Lineup
# ---------------------------------------------------------------------------

adjusted_lineup = apply_scenario(team_data, player_statuses, {}, role_overrides)
deltas = minutes_delta_summary(baseline_lineup, adjusted_lineup) if show_delta else {}

out_players = [p for p in adjusted_lineup.players if p.projected_min == 0]
active_players = [p for p in adjusted_lineup.players if p.projected_min > 0]

# Warnings
if adjusted_lineup.warnings:
    for w in adjusted_lineup.warnings:
        st.markdown(f'<div class="warning-box">⚠️ {w}</div>', unsafe_allow_html=True)

# Total minutes badge
total_min = sum(p.projected_min for p in active_players)
col_tot, col_active, col_out = st.columns(3)
col_tot.metric("Total Minutes", f"{total_min:.0f}", delta=f"Target: {GAME_MINUTES}")
col_active.metric("Active Players", len(active_players))
col_out.metric("Players Out", len(out_players))

st.markdown("---")

# ---------------------------------------------------------------------------
# Matchup adjustments (computed before table so we can show them inline)
# ---------------------------------------------------------------------------

matchup_adjs: dict[str, float] = {}
matchup_summary: dict = {}
h2h_player_mins: dict[str, list[float]] = {}
h2h_foul_notes: dict[str, str] = {}

if selected_opponent:
    with st.spinner(f"Loading matchup data vs {selected_opponent}..."):
        try:
            matchup_adjs    = compute_matchup_adjustments(selected_team, selected_opponent, team_data)
            matchup_summary = get_matchup_summary(selected_team, selected_opponent)
            h2h_player_mins = get_player_h2h_minutes(selected_team, selected_opponent)
            h2h_foul_notes  = get_h2h_foul_notes(selected_team, selected_opponent, team_data)
        except Exception as e:
            st.warning(f"Matchup data unavailable: {e}")

# ---------------------------------------------------------------------------
# Lineup table
# ---------------------------------------------------------------------------

st.markdown('<div class="section-header">Projected Lineup</div>', unsafe_allow_html=True)

# Show matchup context banner when an opponent is selected
if selected_opponent and matchup_summary:
    conf       = matchup_summary.get("confidence", "low")
    conf_color = {"low": "#6c757d", "medium": "#ffc107", "high": "#28a745"}.get(conf, "#6c757d")
    h2h_games  = matchup_summary.get("h2h_games", 0)
    opp_depth  = matchup_summary.get("opp_depth", 8)
    blowout_count  = matchup_summary.get("blowout_count", 0)
    blowout_sample = matchup_summary.get("blowout_sample", 0)
    blowout_str = f"{blowout_count}/{blowout_sample} blowouts" if blowout_sample else "—"

    conf_label = {"low": f"LOW — {h2h_games} H2H game(s) this season", "high": f"HIGH — {h2h_games} H2H games"}.get(conf, conf.upper())

    notes_html = "".join(f"<li style='margin-bottom:3px'>{n}</li>" for n in matchup_summary.get("notes", []))
    st.markdown(
        f'<div class="matchup-banner" style="border-left-color:{conf_color}">'
        f'<strong>vs {selected_opponent}</strong> &nbsp;|&nbsp; '
        f'Confidence: <span style="color:{conf_color};font-weight:600">{conf_label}</span>'
        f' &nbsp;|&nbsp; {opp_depth}-player rotation &nbsp;|&nbsp; {blowout_str} this season'
        f'<ul style="margin:6px 0 0 0;padding-left:16px">{notes_html}</ul>'
        f'</div>',
        unsafe_allow_html=True,
    )

# Decide column header label — use a short team abbreviation so it fits the narrow column
_OPP_ABBREV = {
    "Atlanta Dream":           "ATL",
    "Chicago Sky":             "CHI",
    "Connecticut Sun":         "CON",
    "Dallas Wings":            "DAL",
    "Golden State Valkyries":  "GSV",
    "Indiana Fever":           "IND",
    "Las Vegas Aces":          "LVA",
    "Los Angeles Sparks":      "LAS",
    "Minnesota Lynx":          "MIN",
    "New York Liberty":        "NYL",
    "Phoenix Mercury":         "PHX",
    "Portland Fire":           "POR",
    "Seattle Storm":           "SEA",
    "Toronto Tempo":           "TOR",
    "Washington Mystics":      "WAS",
}
adj_col_label = f"vs {_OPP_ABBREV.get(selected_opponent, selected_opponent[:3].upper())}" if selected_opponent else "Δ"

# Column headers
hc = st.columns([3, 1, 2, 1, 1.2, 1.5, 1.5, 2.5])
hc[0].markdown("**Player**")
hc[1].markdown("**Pos**")
hc[2].markdown("**Status**")
hc[3].markdown("**Last**")
hc[4].markdown('<span title="Recent-weighted average — emphasizes last few games over the full season" style="cursor:help;border-bottom:1px dotted;text-decoration:none"><b>Wtd ⓘ</b></span>', unsafe_allow_html=True)
hc[5].markdown('<span title="Final projected minutes after injury adjustments and starter/bench role assignments" style="cursor:help;border-bottom:1px dotted;text-decoration:none"><b>Proj ⓘ</b></span>', unsafe_allow_html=True)
hc[6].markdown(f'<span title="Change vs fully healthy lineup; shows actual H2H minutes when an opponent is selected" style="cursor:help;border-bottom:1px dotted;text-decoration:none"><b>{adj_col_label} ⓘ</b></span>', unsafe_allow_html=True)
hc[7].markdown("**Note**")

# Build lookup maps
base_map      = {p.name: p.base_min for p in adjusted_lineup.players}
last_game_map = {name: info.get("last_game_min", 0.0) for name, info in team_data.items()}
starters_set  = {p.name for p in adjusted_lineup.players if p.role == "starter"}

COL_WIDTHS = [3, 1, 2, 1, 1.2, 1.5, 1.5, 2.5]


def _render_adj_cell(col, player_name: str, proj_min: float):
    """
    Opponent selected → show actual minutes played in each game vs that opponent this season.
    No opponent → show injury delta vs baseline.
    """
    if selected_opponent:
        games = h2h_player_mins.get(player_name, [])
        if games:
            # Show each game's minutes separated by " / "
            parts = ", ".join(f"{m:.0f}" for m in games)
            col.markdown(f'<span style="font-size:0.82rem">{parts}</span>', unsafe_allow_html=True)
        else:
            col.markdown('<span style="color:#aaa;font-size:0.82rem">—</span>', unsafe_allow_html=True)
    else:
        # Fall back to injury delta vs baseline
        delta = round(proj_min - base_map.get(player_name, proj_min), 1)
        if abs(delta) >= 0.5:
            cls  = "delta-pos" if delta > 0 else "delta-neg"
            sign = "+" if delta > 0 else ""
            col.markdown(f'<span class="{cls}">{sign}{delta:.1f}</span>', unsafe_allow_html=True)
        else:
            col.markdown("—")


# Starters section
st.markdown("**Starters**")
for p in [x for x in adjusted_lineup.players if x.role == "starter" and x.projected_min > 0]:
    c = st.columns(COL_WIDTHS)
    render_player_row(p, base_map[p.name], last_game_map.get(p.name, 0.0), *c, starters_set=starters_set, foul_notes=h2h_foul_notes)
    _render_adj_cell(c[6], p.name, p.projected_min)

# Bench section
st.markdown("**Bench**")
for p in [x for x in adjusted_lineup.players if x.role == "bench" and x.projected_min > 0]:
    c = st.columns(COL_WIDTHS)
    render_player_row(p, base_map[p.name], last_game_map.get(p.name, 0.0), *c, starters_set=starters_set, foul_notes=h2h_foul_notes)
    _render_adj_cell(c[6], p.name, p.projected_min)

# Out section
if out_players:
    st.markdown("**Out**")
    for p in out_players:
        c = st.columns(COL_WIDTHS)
        render_player_row(p, base_map[p.name], last_game_map.get(p.name, 0.0), *c, starters_set=starters_set, foul_notes=h2h_foul_notes)
        c[6].markdown("—")

st.markdown("---")

# ---------------------------------------------------------------------------
# Matchup minutes summary table
# ---------------------------------------------------------------------------

if selected_opponent and h2h_player_mins:
    opp_full    = selected_opponent
    opp_abbrev  = _OPP_ABBREV.get(opp_full, opp_full[:3].upper())
    n_games     = max((len(v) for v in h2h_player_mins.values()), default=0)

    st.markdown(f'<div class="section-header">Minutes vs {opp_full} This Season</div>', unsafe_allow_html=True)
    st.caption(
        f"Actual minutes each player logged in {n_games} game(s) against {opp_full} this season. "
        "Each column is one game, most recent on the right. "
        "Empty = did not play in that matchup."
    )

    h2h_rows = []
    for p in active_players:
        games = h2h_player_mins.get(p.name, [])
        row = {
            "Player": p.name,
            "Role":   "Starter" if p.role == "starter" else "Bench",
            "Proj":   p.projected_min,
        }
        for i in range(n_games):
            col_label = f"G{i + 1}"
            row[col_label] = games[i] if i < len(games) else None
        h2h_rows.append(row)

    if h2h_rows:
        df_h2h = pd.DataFrame(h2h_rows)
        game_cols = [f"G{i+1}" for i in range(n_games)]

        def _fmt_min(v):
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return "—"
            return f"{v:.0f}"

        fmt_map = {"Proj": "{:.1f}"}
        for c in game_cols:
            fmt_map[c] = _fmt_min

        st.dataframe(
            df_h2h.style.format(fmt_map),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("---")

# ---------------------------------------------------------------------------
# Starter Quarter Minutes Breakdown
# ---------------------------------------------------------------------------

if show_charts:
    st.markdown('<div class="section-header">Starter Quarter Minutes Breakdown</div>', unsafe_allow_html=True)
    st.caption(
        "Average minutes each starter plays per quarter, derived from play-by-play data this season. "
        "Use for live trading — shows exactly when starters are typically on the floor."
    )

    # Collect starters that are active and have quarter data
    starter_rows = [
        p for p in adjusted_lineup.players
        if p.role == "starter" and p.projected_min > 0
    ]

    if not starter_rows:
        st.info("No active starters with quarter data available.")
    else:
        Q_COLORS = {1: "#4a90e2", 2: "#7b68ee", 3: "#e67e22", 4: "#27ae60"}
        Q_LABELS = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}
        Q_MAX = 10.0  # max realistic starter minutes in a single quarter

        for p in starter_rows:
            q_data = team_data.get(p.name, {}).get("quarter_avgs", {})

            # Normalise keys — JSON stores as strings "1","2","3","4"
            raw_hist = {int(k): float(v) for k, v in q_data.items() if str(k) in ("1","2","3","4")}

            if not raw_hist:
                # No play-by-play data yet — show a placeholder row
                st.markdown(
                    f'<div style="margin:6px 0;font-size:0.85rem;color:#888">'
                    f'<strong>{p.name}</strong> — no quarter data yet (need play-by-play)</div>',
                    unsafe_allow_html=True,
                )
                continue

            # Scale historical quarter distribution to match projected total minutes.
            # distribute_quarters() preserves the shape but ensures sum == projected_min.
            historical_all = {p.name: raw_hist}
            q_mins = distribute_quarters(p.name, p.projected_min, historical_all)

            total_q = sum(q_mins.values())
            status_color = INJURY_COLOR.get(p.status, "#6c757d")

            # Header row: name + projected total
            st.markdown(
                f'<div style="display:flex;align-items:baseline;gap:10px;margin-top:10px;margin-bottom:3px">'
                f'<span style="font-weight:700;font-size:0.95rem">{p.name}</span>'
                f'<span style="font-size:0.78rem;color:{status_color}">{p.pos}</span>'
                f'<span style="font-size:0.78rem;color:#888">Proj: {p.projected_min:.1f} min total</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Four quarter bars side by side
            bar_cols = st.columns(4)
            for q in [1, 2, 3, 4]:
                mins = q_mins.get(q, 0.0)
                pct  = min(mins / Q_MAX, 1.0)
                bar_w = max(int(pct * 100), 3)  # at least 3px so bar is visible
                color = Q_COLORS[q]

                # Intensity: fade bar if < 3 min (player sits this quarter a lot)
                opacity = 1.0 if mins >= 3.0 else 0.45

                bar_html = (
                    f'<div style="text-align:center">'
                    f'<div style="font-size:0.7rem;font-weight:600;color:#555;margin-bottom:2px">{Q_LABELS[q]}</div>'
                    f'<div style="background:#e9ecef;border-radius:4px;height:12px;overflow:hidden">'
                    f'<div style="width:{bar_w}%;background:{color};height:100%;'
                    f'opacity:{opacity:.2f};border-radius:4px"></div>'
                    f'</div>'
                    f'<div style="font-size:0.82rem;font-weight:700;margin-top:3px">{mins:.1f}</div>'
                    f'</div>'
                )
                bar_cols[q - 1].markdown(bar_html, unsafe_allow_html=True)

        # Team quarter totals — useful for seeing how heavy the starter load is per quarter
        st.markdown("---")
        st.markdown(
            '<div style="font-size:0.8rem;font-weight:600;color:#555;margin-bottom:4px">'
            'Combined starter minutes per quarter</div>',
            unsafe_allow_html=True,
        )

        team_q_totals = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
        for p in starter_rows:
            q_data = team_data.get(p.name, {}).get("quarter_avgs", {})
            raw_hist = {int(k): float(v) for k, v in q_data.items() if str(k) in ("1","2","3","4")}
            if raw_hist:
                scaled = distribute_quarters(p.name, p.projected_min, {p.name: raw_hist})
                for q in [1, 2, 3, 4]:
                    team_q_totals[q] += scaled.get(q, 0.0)

        tot_cols = st.columns(4)
        for q in [1, 2, 3, 4]:
            tot = team_q_totals[q]
            # WNBA: 5 starters × 10 min quarter = 50 max; ~35-42 is typical heavy starter load
            intensity = "🔴" if tot >= 40 else ("🟡" if tot >= 32 else "🟢")
            tot_cols[q - 1].markdown(
                f'<div style="text-align:center;font-size:0.85rem">'
                f'<div style="font-weight:600">{Q_LABELS[q]}</div>'
                f'<div style="font-size:1.1rem;font-weight:700">{tot:.1f}</div>'
                f'<div style="font-size:0.7rem">{intensity}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

# ---------------------------------------------------------------------------
# Delta summary (baseline vs adjusted)
# ---------------------------------------------------------------------------

if show_delta and deltas:
    st.markdown("---")
    st.markdown('<div class="section-header">Minutes Changes vs Healthy Baseline</div>', unsafe_allow_html=True)

    delta_rows = []
    for name, delta in sorted(deltas.items(), key=lambda x: x[1]):
        base_val = next((p.base_min for p in adjusted_lineup.players if p.name == name), 0)
        delta_rows.append({
            "Player": name,
            "Baseline": base_val,
            "Adjusted": round(base_val + delta, 1),
            "Change": delta,
        })
    df_delta = pd.DataFrame(delta_rows)

    def color_delta(val):
        if val > 0: return "color: #28a745; font-weight: bold"
        if val < 0: return "color: #dc3545; font-weight: bold"
        return ""

    st.dataframe(
        df_delta.style.map(color_delta, subset=["Change"]).format(precision=1),
        use_container_width=True,
        hide_index=True,
    )

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

if export_excel:
    rows_export = []
    for p in adjusted_lineup.players:
        base_val = base_map.get(p.name, p.projected_min)
        matchup_adj = matchup_adjs.get(p.name, 0.0) if matchup_adjs else 0.0
        rows_export.append({
            "Player":         p.name,
            "Pos":            p.pos,
            "Role":           p.role,
            "Status":         p.status,
            "Injury":         p.injury,
            "Base Min":       base_val,
            "Proj Min":       p.projected_min,
            "Matchup Adj":    matchup_adj if selected_opponent else "",
            "Adj + Matchup":  round(p.projected_min + matchup_adj, 1) if selected_opponent else "",
            "Change":         round(p.projected_min - base_val, 1),
            "Note":         p.note,
        })

    df_export = pd.DataFrame(rows_export)
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_export.to_excel(writer, index=False, sheet_name="Projections")
    buf.seek(0)
    st.download_button(
        label="Download Excel",
        data=buf,
        file_name=f"{selected_team.replace(' ','_')}_minutes.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# ---------------------------------------------------------------------------
# Full Injury Report
# ---------------------------------------------------------------------------

with st.expander("Full WNBA Injury Report"):
    if injuries:
        inj_rows = [
            {"Player": k, "Team": v.get("team",""), "Status": v.get("status",""),
             "Injury": v.get("injury","")}
            for k, v in injuries.items()
        ]
        df_inj = pd.DataFrame(inj_rows)
        st.dataframe(df_inj, use_container_width=True, hide_index=True)
    else:
        st.info("No injury data available. ESPN may be rate-limiting. Static data in use.")
