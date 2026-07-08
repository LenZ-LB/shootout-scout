"""Check what the game-level GOALIE shootout endpoint returns."""
import requests, json

print("=== Game-level GOALIE shootout (isGame=true) ===")
url = ("https://api.nhle.com/stats/rest/en/goalie/shootout"
       "?isAggregate=false&isGame=true"
       "&cayenneExp=seasonId=20242025%20and%20gameTypeId=2"
       "&start=0&limit=3")
resp = requests.get(url, timeout=20)
print("HTTP:", resp.status_code)
d = resp.json()
print("Total rows:", d.get("total"))
if d.get("data"):
    print("ALL field names:", list(d["data"][0].keys()))
    print()
    # Find the row matching game 2024020240 to prove we can cross-ref
    match = next((r for r in d["data"] if r.get("gameId") == 2024020240), None)
    print("Sample row:", json.dumps(d["data"][0], indent=2))
    if match:
        print("Matched game 2024020240:", json.dumps(match, indent=2))
