import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
}

r = requests.get(
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/5/schedule",
    headers=HEADERS, timeout=12
)
d = r.json()

out = []
for e in d.get("events", [])[:30]:
    comp = e.get("competitions", [{}])[0]
    done = comp.get("status", {}).get("type", {}).get("completed", False)
    season = e.get("season", {})
    out.append(
        f"{e.get('date','')[:10]}  "
        f"season_year={season.get('year','?')}  "
        f"season_type={season.get('type','?')}  "
        f"season_slug={season.get('slug','?')}  "
        f"completed={done}  "
        f"id={e.get('id','')}"
    )

result = "\n".join(out)
print(result)

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_season_type_out.txt"), "w") as f:
    f.write(result)
