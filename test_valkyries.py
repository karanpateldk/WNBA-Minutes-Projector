import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from wnba_scraper import get_team_data, get_all_injuries
from model import apply_scenario

for team in ["Golden State Valkyries", "New York Liberty", "Indiana Fever"]:
    data = get_team_data(team)
    inj = get_all_injuries()
    for p, v in inj.items():
        if p in data and data[p]["status"] == "Active":
            data[p]["status"] = v["status"]

    lineup = apply_scenario(data, {}, {})
    total = sum(p.projected_min for p in lineup.players)
    print(f"\n{'='*60}")
    print(f"{team} — total: {total:.1f} min")
    print(f"{'='*60}")
    active = [p for p in lineup.players if p.projected_min > 0]
    for p in sorted(active, key=lambda x: -x.projected_min):
        print(f"  {p.name:<30} {p.projected_min:>5.1f}  {p.role:<8}  {p.status}")
