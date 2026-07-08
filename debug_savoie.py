"""
Debug: Check EDM vs DAL shootout Nov 4 2025
Known: McDavid, Draisaitl, RNH all shot vs Casey DeSmith
"""
import requests, json

base_pbp = "https://api-web.nhle.com/v1"
base_sched = "https://api-web.nhle.com/v1"

# Find the game from the schedule
print("=== Finding EDM vs DAL game on 2025-11-04 ===")
resp = requests.get(f"{base_sched}/schedule/2025-11-04", timeout=15)
data = resp.json()
game_id = None
for day in data.get("gameWeek", []):
    for g in day.get("games", []):
        teams = {g.get("homeTeam",{}).get("abbrev"), g.get("awayTeam",{}).get("abbrev")}
        if "EDM" in teams and "DAL" in teams:
            game_id = g["id"]
            outcome = g.get("gameOutcome", {})
            print(f"Found: gameId={game_id} lastPeriodType={outcome.get('lastPeriodType')}")

if not game_id:
    print("Game not found in schedule")
    exit()

print()
print(f"=== PBP for game {game_id} ===")
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
    print(f"  type={p.get('typeDescKey'):20s} shooterId={shooter} goalieId={goalie}")

# Check if McDavid (8478402) appears
mcd_plays = [p for p in so_plays if
             (p.get("details",{}).get("shootingPlayerId") == 8478402 or
              p.get("details",{}).get("scoringPlayerId") == 8478402)]
print(f"\nMcDavid SO plays: {len(mcd_plays)}")
if not mcd_plays:
    print("McDavid NOT found in SO plays - checking all play details:")
    for p in so_plays:
        print(f"  {p.get('details')}")

