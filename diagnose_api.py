"""Run this as a GitHub Actions step to see real API field names."""
import requests, json

print("=== SKATER shootout endpoint ===")
url = ("https://api.nhle.com/stats/rest/en/skater/shootout"
       "?isAggregate=false&isGame=false"
       "&cayenneExp=seasonId=20242025%20and%20gameTypeId=2"
       "&start=0&limit=3")
resp = requests.get(url, timeout=20)
print("HTTP:", resp.status_code)
d = resp.json()
print("Total rows:", d.get("total"))
if d.get("data"):
    print("Field names:", list(d["data"][0].keys()))
    print("Sample:", json.dumps(d["data"][0], indent=2))
else:
    print("No data:", d)

print()
print("=== GOALIE shootout endpoint ===")
url2 = ("https://api.nhle.com/stats/rest/en/goalie/shootout"
        "?isAggregate=false&isGame=false"
        "&cayenneExp=seasonId=20242025%20and%20gameTypeId=2"
        "&start=0&limit=2")
resp2 = requests.get(url2, timeout=20)
print("HTTP:", resp2.status_code)
d2 = resp2.json()
print("Total rows:", d2.get("total"))
if d2.get("data"):
    print("Field names:", list(d2["data"][0].keys()))
    print("Sample:", json.dumps(d2["data"][0], indent=2))
else:
    print("No data:", d2)
