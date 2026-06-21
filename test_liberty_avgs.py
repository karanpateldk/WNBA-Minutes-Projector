import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from season_stats import rebuild_team
data = rebuild_team("New York Liberty", force=False)
print("Most recent starters:", data.get("most_recent_starters"))
players = data["players"]
total = sum(p["avg_min"] for p in players.values())
print(f"\nAll players total avg: {total:.1f}")
print("\nPlayer season averages (sorted):")
for name, p in sorted(players.items(), key=lambda x: -x[1]["avg_min"])[:14]:
    print(f"  {name:<30} avg={p['avg_min']:>5.1f}  l3={p['last3_avg']:>5.1f}  gp={p['games_played']}")
