from season_stats import get_all_games_with_dates, _parse_boxscore, _trimmed_avg, _median, ESPN_TEAM_IDS
from collections import defaultdict
import snowflake_connector as _sf

def _mae(e):  return round(sum(abs(x) for x in e)/len(e),3) if e else 0
def _bias(e): return round(sum(e)/len(e),3) if e else 0
def _p4(e):   return round(100*sum(1 for x in e if abs(x)<=4)/len(e),1) if e else 0
def _p2(e):   return round(100*sum(1 for x in e if abs(x)<=2)/len(e),1) if e else 0

def _base_weights(n, season, last3):
    if n < 5:   ws,wr = 1.00,0.00
    elif n<10:  ws,wr = 0.70,0.30
    elif n<20:  ws,wr = 0.55,0.45
    elif n<30:  ws,wr = 0.40,0.60
    else:       ws,wr = 0.25,0.75
    if season>0 and abs(last3-season)/season>=0.20:
        boost=min(abs(last3-season)/season-0.20,0.15)
        wr=min(wr+boost,0.90); ws=1.0-wr
    return ws, wr

def _finish_blend(season_avg, hist, ws, wr):
    last3 = _median(hist[-3:]) if len(hist)>=3 else season_avg
    last1 = hist[-1] if hist else None
    if last1 and last1>=0.5 and wr>0:
        w1=wr*0.40; w3=wr*0.60
        recent=last3*(w3/(w3+w1))+last1*(w1/(w3+w1))
        return round(season_avg*ws+recent*(w3+w1),1)
    return round(season_avg*ws+last3*wr,1)

def blend_current(hist, margins=None):
    if not hist: return 0.0
    n=len(hist); season=_trimmed_avg(hist)
    last3=_median(hist[-3:]) if n>=3 else season
    ws,wr=_base_weights(n,season,last3)
    return _finish_blend(season, hist, ws, wr)

def blend_blowout_discount(hist, margins):
    if not hist or not margins or len(margins)!=len(hist):
        return blend_current(hist)
    n=len(hist)
    weights=[0.5 if abs(m)>=15 else 1.0 for m in margins]
    w_total=sum(weights)
    season_w=round(sum(h*w for h,w in zip(hist,weights))/w_total,1) if w_total else _trimmed_avg(hist)
    last3=_median(hist[-3:]) if n>=3 else season_w
    ws,wr=_base_weights(n,season_w,last3)
    return _finish_blend(season_w, hist, ws, wr)

def blend_game_type(hist, margins):
    if not hist or not margins or len(margins)!=len(hist):
        return blend_current(hist)
    close,blow_w,blow_l=[],[],[]
    for h,m in zip(hist,margins):
        if abs(m)<10:   close.append(h)
        elif m>=15:     blow_w.append(h)
        elif m<=-15:    blow_l.append(h)
    if not close:
        return blend_current(hist)
    ca=_trimmed_avg(close)
    bw=_trimmed_avg(blow_w) if blow_w else ca
    bl=_trimmed_avg(blow_l) if blow_l else ca
    season_gt=round(ca*0.60+bw*0.20+bl*0.20,1)
    n=len(hist); last3=_median(hist[-3:]) if n>=3 else season_gt
    ws,wr=_base_weights(n,season_gt,last3)
    return _finish_blend(season_gt, hist, ws, wr)

def pred_norm_fix(pred, is_starter):
    if is_starter: return round(pred+0.72,1)
    return round(max(pred-0.87,1.0),1)

# combined: blowout_disc + game_type season avg + norm fix
def blend_combined(hist, margins, is_starter):
    if not hist: return 0.0
    n=len(hist)
    if margins and len(margins)==n:
        # game-type weighted season avg
        close,blow_w,blow_l=[],[],[]
        for h,m in zip(hist,margins):
            if abs(m)<10:   close.append(h)
            elif m>=15:     blow_w.append(h)
            elif m<=-15:    blow_l.append(h)
        if close:
            ca=_trimmed_avg(close)
            bw=_trimmed_avg(blow_w) if blow_w else ca
            bl=_trimmed_avg(blow_l) if blow_l else ca
            season_base=round(ca*0.60+bw*0.20+bl*0.20,1)
        else:
            weights=[0.5 if abs(m)>=15 else 1.0 for m in margins]
            w_total=sum(weights)
            season_base=round(sum(h*w for h,w in zip(hist,weights))/w_total,1) if w_total else _trimmed_avg(hist)
    else:
        season_base=_trimmed_avg(hist)
    last3=_median(hist[-3:]) if n>=3 else season_base
    ws,wr=_base_weights(n,season_base,last3)
    pred=_finish_blend(season_base, hist, ws, wr)
    return pred_norm_fix(pred, is_starter)

