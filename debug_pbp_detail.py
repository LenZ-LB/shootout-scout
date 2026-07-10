"""
Check exactly what detail fields exist on SO play events.
Using the EDM vs CGY game we know well (2025020006).
"""
import requests, json

pbp_base = "https://api-web.nhle.com/v1"
game_id = 2025020006

resp = requests.get(f"{pbp_base}/gamecenter/{game_id}/play-by-play", timeout=15)
pbp = resp.json()

so_plays = [p for p in pbp.get("plays", [])
            if p.get("periodDescriptor", {}).get("periodType") == "SO"
            and p.get("typeDescKey") not in ("period-start","period-end","game-end","shootout-complete")]

print(f"SO attempt plays: {len(so_plays)}")
print()
for p in so_plays:
    print(f"type: {p.get('typeDescKey')}")
    print(f"sortOrder: {p.get('sortOrder')}")
    print(f"details: {json.dumps(p.get('details',{}), indent=2)}")
    print()
