import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from wnba_scraper import get_team_data, get_all_injuries
from model import _weighted_minutes, _apply_injury_scale

data = get_team_data("New York Liberty")
inj = get_all_injuries()
for p, v in inj.items():
    if p in data and data[p]["status"] == "Active":
        data[p]["status"] = v["status"]

active = {k: v for k, v in data.items() if v["status"] not in ("DNP", "Out", "Doubtful")}
print(f"Active players: {len(active)}")
total_base = 0
for name, p in sorted(active.items(), key=lambda x: -x[1]["avg_min"]):
    base = _weighted_minutes(p["avg_min"], p.get("last3_avg", p["avg_min"]), p.get("games_played", 0))
    role = p["role"]
    gp = p.get("games_played", 0)
    if role == "bench" and gp <= 2:
        base = min(base, 18.0)
    total_base += base
    print(f"  {name:<30} avg={p['avg_min']:>5.1f} l3={p.get('last3_avg',0):>5.1f} gp={gp:>3}  base={base:>5.1f}  {role}")
print(f"\nTotal base: {total_base:.1f}  Scale to 200: {200/total_base:.3f}")
print(f"\nAfter normalization:")
for name, p in sorted(active.items(), key=lambda x: -x[1]["avg_min"]):
    base = _weighted_minutes(p["avg_min"], p.get("last3_avg", p["avg_min"]), p.get("games_played", 0))
    if p["role"] == "bench" and p.get("games_played", 0) <= 2:
        base = min(base, 18.0)
    scaled = round(base * (200 / total_base), 1)
    print(f"  {name:<30} {scaled:>5.1f}")
