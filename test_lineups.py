import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from wnba_scraper import get_lineup_for_team

teams = ["Indiana Fever", "Atlanta Dream", "Minnesota Lynx", "Golden State Valkyries", "New York Liberty"]

for team in teams:
    lineup = get_lineup_for_team(team)
    if lineup:
        inj = lineup.get("game_injuries", {})
        inj_str = str({k: v["status"] for k, v in inj.items()}) if inj else "none"
        print(team)
        print("  Source:", lineup.get("source"))
        print("  Confirmed:", lineup.get("confirmed"))
        print("  Opponent:", lineup.get("opponent"), "|", lineup.get("game_time"))
        print("  Starters:", lineup.get("starters"))
        print("  Game injuries:", inj_str)
    else:
        print(team, "- no game today")
    print()
