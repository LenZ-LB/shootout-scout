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

-- Individual shootout attempts with full detail — source of truth for
-- shot type breakdowns, round performance, and goalie tendency analysis.
-- Rebuilt by --build-splits. Each row = one attempt in one game.
CREATE TABLE IF NOT EXISTS so_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     INTEGER NOT NULL,
    season      TEXT NOT NULL,
    game_date   TEXT,
    home_team   TEXT,
    away_team   TEXT,
    round_num   INTEGER,
    shooter_id  INTEGER NOT NULL,
    goalie_id   INTEGER NOT NULL,
    result      TEXT NOT NULL,
    shot_type   TEXT,
    miss_reason TEXT,
    UNIQUE(game_id, shooter_id, round_num)
);
CREATE INDEX IF NOT EXISTS idx_soa_shooter ON so_attempts(shooter_id);
CREATE INDEX IF NOT EXISTS idx_soa_goalie  ON so_attempts(goalie_id);
CREATE INDEX IF NOT EXISTS idx_soa_season  ON so_attempts(season);

-- current NHL rosters — rebuilt from scratch on every --update-rosters run
-- only players in this table appear in team panels on the page
-- everyone else is still in the db for search/history but has no team assignment
CREATE TABLE IF NOT EXISTS active_rosters (
    player_id   INTEGER PRIMARY KEY,
    full_name   TEXT,
    team_abbrev TEXT NOT NULL,
    position    TEXT,
    is_goalie   INTEGER DEFAULT 0,
    jersey_number INTEGER
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
    try:
        conn.execute("ALTER TABLE active_rosters ADD COLUMN jersey_number INTEGER")
        print("  Migrated: added jersey_number column to active_rosters")
    except sqlite3.OperationalError:
        pass
    # so_attempts created by CREATE TABLE IF NOT EXISTS — no migration needed
    try:
        conn.execute("ALTER TABLE so_attempts ADD COLUMN game_date TEXT")
        print("  Migrated: added game_date column to so_attempts")
    except sqlite3.OperationalError:
        pass
    for col in ("home_team", "away_team"):
        try:
            conn.execute(f"ALTER TABLE so_attempts ADD COLUMN {col} TEXT")
            print(f"  Migrated: added {col} column to so_attempts")
        except sqlite3.OperationalError:
            pass
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
                num = p.get("sweaterNumber")
                conn.execute("""
                    INSERT OR REPLACE INTO active_rosters (player_id, full_name, team_abbrev, position, is_goalie, jersey_number)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (pid, name, team, pos, is_goalie, num))
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

    # Build active roster lookup: player_id -> {team, name, is_goalie, jersey_number}
    active = {}
    for row in conn.execute("SELECT player_id, full_name, team_abbrev, is_goalie, jersey_number FROM active_rosters"):
        active[row["player_id"]] = {
            "team": row["team_abbrev"],
            "name": row["full_name"],
            "is_goalie": row["is_goalie"],
            "jersey_number": row["jersey_number"],
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
            "number": ar.get("jersey_number"),
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

    # attach vs_goalie splits with shot type breakdown per goalie
    for row in conn.execute("""
        SELECT player_id, goalie_name, goals, attempts FROM vs_goalie_splits
    """):
        pid = row["player_id"]
        if pid in players_map:
            players_map[pid]["vs_goalie"][row["goalie_name"]] = [row["goals"], row["attempts"]]

    # per-goalie shot type breakdown from so_attempts
    for row in conn.execute("""
        SELECT sa.shooter_id, gs.goalie_name, sa.shot_type,
               SUM(CASE WHEN sa.result='goal' THEN 1 ELSE 0 END) goals,
               COUNT(*) attempts
        FROM so_attempts sa
        LEFT JOIN vs_goalie_splits gs ON gs.player_id=sa.shooter_id AND gs.goalie_id=sa.goalie_id
        GROUP BY sa.shooter_id, sa.goalie_id, sa.shot_type
    """):
        pid = row["shooter_id"]
        gname = row["goalie_name"]
        if pid not in players_map or not gname:
            continue
        if "vs_goalie_shots" not in players_map[pid]:
            players_map[pid]["vs_goalie_shots"] = {}
        if gname not in players_map[pid]["vs_goalie_shots"]:
            players_map[pid]["vs_goalie_shots"][gname] = {}
        st = row["shot_type"] or "unknown"
        players_map[pid]["vs_goalie_shots"][gname][st] = [row["goals"], row["attempts"]]

    # attach shot type breakdown and round performance from so_attempts
    for row in conn.execute("""
        SELECT shooter_id, shot_type, result, round_num, miss_reason
        FROM so_attempts
    """):
        pid = row["shooter_id"]
        if pid not in players_map:
            continue
        p = players_map[pid]

        # shot_types: {wrist: [goals, att], snap: [...], ...}
        if "shot_types" not in p:
            p["shot_types"] = {}
        st = row["shot_type"] or "unknown"
        if st not in p["shot_types"]:
            p["shot_types"][st] = [0, 0]
        if row["result"] == "goal":
            p["shot_types"][st][0] += 1
        p["shot_types"][st][1] += 1

        # by_round: {1: [goals, att], 2: [...], ...}
        if "by_round" not in p:
            p["by_round"] = {}
        rn = str(row["round_num"] or "?")
        if rn not in p["by_round"]:
            p["by_round"][rn] = [0, 0]
        if row["result"] == "goal":
            p["by_round"][rn][0] += 1
        p["by_round"][rn][1] += 1

        # miss_reasons: {above-crossbar: 3, hit-post: 1, ...}
        if row["result"] == "miss" and row["miss_reason"]:
            if "miss_reasons" not in p:
                p["miss_reasons"] = {}
            mr = row["miss_reason"]
            p["miss_reasons"][mr] = p["miss_reasons"].get(mr, 0) + 1

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
            "number": ar.get("jersey_number"),
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

    # attach goalie detail from so_attempts
    for row in conn.execute("""
        SELECT goalie_id, shot_type, result, round_num, miss_reason
        FROM so_attempts
    """):
        gid = row["goalie_id"]
        if gid not in goalies_map:
            continue
        g = goalies_map[gid]

        # shots_faced_by_type: {wrist: [saves, faced], ...}
        if "shots_by_type" not in g:
            g["shots_by_type"] = {}
        st = row["shot_type"] or "unknown"
        if st not in g["shots_by_type"]:
            g["shots_by_type"][st] = [0, 0]
        if row["result"] != "goal":
            g["shots_by_type"][st][0] += 1  # save or miss
        g["shots_by_type"][st][1] += 1

        # by_round
        if "by_round" not in g:
            g["by_round"] = {}
        rn = str(row["round_num"] or "?")
        if rn not in g["by_round"]:
            g["by_round"][rn] = [0, 0]
        if row["result"] != "goal":
            g["by_round"][rn][0] += 1
        g["by_round"][rn][1] += 1

        # how shots missed (miss_reasons — shots that missed without goalie making save)
        if row["result"] == "miss" and row["miss_reason"]:
            if "miss_reasons" not in g:
                g["miss_reasons"] = {}
            mr = row["miss_reason"]
            g["miss_reasons"][mr] = g["miss_reasons"].get(mr, 0) + 1

    goalies_out = [g for g in goalies_map.values() if g["active"] or g["faced"] > 0]

    with open(os.path.join(out_dir, "players.json"), "w") as f:
        json.dump(players_out, f, indent=2)
    with open(os.path.join(out_dir, "goalies.json"), "w") as f:
        json.dump(goalies_out, f, indent=2)

    print(f"Exported {len(players_out)} players, {len(goalies_out)} goalies to {out_dir}/")
    export_alltime(out_dir=out_dir, conn_override=conn)
    export_so_order(out_dir=out_dir, conn_override=conn)
    conn.close()


def export_so_order(out_dir="data", conn_override=None):
    """
    Export per-team shootout order history.
    Uses home_team/away_team when available (populated by build-splits).
    Falls back to skater_shootout team_abbrev for older data.
    """
    import os, json
    from collections import defaultdict
    os.makedirs(out_dir, exist_ok=True)
    conn = conn_override or get_conn()
    conn.row_factory = sqlite3.Row

    # Build game info from stored home/away where available
    game_info = {}
    for row in conn.execute("""
        SELECT DISTINCT game_id, game_date, home_team, away_team
        FROM so_attempts WHERE home_team IS NOT NULL AND away_team IS NOT NULL
    """):
        game_info[row["game_id"]] = {
            "date": row["game_date"],
            "home": row["home_team"],
            "away": row["away_team"],
        }

    # Get all attempts with player names and team info
    rows = conn.execute("""
        SELECT sa.game_id, sa.season, sa.game_date, sa.home_team, sa.away_team,
               COALESCE(ss.team_abbrev, ar.team_abbrev) AS fallback_team,
               COALESCE(
                   (SELECT sk.full_name FROM skater_shootout sk WHERE sk.player_id=sa.shooter_id LIMIT 1),
                   ar.full_name, 'Player #'||sa.shooter_id
               ) AS shooter_name,
               ar.jersey_number,
               sa.shooter_id, sa.goalie_id, sa.round_num,
               sa.result, sa.shot_type, sa.miss_reason
        FROM so_attempts sa
        LEFT JOIN skater_shootout ss ON ss.player_id=sa.shooter_id AND ss.season=sa.season
        LEFT JOIN active_rosters ar ON ar.player_id=sa.shooter_id
        ORDER BY sa.season DESC, sa.game_id, sa.round_num
    """).fetchall()

    # Build goalie->team lookup from active_rosters
    goalie_teams = {}
    for row in conn.execute("SELECT player_id, team_abbrev FROM active_rosters WHERE is_goalie=1"):
        goalie_teams[row["player_id"]] = row["team_abbrev"]
    # Also from goalie_shootout for historical goalies
    for row in conn.execute("SELECT goalie_id, team_abbrev FROM goalie_shootout GROUP BY goalie_id"):
        if row["goalie_id"] not in goalie_teams:
            goalie_teams[row["goalie_id"]] = row["team_abbrev"]

    # Group attempts by game
    game_attempts = defaultdict(list)
    for r in rows:
        game_attempts[r["game_id"]].append(r)

    # Build per-team output
    out = defaultdict(lambda: defaultdict(list))

    for gid, attempts in game_attempts.items():
        info = game_info.get(gid)
        has_home_away = info and info.get("home") and info.get("away")
        season = attempts[0]["season"]

        if has_home_away:
            home, away = info["home"], info["away"]
            teams = {home, away}
            date = info.get("date")
        else:
            # Fallback: derive teams from fallback_team field
            teams = {r["fallback_team"] for r in attempts if r["fallback_team"]}
            home, away = None, None
            date = None

        if len(teams) < 2:
            continue

        # Assign each shooter to a team
        # Priority: fallback_team (from skater_shootout, recorded at game time) is most accurate
        # for historical data. Goalie context is used only when fallback_team doesn't match
        # either home or away team (e.g. traded players showing current team).
        shooter_teams = {}
        for r in attempts:
            sid = r["shooter_id"]
            ft = r["fallback_team"]
            if has_home_away:
                if ft in (home, away):
                    # skater_shootout team is valid for this game
                    shooter_teams[sid] = ft
                else:
                    # fallback_team doesn't match either team — use goalie context
                    gt = goalie_teams.get(r["goalie_id"])
                    if gt == home:
                        shooter_teams[sid] = away
                    elif gt == away:
                        shooter_teams[sid] = home
                    else:
                        shooter_teams[sid] = ft  # last resort
            else:
                shooter_teams[sid] = ft

        # Determine winner from last goal's shooter team
        goals = [r for r in attempts if r["result"] == "goal"]
        winner = shooter_teams.get(goals[-1]["shooter_id"]) if goals else None

        for team in teams:
            opponent = next((t for t in teams if t != team), "?")
            outcome = "W" if winner == team else ("L" if winner else "?")
            team_attempts = [
                {
                    "round": r["round_num"],
                    "shooter": r["shooter_name"],
                    "shooter_id": r["shooter_id"],
                    "number": r["jersey_number"],
                    "result": r["result"],
                    "shot_type": r["shot_type"] or "unknown",
                    "miss_reason": r["miss_reason"],
                }
                for r in attempts
                if shooter_teams.get(r["shooter_id"]) == team
            ]
            if team_attempts:
                out[team][season].append({
                    "game_id": gid,
                    "date": date,
                    "opponent": opponent,
                    "outcome": outcome,
                    "attempts": sorted(team_attempts, key=lambda x: x["round"] or 99),
                })

    # Sort games oldest-first within each season
    final_out = {}
    for team, seasons in out.items():
        final_out[team] = {}
        for season, games in seasons.items():
            final_out[team][season] = sorted(games, key=lambda g: g["game_id"])

    with open(os.path.join(out_dir, "so_order.json"), "w") as f:
        json.dump(final_out, f, indent=2)
    if not conn_override:
        conn.close()
    print(f"Exported shootout order to {out_dir}/so_order.json ({len(final_out)} teams)")


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
        """
        Get gameIds for all regular season games that went to a shootout,
        using the NHL schedule API (lastPeriodType == 'SO').
        Much more reliable than the skater stats endpoint which returns
        rows for all dressed players regardless of shootout participation.
        """
        game_ids = set()
        # Season year e.g. 20252026 -> start Oct year1, end Jun year2
        year1 = int(season[:4])
        year2 = int(season[4:])
        from datetime import date, timedelta
        cursor = date(year1, 10, 1)
        end    = date(year2, 6, 30)
        base_sched = "https://api-web.nhle.com/v1"
        while cursor <= end:
            url = f"{base_sched}/schedule/{cursor.isoformat()}"
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  [warn] schedule fetch failed {cursor}: {e}")
                cursor += timedelta(days=7)
                continue
            for day in data.get("gameWeek", []):
                for g in day.get("games", []):
                    if g.get("gameType") != 2:
                        continue
                    if g.get("gameState") not in ("OFF", "FINAL"):
                        continue
                    outcome = g.get("gameOutcome") or {}
                    if outcome.get("lastPeriodType") == "SO":
                        game_ids.add(g["id"])
            cursor += timedelta(days=7)
            time.sleep(0.2)
        return game_ids

    def get_splits_from_pbp(game_id):
        """
        Returns list of (shooter_id, goalie_id, scored) from SO period plays.
        Pure shootingPlayerId -> goalieInNetId mapping. No team logic.
        For older seasons where goalieInNetId is null on missed-shots,
        infers the goalie from other plays in the same game for that team.
        Returns (pairs, game_date, home_abbrev, away_abbrev)
        """
        try:
            resp = requests.get(f"{pbp_base}/gamecenter/{game_id}/play-by-play", timeout=15)
            resp.raise_for_status()
            pbp = resp.json()
        except Exception as e:
            if debug:
                print(f"    [warn] PBP failed game {game_id}: {e}")
            return [], None, None, None

        game_date = pbp.get("gameDate")
        home_abbrev = pbp.get("homeTeam", {}).get("abbrev")
        away_abbrev = pbp.get("awayTeam", {}).get("abbrev")
        home_id = pbp.get("homeTeam", {}).get("id")
        away_id = pbp.get("awayTeam", {}).get("id")

        # First pass: collect all SO plays and build team->goalie map from known plays
        so_plays = []
        team_goalie = {}  # eventOwnerTeamId -> goalie they faced (the OTHER team's goalie)
        for play in pbp.get("plays", []):
            if play.get("periodDescriptor", {}).get("periodType") != "SO":
                continue
            d = play.get("details", {})
            type_key    = play.get("typeDescKey", "")
            shooter_id  = d.get("shootingPlayerId") or d.get("scoringPlayerId")
            goalie_id   = d.get("goalieInNetId")
            owner_tid   = d.get("eventOwnerTeamId")
            shot_type   = d.get("shotType")
            miss_reason = d.get("reason")  # only on missed-shot
            if type_key not in ("shot-on-goal", "goal", "missed-shot", "failed-shot-attempt"):
                continue
            if not shooter_id:
                continue
            so_plays.append((shooter_id, goalie_id, type_key, owner_tid, shot_type, miss_reason))
            # If goalie is known, record which goalie this team faces
            if goalie_id and owner_tid:
                team_goalie[owner_tid] = goalie_id

        # If team_goalie is still incomplete, try rosterSpots as fallback
        # A goalie on teamId=X faces shooters from the OTHER team
        if len(team_goalie) < 2:
            so_goalie_ids = {goalie_id for _, goalie_id, _, _, _, _ in so_plays if goalie_id}
            for spot in pbp.get("rosterSpots", []):
                if spot.get("positionCode") != "G":
                    continue
                pid = spot.get("playerId")
                tid = spot.get("teamId")
                if pid not in so_goalie_ids:
                    continue
                # This goalie faced shots — shooting team is the OTHER team
                other_tid = away_id if tid == home_id else home_id
                if other_tid and other_tid not in team_goalie:
                    team_goalie[other_tid] = pid

        # Last resort: look up the game-level goalie stats which explicitly
        # record shootoutWins/shootoutLosses per goalie per game.
        # The winning goalie faced the losing team's shooters and vice versa.
        if len(team_goalie) < 2:
            try:
                g_url = (f"{stats_base}/goalie/shootout"
                         f"?isAggregate=false&isGame=true"
                         f"&cayenneExp=gameId={game_id}"
                         f"&start=0&limit=10")
                g_resp = requests.get(g_url, timeout=15)
                g_resp.raise_for_status()
                g_data = g_resp.json()
                for row in g_data.get("data", []):
                    gid  = row.get("playerId")
                    team = row.get("teamAbbrev", "")
                    wins = row.get("shootoutWins", 0)
                    loss = row.get("shootoutLosses", 0)
                    if not gid or (wins == 0 and loss == 0):
                        continue
                    # Find this goalie's teamId from rosterSpots
                    goalie_team_id = next(
                        (s.get("teamId") for s in pbp.get("rosterSpots", [])
                         if s.get("playerId") == gid and s.get("positionCode") == "G"),
                        None
                    )
                    if goalie_team_id:
                        # This goalie's OPPONENTS are the shooting team
                        shooting_tid = away_id if goalie_team_id == home_id else home_id
                        if shooting_tid and shooting_tid not in team_goalie:
                            team_goalie[shooting_tid] = gid
            except Exception as e:
                if debug:
                    print(f"    [warn] goalie stats fallback failed game {game_id}: {e}")

        # Second pass: build pairs with full detail, filling null goalies from team_goalie map
        pairs = []
        round_counter = {}  # team_id -> round number
        for shooter_id, goalie_id, type_key, owner_tid, shot_type, miss_reason in so_plays:
            if not goalie_id and owner_tid:
                goalie_id = team_goalie.get(owner_tid)
            if not shooter_id or not goalie_id:
                continue
            # Derive round number per team
            if owner_tid not in round_counter:
                round_counter[owner_tid] = 0
            round_counter[owner_tid] += 1
            round_num = round_counter[owner_tid]
            result = "goal" if type_key == "goal" else ("miss" if type_key in ("missed-shot","failed-shot-attempt") else "save")
            pairs.append((shooter_id, goalie_id, result, shot_type, miss_reason, round_num))
        return pairs, game_date, home_abbrev, away_abbrev

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
            pairs, game_date, home_abbrev, away_abbrev = get_splits_from_pbp(game_id)
            for shooter_id, goalie_id, result, shot_type, miss_reason, round_num in pairs:
                gname = goalie_names.get(goalie_id, f"Goalie #{goalie_id}")
                key = (shooter_id, goalie_id)
                if key not in all_splits:
                    all_splits[key] = [0, 0, gname]
                if result == "goal":
                    all_splits[key][0] += 1
                all_splits[key][1] += 1

                # Store individual attempt detail
                conn.execute("""
                    INSERT OR IGNORE INTO so_attempts
                        (game_id, season, game_date, home_team, away_team, round_num, shooter_id, goalie_id, result, shot_type, miss_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (game_id, season, game_date, home_abbrev, away_abbrev, round_num, shooter_id, goalie_id, result, shot_type, miss_reason))
            time.sleep(0.15)
        conn.commit()

        if debug:
            print(f"    Running total: {len(all_splits)} shooter-goalie pairs so far")

    for (sid, gid), (goals, att, gname) in all_splits.items():
        conn.execute("""
            INSERT INTO vs_goalie_splits (player_id, goalie_id, goalie_name, goals, attempts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(player_id, goalie_id) DO UPDATE SET
                goalie_name=excluded.goalie_name,
                goals=excluded.goals,
                attempts=excluded.attempts
        """, (sid, gid, gname, goals, att))
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM vs_goalie_splits").fetchone()[0]
    atts = conn.execute("SELECT COUNT(*) FROM so_attempts").fetchone()[0]
    conn.close()
    print(f"vs-goalie splits complete — {total} pairs, {atts} individual attempts stored.")



def backfill_game_dates(debug=False):
    """
    Update NULL game_date, home_team, away_team on existing so_attempts rows.
    Fetches PBP for each unique game_id that is missing this data.
    Much faster than a full --build-splits rerun.
    """
    conn = get_conn()
    pbp_base = "https://api-web.nhle.com/v1"

    # Find all game_ids that need updating
    missing = conn.execute("""
        SELECT DISTINCT game_id FROM so_attempts
        WHERE game_date IS NULL OR home_team IS NULL
        ORDER BY game_id
    """).fetchall()
    game_ids = [r[0] for r in missing]
    print(f"Backfilling dates/teams for {len(game_ids)} games...")

    updated = 0
    for i, game_id in enumerate(game_ids):
        try:
            resp = requests.get(f"{pbp_base}/gamecenter/{game_id}/play-by-play", timeout=15)
            resp.raise_for_status()
            pbp = resp.json()
            game_date  = pbp.get("gameDate")
            home_abbrev = pbp.get("homeTeam", {}).get("abbrev")
            away_abbrev = pbp.get("awayTeam", {}).get("abbrev")
            conn.execute("""
                UPDATE so_attempts
                SET game_date=?, home_team=?, away_team=?
                WHERE game_id=? AND (game_date IS NULL OR home_team IS NULL)
            """, (game_date, home_abbrev, away_abbrev, game_id))
            updated += 1
            if debug and i % 50 == 0:
                print(f"  {i}/{len(game_ids)} done...")
        except Exception as e:
            if debug:
                print(f"  [warn] game {game_id}: {e}")
        time.sleep(0.15)

    conn.commit()
    conn.close()
    print(f"Done — updated {updated} games.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="NHL Shootout Stats Pipeline v2")
    ap.add_argument("--init-db", action="store_true")
    ap.add_argument("--backfill", nargs=2, metavar=("START_SEASON", "END_SEASON"),
                    help="e.g. --backfill 20052006 20252026")
    ap.add_argument("--build-splits", nargs=2, metavar=("START_SEASON", "END_SEASON"),
                    help="Build vs-goalie splits e.g. --build-splits 20052006 20252026")
    ap.add_argument("--backfill-dates", action="store_true",
                    help="Fill in NULL game_date/home_team/away_team on existing so_attempts rows")
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
    if args.backfill_dates:
        backfill_game_dates(debug=args.debug)
    if args.update_rosters:
        update_rosters()
    if args.export_json:
        export_json()
    if not any([args.init_db, args.backfill, args.build_splits, args.backfill_dates,
                args.update_rosters, args.export_json]):
        print(__doc__)
