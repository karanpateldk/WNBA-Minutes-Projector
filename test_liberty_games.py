import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from season_stats import get_all_game_ids, _parse_boxscore, ESPN_TEAM_IDS

team_id = ESPN_TEAM_IDS["New York Liberty"]
game_ids = get_all_game_ids("New York Liberty")
print(f"Last 3 games for NY Liberty (team_id={team_id}):")
for gid in game_ids[-3:]:
    box = _parse_boxscore(gid, team_id)
    print(f"\n  Game {gid}:")
    for p in sorted(box, key=lambda x: -x["minutes"]):
        if p["minutes"] > 0:
            s = "START" if p["starter"] else "bench"
            print(f"    {p['name']:<30} {p['minutes']:>5.1f}  {s}")
