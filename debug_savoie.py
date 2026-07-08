"""
Targeted debug: check EDM shooters from the known EDM vs CGY game.
Focus on why RNH/Bouchard/Savoie have no splits and why Kapanen has wrong ones.
"""
import requests, json

base_stats = "https://api.nhle.com/stats/rest/en"
base_pbp   = "https://api-web.nhle.com/v1"

TARGETS = {
    8476459: "Ryan Nugent-Hopkins",
    8478402: "Connor McDavid",
    8478444: "Kasperi Kapanen",
    8480803: "Evan Bouchard",
    8483512: "Matt Savoie",
    8477934: "Leon Draisaitl",
}

print("=== Fetching ALL 20252026 game-level skater rows ===")
all_rows = []
start = 0
while True:
    url = (f"{base_stats}/skater/shootout"
           f"?isAggregate=false&isGame=true"
           f"&cayenneExp=seasonId=20252026%20and%20gameTypeId=2"
           f"&start={start}&limit=100")
    import time
    time.sleep(1)
    resp = requests.get(url, timeout=20)
    print(f"  HTTP {resp.status_code} start={start} len={len(resp.text)}")
    if resp.status_code != 200 or not resp.text.strip():
        print(f"  Bad response, stopping: {resp.text[:200]}")
        break
    data = resp.json()
    rows = data.get("data", [])
    all_rows.extend(rows)
    total = data.get("total", 0)
    start += 100
    if start >= total or not rows:
        break

print(f"Total rows fetched: {len(all_rows)}")

print()
print("=== Target player rows ===")
edm_cgy_game = None
for pid, name in TARGETS.items():
    player_rows = [r for r in all_rows if r["playerId"] == pid]
    if player_rows:
        for r in player_rows:
            print(f"  {name:25s} gameId={r['gameId']} date={r['gameDate']} team={r['teamAbbrev']} opp={r['opponentTeamAbbrev']} shots={r['shootoutShots']} goals={r['shootoutGoals']}")
            if r['opponentTeamAbbrev'] == 'CGY':
                edm_cgy_game = r['gameId']
    else:
        print(f"  {name:25s} NOT IN API DATA")

print()
print("=== Kapanen rows in detail ===")
kap_rows = [r for r in all_rows if r["playerId"] == 8478444]
for r in kap_rows:
    print(f"  gameId={r['gameId']} team={r['teamAbbrev']} opp={r['opponentTeamAbbrev']} shots={r['shootoutShots']}")

# Check the EDM vs CGY game PBP
if edm_cgy_game:
    print()
    print(f"=== PBP for EDM vs CGY game {edm_cgy_game} ===")
    resp2 = requests.get(f"{base_pbp}/gamecenter/{edm_cgy_game}/play-by-play", timeout=15)
    pbp = resp2.json()
    home = pbp.get("homeTeam", {})
    away = pbp.get("awayTeam", {})
    print(f"Home: {home.get('abbrev')} id={home.get('id')}")
    print(f"Away: {away.get('abbrev')} id={away.get('id')}")
    so_plays = [p for p in pbp.get("plays", [])
                if p.get("periodDescriptor", {}).get("periodType") == "SO"]
    print(f"SO plays: {len(so_plays)}")
    for p in so_plays:
        d = p.get("details", {})
        print(f"  type={p.get('typeDescKey'):25s} shooterId={d.get('shootingPlayerId') or d.get('scoringPlayerId')} goalieInNetId={d.get('goalieInNetId')} ownerTeamId={d.get('eventOwnerTeamId')}")
    goalies = [s for s in pbp.get("rosterSpots", []) if s.get("positionCode") == "G"]
    print(f"Dressed goalies:")
    for g in goalies:
        print(f"  id={g.get('playerId')} teamId={g.get('teamId')} {g.get('firstName',{}).get('default')} {g.get('lastName',{}).get('default')}")
else:
    print("EDM vs CGY game not found in API rows for these players")
