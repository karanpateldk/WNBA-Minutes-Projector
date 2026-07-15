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
from wnba_scraper import get_team_data, get_all_injuries, get_lineup_info, get_all_players, get_schedule_context
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

@st.cache_data(ttl=21600)
def _load_all_players() -> list[str]:
    return get_all_players()

# Team brand primary colors — used for header accent and section dividers.
# All chosen to be legible on both light and dark backgrounds.
TEAM_COLORS = {
    "Atlanta Dream":           "#E03A3E",  # red — works both modes
    "Chicago Sky":             "#4A8FBF",  # mid blue — darkened for light mode visibility
    "Connecticut Sun":         "#E07818",  # orange — works both modes
    "Dallas Wings":            "#4A90D9",  # blue — works both modes
    "Golden State Valkyries":  "#7B4FBF",  # purple — works both modes
    "Indiana Fever":           "#C8A000",  # darkened yellow — visible on white
    "Las Vegas Aces":          "#808080",  # mid gray — visible on both modes
    "Los Angeles Sparks":      "#C89800",  # darkened gold — visible on white
    "Minnesota Lynx":          "#3A7ABF",  # lightened blue — visible on dark
    "New York Liberty":        "#3AAFA0",  # darkened teal — visible on white
    "Phoenix Mercury":         "#E56020",  # orange — works both modes
    "Portland Fire":           "#D62B2B",  # red — works both modes
    "Seattle Storm":           "#3A8A50",  # lightened green — visible on dark
    "Toronto Tempo":           "#CE1141",  # red — works both modes
    "Washington Mystics":      "#C8102E",  # red — works both modes
}

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="WNBA Minutes Projector",  # v2026.06.30
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
        letter-spacing: 0.02em; white-space: nowrap;
    }
    /* Prevent column headers and player names from mid-word wrapping. */
    [data-testid="column"] p,
    [data-testid="column"] span,
    [data-testid="column"] b,
    [data-testid="column"] strong {
        white-space: nowrap !important;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    [data-testid="column"]:last-child p,
    [data-testid="column"]:last-child span {
        white-space: normal !important;
        overflow: visible;
        line-height: 1.3;
    }
    [data-testid="column"]:last-child [data-testid="stMarkdownContainer"] {
        margin-bottom: 0 !important;
        padding-bottom: 0 !important;
    }
    .mins-big { font-size: 1.4rem; font-weight: 700; }

    /* starter / bench divider */
    .role-divider {
        border: none;
        border-top: 1px dashed rgba(128,128,128,0.35);
        margin: 6px 0 8px 0;
    }

    /* warning box */
    .warning-box {
        background: rgba(255, 193, 7, 0.15);
        border-left: 4px solid #ffc107;
        padding: 10px; border-radius: 4px;
    }

    /* section header */
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
    .player-card-name { font-weight: 700; font-size: 0.95rem; margin-bottom: 2px; }
    .player-card-meta { font-size: 0.75rem; opacity: 0.6; margin-bottom: 6px; }

    /* info banners */
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
        background: rgba(128,128,128,0.08);
        border-left: 3px solid rgba(128,128,128,0.4);
        padding: 8px 12px; border-radius: 4px; margin-bottom: 8px; font-size: 0.85rem;
    }
    .matchup-banner {
        background: rgba(128,128,128,0.08);
        border-left: 3px solid rgba(128,128,128,0.4);
        padding: 10px 14px; border-radius: 4px; margin-bottom: 8px; font-size: 0.83rem;
    }

    /* sidebar polish */
    [data-testid="stSidebar"] .stSelectbox label {
        font-size: 0.8rem; font-weight: 600; opacity: 0.7;
        text-transform: uppercase; letter-spacing: 0.05em;
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
                if (e.key === "Backspace") {
                    e.stopPropagation();
                    e.preventDefault();
                    input.value = "";
                    input.dispatchEvent(new Event("input", {bubbles: true}));
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
# Auto-clear caches on every code change OR Streamlit reboot.
#
# Sentinel file stores:  "<app_mtime>|<process_pid>"
# Clear triggers:
#   1. app.py was modified  → code changed, always refresh
#   2. PID changed          → server restarted (reboot button or manual restart)
# Normal page interactions (same mtime + same PID) → skip, no overhead.
# ---------------------------------------------------------------------------
import os as _os
from pathlib import Path as _Path

_data_dir  = _Path(__file__).parent / "data"
_sentinel  = _Path(__file__).parent / ".last_clear_stamp"
_app_mtime = str(_Path(__file__).stat().st_mtime)
_pid       = str(_os.getpid())
_stamp     = f"{_app_mtime}|{_pid}"

_should_clear = True
if _sentinel.exists():
    try:
        _should_clear = _sentinel.read_text().strip() != _stamp
    except Exception:
        pass

if _should_clear:
    for _pat in ("season_*.json", "espn_roster_*.json", "schedule_*.json"):
        for _f in _data_dir.glob(_pat):
            try:
                _f.unlink()
            except Exception:
                pass
    try:
        _sentinel.write_text(_stamp)
    except Exception:
        pass

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
if "manual_added_players" not in st.session_state:
    st.session_state.manual_added_players = {}
if "manual_expander_open" not in st.session_state:
    st.session_state.manual_expander_open = False


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_team(team_name: str) -> dict:
    return get_team_data(team_name)

def load_lineup_info(team_name: str) -> dict:
    return get_lineup_info(team_name)

@st.cache_data(ttl=900)   # injuries hit the network (PDF + ESPN); cache to avoid slow load on every render
def load_injuries() -> dict:
    return get_all_injuries()

def load_season_stats(team_name: str) -> dict:
    return get_team_season_stats(team_name)

@st.cache_data(ttl=1800)
def load_schedule_context(team_name: str) -> dict:
    return get_schedule_context(team_name)


# ---------------------------------------------------------------------------
# Helper: render a single player row
# ---------------------------------------------------------------------------

def render_player_row(
    p: PlayerProjection,
    base_min: float,
    last_game_min: float,
    col_name, col_status, col_last, col_base, col_proj, col_conf, col_adj, col_note,
    starters_set: set,
    foul_notes: dict | None = None,
):
    """Render all columns except col_adj — caller fills that externally."""
    color = INJURY_COLOR.get(p.status, "#6c757d")

    with col_name:
        # Pos is wrapped with last name so it never orphans on a new line
        parts = p.name.rsplit(" ", 1)
        if len(parts) == 2:
            _name_html = (
                f'<b>{parts[0]}</b> '
                f'<span style="white-space:nowrap"><b>{parts[1]}</b>'
                f' <span style="font-size:0.75rem;opacity:0.7;font-weight:500">({p.pos})</span></span>'
            )
        else:
            _name_html = (
                f'<span style="white-space:nowrap"><b>{p.name}</b>'
                f' <span style="font-size:0.75rem;opacity:0.7;font-weight:500">({p.pos})</span></span>'
            )
        st.markdown(_name_html, unsafe_allow_html=True)

    with col_status:
        st.markdown(
            f'<span class="status-badge" style="background:{color};white-space:nowrap;'
            f'font-size:0.72rem">'
            f'{p.display_status}</span>',
            unsafe_allow_html=True,
        )

    with col_last:
        if last_game_min and last_game_min > 0:
            st.markdown(f"{last_game_min:.0f}")
        else:
            st.markdown("—")

    with col_base:
        st.markdown(f"{base_min:.1f}")

    with col_proj:
        if p.projected_min == 0:
            st.markdown("**OUT**")
        else:
            st.markdown(
                f'<div style="font-size:1.4rem;font-weight:700;line-height:1.3">{p.projected_min:.1f}</div>',
                unsafe_allow_html=True,
            )

    with col_conf:
        conf = getattr(p, 'confidence', 0)
        if p.projected_min == 0:
            dot_color = "#dc3545"
        else:
            dot_color = "#28a745" if conf >= 70 else ("#e6a817" if conf >= 45 else "#dc3545")
        st.markdown(
            f'<div style="text-align:center;font-size:1.1rem;color:{dot_color};padding-top:4px">&#9679;</div>',
            unsafe_allow_html=True,
        )

    # col_adj is intentionally left empty here — filled by _render_adj_cell() after the call

    with col_note:
        foul_note   = (foul_notes or {}).get(p.name)
        _pinfo = team_data.get(p.name, {})

        _LT_KEYWORDS = (
            "surgery", "surgical", "torn", "tear", "fracture", "fractured",
            "out indefinitely", "indefinite", "season", "months", "month",
            "weeks", "week", "acl", "achilles", "stress fracture", "labrum",
        )
        _inj_lower = (p.injury or "").lower()
        _inj_is_longterm = any(kw in _inj_lower for kw in _LT_KEYWORDS)

        _absent = (
            last_game_min == 0
            and p.projected_min > 0
            and _pinfo.get("games_played", 0) >= 3
            and (_pinfo.get("recently_active", False) or _pinfo.get("last3_avg", 0) > 0)
        )
        _games_missed = _pinfo.get("games_missed_streak", 0) if _absent else 0

        long_term_injury = (
            _absent
            and p.status in ("Active", "Questionable", "Day-To-Day")
            and (
                _games_missed >= 5
                or _inj_is_longterm
                or (p.injury and _games_missed >= 2)
            )
        )
        dnp_last = (
            _absent
            and p.status == "Active"
            and not p.injury
            and not long_term_injury
        )
        last3_range = 0.0
        if p.name in team_data and isinstance(team_data[p.name], dict):
            last3_range = team_data[p.name].get("last3_range", 0.0) or 0.0
        volatile = (
            last3_range >= 14.0
            and p.status in ("Active", "Probable")
            and p.projected_min > 0
        )

        _generic = {"out", "day-to-day", "dtd", "gtd", "game time decision", "available", "active", ""}
        clean_injury = p.injury if p.injury.lower().strip() not in _generic else ""

        # Build a single compact note string — rendered in small text, no extra row height
        _note_text = ""
        _note_color = "#6c757d"
        if foul_note:
            _note_text = f"⚠ {foul_note}"
            _note_color = "#fd7e14"
        elif p.note and p.replaced_player and p.replaced_player in starters_set:
            _note_text = p.note
        elif p.status in ("Questionable", "Doubtful", "Day-To-Day"):
            if clean_injury:
                _note_text = clean_injury
                if _absent and _games_missed >= 1:
                    _note_text += " · missed last"
                _note_color = color
            elif _absent and _games_missed >= 1:
                _note_text = "Missed last game"
                _note_color = color
        elif p.status == "Out" and p.projected_min == 0:
            dnp_type = team_data.get(p.name, {}).get("dnp_type", "injury")
            _note_text = "Coach's Decision" if dnp_type == "coach" else (clean_injury or "")
            _note_color = color
        elif clean_injury and p.status not in ("Active", "Probable"):
            _note_text = clean_injury
            _note_color = color
        elif long_term_injury:
            _note_text = "Long-term injury"
            _note_color = "#fd7e14"
        elif dnp_last:
            _dnp_type = _pinfo.get("dnp_type", "")
            _note_text = "DNP last game" if _dnp_type == "coach" else "Missed last game"
        elif volatile:
            _note_text = "Volatile mins"

        if _note_text:
            st.markdown(
                f'<span style="font-size:0.72rem;color:{_note_color};font-weight:600;'
                f'white-space:normal;line-height:1.3">{_note_text}</span>',
                unsafe_allow_html=True,
            )


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown(
        '<p style="font-size:1.5rem;font-weight:700;margin-bottom:0">WNBA Minutes Projector</p>'
        '<p style="font-size:1rem;color:#888;margin-top:2px">Karan Patel &nbsp;·&nbsp; <span style="font-size:0.8rem">build 2026-07-01.1</span></p>',
        unsafe_allow_html=True,
    )
    st.markdown("---")

    team_list = sorted(TEAMS.keys())
    default_idx = team_list.index("Atlanta Dream") if "Atlanta Dream" in team_list else 0
    selected_team = st.selectbox("Select Team", team_list, index=default_idx)
    if selected_team != st.session_state.manual_last_team and st.session_state.manual_last_team != "":
        # Team switched — wipe manual rows, role overrides, and opponent selection
        st.session_state.manual_row_ids = [0]
        st.session_state.manual_next_id = 1
        st.session_state.manual_added_players = {}
        st.session_state.role_overrides = {}
        st.session_state.selected_opponent = None
    st.session_state.manual_last_team = selected_team
    st.session_state.selected_team = selected_team

    # Opponent selector — filters out the selected team itself
    opp_options = ["— No opponent selected —"] + [t for t in team_list if t != selected_team]
    prev_opp = st.session_state.get("selected_opponent")
    prev_idx = opp_options.index(prev_opp) if prev_opp and prev_opp in opp_options else 0
    selected_opponent_raw = st.selectbox(
        "Opponent",
        opp_options,
        index=prev_idx,
        key="opponent_selector",
    )
    selected_opponent = None if selected_opponent_raw.startswith("—") else selected_opponent_raw
    st.session_state.selected_opponent = selected_opponent

    _tc = TEAM_COLORS.get(selected_team, "#4a90e2")
    st.markdown(
        f'<div style="height:3px;background:{_tc};border-radius:2px;margin:4px 0 12px 0"></div>',
        unsafe_allow_html=True,
    )
    st.markdown("**Data Sources**")
    st.caption("Stats & play-by-play: Sportradar (Snowflake)")
    st.caption("Injuries: Official WNBA PDF + Sportradar")
    st.caption("Auto-refreshes every 4 hours.")

    st.markdown("---")
    show_charts = st.checkbox("Show quarter breakdown", value=True)
    show_delta = st.checkbox("Show delta vs baseline", value=False)
    export_excel = st.button("Export to Excel", use_container_width=True)


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------

_team_color = TEAM_COLORS.get(selected_team, "#4a90e2")
st.markdown(
    f'<div style="border-left:5px solid {_team_color};padding-left:12px;margin-bottom:4px">'
    f'<span style="font-size:1.6rem;font-weight:800">{selected_team}</span>'
    f'<span style="font-size:1rem;opacity:0.6;margin-left:10px">Minutes Projections</span>'
    f'</div>',
    unsafe_allow_html=True,
)

with st.spinner("Loading team data..."):
    team_data = dict(load_team(selected_team))
    injuries = load_injuries()
    lineup_info = load_lineup_info(selected_team)

# Pull and strip metadata before passing team_data to the model.
# The model only expects player-keyed dicts; __meta__ would cause a KeyError.
_meta = team_data.pop("__meta__", {})
_rotation_depth  = _meta.get("rotation_depth", 8)
_bench_slots     = _meta.get("bench_slots", 3)
_last_updated    = _meta.get("last_updated", "")[:10]
_games_processed = _meta.get("games_processed", 0)
_role_avg_starter = _meta.get("role_avg_starter", 0.0)
_role_avg_bench   = _meta.get("role_avg_bench", 0.0)
_team_name_meta   = _meta.get("team_name", selected_team)

# Inject team-wide constants into each player dict so model.py can read them.
# Also store team name so redistribution can fetch Snowflake without-player averages.
team_data["__team_name__"] = _team_name_meta
for _pname in team_data:
    if isinstance(team_data[_pname], dict):
        team_data[_pname]["role_avg_starter"] = _role_avg_starter
        team_data[_pname]["role_avg_bench"]   = _role_avg_bench
        team_data[_pname]["rotation_depth"]   = _rotation_depth

# Inject live injury statuses — always overrides cached team data.
# The injury report is fresher than the season stats cache so it always wins.
for player, inj_info in injuries.items():
    if player in team_data:
        team_data[player]["status"]   = inj_info.get("status", "Active")
        team_data[player]["injury"]   = inj_info.get("injury", "")
        team_data[player]["dnp_type"] = inj_info.get("dnp_type", "injury")

# ---------------------------------------------------------------------------
# Lineup source banner + schedule context
# ---------------------------------------------------------------------------

# Fetch schedule context (prev result + next game) — purely display, no model impact
_sched = load_schedule_context(selected_team)
_prev  = _sched.get("prev")
_next  = _sched.get("next")

def _schedule_line() -> str:
    """Build a one-line schedule context string for the banner."""
    parts = []
    if _prev:
        icon = "&#9989;" if _prev.get("win") else "&#10060;"
        loc  = "vs" if _prev.get("home") else "@"
        parts.append(
            f'{icon} Last ({_prev["date"]}): {loc} {_prev["opponent"]} '
            f'<strong>{_prev["team_score"]}-{_prev["opp_score"]}</strong>'
        )
    if _next:
        loc  = "vs" if _next.get("home") else "@"
        time = _next.get("game_time") or ""
        time_str = f', {time}' if time else ""
        parts.append(
            f'&#128197; Next ({_next["date"]}): {loc} {_next["opponent"]}{time_str}'
        )
    return '&nbsp;&nbsp;<span style="opacity:0.35">|</span>&nbsp;&nbsp;'.join(parts) if parts else ""

_sched_html = _schedule_line()

# Back-to-back detection: today's game is the second night of a back-to-back.
# Requires next game date == today AND prev game date == yesterday.
_is_b2b = False
if _prev and _next:
    try:
        from datetime import date as _date, datetime as _dt
        _today     = _date.today()
        _yesterday = _today - __import__('datetime').timedelta(days=1)
        _d_prev    = _dt.strptime(_prev["date"], "%Y-%m-%d").date()
        _d_next    = _dt.strptime(_next["date"], "%Y-%m-%d").date()
        _is_b2b    = (_d_next == _today and _d_prev == _yesterday)
    except Exception:
        pass

_b2b_note = (
    '<br><span style="color:#e05252;font-size:0.8rem;font-weight:700">'
    '&#9889; Back-to-back — expect more bench, fewer starter minutes</span>'
    if _is_b2b else ""
)

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
    sched_row    = f'<br><span style="font-size:0.78rem;opacity:0.65">{_sched_html}</span>' if _sched_html else ""

    st.markdown(
        f'<div class="{css}">'
        f'<strong>{icon} {label}</strong> (via {source}){opp_str}{time_str}<br>'
        f'<span style="font-size:0.9rem">{starters_str}</span>'
        f'{sched_row}'
        f'{_b2b_note}'
        f'</div>',
        unsafe_allow_html=True,
    )
else:
    _has_game_today = bool(lineup_info.get("game_time") or lineup_info.get("opponent"))
    _sched_row = f'<br><span style="font-size:0.78rem;opacity:0.65">{_sched_html}</span>' if _sched_html else ""
    if _has_game_today:
        _opp   = lineup_info.get("opponent", "")
        _gtime = lineup_info.get("game_time", "")
        _opp_str  = f" vs {_opp}" if _opp else ""
        _time_str = f" — tip-off {_gtime}" if _gtime else ""
        st.markdown(
            f'<div class="banner-projected">'
            f'<strong>⏳ Waiting for lineup{_opp_str}{_time_str}</strong><br>'
            f'<span style="font-size:0.85rem;opacity:0.8">'
            f'Lineup not yet announced. Projecting based on recent rotation — '
            f'check team page or beat writers on X near tip-off for confirmation.'
            f'</span>'
            f'{_sched_row}'
            f'{_b2b_note}'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        _no_game_html = (
            '<div class="banner-stats">'
            '📅 No game today. Projections based on most recent rotation.'
            + (_sched_row)
            + '</div>'
        )
        st.markdown(_no_game_html, unsafe_allow_html=True)

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

# Inject manually added players into team_data BEFORE building player lists
# so they appear in the status grid and projection on the same render.
# session_state.manual_added_players is populated by the expander on the prior run.
if "manual_added_players" not in st.session_state:
    st.session_state.manual_added_players = {}
for _rid, _entry in st.session_state.manual_added_players.items():
    _name = _entry["name"]
    _season_info = next(
        (v for k, v in team_data.items()
         if k == _name and isinstance(v, dict)), {}
    )
    if _season_info.get("dnp_rate", 0) >= 0.40 and _entry.get("status") == "Active":
        _entry = dict(_entry)
        _entry["status"] = "Out"
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
        team_data[_name]["last_game_min"]    = None   # suppress last1 signal so model uses entered value
        team_data[_name]["games_played"]     = 1      # forces _weighted_minutes to use entered value directly
        team_data[_name]["pos"]              = _entry["pos"]
        team_data[_name]["role"]             = _entry["role"]
        team_data[_name]["status"]           = _entry["status"]
        team_data[_name]["zero_min_season"]  = False

# Separate players by category so the UI is scannable:
#  1. Active / injured players (relevant to tonight)
#  2. Zero-minute-season players (never played this year — auto-Out)
# Filter to only dict entries — skip internal keys like __team_name__
player_names = [k for k, v in team_data.items() if isinstance(v, dict)]

_manually_added_names = {e["name"] for e in st.session_state.manual_added_players.values() if e.get("name")}
zero_min_players = [p for p in player_names if team_data[p].get("zero_min_season") and p not in _manually_added_names]
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

_expander_open = bool(st.session_state.manual_added_players)
with st.expander("+ Add / override players manually", expanded=_expander_open):
    manual_options = ["— select player —"] + _load_all_players()

    # Header row
    hdr1, hdr2, hdr3, hdr4, hdr5, hdr_del = st.columns([3, 1.2, 1.5, 1.5, 1.5, 0.8])
    hdr1.caption("Player")
    hdr2.caption("Pos")
    hdr3.caption("Role")
    hdr4.caption("Mins")
    hdr5.caption("Status")
    hdr_del.caption("Remove")

    rows_to_delete = []

    for row_i, rid in enumerate(st.session_state.manual_row_ids):
        min_key     = f"manual_min_val_{rid}"
        min_txt_key = f"manual_min_text_{rid}"
        pending_key = f"manual_min_pending_{rid}"

        if pending_key in st.session_state:
            st.session_state[min_key]     = st.session_state.pop(pending_key)
            st.session_state[min_txt_key] = str(st.session_state[min_key])

        if min_key not in st.session_state:
            st.session_state[min_key]     = 10
            st.session_state[min_txt_key] = "10"

        # Row 1: all main widgets at equal height — ✕ aligns with the dropdowns
        mc1, mc2, mc3, mc4, mc5, mc_del = st.columns([3, 1.2, 1.5, 1.5, 1.5, 0.8])
        with mc1:
            pick_col, clear_col = st.columns([5, 1])
            with pick_col:
                manual_pick = st.selectbox("Player", manual_options, key=f"manual_pick_{rid}",
                                           label_visibility="collapsed")
            with clear_col:
                if manual_pick != "— select player —":
                    if st.button("✕", key=f"clear_pick_{rid}", help="Clear player"):
                        st.session_state[f"manual_pick_{rid}"] = "— select player —"
                        st.rerun()
            effective_name = manual_pick if manual_pick != "— select player —" else ""
        with mc2:
            manual_pos = st.selectbox("Pos", POSITIONS, key=f"manual_pos_{rid}",
                                      label_visibility="collapsed")
        with mc3:
            manual_role = st.selectbox("Role", ["starter", "bench"], key=f"manual_role_{rid}",
                                       label_visibility="collapsed")
        with mc4:
            typed = st.text_input("Mins", key=min_txt_key, label_visibility="collapsed")
            try:
                manual_min = max(0, min(40, int(typed)))
                st.session_state[min_key] = manual_min
            except ValueError:
                manual_min = st.session_state[min_key]
        with mc5:
            manual_status = st.selectbox("Status", get_status_options(), key=f"manual_status_{rid}",
                                         label_visibility="collapsed")
        with mc_del:
            if st.button("✕", key=f"del_row_{rid}", use_container_width=True):
                rows_to_delete.append(rid)

        # Row 2: − and + buttons sit directly under the Mins input
        _, _, _, mc4b, _, _ = st.columns([3, 1.2, 1.5, 1.5, 1.5, 0.8])
        with mc4b:
            btn_minus, btn_plus = st.columns(2)
            with btn_minus:
                if st.button("−", key=f"min_minus_{rid}", use_container_width=True):
                    st.session_state[pending_key] = max(0, st.session_state[min_key] - 1)
                    st.rerun()
            with btn_plus:
                if st.button("+", key=f"min_plus_{rid}", use_container_width=True):
                    st.session_state[pending_key] = min(40, st.session_state[min_key] + 1)
                    st.rerun()

        # Register or clear this player entry on every render
        if effective_name and rid not in rows_to_delete:
            st.session_state.manual_added_players[rid] = {
                "name":   effective_name,
                "pos":    manual_pos,
                "role":   manual_role,
                "min":    manual_min,
                "status": manual_status,
            }
            st.success(f"Added: {effective_name} — {manual_pos}, {manual_role}, {manual_min} min")
        elif not effective_name:
            st.session_state.manual_added_players.pop(rid, None)
            rows_to_delete.append(rid)

        if row_i < len(st.session_state.manual_row_ids) - 1:
            st.markdown("---")

    if st.button("+ Add another player", key="add_row_btn"):
        st.session_state.manual_row_ids.append(st.session_state.manual_next_id)
        st.session_state.manual_next_id += 1
        st.rerun()

    if rows_to_delete:
        for r in rows_to_delete:
            st.session_state.pop(f"manual_min_val_{r}", None)
            st.session_state.pop(f"manual_min_text_{r}", None)
            st.session_state.pop(f"manual_min_pending_{r}", None)
            st.session_state.pop(f"manual_pick_{r}", None)
            st.session_state.pop(f"clear_pick_{r}", None)
            st.session_state.pop(f"manual_pos_{r}", None)
            st.session_state.pop(f"manual_role_{r}", None)
            st.session_state.pop(f"manual_status_{r}", None)
            st.session_state.manual_added_players.pop(r, None)
        st.session_state.manual_row_ids = [r for r in st.session_state.manual_row_ids if r not in rows_to_delete]
        if not st.session_state.manual_row_ids:
            st.session_state.manual_row_ids = [st.session_state.manual_next_id]
            st.session_state.manual_next_id += 1
        st.rerun()

# Full sync pass after expander — handles both first-add and minute changes.
# The early inject only saw last render's session_state; this pass sees the current one.
for _rid, _entry in st.session_state.manual_added_players.items():
    _name = _entry["name"]
    if not _name:
        continue
    _season_info = next(
        (v for k, v in team_data.items() if k == _name and isinstance(v, dict)), {}
    )
    _eff_status = _entry["status"]
    if _season_info.get("dnp_rate", 0) >= 0.40 and _eff_status == "Active":
        _eff_status = "Out"
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
            "status":           _eff_status,
            "injury":           "",
            "zero_min_season":  False,
            "recently_active":  True,
        }
    else:
        team_data[_name]["avg_min"]         = _entry["min"]
        team_data[_name]["last3_avg"]       = _entry["min"]
        team_data[_name]["clean_avg_min"]   = _entry["min"]
        team_data[_name]["last3_clean_avg"] = _entry["min"]
        team_data[_name]["last_game_min"]   = None   # suppress last1 signal
        team_data[_name]["games_played"]    = 1      # forces _weighted_minutes to use entered value
        team_data[_name]["role"]            = _entry["role"]
        team_data[_name]["pos"]             = _entry["pos"]
        team_data[_name]["status"]          = _eff_status
        team_data[_name]["zero_min_season"] = False
    # Always write player_statuses — overrides any auto-Out from zero_min_season
    player_statuses[_name] = player_statuses.get(_name, _eff_status)

st.markdown("---")

# ---------------------------------------------------------------------------
# Adjusted Lineup
# ---------------------------------------------------------------------------

# Fetch opponent pace from Snowflake when an opponent is selected
_opp_pace = 0.0
if selected_opponent:
    pass  # opponent pace via Snowflake not available; CSVs are primary data source

adjusted_lineup = apply_scenario(team_data, player_statuses, {}, role_overrides, opp_pace=_opp_pace)
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

# Confidence legend — dots match what appears in the Conf column
st.markdown(
    '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap;font-size:0.75rem">'
    '<span style="font-weight:600;opacity:0.8;white-space:nowrap">Conf &#9679;:</span>'
    '<span style="color:#28a745;font-size:1rem;line-height:1">&#9679;</span>'
    '<span style="white-space:nowrap;opacity:0.8">HIGH — consistent role &amp; sample</span>'
    '<span style="opacity:0.35">&nbsp;|&nbsp;</span>'
    '<span style="color:#e6a817;font-size:1rem;line-height:1">&#9679;</span>'
    '<span style="white-space:nowrap;opacity:0.8">MED — some variability</span>'
    '<span style="opacity:0.35">&nbsp;|&nbsp;</span>'
    '<span style="color:#dc3545;font-size:1rem;line-height:1">&#9679;</span>'
    '<span style="white-space:nowrap;opacity:0.8">LOW — small sample or injury</span>'
    '</div>',
    unsafe_allow_html=True,
)

# Show matchup context banner when an opponent is selected
if selected_opponent and matchup_summary:
    conf       = matchup_summary.get("confidence", "low")
    conf_color = {"low": "#6c757d", "medium": "#ffc107", "high": "#28a745"}.get(conf, "#6c757d")
    h2h_games  = matchup_summary.get("h2h_games", 0)
    opp_depth  = matchup_summary.get("opp_depth", 8)
    blowout_count  = matchup_summary.get("blowout_count", 0)
    blowout_sample = matchup_summary.get("blowout_sample", 0)
    blowout_str = f"{blowout_count}/{blowout_sample} blowouts" if blowout_sample else "—"

    h2h_label = f"{h2h_games} H2H game{'s' if h2h_games != 1 else ''} this season" if h2h_games else "No H2H games yet"
    notes_html = "".join(f"<li style='margin-bottom:3px'>{n}</li>" for n in matchup_summary.get("notes", []))
    st.markdown(
        f'<div class="matchup-banner" style="border-left-color:{conf_color}">'
        f'<strong>vs {selected_opponent}</strong> &nbsp;|&nbsp; '
        f'<span style="color:{conf_color};font-weight:600">{h2h_label}</span>'
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
adj_col_label = f"vs {_OPP_ABBREV.get(selected_opponent, selected_opponent[:3].upper())}" if selected_opponent else "Adj"

# Column headers — 8 cols: name, status, last, wtd, proj, conf, adj, note
hc = st.columns([3, 2.4, 1, 1.2, 1.4, 1.1, 1.5, 1.8])
_hs = "white-space:nowrap;cursor:help;border-bottom:1px dotted;text-decoration:none"
hc[0].markdown('<span style="white-space:nowrap"><b>Player</b></span>', unsafe_allow_html=True)
hc[1].markdown('<span style="white-space:nowrap"><b>Status</b></span>', unsafe_allow_html=True)
hc[2].markdown('<span style="white-space:nowrap"><b>Last</b></span>', unsafe_allow_html=True)
hc[3].markdown(f'<span title="Recent-weighted average — emphasizes last few games over the full season" style="{_hs}"><b>Wtd</b></span>', unsafe_allow_html=True)
hc[4].markdown(f'<span title="Projected minutes — sample-size-aware blend of season average and recent games, adjusted for injury status, role, and rotation" style="{_hs}"><b>Proj</b></span>', unsafe_allow_html=True)
hc[5].markdown(f'<div style="text-align:center"><span title="Confidence in this projection — see color key above" style="{_hs}"><b>Conf</b></span></div>', unsafe_allow_html=True)
hc[6].markdown(f'<span title="{"Minutes vs " + selected_opponent + " this season" if selected_opponent else "Minutes gained or lost vs this player\'s normal average due to tonight\'s statuses"}" style="{_hs}"><b>{adj_col_label}</b></span>', unsafe_allow_html=True)
hc[7].markdown('<span style="white-space:nowrap"><b>Note</b></span>', unsafe_allow_html=True)

# Build lookup maps
base_map      = {p.name: p.base_min for p in adjusted_lineup.players}
last_game_map = {name: info.get("last_game_min", 0.0) for name, info in team_data.items() if isinstance(info, dict)}
starters_set  = {p.name for p in adjusted_lineup.players if p.role == "starter"}

COL_WIDTHS = [3.2, 2.4, 1, 1.2, 1.4, 1.1, 1.5, 1.8]


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
st.markdown(
    f'<div style="border-left:3px solid {_team_color};padding-left:8px;font-weight:700;'
    f'font-size:0.9rem;text-transform:uppercase;letter-spacing:0.06em;opacity:0.8;margin-bottom:4px">'
    f'Starters</div>',
    unsafe_allow_html=True,
)
for p in [x for x in adjusted_lineup.players if x.role == "starter" and x.projected_min > 0]:
    c = st.columns(COL_WIDTHS)
    render_player_row(p, base_map[p.name], last_game_map.get(p.name, 0.0), *c, starters_set=starters_set, foul_notes=h2h_foul_notes)
    _render_adj_cell(c[7], p.name, p.projected_min)

# Starter / bench divider
st.markdown('<hr class="role-divider">', unsafe_allow_html=True)

# Bench section
st.markdown(
    f'<div style="border-left:3px solid {_team_color};padding-left:8px;font-weight:700;'
    f'font-size:0.9rem;text-transform:uppercase;letter-spacing:0.06em;opacity:0.8;margin-bottom:4px">'
    f'Bench</div>',
    unsafe_allow_html=True,
)
for p in [x for x in adjusted_lineup.players if x.role == "bench" and x.projected_min > 0]:
    c = st.columns(COL_WIDTHS)
    render_player_row(p, base_map[p.name], last_game_map.get(p.name, 0.0), *c, starters_set=starters_set, foul_notes=h2h_foul_notes)
    _render_adj_cell(c[7], p.name, p.projected_min)

# Out section
if out_players:
    st.markdown('<hr class="role-divider">', unsafe_allow_html=True)
    st.markdown(
        '<div style="padding-left:8px;font-weight:700;font-size:0.9rem;'
        'text-transform:uppercase;letter-spacing:0.06em;opacity:0.5;margin-bottom:4px">'
        'Out</div>',
        unsafe_allow_html=True,
    )
    for p in out_players:
        c = st.columns(COL_WIDTHS)
        render_player_row(p, base_map[p.name], last_game_map.get(p.name, 0.0), *c, starters_set=starters_set, foul_notes=h2h_foul_notes)
        c[7].markdown("—")

st.markdown("---")

# ---------------------------------------------------------------------------
# Matchup minutes summary table
# ---------------------------------------------------------------------------

if selected_opponent and h2h_player_mins:
    opp_full    = selected_opponent
    opp_abbrev  = _OPP_ABBREV.get(opp_full, opp_full[:3].upper())
    n_games     = max((len(v) for v in h2h_player_mins.values()), default=0)

    st.markdown(f'<div class="section-header">Minutes vs {opp_full} This Season</div>', unsafe_allow_html=True)
    st.caption(f"Actual minutes each player logged against {opp_full} this season. Most recent game on the right.")

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
    st.markdown(
        '<div style="font-size:0.72rem;opacity:0.6;margin-bottom:6px;display:flex;gap:10px;flex-wrap:wrap">'
        '<span>Average minutes per quarter from play-by-play data this season.</span>'
        '<span style="white-space:nowrap"><span style="color:#4a90e2">&#9646;</span> Q1</span>'
        '<span style="white-space:nowrap"><span style="color:#7b68ee">&#9646;</span> Q2</span>'
        '<span style="white-space:nowrap"><span style="color:#e67e22">&#9646;</span> Q3</span>'
        '<span style="white-space:nowrap"><span style="color:#27ae60">&#9646;</span> Q4</span>'
        '<span style="white-space:nowrap;opacity:0.7">Faded bar = player typically sits that quarter</span>'
        '</div>',
        unsafe_allow_html=True,
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
                    f'<div style="margin:6px 0;font-size:0.85rem;opacity:0.55">'
                    f'<strong>{p.name}</strong> — no quarter data yet</div>',
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
                f'<span style="font-size:0.78rem;opacity:0.55">Proj: {p.projected_min:.1f} min total</span>'
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
                    f'<div style="font-size:0.7rem;font-weight:600;opacity:0.6;margin-bottom:2px">{Q_LABELS[q]}</div>'
                    f'<div style="background:rgba(128,128,128,0.18);border-radius:4px;height:12px;overflow:hidden">'
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
            '<div style="font-size:0.8rem;font-weight:600;opacity:0.7;margin-bottom:2px">'
            'Combined starter minutes per quarter</div>'
            '<div style="font-size:0.72rem;opacity:0.6;margin-bottom:6px">'
            '🟢 Light load (&lt;32 min) &nbsp;|&nbsp; '
            '🟡 Moderate (32–39 min) &nbsp;|&nbsp; '
            '🔴 Heavy (&ge;40 min) &nbsp;&mdash;&nbsp; max possible: 50 min (5 starters &times; 10)</div>',
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
        inj_rows = []
        for k, v in sorted(injuries.items(), key=lambda x: (x[1].get("team",""), x[0])):
            status = v.get("status", "")
            dnp_type = v.get("dnp_type", "injury")
            # Show "Coach's Decision" instead of the injury field for healthy scratches
            injury_display = (
                "Coach's Decision" if dnp_type == "coach"
                else v.get("injury", "")
            )
            comment = v.get("comment", "")
            inj_rows.append({
                "Player":  k,
                "Team":    v.get("team", "—"),
                "Status":  status,
                "Injury":  injury_display,
                "Note":    comment,
            })
        df_inj = pd.DataFrame(inj_rows)
        st.dataframe(df_inj, use_container_width=True, hide_index=True)
        st.caption(f"Source: Official WNBA injury report + Sportradar · {len(inj_rows)} players listed")
    else:
        st.info("No injury data available.")

# ---------------------------------------------------------------------------
# Model Accuracy Tab
# ---------------------------------------------------------------------------

st.markdown("---")

_tab_accuracy, = st.tabs(["📊  Model Accuracy"])

@st.cache_data(ttl=3600, show_spinner=False)
def _run_backtest_cached(team: str) -> dict:
    try:
        from backtest import run_backtest
        return run_backtest(team, min_games=5)
    except Exception as e:
        return {"error": str(e)}

_METHOD_LABELS = {
    "season_avg":     "Season Avg",
    "last3_median":   "Last 3 Median",
    "last5_median":   "Last 5 Median",
    "last10_median":  "Last 10 Median",
    "ewma":           "EWMA",
    "ewma_context":   "EWMA + Context",
    "weighted_blend": "Weighted Blend ★",
}
_METHOD_ORDER = list(_METHOD_LABELS.keys())

with _tab_accuracy:
    st.markdown("## 📊 Model Accuracy")
    st.markdown(
        "Walk-forward backtests compare 7 forecasting methods on held-out games. "
        "Each prediction uses only data available before that game — no lookahead. "
        "**Powered by Sportradar play-by-play data via Snowflake.**"
    )

    bt_col1, bt_col2 = st.columns([2, 1])
    with bt_col1:
        bt_team = st.selectbox(
            "Team to backtest",
            sorted(list(TEAMS.keys())),
            index=sorted(list(TEAMS.keys())).index(selected_team),
            key="bt_team_select",
        )
    with bt_col2:
        run_bt = st.button("Run Backtest", type="primary", use_container_width=True, key="run_bt_btn")
        all_teams_bt = st.checkbox("All teams (slower)", key="bt_all_teams")

    if run_bt:
        if all_teams_bt:
            with st.spinner("Running backtest across all 15 teams…"):
                from season_stats import ESPN_TEAM_IDS
                from collections import defaultdict
                team_results = {}
                for _t in sorted(ESPN_TEAM_IDS.keys()):
                    _r = _run_backtest_cached(_t)
                    if _r and "summary" in _r:
                        team_results[_t] = _r["summary"]

            if team_results:
                agg_rows = []
                for method in _METHOD_ORDER:
                    maes = [team_results[t][method]["mae"] for t in team_results if method in team_results[t]]
                    if maes:
                        agg_rows.append({
                            "Method":   _METHOD_LABELS[method],
                            "Avg MAE":  round(sum(maes) / len(maes), 2),
                            "Teams":    len(maes),
                        })
                df_agg = pd.DataFrame(agg_rows).sort_values("Avg MAE")
                st.markdown("### Aggregate Results — All Teams")
                st.caption(f"Averaged across {len(team_results)} teams")
                st.dataframe(
                    df_agg.style.format({"Avg MAE": "{:.2f}"}),
                    use_container_width=True, hide_index=True,
                )
                with st.expander("Per-team breakdown (Weighted Blend)"):
                    per_team_rows = []
                    for _t, _summary in sorted(team_results.items()):
                        b = _summary.get("weighted_blend", {})
                        per_team_rows.append({
                            "Team": _t, "MAE": b.get("mae"), "RMSE": b.get("rmse"),
                            "MedAE": b.get("medae"), "≤2min %": b.get("pct_within_2"),
                            "≤4min %": b.get("pct_within_4"), "Bias": b.get("bias"), "n": b.get("n", 0),
                        })
                    st.dataframe(pd.DataFrame(per_team_rows), use_container_width=True, hide_index=True)
        else:
            with st.spinner(f"Running backtest for {bt_team}…"):
                bt_result = _run_backtest_cached(bt_team)

            if "error" in bt_result:
                st.error(f"Backtest failed: {bt_result['error']}")
            elif "summary" in bt_result:
                summary = bt_result["summary"]
                n_samples = summary.get("weighted_blend", {}).get("n", 0)
                st.markdown(f"### {bt_team} — Backtest Results")
                st.caption(
                    f"{n_samples} player-game predictions · walk-forward · "
                    f"Sportradar play-by-play via Snowflake"
                )
                rows = []
                for method in _METHOD_ORDER:
                    s = summary.get(method, {})
                    if not s:
                        continue
                    rows.append({
                        "Method":  _METHOD_LABELS[method],
                        "MAE":     s["mae"],
                        "RMSE":    s["rmse"],
                        "MedAE":   s["medae"],
                        "≤2min %": s["pct_within_2"],
                        "≤4min %": s["pct_within_4"],
                        "Bias":    s["bias"],
                        "n":       s["n"],
                    })
                df_bt = pd.DataFrame(rows)
                st.dataframe(
                    df_bt.style.format({"MAE": "{:.2f}", "RMSE": "{:.2f}", "MedAE": "{:.2f}",
                                        "≤2min %": "{:.1f}%", "≤4min %": "{:.1f}%", "Bias": "{:.2f}"}),
                    use_container_width=True, hide_index=True,
                )
                blend = summary.get("weighted_blend", {})
                if blend:
                    st.markdown("#### Production Model — Weighted Blend ★")
                    m1, m2, m3, m4, m5 = st.columns(5)
                    m1.metric("MAE", f"{blend['mae']:.2f} min")
                    m2.metric("RMSE", f"{blend['rmse']:.2f} min")
                    m3.metric("MedAE", f"{blend['medae']:.2f} min")
                    m4.metric("Within 2 min", f"{blend['pct_within_2']:.1f}%")
                    m5.metric("Within 4 min", f"{blend['pct_within_4']:.1f}%")
                    bias_note = "over-projects" if blend["bias"] > 0.3 else ("under-projects" if blend["bias"] < -0.3 else "well-calibrated")
                    st.caption(f"Bias: {blend['bias']:+.2f} min ({bias_note})")
                records = bt_result.get("records", [])
                if records:
                    df_rec = pd.DataFrame(records)
                    df_rec["error"] = df_rec["pred_blend"] - df_rec["actual"]
                    st.markdown("#### Error Distribution — Weighted Blend")
                    st.caption("Positive = over-projected, negative = under-projected")
                    st.bar_chart(
                        df_rec["error"].round(0).value_counts().sort_index().rename("Count"),
                        use_container_width=True,
                    )
            else:
                st.warning(f"Not enough game data to backtest {bt_team} yet (need 6+ games).")
    else:
        st.info("Select a team and click **Run Backtest** to see accuracy metrics.", icon="📊")
