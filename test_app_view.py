"""Simulates exactly what the app does and shows the final player list."""
import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from wnba_scraper import get_team_data, get_all_injuries

for team in ["New York Liberty", "Indiana Fever", "Minnesota Lynx"]:
    team_data = get_team_data(team)
    injuries  = get_all_injuries()

    # Replicate exactly what app.py does
    for player, inj_info in injuries.items():
        if player in team_data:
            if not team_data[player].get("status") or team_data[player]["status"] == "Active":
                team_data[player]["status"] = inj_info.get("status", "Active")
            team_data[player]["injury"] = inj_info.get("injury", "")

    print(f"\n{'='*65}")
    print(f"{team} — {len(team_data)} players shown in app")
    print(f"{'='*65}")
    print(f"  {'Player':<30} {'Avg':>5} {'GP':>4} {'Role':<8} {'Status'}")
    for name, p in sorted(team_data.items(), key=lambda x: (x[1]["status"]!="Active", -x[1]["avg_min"])):
        print(f"  {name:<30} {p['avg_min']:>5.1f} {p['games_played']:>4} {p['role']:<8} {p['status']}")
