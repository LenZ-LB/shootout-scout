"""
NHL Shootout Stats — Data Pipeline
===================================
Run this on YOUR machine/server (not in Claude's sandbox — it has no internet
access to nhl.com). This script is a working starting point, not a black box:
review the endpoints below, they are the public, unauthenticated NHL API
(api-web.nhle.com) that powers NHL.com itself. Endpoints occasionally change
shape between seasons — if a call starts failing, check the response with
--debug and adjust the parsing function, the rest of the pipeline doesn't
change.

WHAT THIS BUILDS
-----------------
A local SQLite database (shootout.db) with three tables:

  players              player_id, full_name, team_abbrev, position, active
  goalies              goalie_id, full_name, team_abbrev, active
  shootout_attempts    game_id, season, game_date, shooter_id, goalie_id,
                       shooter_team, goalie_team, result ('goal'/'miss'), period

Everything on your Shootout Stats page (career totals, per-season splits,
vs-specific-goalie splits, team breakdowns) is a GROUP BY / WHERE query over
shootout_attempts — you never hand-maintain totals, they're always derived.

WHAT THIS CANNOT GET FROM NHL.com
-----------------------------------
Junior/AHL shootout data (the "JUNOR/AHL" block in your template) isn't in the
NHL API. That has to stay a manually logged table (see `manual_attempts`
below) fed from Instat/your own tracking — same as you're doing today, just
normalized into the same schema so the page can display it alongside NHL data.

HOW TO RUN
----------
  pip install requests
  python nhl_shootout_pipeline.py --init-db
  python nhl_shootout_pipeline.py --backfill 2005 2026   # one-time historical load
  python nhl_shootout_pipeline.py --update-today          # run daily via cron/Task Scheduler
  python nhl_shootout_pipeline.py --update-rosters        # run daily, catches trades/call-ups

SUGGESTED SCHEDULE (cron, run on a machine that stays on, or a small VM):
  0 9 * * *  python nhl_shootout_pipeline.py --update-rosters
  */30 * * * *  python nhl_shootout_pipeline.py --update-today   # during game windows
Your website's backend then just reads shootout.db (or a Postgres copy of it
if the site has multiple concurrent users) — it never talks to NHL.com directly.
"""

import argparse
import sqlite3
import sys
import time
from datetime import date, timedelta

import requests

