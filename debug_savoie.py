"""
Debug: Check EDM vs WPG shootout Oct 20 2019 - Hellebuyck missing
"""
import requests, json, time
from datetime import date, timedelta

base_pbp = "https://api-web.nhle.com/v1"

print("=== Finding EDM vs WPG game around 2019-10-20 ===")
game_id = None
for delta in range(-2, 4):
    d = date(2019, 10, 20) + timedelta(days=delta)
    resp = requests.get(f"{base_pbp}/schedule/{d.isoformat()}", timeout=15)
    data = resp.json()
    for day in data.get("gameWeek", []):
        for g in day.get("games", []):
            home = g.get("homeTeam", {}).get("abbrev", "")
            away = g.get("awayTeam", {}).get("abbrev", "")
            if "EDM" in (home, away) and "WPG" in (home, away):
                game_id = g["id"]
                outcome = g.get("gameOutcome", {})
                print(f"Found on {d}: gameId={game_id} lastPeriodType={outcome.get('lastPeriodType')}")
    if game_id:
        break

if not game_id:
    print("Game not found"); exit()

print()
resp2 = requests.get(f"{base_pbp}/gamecenter/{game_id}/play-by-play", timeout=15)
pbp = resp2.json()
home = pbp.get("homeTeam", {})
away = pbp.get("awayTeam", {})
home_id = home.get("id")
away_id = away.get("id")
print(f"Home: {home.get('abbrev')} id={home_id}  Away: {away.get('abbrev')} id={away_id}")

so_plays = [p for p in pbp.get("plays", [])
            if p.get("periodDescriptor", {}).get("periodType") == "SO"]
print(f"SO plays: {len(so_plays)}")
for p in so_plays:
    d = p.get("details", {})
    print(f"  type={p.get('typeDescKey'):25s} shooterId={d.get('shootingPlayerId') or d.get('scoringPlayerId')} goalieId={d.get('goalieInNetId')} ownerTeamId={d.get('eventOwnerTeamId')}")

goalies = [s for s in pbp.get("rosterSpots", []) if s.get("positionCode") == "G"]
print(f"\nDressed goalies:")
for g in goalies:
    print(f"  id={g.get('playerId')} teamId={g.get('teamId')} {g.get('firstName',{}).get('default')} {g.get('lastName',{}).get('default')}")

# Simulate our inference logic
team_goalie = {}
so_goalie_ids = set()
for p in so_plays:
    d = p.get("details", {})
    type_key = p.get("typeDescKey", "")
    goalie_id = d.get("goalieInNetId")
    owner_tid = d.get("eventOwnerTeamId")
    if type_key not in ("shot-on-goal","goal","missed-shot","failed-shot-attempt"):
        continue
    if goalie_id:
        so_goalie_ids.add(goalie_id)
        if owner_tid:
            team_goalie[owner_tid] = goalie_id

print(f"\nteam_goalie from plays: {team_goalie}")
print(f"Known goalie IDs from plays: {so_goalie_ids}")

# Rosterspot fallback
for spot in pbp.get("rosterSpots", []):
    if spot.get("positionCode") != "G":
        continue
    pid = spot.get("playerId")
    tid = spot.get("teamId")
    if pid in so_goalie_ids:
        other_tid = away_id if tid == home_id else home_id
        if other_tid and other_tid not in team_goalie:
            team_goalie[other_tid] = pid
            print(f"  Inferred: team {other_tid} faced goalie {pid}")

print(f"Final team_goalie map: {team_goalie}")

# Simulate full pair extraction
print("\nSimulated pairs:")
for p in so_plays:
    d = p.get("details", {})
    type_key = p.get("typeDescKey", "")
    shooter = d.get("shootingPlayerId") or d.get("scoringPlayerId")
    goalie  = d.get("goalieInNetId")
    owner   = d.get("eventOwnerTeamId")
    if type_key not in ("shot-on-goal","goal","missed-shot","failed-shot-attempt"):
        continue
    if not goalie and owner:
        goalie = team_goalie.get(owner)
    if shooter and goalie:
        print(f"  shooter={shooter} goalie={goalie} scored={1 if type_key=='goal' else 0}")
    else:
        print(f"  MISSING: shooter={shooter} goalie={goalie} type={type_key}")
