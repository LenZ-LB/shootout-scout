"""
Minimal test: directly write a known split into vs_goalie_splits
and verify export_json picks it up.
McDavid (8478402) vs Lundqvist (8471418)
"""
import sqlite3, json, sys
sys.path.insert(0, '.')
from nhl_shootout_pipeline import get_conn, export_json

conn = get_conn()

# Check the table exists
try:
    count = conn.execute("SELECT COUNT(*) FROM vs_goalie_splits").fetchone()[0]
    print(f"vs_goalie_splits exists, {count} rows")
except Exception as e:
    print(f"Table error: {e}")
    conn.close()
    exit(1)

# Write a test split directly
conn.execute("""
    INSERT OR REPLACE INTO vs_goalie_splits 
    (player_id, goalie_id, goalie_name, goals, attempts)
    VALUES (8478402, 8471418, 'Henrik Lundqvist', 2, 5)
""")
conn.commit()

count = conn.execute("SELECT COUNT(*) FROM vs_goalie_splits").fetchone()[0]
print(f"After insert: {count} rows")

row = conn.execute("SELECT * FROM vs_goalie_splits WHERE player_id=8478402").fetchone()
print(f"McDavid row: {row}")
conn.close()

# Now export and check players.json
export_json()
players = json.load(open('data/players.json'))
mcd = next((p for p in players if p['id'] == 8478402), None)
if mcd:
    print(f"McDavid in players.json: vs_goalie={mcd.get('vs_goalie')}")
else:
    print("McDavid NOT FOUND in players.json")
