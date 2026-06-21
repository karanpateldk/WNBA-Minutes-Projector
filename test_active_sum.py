import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from wnba_scraper import get_team_data, get_all_injuries

data = get_team_data("New York Liberty")
inj = get_all_injuries()
for p, v in inj.items():
    if p in data and data[p]["status"] == "Active":
        data[p]["status"] = v["status"]

active = {k: v for k, v in data.items() if v["status"] not in ("DNP", "Out", "Doubtful")}
print(f"Active players: {len(active)}")
print(f"Sum of avg_min: {sum(v['avg_min'] for v in active.values()):.1f}")
for name, p in sorted(active.items(), key=lambda x: -x[1]["avg_min"]):
    print(f"  {name:<30} avg={p['avg_min']:>5.1f}  status={p['status']}")
