"""Quick sanity check — run this before launching the Streamlit app."""

from roster_data import ROSTERS, GAME_MINUTES
from model import apply_scenario, minutes_delta_summary

TEAM = "New York Liberty"

def test_baseline():
    team_data = {
        player: {**info, "status": "Active", "injury": ""}
        for player, info in ROSTERS[TEAM].items()
    }
    lineup = apply_scenario(team_data, {}, {})
    total = sum(p.projected_min for p in lineup.players)
    assert abs(total - GAME_MINUTES) < 1.0, f"Total minutes off: {total}"
    print(f"Baseline OK — total: {total:.1f} min across {len(lineup.players)} players")
    for p in sorted(lineup.players, key=lambda x: -x.projected_min):
        print(f"  {p.name:<28} {p.role:<8} {p.projected_min:.1f} min")

def test_sabrina_out():
    team_data = {
        player: {**info, "status": "Active", "injury": ""}
        for player, info in ROSTERS[TEAM].items()
    }
    baseline = apply_scenario(team_data, {}, {})

    adjusted = apply_scenario(team_data, {"Sabrina Ionescu": "Out"}, {})
    deltas = minutes_delta_summary(baseline, adjusted)

    total = sum(p.projected_min for p in adjusted.players)
    sabrina = next((p for p in adjusted.players if p.name == "Sabrina Ionescu"), None)
    assert sabrina and sabrina.projected_min == 0, "Sabrina should be 0 min"
    assert abs(total - GAME_MINUTES) < 1.0, f"Total off after Sabrina out: {total}"
    print(f"\nSabrina OUT — total: {total:.1f} min")
    for name, delta in sorted(deltas.items(), key=lambda x: -x[1]):
        sign = "+" if delta > 0 else ""
        print(f"  {name:<28} {sign}{delta:.1f}")

def test_sabrina_questionable_extended():
    team_data = {
        player: {**info, "status": "Active", "injury": ""}
        for player, info in ROSTERS[TEAM].items()
    }
    baseline = apply_scenario(team_data, {}, {})
    adjusted = apply_scenario(
        team_data,
        {"Sabrina Ionescu": "Questionable"},
        {"Sabrina Ionescu": "extended"},
    )
    sabrina_base = next(p for p in baseline.players if p.name == "Sabrina Ionescu")
    sabrina_adj = next(p for p in adjusted.players if p.name == "Sabrina Ionescu")
    print(f"\nSabrina QUESTIONABLE (extended): {sabrina_base.projected_min:.1f} -> {sabrina_adj.projected_min:.1f} min")
    total = sum(p.projected_min for p in adjusted.players)
    print(f"Total: {total:.1f}")

if __name__ == "__main__":
    test_baseline()
    test_sabrina_out()
    test_sabrina_questionable_extended()
    print("\nAll tests passed.")
