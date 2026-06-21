import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from wnba_scraper import get_team_data, get_all_injuries
from model import apply_scenario

for team in ["Indiana Fever", "New York Liberty"]:
    team_data = get_team_data(team)
    injuries  = get_all_injuries()
    for player, inj_info in injuries.items():
        if player in team_data:
            if not team_data[player].get("status") or team_data[player]["status"] == "Active":
                team_data[player]["status"] = inj_info.get("status", "Active")

    lineup = apply_scenario(team_data, {}, {})
    total  = sum(p.projected_min for p in lineup.players)

    print(f"\n{team} — {len(lineup.players)} players, {total:.1f} total min")
    print(f"  {'Player':<30} {'Proj':>6}  Status")
    for p in lineup.players:
        if p.projected_min > 0:
            print(f"  {p.name:<30} {p.projected_min:>6.1f}  {p.status}")
    dnp = [p for p in lineup.players if p.projected_min == 0]
    if dnp:
        print(f"  --- DNP/Out ({len(dnp)}) ---")
        for p in dnp:
            print(f"  {p.name:<30}    0.0  {p.status}")
