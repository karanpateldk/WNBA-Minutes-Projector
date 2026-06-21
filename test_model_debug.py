import sys, os
sys.path.insert(0, r"C:\Users\kar.patel\wnba_minutes")
os.chdir(r"C:\Users\kar.patel\wnba_minutes")

from wnba_scraper import get_team_data, get_all_injuries
from model import _weighted_minutes, _apply_injury_scale, _redistribute_minutes, _normalize_to_total, PlayerProjection, GAME_MINUTES

data = get_team_data("New York Liberty")
inj = get_all_injuries()
for p, v in inj.items():
    if p in data and data[p]["status"] == "Active":
        data[p]["status"] = v["status"]

projections = []
out_players = []
for player, info in data.items():
    status = info.get("status", "Active")
    gp = info.get("games_played", 0)
    base_min = _weighted_minutes(info["avg_min"], info.get("last3_avg", info["avg_min"]), gp)
    if info.get("role") == "bench" and gp <= 2:
        base_min = min(base_min, 18.0)
    proj_min = _apply_injury_scale(base_min, status)
    p = PlayerProjection(
        name=player, pos=info["pos"], role=info["role"], depth=info["depth"],
        base_min=round(base_min, 1), projected_min=proj_min,
        status=status, injury=info.get("injury", "")
    )
    projections.append(p)
    if proj_min == 0.0:
        out_players.append((player, info))

active_before = [p for p in projections if p.projected_min > 0]
print(f"BEFORE redistribute: {len(active_before)} active, total={sum(p.projected_min for p in active_before):.1f}")
for p in sorted(active_before, key=lambda x: -x.projected_min):
    print(f"  {p.name:<30} {p.projected_min:>6.1f}  {p.role}  {p.status}")

if out_players:
    projections = _redistribute_minutes(projections, out_players, data)

active_after = [p for p in projections if p.projected_min > 0]
print(f"\nAFTER redistribute: {len(active_after)} active, total={sum(p.projected_min for p in active_after):.1f}")
for p in sorted(active_after, key=lambda x: -x.projected_min):
    print(f"  {p.name:<30} {p.projected_min:>6.1f}  {p.role}  {p.status}")

projections = _normalize_to_total(projections, GAME_MINUTES)
active_final = [p for p in projections if p.projected_min > 0]
print(f"\nFINAL: {len(active_final)} active, total={sum(p.projected_min for p in active_final):.1f}")
for p in sorted(active_final, key=lambda x: -x.projected_min):
    print(f"  {p.name:<30} {p.projected_min:>6.1f}  {p.role}")
