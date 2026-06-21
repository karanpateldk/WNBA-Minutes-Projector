import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from wnba_scraper import scrape_wnba_injuries, get_team_data

# Step 1: what does the injury lookup return directly?
injuries = scrape_wnba_injuries()
print("=== INJURY LOOKUP DIRECT ===")
for name in ["Sabrina Ionescu", "Napheesa Collier", "Courtney Vandersloot", "DiJonai Carrington"]:
    print(f"  {name}: {injuries.get(name, 'NOT FOUND')}")

# Step 2: what does get_team_data return for those players?
print("\n=== get_team_data (NY Liberty) ===")
data = get_team_data("New York Liberty")
for name in ["Sabrina Ionescu", "Courtney Vandersloot", "Marine Fauthoux"]:
    p = data.get(name)
    if p:
        print(f"  {name}: status={p['status']} avg_min={p['avg_min']} gp={p['games_played']}")
    else:
        print(f"  {name}: NOT IN team_data (filtered out)")

print("\n=== get_team_data (Minnesota Lynx) ===")
data2 = get_team_data("Minnesota Lynx")
for name in ["Napheesa Collier", "Dorka Juhasz"]:
    p = data2.get(name)
    if p:
        print(f"  {name}: status={p['status']} avg_min={p['avg_min']} gp={p['games_played']}")
    else:
        print(f"  {name}: NOT IN team_data (filtered out)")
