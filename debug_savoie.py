"""
Debug: Check EDM vs NJD shootout Oct 10 2019 - RNH vs Blackwood missing
"""
import requests, json

base_pbp = "https://api-web.nhle.com/v1"

print("=== Finding EDM vs NJD game on 2019-10-10 ===")
resp = requests.get(f"{base_pbp}/schedule/2019-10-10", timeout=15)
data = resp.json()
game_id = None
for day in data.get("gameWeek", []):
    for g in day.get("games", []):
        teams = {g.get("homeTeam",{}).get("abbrev"), g.get("awayTeam",{}).get("abbrev")}
        if "EDM" in teams and "NJD" in teams:
            game_id = g["id"]
            outcome = g.get("gameOutcome", {})
            print(f"Found: gameId={game_id} lastPeriodType={outcome.get('lastPeriodType')}")

if not game_id:
    print("Game not found")
    exit()

print()
print(f"=== PBP SO plays for game {game_id} ===")
resp2 = requests.get(f"{base_pbp}/gamecenter/{game_id}/play-by-play", timeout=15)
pbp = resp2.json()
home = pbp.get("homeTeam", {})
away = pbp.get("awayTeam", {})
print(f"Home: {home.get('abbrev')} Away: {away.get('abbrev')}")

so_plays = [p for p in pbp.get("plays", [])
            if p.get("periodDescriptor", {}).get("periodType") == "SO"]
print(f"SO plays: {len(so_plays)}")
for p in so_plays:
    d = p.get("details", {})
    shooter = d.get("shootingPlayerId") or d.get("scoringPlayerId")
    goalie  = d.get("goalieInNetId")
    print(f"  type={p.get('typeDescKey'):25s} shooterId={shooter} goalieId={goalie}")

# Check all unique typeDescKeys in this game's SO period
types = set(p.get("typeDescKey") for p in so_plays)
print(f"\nAll SO event types: {types}")

# Check RNH specifically (8476459)
rnh = [p for p in so_plays if
       p.get("details",{}).get("shootingPlayerId") == 8476459 or
       p.get("details",{}).get("scoringPlayerId") == 8476459]
print(f"RNH plays: {len(rnh)}")


