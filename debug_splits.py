"""
Debug why vs-goalie splits are coming up empty.
Fetches a small sample and tests the join manually.
"""
import requests, json

base = "https://api.nhle.com/stats/rest/en"
season = "20242025"

print("=== GOALIE game rows (first 5) ===")
url = (f"{base}/goalie/shootout"
       f"?isAggregate=false&isGame=true"
       f"&cayenneExp=seasonId={season}%20and%20gameTypeId=2"
       f"&start=0&limit=5")
resp = requests.get(url, timeout=20)
d = resp.json()
print(f"Total goalie rows: {d.get('total')}")
goalie_rows = d.get('data', [])
for r in goalie_rows[:3]:
    print(f"  gameId={r['gameId']} teamAbbrev={r.get('teamAbbrev')} goalie={r.get('goalieFullName')}")

# Build goalie map from all rows
print()
print("=== Building goalie map from ALL goalie rows ===")
all_goalie = []
start = 0
while True:
    url = (f"{base}/goalie/shootout"
           f"?isAggregate=false&isGame=true"
           f"&cayenneExp=seasonId={season}%20and%20gameTypeId=2"
           f"&start={start}&limit=100")
    resp = requests.get(url, timeout=20)
    data = resp.json()
    rows = data.get('data', [])
    all_goalie.extend(rows)
    if start + 100 >= data.get('total', 0):
        break
    start += 100

print(f"Total goalie rows fetched: {len(all_goalie)}")
goalie_map = {}
for r in all_goalie:
    key = (r['gameId'], r.get('teamAbbrev',''))
    goalie_map[key] = (r['playerId'], r.get('goalieFullName',''))
print(f"Unique (gameId, team) keys: {len(goalie_map)}")
if goalie_map:
    sample_key = list(goalie_map.keys())[0]
    print(f"Sample key: {sample_key} -> {goalie_map[sample_key]}")

print()
print("=== SKATER game rows (first 5) ===")
url2 = (f"{base}/skater/shootout"
        f"?isAggregate=false&isGame=true"
        f"&cayenneExp=seasonId={season}%20and%20gameTypeId=2"
        f"&start=0&limit=5")
resp2 = requests.get(url2, timeout=20)
d2 = resp2.json()
print(f"Total skater rows: {d2.get('total')}")
for r in d2.get('data', [])[:3]:
    print(f"  gameId={r['gameId']} team={r.get('teamAbbrev')} opp={r.get('opponentTeamAbbrev')} shooter={r.get('skaterFullName')} shots={r.get('shootoutShots')}")
    # Test the join
    key = (r['gameId'], r.get('opponentTeamAbbrev',''))
    match = goalie_map.get(key)
    print(f"    -> join key={key} match={match}")
