import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from wnba_scraper import get_team_data, get_all_injuries
from model import _weighted_minutes, _apply_injury_scale, PlayerProjection

data = get_team_data("New York Liberty")
inj = get_all_injuries()
for p, v in inj.items():
    if p in data and data[p]["status"] == "Active":
        data[p]["status"] = v["status"]

out_players = []
for player, info in data.items():
    status = info.get("status", "Active")
    gp = info.get("games_played", 0)
    base_min = _weighted_minutes(info["avg_min"], info.get("last3_avg", info["avg_min"]), gp)
    if info.get("role") == "bench" and gp <= 2:
        base_min = min(base_min, 18.0)
    proj_min = _apply_injury_scale(base_min, status)
    if proj_min == 0.0 and status in ("Out", "Doubtful"):
        out_players.append((player, info, base_min, status))

print(f"Out players being redistributed: {len(out_players)}")
total_redistributed = 0
for name, info, base, status in sorted(out_players, key=lambda x: -x[2]):
    print(f"  {name:<30} base={base:>5.1f}  status={status}  gp={info.get('games_played',0)}")
    total_redistributed += base
print(f"\nTotal minutes being redistributed: {total_redistributed:.1f}")
