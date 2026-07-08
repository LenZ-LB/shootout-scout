"""
Debug vs-goalie split issues using EDM vs CGY 2025-26 as reference.
Known: 8 EDM shooters vs Dustin Wolf in one game.
Checks: duplicate rows, missing players, PBP goalie extraction.
"""
import requests, json

base_stats = "https://api.nhle.com/stats/rest/en"
base_pbp   = "https://api-web.nhle.com/v1"

KNOWN_SHOOTERS = [
    "mcdavid", "draisaitl", "nugent-hopkins", "tomasek",
    "howard", "savoie", "bouchard", "kapanen"
]

print("=== Fetching ALL 20252026 game-level skater rows ===")
all_rows = []
start = 0
while True:
    url = (f"{base_stats}/skater/shootout"
           f"?isAggregate=false&isGame=true"
           f"&cayenneExp=seasonId=20252026%20and%20gameTypeId=2"
           f"&start={start}&limit=100")
    resp = requests.get(url, timeout=20)
    data = resp.json()
    rows = data.get("data", [])
    all_rows.extend(rows)
    total = data.get("total", 0)
    start += 100
    if start >= total or not rows:
        break

print(f"Total rows: {len(all_rows)}")

# Find our known shooters
print()
print("=== Known EDM shooters in API data ===")
edm_game_ids = set()
for shooter in KNOWN_SHOOTERS:
    matches = [r for r in all_rows if shooter in r.get("skaterFullName", "").lower()]
    if matches:
        for r in matches:
            print(f"  {r['skaterFullName']:25s} gameId={r['gameId']} date={r['gameDate']} shots={r['shootoutShots']} goals={r['shootoutGoals']} opp={r['opponentTeamAbbrev']}")
            edm_game_ids.add(r["gameId"])
    else:
        print(f"  {shooter:25s} NOT FOUND in API data")

# Check for duplicates
print()
print("=== Checking for duplicate rows (same player + gameId) ===")
seen = {}
for r in all_rows:
    key = (r["playerId"], r["gameId"])
    if key in seen:
        print(f"  DUPLICATE: {r['skaterFullName']} gameId={r['gameId']}")
    seen[key] = r

print(f"  No duplicates found" if len(seen) == len(all_rows) else f"  {len(all_rows) - len(seen)} duplicates found")

# Check PBP for the EDM vs CGY game
if edm_game_ids:
    game_id = list(edm_game_ids)[0]
    print()
    print(f"=== PBP for EDM vs CGY game {game_id} ===")
    resp2 = requests.get(f"{base_pbp}/gamecenter/{game_id}/play-by-play", timeout=15)
    pbp = resp2.json()

    home = pbp.get("homeTeam", {})
    away = pbp.get("awayTeam", {})
    print(f"Home: {home.get('abbrev')} (id={home.get('id')})")
    print(f"Away: {away.get('abbrev')} (id={away.get('id')})")

    so_plays = [p for p in pbp.get("plays", [])
                if p.get("periodDescriptor", {}).get("periodType") == "SO"]
    print(f"SO plays: {len(so_plays)}")
    for p in so_plays:
        d = p.get("details", {})
        print(f"  type={p.get('typeDescKey'):20s} shootingTeam={d.get('eventOwnerTeamId')} goalieInNetId={d.get('goalieInNetId')} shootingPlayerId={d.get('shootingPlayerId') or d.get('scoringPlayerId')}")

    goalies = [s for s in pbp.get("rosterSpots", []) if s.get("positionCode") == "G"]
    print(f"Dressed goalies ({len(goalies)}):")
    for g in goalies:
        print(f"  id={g.get('playerId')} teamId={g.get('teamId')} {g.get('firstName',{}).get('default')} {g.get('lastName',{}).get('default')}")
