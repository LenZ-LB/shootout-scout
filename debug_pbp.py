"""
Test PBP goalie extraction for a known shootout game.
Game 2024020240: PHI vs SJS (we know Konecny shot vs Vanecek)
"""
import requests, json

game_id = 2024020240
url = f"https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play"
resp = requests.get(url, timeout=15)
print(f"HTTP: {resp.status_code}")
pbp = resp.json()

print(f"Keys: {list(pbp.keys())}")

home = pbp.get("homeTeam", {})
away = pbp.get("awayTeam", {})
print(f"Home: id={home.get('id')} abbrev={home.get('abbrev')}")
print(f"Away: id={away.get('id')} abbrev={away.get('abbrev')}")

# Find SO plays
plays = pbp.get("plays", [])
so_plays = [p for p in plays if p.get("periodDescriptor", {}).get("periodType") == "SO"]
print(f"\nTotal plays: {len(plays)}, SO plays: {len(so_plays)}")
for p in so_plays[:5]:
    print(f"  typeDescKey={p.get('typeDescKey')} details={p.get('details')}")

# Check rosterSpots for goalies
spots = pbp.get("rosterSpots", [])
goalies = [s for s in spots if s.get("positionCode") == "G"]
print(f"\nRosterSpot goalies: {len(goalies)}")
for g in goalies:
    print(f"  playerId={g.get('playerId')} teamId={g.get('teamId')} name={g.get('firstName',{}).get('default')} {g.get('lastName',{}).get('default')}")
