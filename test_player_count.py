import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from wnba_scraper import get_team_data

for team in ["Indiana Fever", "New York Liberty"]:
    data = get_team_data(team)
    print(f"\n{team} — {len(data)} players total")
    for name, p in sorted(data.items(), key=lambda x: -x[1]["avg_min"]):
        print(f"  {name:<30} avg={p['avg_min']:>5.1f}  gp={p.get('games_played',0):>3}  role={p['role']:<7}  status={p['status']}")
