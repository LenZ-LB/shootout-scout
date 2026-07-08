"""
Diagnose the game-level shootout API field names.
Run this before build-splits to confirm the goalie field name.
"""
import requests, json

print("=== Game-level skater shootout (isGame=true) ===")
url = ("https://api.nhle.com/stats/rest/en/skater/shootout"
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
    print("Sample row:")
    print(json.dumps(d["data"][0], indent=2))
else:
    print("No data:", d)
