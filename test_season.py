import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from season_stats import rebuild_team

print("Building Indiana Fever season stats from all completed games...")
data = rebuild_team("Indiana Fever", force=True)
print(f"Games processed: {data['games_processed']}")
print(f"Most recent starters: {data['most_recent_starters']}")
print()
print(f"{'Player':<30} {'AvgMin':>7} {'L3Avg':>7} {'GP':>4} {'Start%':>7}  Q1   Q2   Q3   Q4")
for name, p in sorted(data["players"].items(), key=lambda x: -x[1]["avg_min"]):
    q = p["quarter_avgs"]
    print(f"{name:<30} {p['avg_min']:>7.1f} {p['last3_avg']:>7.1f} {p['games_played']:>4} {p['starter_pct']:>7.0%}  {q.get(1,0):>3.1f}  {q.get(2,0):>3.1f}  {q.get(3,0):>3.1f}  {q.get(4,0):>3.1f}")