DB_PATH = "shootout.db"
BASE = "https://api-web.nhle.com/v1"
TEAM_ABBREVS = [
    "ANA", "BOS", "BUF", "CGY", "CAR", "CHI", "COL", "CBJ", "DAL", "DET",
    "EDM", "FLA", "LAK", "MIN", "MTL", "NSH", "NJD", "NYI", "NYR", "OTT",
    "PHI", "PIT", "SJS", "SEA", "STL", "TBL", "TOR", "UTA", "VAN", "VGK",
    "WSH", "WPG",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    team_abbrev TEXT,
    position TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS goalies (
    goalie_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    team_abbrev TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS shootout_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    game_date TEXT NOT NULL,
    round_num INTEGER,
    shooter_id INTEGER NOT NULL,
    shooter_team TEXT,
    goalie_id INTEGER NOT NULL,
    goalie_team TEXT,
    result TEXT CHECK(result IN ('goal', 'miss')) NOT NULL,
    UNIQUE(game_id, shooter_id, round_num)
);

-- Manually logged (Junior/AHL, or anything outside NHL.com's data) — same
-- shape as shootout_attempts so queries can UNION the two seamlessly.
CREATE TABLE IF NOT EXISTS manual_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_date TEXT,
    league TEXT,             -- 'AHL', 'WHL', 'OHL', 'QMJHL', etc.
    season TEXT,
    shooter_name TEXT NOT NULL,
    shooter_team TEXT,
    goalie_name TEXT,
    goalie_team TEXT,
    result TEXT CHECK(result IN ('goal', 'miss')) NOT NULL,
    logged_by TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_so_shooter ON shootout_attempts(shooter_id);
CREATE INDEX IF NOT EXISTS idx_so_goalie ON shootout_attempts(goalie_id);
CREATE INDEX IF NOT EXISTS idx_so_season ON shootout_attempts(season);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"Initialized {DB_PATH}")


def update_rosters(debug=False):
    conn = get_conn()
    for team in TEAM_ABBREVS:
        url = f"{BASE}/roster/{team}/current"
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [warn] roster fetch failed for {team}: {e}")
            continue

        for group_key, is_goalie in (("forwards", False), ("defensemen", False), ("goalies", True)):
            for p in data.get(group_key, []):
                pid = p["id"]
                name = f"{p['firstName']['default']} {p['lastName']['default']}"
                pos = p.get("positionCode", "")
                if is_goalie:
                    sql = """INSERT INTO goalies (goalie_id, full_name, team_abbrev, active)
                             VALUES (?, ?, ?, 1)
                             ON CONFLICT(goalie_id) DO UPDATE SET
                               full_name=excluded.full_name, team_abbrev=excluded.team_abbrev, active=1"""
                    conn.execute(sql, (pid, name, team))
                else:
                    sql = """INSERT INTO players (player_id, full_name, team_abbrev, position, active)
                             VALUES (?, ?, ?, ?, 1)
                             ON CONFLICT(player_id) DO UPDATE SET
                               full_name=excluded.full_name, team_abbrev=excluded.team_abbrev,
                               position=excluded.position, active=1"""
                    conn.execute(sql, (pid, name, team, pos))
        conn.commit()
        time.sleep(0.3)  # be polite to the API
        if debug:
            print(f"  rostered {team}")
    conn.close()
    print("Rosters updated.")


def _extract_shootout_attempts(pbp_json):
    """Pull shootout rows out of a play-by-play payload. Returns list of dicts."""
    attempts = []
    for play in pbp_json.get("plays", []):
        # Shootout plays live in period descriptor periodType == 'SO'
        period_desc = play.get("periodDescriptor", {})
        if period_desc.get("periodType") != "SO":
            continue
        details = play.get("details", {})
        type_key = play.get("typeDescKey", "")
        if type_key not in ("goal", "shot-on-goal", "missed-shot", "shootout-attempt"):
            continue
        shooter_id = details.get("shootingPlayerId") or details.get("scoringPlayerId")
        goalie_id = details.get("goalieInNetId")
        if not shooter_id or not goalie_id:
            continue
        result = "goal" if type_key == "goal" else "miss"
        attempts.append({
            "shooter_id": shooter_id,
            "goalie_id": goalie_id,
            "shooter_team": details.get("eventOwnerTeamId"),
            "result": result,
            "round_num": play.get("sortOrder"),
        })
    return attempts


def fetch_game(game_id, season, game_date, debug=False):
    url = f"{BASE}/gamecenter/{game_id}/play-by-play"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [warn] pbp fetch failed for game {game_id}: {e}")
        return []
    if debug:
        print(f"  fetched game {game_id}")
    attempts = _extract_shootout_attempts(data)
    for a in attempts:
        a["game_id"] = game_id
        a["season"] = season
        a["game_date"] = game_date
    return attempts


def store_attempts(conn, attempts):
    for a in attempts:
        conn.execute(
            """INSERT OR IGNORE INTO shootout_attempts
               (game_id, season, game_date, round_num, shooter_id, shooter_team, goalie_id, goalie_team, result)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (a["game_id"], a["season"], a["game_date"], a.get("round_num"),
             a["shooter_id"], a.get("shooter_team"), a["goalie_id"], a.get("goalie_team"), a["result"]),
        )
    conn.commit()


def update_today(debug=False):
    """Pull today's schedule, fetch play-by-play for any completed game, store shootout rows."""
    today = date.today().isoformat()
    url = f"{BASE}/schedule/{today}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    conn = get_conn()
    for day in data.get("gameWeek", []):
        for g in day.get("games", []):
            if g.get("gameState") not in ("OFF", "FINAL"):
                continue
            game_id = g["id"]
            season = str(g.get("season"))
            game_date = day.get("date", today)
            attempts = fetch_game(game_id, season, game_date, debug=debug)
            if attempts:
                store_attempts(conn, attempts)
                print(f"  game {game_id}: stored {len(attempts)} shootout attempts")
    conn.close()


def backfill(start_year, end_year, debug=False):
    """
    Historical load since the shootout began (2005-06). This walks the
    schedule week by week for each season and pulls play-by-play for every
    completed game — it's slow (NHL has ~1,300 games/season) and polite
    sleeps are intentional. Expect this to take a while; run it once,
    overnight, not in a loop.
    """
    conn = get_conn()
    for year in range(start_year, end_year):
        season = f"{year}{year+1}"
        print(f"Season {season}...")
        cursor_date = date(year, 10, 1)
        end_date = date(year + 1, 6, 30)
        while cursor_date <= end_date:
            url = f"{BASE}/schedule/{cursor_date.isoformat()}"
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  [warn] schedule fetch failed {cursor_date}: {e}")
                cursor_date += timedelta(days=7)
                continue
            for day in data.get("gameWeek", []):
                for g in day.get("games", []):
                    if g.get("gameState") not in ("OFF", "FINAL"):
                        continue
                    if g.get("gameType") != 2:  # regular season only, no shootouts in playoffs
                        continue
                    attempts = fetch_game(g["id"], season, day.get("date"), debug=debug)
                    if attempts:
                        store_attempts(conn, attempts)
                        print(f"  {day.get('date')} game {g['id']}: {len(attempts)} attempts")
                    time.sleep(0.2)
            cursor_date += timedelta(days=7)
    conn.close()


def export_json(out_dir="data"):
    """
    Dump the database into flat JSON files the static page reads directly:
      data/players.json  — every shooter with career/season/vs-goalie splits
      data/goalies.json  — every goalie with career shootout save totals
      data/minor.json    — passthrough of manually logged Junior/AHL data
    Run this after any --update-* call, then commit the data/ folder.
    """
    import os, json
    os.makedirs(out_dir, exist_ok=True)
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    players_out = []
    cur.execute("SELECT player_id, full_name, team_abbrev FROM players")
    for pr in cur.fetchall():
        pid = pr["player_id"]
        cur2 = conn.cursor()
        cur2.execute("SELECT result, COUNT(*) c FROM shootout_attempts WHERE shooter_id=? GROUP BY result", (pid,))
        totals = {r["result"]: r["c"] for r in cur2.fetchall()}
        goals, att = totals.get("goal", 0), totals.get("goal", 0) + totals.get("miss", 0)
        if att == 0:
            continue  # no logged attempts, skip

        seasons = {}
        cur2.execute("SELECT season, result, COUNT(*) c FROM shootout_attempts WHERE shooter_id=? GROUP BY season, result", (pid,))
        for r in cur2.fetchall():
            s = seasons.setdefault(r["season"], [0, 0])
            if r["result"] == "goal":
                s[0] += r["c"]
            s[1] += r["c"]

        vs_goalie = {}
        cur2.execute("""SELECT g.full_name gname, sa.result, COUNT(*) c
                        FROM shootout_attempts sa JOIN goalies g ON g.goalie_id = sa.goalie_id
                        WHERE sa.shooter_id=? GROUP BY g.full_name, sa.result""", (pid,))
        for r in cur2.fetchall():
            vg = vs_goalie.setdefault(r["gname"], [0, 0])
            if r["result"] == "goal":
                vg[0] += r["c"]
            vg[1] += r["c"]

        players_out.append({
            "id": pid, "name": pr["full_name"], "team": pr["team_abbrev"],
            "career": [goals, att], "seasons": seasons, "vs_goalie": vs_goalie,
        })

    goalies_out = []
    cur.execute("SELECT goalie_id, full_name, team_abbrev FROM goalies")
    for gr in cur.fetchall():
        gid = gr["goalie_id"]
        cur2 = conn.cursor()
        cur2.execute("SELECT result, COUNT(*) c FROM shootout_attempts WHERE goalie_id=? GROUP BY result", (gid,))
        totals = {r["result"]: r["c"] for r in cur2.fetchall()}
        stopped, faced = totals.get("miss", 0), totals.get("goal", 0) + totals.get("miss", 0)
        if faced == 0:
            continue
        goalies_out.append({
            "id": gid, "name": gr["full_name"], "team": gr["team_abbrev"],
            "stopped": stopped, "faced": faced, "record": None,
        })

    with open(os.path.join(out_dir, "players.json"), "w") as f:
        json.dump(players_out, f, indent=2)
    with open(os.path.join(out_dir, "goalies.json"), "w") as f:
        json.dump(goalies_out, f, indent=2)
    conn.close()
    print(f"Exported {len(players_out)} players, {len(goalies_out)} goalies to {out_dir}/")



    ap = argparse.ArgumentParser()
    ap.add_argument("--init-db", action="store_true")
    ap.add_argument("--update-rosters", action="store_true")
    ap.add_argument("--update-today", action="store_true")
    ap.add_argument("--backfill", nargs=2, type=int, metavar=("START_YEAR", "END_YEAR"))
    ap.add_argument("--export-json", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.init_db:
        init_db()
    if args.update_rosters:
        update_rosters(debug=args.debug)
    if args.update_today:
        update_today(debug=args.debug)
    if args.backfill:
        backfill(args.backfill[0], args.backfill[1], debug=args.debug)
    if args.export_json:
        export_json()
    if not any([args.init_db, args.update_rosters, args.update_today, args.backfill, args.export_json]):
        print(__doc__)
