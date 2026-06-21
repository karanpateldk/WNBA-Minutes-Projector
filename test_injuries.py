import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

import ast
for f in ["app.py", "wnba_scraper.py", "season_stats.py", "model.py"]:
    ast.parse(open(f, encoding="utf-8").read())
    print(f + " OK")

from wnba_scraper import scrape_wnba_injuries
inj = scrape_wnba_injuries()
print(f"\nInjuries loaded: {len(inj)} players")
for name, info in sorted(inj.items()):
    print(f"  {name:<30} {info['status']:<15} {info['team']}")
