"""
NHL Shootout Stats — Data Pipeline (v2)
========================================
Uses nhl-api-py (pip install nhl-api-py) which wraps the current NHL stats
API correctly, including the dedicated 'shootout' report type. This replaces
the old play-by-play approach which was silently missing attempts in pre-2021
seasons due to API format changes.

HOW TO RUN
----------
  pip install nhl-api-py requests
  python nhl_shootout_pipeline.py --init-db
  python nhl_shootout_pipeline.py --backfill 20052006 20252026
  python nhl_shootout_pipeline.py --update-rosters
  python nhl_shootout_pipeline.py --export-json

GitHub Actions: use the backfill-history.yml and update-data.yml workflows.
The backfill takes season strings like 20052006 (not year integers).

WHAT THIS BUILDS
-----------------
  data/players.json   — shooter career/season/vs-goalie splits
  data/goalies.json   — goalie career shootout save totals
  data/alltime.json   — league-wide leaderboards
  data/minor.json     — manually logged AHL/junior data (hand-maintained)
  shootout.db         — SQLite source of truth behind all JSON exports
"""

import argparse
import json
import os
import sqlite3
import time
from datetime import date

import requests

DB_PATH = "shootout.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS skater_shootout (
    player_id   INTEGER NOT NULL,
    full_name   TEXT NOT NULL,
    team_abbrev TEXT,
    season      TEXT NOT NULL,
    goals       INTEGER DEFAULT 0,
    attempts    INTEGER DEFAULT 0,
    PRIMARY KEY (player_id, season)
);

CREATE TABLE IF NOT EXISTS goalie_shootout (
    goalie_id   INTEGER NOT NULL,
    full_name   TEXT NOT NULL,
    team_abbrev TEXT,
    season      TEXT NOT NULL,
    saves       INTEGER DEFAULT 0,
    shots_against INTEGER DEFAULT 0,
    wins        INTEGER DEFAULT 0,
    losses      INTEGER DEFAULT 0,
    PRIMARY KEY (goalie_id, season)
);

-- vs-goalie splits pulled from play-by-play (best effort, new seasons only)
CREATE TABLE IF NOT EXISTS vs_goalie_splits (
    player_id   INTEGER NOT NULL,
    goalie_id   INTEGER NOT NULL,
    goalie_name TEXT,
    goals       INTEGER DEFAULT 0,
    attempts    INTEGER DEFAULT 0,
    PRIMARY KEY (player_id, goalie_id)
);

-- manually logged Junior/AHL data
CREATE TABLE IF NOT EXISTS manual_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_date   TEXT,
    league      TEXT,
    season      TEXT,
    shooter_name TEXT NOT NULL,
    shooter_team TEXT,
    goalie_name  TEXT,
    goalie_team  TEXT,
    result       TEXT CHECK(result IN ('goal','miss')) NOT NULL,
    logged_by   TEXT,
    notes       TEXT
);

