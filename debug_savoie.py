"""
Comprehensive SO event type audit across multiple seasons.
Finds every unique typeDescKey in SO periods, checks for null goalieInNetId patterns,
and identifies any event types we might be missing.
"""
import requests, json, time
from datetime import date, timedelta
from collections import defaultdict

base_pbp   = "https://api-web.nhle.com/v1"

# Sample a few seasons across different eras to catch format changes
SAMPLE_SEASONS = [
    ("2007-10-10", "2007-10-17"),  # early shootout era
    ("2012-01-20", "2012-01-27"),  # mid era
    ("2016-10-15", "2016-10-22"),  # pre-API-change
    ("2019-10-10", "2019-10-17"),  # known issue era
    ("2021-10-15", "2021-10-22"),  # post-API-change
    ("2023-10-12", "2023-10-19"),  # recent
]

def get_so_games_in_range(start, end):
    game_ids = []
    cursor = date.fromisoformat(start)
    end_d  = date.fromisoformat(end)
    while cursor <= end_d:
        try:
            resp = requests.get(f"{base_pbp}/schedule/{cursor.isoformat()}", timeout=15)
            data = resp.json()
            for day in data.get("gameWeek", []):
                for g in day.get("games", []):
                    if g.get("gameType") != 2:
                        continue
                    if g.get("gameState") not in ("OFF", "FINAL"):
                        continue
                    if (g.get("gameOutcome") or {}).get("lastPeriodType") == "SO":
                        game_ids.append(g["id"])
        except Exception as e:
            print(f"  [warn] schedule {cursor}: {e}")
        cursor += timedelta(days=1)
        time.sleep(0.1)
    return game_ids

# Aggregate stats across all sampled games
all_event_types = defaultdict(int)
null_goalie_by_type = defaultdict(int)
total_games = 0
total_so_plays = 0
missing_shooter = 0

print("Sampling SO games across eras...\n")

for start, end in SAMPLE_SEASONS:
    print(f"=== {start} to {end} ===")
    game_ids = get_so_games_in_range(start, end)
    print(f"  {len(game_ids)} SO games found")

    for game_id in game_ids[:5]:  # sample up to 5 games per window
        try:
            resp = requests.get(f"{base_pbp}/gamecenter/{game_id}/play-by-play", timeout=15)
            pbp = resp.json()
        except Exception as e:
            print(f"  [warn] PBP failed {game_id}: {e}")
            continue

        total_games += 1
        for play in pbp.get("plays", []):
            if play.get("periodDescriptor", {}).get("periodType") != "SO":
                continue
            type_key = play.get("typeDescKey", "UNKNOWN")
            d = play.get("details", {})
            shooter = d.get("shootingPlayerId") or d.get("scoringPlayerId")
            goalie  = d.get("goalieInNetId")

            all_event_types[type_key] += 1
            total_so_plays += 1

            if type_key not in ("period-start", "period-end", "game-end",
                                "shootout-complete", "stoppage"):
                if not goalie:
                    null_goalie_by_type[type_key] += 1
                if not shooter:
                    missing_shooter += 1

        time.sleep(0.15)

print()
print("=" * 60)
print(f"SUMMARY: {total_games} games, {total_so_plays} SO plays total")
print()
print("All SO event types seen:")
for t, count in sorted(all_event_types.items()):
    null = null_goalie_by_type.get(t, 0)
    pct = f" ({null}/{count} null goalie)" if null else ""
    print(f"  {t:30s} {count:4d}{pct}")

print()
print(f"Plays missing shooter ID: {missing_shooter}")
print()
print("Event types with null goalieInNetId (need inference):")
for t, count in sorted(null_goalie_by_type.items()):
    print(f"  {t}: {count} occurrences")
