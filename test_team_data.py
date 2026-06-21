import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from wnba_scraper import get_team_data

for team in ["Indiana Fever", "Portland Fire", "Toronto Tempo"]:
    print(f"\n{'='*60}")
    print(f"{team}")
    print('='*60)
    data = get_team_data(team)
    starters = [(k,v) for k,v in data.items() if v["role"] == "starter"]
    bench    = [(k,v) for k,v in data.items() if v["role"] == "bench"]
    print(f"STARTERS ({len(starters)}):")
    for k,v in sorted(starters, key=lambda x: -x[1]["avg_min"]):
        print(f"  {k:<30} avg={v['avg_min']:>5.1f}  start%={v.get('starter_pct',0):.0%}  status={v['status']}")
    print(f"BENCH ({len(bench)}):")
    for k,v in sorted(bench, key=lambda x: -x[1]["avg_min"])[:6]:
        print(f"  {k:<30} avg={v['avg_min']:>5.1f}  start%={v.get('starter_pct',0):.0%}")