-- current NHL rosters — rebuilt from scratch on every --update-rosters run
-- only players in this table appear in team panels on the page
-- everyone else is still in the db for search/history but has no team assignment
CREATE TABLE IF NOT EXISTS active_rosters (
    player_id   INTEGER PRIMARY KEY,
    full_name   TEXT,
    team_abbrev TEXT NOT NULL,
    position    TEXT,
    is_goalie   INTEGER DEFAULT 0
);
"""

SEASONS = [
    "20052006","20062007","20072008","20082009","20092010",
    "20102011","20112012","20122013","20132014","20142015",
    "20152016","20162017","20172018","20182019","20192020",
    "20202021","20212022","20222023","20232024","20242025","20252026",
]

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    # Migrate existing databases that predate the wins/losses columns
    for col in ("wins", "losses"):
        try:
            conn.execute(f"ALTER TABLE goalie_shootout ADD COLUMN {col} INTEGER DEFAULT 0")
            print(f"  Migrated: added {col} column to goalie_shootout")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()
    print(f"Initialized {DB_PATH}")

def _fetch_shootout_stats(season, report_type, retries=3):
    """
    Pulls from the NHL stats API /skater/shootout or /goalie/shootout endpoint.
    gameTypeId=2 = regular season only (1=preseason, 3=playoffs, 4=ASG).
    isAggregate=false + isGame=false = season-level totals, not game-by-game rows.
    Handles pagination — the API caps at 100 rows per page.
    """
    base = "https://api.nhle.com/stats/rest/en"
    endpoint = "skater" if report_type == "skater" else "goalie"
    all_rows = []
    start = 0
    limit = 100
    while True:
        url = (
            f"{base}/{endpoint}/shootout"
            f"?isAggregate=false&isGame=false"
            f"&cayenneExp=seasonId={season}%20and%20gameTypeId=2"
            f"&start={start}&limit={limit}"
        )
        for attempt in range(retries):
            try:
                resp = requests.get(url, timeout=20)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt == retries - 1:
                    print(f"  [warn] {endpoint} shootout fetch failed for {season}: {e}")
                    return all_rows
                time.sleep(1.5 * (attempt + 1))
        rows = data.get("data", [])
        all_rows.extend(rows)
        total = data.get("total", 0)
        start += limit
        if start >= total:
            break
        time.sleep(0.3)
    return all_rows

def backfill(start_season, end_season, debug=False):
    """
    Pull shootout stats for every season between start_season and end_season
    (inclusive) using the NHL stats API's dedicated shootout report.
    Season format: 8-digit string e.g. '20052006'.
    Upserts so re-running is safe.
    """
    conn = get_conn()

    # Figure out which seasons to pull
    try:
        s_idx = SEASONS.index(start_season)
        e_idx = SEASONS.index(end_season)
    except ValueError:
        # If exact match fails, find closest
        s_idx = 0
        e_idx = len(SEASONS) - 1
        for i, s in enumerate(SEASONS):
            if s >= start_season:
                s_idx = i
                break
        for i, s in enumerate(SEASONS):
            if s <= end_season:
                e_idx = i

    target_seasons = SEASONS[s_idx:e_idx + 1]
    print(f"Backfilling {len(target_seasons)} seasons: {target_seasons[0]} to {target_seasons[-1]}")

    for season in target_seasons:
        print(f"  Season {season}...")

        # Skaters
        skater_rows = _fetch_shootout_stats(season, "skater")
        for r in skater_rows:
            pid = r.get("playerId")
            if not pid:
                continue
            conn.execute("""
                INSERT INTO skater_shootout (player_id, full_name, team_abbrev, season, goals, attempts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_id, season) DO UPDATE SET
                    full_name=excluded.full_name,
                    team_abbrev=excluded.team_abbrev,
                    goals=excluded.goals,
                    attempts=excluded.attempts
            """, (
                pid,
                r.get("skaterFullName", f"Player #{pid}"),
                r.get("teamAbbrevs", ""),
                season,
                r.get("shootoutGoals", 0),
                r.get("shootoutShots", 0),
            ))
        if debug:
            print(f"    {len(skater_rows)} skaters")

        # Goalies
        goalie_rows = _fetch_shootout_stats(season, "goalie")
        for r in goalie_rows:
            gid = r.get("playerId")
            if not gid:
                continue
            shots = r.get("shootoutShotsAgainst", 0)
            saves = r.get("shootoutSaves", 0)
            wins = r.get("shootoutWins", 0)
            losses = r.get("shootoutLosses", 0)
            conn.execute("""
                INSERT INTO goalie_shootout (goalie_id, full_name, team_abbrev, season, saves, shots_against, wins, losses)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(goalie_id, season) DO UPDATE SET
                    full_name=excluded.full_name,
                    team_abbrev=excluded.team_abbrev,
                    saves=excluded.saves,
                    shots_against=excluded.shots_against,
                    wins=excluded.wins,
                    losses=excluded.losses
            """, (
                gid,
                r.get("goalieFullName", f"Goalie #{gid}"),
                r.get("teamAbbrevs", ""),
                season,
                saves,
                shots,
                wins,
                losses,
            ))
        if debug:
            print(f"    {len(goalie_rows)} goalies")

        conn.commit()
        time.sleep(0.4)

    conn.close()
    print("Backfill complete.")

def update_rosters():
    """
    Rebuilds the active_rosters table from scratch using current NHL rosters.
    Only players in active_rosters appear in team panels on the page —
    retired/unsigned players are invisible in team views but still searchable
    in all-time history.
    """
    BASE = "https://api-web.nhle.com/v1"
    TEAM_ABBREVS = [
        "ANA","BOS","BUF","CGY","CAR","CHI","COL","CBJ","DAL","DET",
        "EDM","FLA","LAK","MIN","MTL","NSH","NJD","NYI","NYR","OTT",
        "PHI","PIT","SJS","SEA","STL","TBL","TOR","UTA","VAN","VGK","WSH","WPG",
    ]
    conn = get_conn()
    # Wipe and rebuild — ensures departed players are immediately removed
    conn.execute("DELETE FROM active_rosters")
    conn.commit()

    current_season = SEASONS[-1]
    for team in TEAM_ABBREVS:
        url = f"{BASE}/roster/{team}/current"
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [warn] roster fetch failed for {team}: {e}")
            continue

        for group, is_goalie in (("forwards", 0), ("defensemen", 0), ("goalies", 1)):
            for p in data.get(group, []):
                pid = p["id"]
                name = f"{p['firstName']['default']} {p['lastName']['default']}"
                pos = p.get("positionCode", "")
                conn.execute("""
                    INSERT OR REPLACE INTO active_rosters (player_id, full_name, team_abbrev, position, is_goalie)
                    VALUES (?, ?, ?, ?, ?)
                """, (pid, name, team, pos, is_goalie))
                # Also keep current season stats row team current
                table = "goalie_shootout" if is_goalie else "skater_shootout"
                id_col = "goalie_id" if is_goalie else "player_id"
                conn.execute(
                    f"UPDATE {table} SET team_abbrev=? WHERE {id_col}=? AND season=?",
                    (team, pid, current_season)
                )
        conn.commit()
        time.sleep(0.3)
        print(f"  {team} rostered")

    total = conn.execute("SELECT COUNT(*) FROM active_rosters").fetchone()[0]
    conn.close()
    print(f"Rosters updated — {total} active players/goalies in db.")

def export_json(out_dir="data"):
    os.makedirs(out_dir, exist_ok=True)
    conn = get_conn()
    conn.row_factory = sqlite3.Row

    # Build active roster lookup: player_id -> {team, name, is_goalie}
    active = {}
    for row in conn.execute("SELECT player_id, full_name, team_abbrev, is_goalie FROM active_rosters"):
        active[row["player_id"]] = {
            "team": row["team_abbrev"],
            "name": row["full_name"],
            "is_goalie": row["is_goalie"],
        }

    # --- Players ---
    # Start from active_rosters so every current NHL skater appears in their
    # team panel even with zero career shootout attempts
    players_map = {}
    for pid, ar in active.items():
        if ar["is_goalie"]:
            continue
        players_map[pid] = {
            "id": pid,
            "name": ar["name"],
            "team": ar["team"],
            "active": True,
            "career": [0, 0],
            "seasons": {},
            "vs_goalie": {},
        }

    # Layer in historical shootout stats for everyone (active or not)
    for row in conn.execute("""
        SELECT player_id, full_name, team_abbrev, season, goals, attempts
        FROM skater_shootout ORDER BY season
    """):
        pid = row["player_id"]
        if pid not in players_map:
            # Not on current roster — still include for history/search/all-time
            players_map[pid] = {
                "id": pid,
                "name": row["full_name"],
                "team": "",   # blank = not on current roster, won't show in team panels
                "active": False,
                "career": [0, 0],
                "seasons": {},
                "vs_goalie": {},
            }
        p = players_map[pid]
        p["career"][0] += row["goals"]
        p["career"][1] += row["attempts"]
        p["seasons"][row["season"]] = [row["goals"], row["attempts"]]

    # attach vs_goalie splits
    for row in conn.execute("""
        SELECT player_id, goalie_name, goals, attempts FROM vs_goalie_splits
    """):
        pid = row["player_id"]
        if pid in players_map:
            players_map[pid]["vs_goalie"][row["goalie_name"]] = [row["goals"], row["attempts"]]

    players_out = [p for p in players_map.values() if p["active"] or p["career"][1] > 0]
    players_out.sort(key=lambda p: (not p["active"], -p["career"][1]))

    # --- Goalies ---
    # Same pattern — seed from active_rosters first
    goalies_map = {}
    for pid, ar in active.items():
        if not ar["is_goalie"]:
            continue
        goalies_map[pid] = {
            "id": pid,
            "name": ar["name"],
            "team": ar["team"],
            "active": True,
            "stopped": 0,
            "faced": 0,
            "wins": 0,
            "losses": 0,
        }

    for row in conn.execute("""
        SELECT goalie_id, full_name, team_abbrev, season, saves, shots_against, wins, losses
        FROM goalie_shootout ORDER BY season
    """):
        gid = row["goalie_id"]
        if gid not in goalies_map:
            goalies_map[gid] = {
                "id": gid,
                "name": row["full_name"],
                "team": "",
                "active": False,
                "stopped": 0,
                "faced": 0,
                "wins": 0,
                "losses": 0,
            }
        g = goalies_map[gid]
        g["stopped"] += row["saves"]
        g["faced"]   += row["shots_against"]
        g["wins"]    += row["wins"]
        g["losses"]  += row["losses"]

    for g in goalies_map.values():
        g["record"] = f"{g['wins']} W \u2013 {g['losses']} L in career shootouts"

    goalies_out = [g for g in goalies_map.values() if g["active"] or g["faced"] > 0]

    with open(os.path.join(out_dir, "players.json"), "w") as f:
        json.dump(players_out, f, indent=2)
    with open(os.path.join(out_dir, "goalies.json"), "w") as f:
        json.dump(goalies_out, f, indent=2)

    print(f"Exported {len(players_out)} players, {len(goalies_out)} goalies to {out_dir}/")
    export_alltime(out_dir=out_dir, conn_override=conn)
    conn.close()

def export_alltime(out_dir="data", top_n=25, min_attempts=15, min_faced=20, conn_override=None):
    os.makedirs(out_dir, exist_ok=True)
    conn = conn_override or get_conn()

    def query(sql, params=()):
        return [dict(zip([d[0] for d in conn.execute(sql, params).description], row))
                for row in conn.execute(sql, params).fetchall()]

    top_goals = query(f"""
        SELECT player_id, full_name AS name, team_abbrev AS team,
               SUM(goals) AS goals, SUM(attempts) AS att
        FROM skater_shootout GROUP BY player_id
        ORDER BY goals DESC LIMIT {top_n}
    """)
    top_pct = query(f"""
        SELECT player_id, full_name AS name, team_abbrev AS team,
               SUM(goals) AS goals, SUM(attempts) AS att
        FROM skater_shootout GROUP BY player_id
        HAVING att >= {min_attempts}
        ORDER BY (CAST(SUM(goals) AS FLOAT)/SUM(attempts)) DESC LIMIT {top_n}
    """)
    top_sv = query(f"""
        SELECT goalie_id, full_name AS name, team_abbrev AS team,
               SUM(saves) AS stopped, SUM(shots_against) AS faced
        FROM goalie_shootout GROUP BY goalie_id
        HAVING faced >= {min_faced}
        ORDER BY (CAST(SUM(saves) AS FLOAT)/SUM(shots_against)) DESC LIMIT {top_n}
    """)
    most_faced = query(f"""
        SELECT goalie_id, full_name AS name, team_abbrev AS team,
               SUM(saves) AS stopped, SUM(shots_against) AS faced
        FROM goalie_shootout GROUP BY goalie_id
        ORDER BY faced DESC LIMIT {top_n}
    """)

    out = {
        "top_goals": top_goals,
        "top_shooting_pct": top_pct,
        "top_goalie_save_pct": top_sv,
        "most_shots_faced": most_faced,
        "min_attempts_threshold": min_attempts,
        "min_faced_threshold": min_faced,
    }
    with open(os.path.join(out_dir, "alltime.json"), "w") as f:
        json.dump(out, f, indent=2)
    if not conn_override:
        conn.close()
    print(f"Exported all-time leaderboards to {out_dir}/alltime.json")


def build_vs_goalie_splits(start_season, end_season, debug=False):
    """
    Builds shooter vs goalie splits purely from play-by-play SO period data.
    Maps shootingPlayerId -> goalieInNetId directly — no team logic needed.
    Game list sourced from skater stats API to find which games had shootouts.
    """
    conn = get_conn()
    conn.row_factory = sqlite3.Row

    try:
        s_idx = SEASONS.index(start_season)
        e_idx = SEASONS.index(end_season)
    except ValueError:
        s_idx, e_idx = 0, len(SEASONS) - 1

    target_seasons = SEASONS[s_idx:e_idx + 1]
    print(f"Building vs-goalie splits for {len(target_seasons)} seasons...")
    conn.execute("DELETE FROM vs_goalie_splits")
    conn.commit()

    stats_base = "https://api.nhle.com/stats/rest/en"
    pbp_base   = "https://api-web.nhle.com/v1"

    def get_shootout_game_ids(season):
        """Get unique gameIds where at least one player took a shootout shot."""
        game_ids = set()
        start, limit = 0, 100
        while True:
            url = (f"{stats_base}/skater/shootout"
                   f"?isAggregate=false&isGame=true"
                   f"&cayenneExp=seasonId={season}%20and%20gameTypeId=2"
                   f"&sort=shootoutShots&direction=DESC"
                   f"&start={start}&limit={limit}")
            for attempt in range(3):
                try:
                    resp = requests.get(url, timeout=20)
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"  [warn] game list fetch failed {season}: {e}")
                        return game_ids
                    time.sleep(1.5 * (attempt + 1))
            batch = data.get("data", [])
            for r in batch:
                if r.get("shootoutShots", 0) > 0:
                    game_ids.add(r["gameId"])
            # Stop when we hit zero-shot rows
            if not batch or batch[-1].get("shootoutShots", 0) == 0:
                break
            start += limit
            if start >= data.get("total", 0):
                break
            time.sleep(0.2)
        return game_ids

    def get_splits_from_pbp(game_id):
        """
        Returns list of (shooter_id, goalie_id, scored) from SO period plays.
        Pure shootingPlayerId -> goalieInNetId mapping. No team logic.
        """
        try:
            resp = requests.get(f"{pbp_base}/gamecenter/{game_id}/play-by-play", timeout=15)
            resp.raise_for_status()
            pbp = resp.json()
        except Exception as e:
            if debug:
                print(f"    [warn] PBP failed game {game_id}: {e}")
            return []

        pairs = []
        for play in pbp.get("plays", []):
            if play.get("periodDescriptor", {}).get("periodType") != "SO":
                continue
            d = play.get("details", {})
            shooter_id = d.get("shootingPlayerId") or d.get("scoringPlayerId")
            goalie_id  = d.get("goalieInNetId")
            type_key   = play.get("typeDescKey", "")
            if shooter_id and goalie_id and type_key in ("shot-on-goal", "goal", "missed-shot"):
                scored = 1 if type_key == "goal" else 0
                pairs.append((shooter_id, goalie_id, scored))
        return pairs

    # Build goalie name lookup
    goalie_names = {}
    for row in conn.execute("SELECT goalie_id, full_name FROM goalie_shootout GROUP BY goalie_id"):
        goalie_names[row["goalie_id"]] = row["full_name"]
    for row in conn.execute("SELECT player_id, full_name FROM active_rosters WHERE is_goalie=1"):
        goalie_names[row["player_id"]] = row["full_name"]

    all_splits = {}  # (shooter_id, goalie_id) -> [goals, att, goalie_name]

    for season in target_seasons:
        print(f"  Season {season}...")
        game_ids = get_shootout_game_ids(season)
        print(f"    {len(game_ids)} shootout games found")

        for game_id in sorted(game_ids):
            pairs = get_splits_from_pbp(game_id)
            for shooter_id, goalie_id, scored in pairs:
                gname = goalie_names.get(goalie_id, f"Goalie #{goalie_id}")
                key = (shooter_id, goalie_id)
                if key not in all_splits:
                    all_splits[key] = [0, 0, gname]
                all_splits[key][0] += scored
                all_splits[key][1] += 1
            time.sleep(0.15)

        if debug:
            print(f"    Running total: {len(all_splits)} shooter-goalie pairs so far")

    for (sid, gid), (goals, att, gname) in all_splits.items():
        conn.execute("""
            INSERT INTO vs_goalie_splits (player_id, goalie_id, goalie_name, goals, attempts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(player_id, goalie_id) DO UPDATE SET
                goalie_name=excluded.goalie_name,
                goals=goals+excluded.goals,
                attempts=attempts+excluded.attempts
        """, (sid, gid, gname, goals, att))
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM vs_goalie_splits").fetchone()[0]
    conn.close()
    print(f"vs-goalie splits complete — {total} total shooter-goalie pairs.")



if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="NHL Shootout Stats Pipeline v2")
    ap.add_argument("--init-db", action="store_true")
    ap.add_argument("--backfill", nargs=2, metavar=("START_SEASON", "END_SEASON"),
                    help="e.g. --backfill 20052006 20252026")
    ap.add_argument("--build-splits", nargs=2, metavar=("START_SEASON", "END_SEASON"),
                    help="Build vs-goalie splits e.g. --build-splits 20052006 20252026")
    ap.add_argument("--update-rosters", action="store_true")
    ap.add_argument("--export-json", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.init_db:
        init_db()
    if args.backfill:
        backfill(args.backfill[0], args.backfill[1], debug=args.debug)
    if args.build_splits:
        build_vs_goalie_splits(args.build_splits[0], args.build_splits[1], debug=args.debug)
    if args.update_rosters:
        update_rosters()
    if args.export_json:
        export_json()
    if not any([args.init_db, args.backfill, args.build_splits, args.update_rosters, args.export_json]):
        print(__doc__)