sf_avail = _sf.is_available()
errors = {k:[] for k in ["current","blowout","gametype","normfix","combined",
                          "sc","sb","bc","bb","sg","bg","sn","bn","sComb","bComb"]}

for team in sorted(ESPN_TEAM_IDS.keys()):
    team_id=ESPN_TEAM_IDS[team]
    games=get_all_games_with_dates(team)
    if len(games)<6: continue
    boxscores={gid:_parse_boxscore(gid,team_id) for gid,_ in games}
    try:
        if sf_avail:
            sf_m=_sf.get_team_margins(team)
            margin_map={games[i][0]:sf_m[i] for i in range(min(len(games),len(sf_m)))}
        else:
            margin_map={}
    except Exception:
        margin_map={}

    ph=defaultdict(list); pm=defaultdict(list)
    for gid,_ in games:
        for p in boxscores[gid]:
            if not p["dnp"] and p["minutes"]>=0.5:
                ph[p["name"]].append((gid,p["minutes"]))
                pm[p["name"]].append((gid,margin_map.get(gid,0.0)))

    for ti in range(5,len(games)):
        tgid,_=games[ti]
        tr={gid for gid,_ in games[:ti]}
        for p in boxscores.get(tgid,[]):
            if p["dnp"] or p["minutes"]<0.5: continue
            hist=[m for gid,m in ph[p["name"]] if gid in tr]
            marg=[m for gid,m in pm[p["name"]] if gid in tr]
            if len(hist)<3: continue
            actual=p["minutes"]; is_s=p.get("starter",False)
            pc=blend_current(hist); pb=blend_blowout_discount(hist,marg)
            pg=blend_game_type(hist,marg); pn=pred_norm_fix(pc,is_s)
            pco=blend_combined(hist,marg,is_s)
            for k,v in [("current",pc),("blowout",pb),("gametype",pg),("normfix",pn),("combined",pco)]:
                errors[k].append(v-actual)
            sk="s" if is_s else "b"
            for k,v in [(sk+"c",pc),(sk+"b",pb),(sk+"g",pg),(sk+"n",pn),(sk+"Comb",pco)]:
                errors[k].append(v-actual)

print("=== OVERALL (all players) ===")
print(f'{"Method":<16} {"MAE":>6} {"<=2%":>7} {"<=4%":>7} {"Bias":>7}  n={len(errors["current"])}')
print("-"*56)
for k,l in [("current","Current"),("blowout","Blowout disc"),("gametype","Game-type"),
            ("normfix","Norm fix"),("combined","All combined")]:
    e=errors[k]
    print(f'{l:<16} {_mae(e):>6} {_p2(e):>7}% {_p4(e):>7}% {_bias(e):>7}')

print()
print("=== STARTERS ===")
print(f'{"Method":<16} {"MAE":>6} {"<=4%":>7} {"Bias":>7}  n={len(errors["sc"])}')
print("-"*44)
for k,l in [("sc","Current"),("sb","Blowout disc"),("sg","Game-type"),("sn","Norm fix"),("sComb","All combined")]:
    e=errors[k]
    print(f'{l:<16} {_mae(e):>6} {_p4(e):>7}% {_bias(e):>7}')

print()
print("=== BENCH ===")
print(f'{"Method":<16} {"MAE":>6} {"<=4%":>7} {"Bias":>7}  n={len(errors["bc"])}')
print("-"*44)
for k,l in [("bc","Current"),("bb","Blowout disc"),("bg","Game-type"),("bn","Norm fix"),("bComb","All combined")]:
    e=errors[k]
    print(f'{l:<16} {_mae(e):>6} {_p4(e):>7}% {_bias(e):>7}')
