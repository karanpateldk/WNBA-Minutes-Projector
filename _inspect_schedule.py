import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests

r = requests.get(
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/5/schedule",
    timeout=12
)
d = r.json()
lines = []
for e in d.get("events", [])[:25]:
    comp = e.get("competitions", [{}])[0]
    done = comp.get("status", {}).get("type", {}).get("completed", "?")
    season = e.get("season", {})
    lines.append(
        f"{e.get('date','')[:10]}  yr:{season.get('year','?')}  "
        f"type:{season.get('type','?')}  slug:{season.get('slug','?')}  "
        f"done:{done}  id:{e.get('id','')}"
    )

with open("_schedule_out.txt", "w") as f:
    f.write("\n".join(lines))
print("done")
