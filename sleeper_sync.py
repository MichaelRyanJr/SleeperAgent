#!/usr/bin/env python3
"""
Sleeper League Sync
-------------------
Pulls *all* essential data for a Sleeper league and emits a compact, ChatGPT‑friendly export
that you can upload in one go for trade/waiver analysis.

Features
- Pure standard library (no pip installs).
- Fetches league, users, rosters, matchups (for a range of weeks), transactions, drafts.
- Downloads the big players map once, then trims it to only the players used in your league.
- Produces both raw JSON files and tidy CSVs.
- Writes a single `league_state.json` summarizing the whole league for easy ingestion.
- Optionally zips the whole export folder.

Usage
------
python sleeper_sync.py --league 1265837618587762688 --season 2025 --weeks 1-5 --out ./sleeper_export --zip

Hints
- Omit --weeks to auto-detect the current NFL week and pull 1..current.
- Run again later; it will only add/overwrite the JSON in place and regenerate summaries fast.
- For multiple leagues, run once per league (use different --out paths or let the script name per‑league folders).
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

BASE = "https://api.sleeper.app/v1"
NFL_STATE_URL = f"{BASE}/state/nfl"
PLAYERS_URL = f"{BASE}/players/nfl"
USER_AGENT = "sleeper-sync/1.0 (stdlib)"

# ----------------------------
# HTTP helpers
# ----------------------------

def http_get_json(url: str, retry: int = 3, backoff: float = 0.75) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, retry + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retry:
                time.sleep(backoff * attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retry:
                time.sleep(backoff * attempt)
                continue
            raise


def league_url(league_id: str) -> str:
    return f"{BASE}/league/{league_id}"


def league_users_url(league_id: str) -> str:
    return f"{BASE}/league/{league_id}/users"


def league_rosters_url(league_id: str) -> str:
    return f"{BASE}/league/{league_id}/rosters"


def league_matchups_url(league_id: str, week: int) -> str:
    return f"{BASE}/league/{league_id}/matchups/{week}"


def league_transactions_url(league_id: str, week: int) -> str:
    return f"{BASE}/league/{league_id}/transactions/{week}"


def league_drafts_url(league_id: str) -> str:
    return f"{BASE}/league/{league_id}/drafts"


def draft_picks_url(draft_id: str) -> str:
    return f"{BASE}/draft/{draft_id}/picks"


# ----------------------------
# Core pull
# ----------------------------

def pull_league_bundle(league_id: str,
                        season: Optional[int],
                        weeks: Optional[Iterable[int]],
                        outdir: Path,
                        include_players: bool = True) -> Dict[str, Any]:
    outdir.mkdir(parents=True, exist_ok=True)

    # 1) League + state
    league = http_get_json(league_url(league_id))
    with open(outdir / "league.json", "w", encoding="utf-8") as f:
        json.dump(league, f, indent=2)

    nfl_state = http_get_json(NFL_STATE_URL)
    with open(outdir / "nfl_state.json", "w", encoding="utf-8") as f:
        json.dump(nfl_state, f, indent=2)

    if season is None:
        # Sleeper stores season as string (e.g., "2025")
        try:
            season = int(league.get("season"))
        except Exception:
            season = dt.datetime.now().year

    # 2) Users & rosters
    users = http_get_json(league_users_url(league_id))
    with open(outdir / "users.json", "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)

    rosters = http_get_json(league_rosters_url(league_id))
    with open(outdir / "rosters.json", "w", encoding="utf-8") as f:
        json.dump(rosters, f, indent=2)

    # 3) Drafts & picks (optional if league has no drafts)
    drafts = http_get_json(league_drafts_url(league_id))
    with open(outdir / "drafts.json", "w", encoding="utf-8") as f:
        json.dump(drafts, f, indent=2)

    all_picks: List[Dict[str, Any]] = []
    for d in drafts or []:
        did = str(d.get("draft_id"))
        if did:
            picks = http_get_json(draft_picks_url(did))
            all_picks.extend(picks)
    if all_picks:
        with open(outdir / "draft_picks.json", "w", encoding="utf-8") as f:
            json.dump(all_picks, f, indent=2)

    # 4) Figure out which weeks to pull
    if weeks is None:
        # Pull 1..current regular season week, falling back to 1..17
        current_week = int(nfl_state.get("week") or 17)
        weeks = range(1, current_week + 1)
    weeks = list(sorted(set(int(w) for w in weeks)))

    # 5) Matchups & transactions per week
    matchups_by_week: Dict[int, Any] = {}
    txns_by_week: Dict[int, Any] = {}
    for w in weeks:
        matchups = http_get_json(league_matchups_url(league_id, w))
        matchups_by_week[w] = matchups
        with open(outdir / f"matchups_week_{w}.json", "w", encoding="utf-8") as f:
            json.dump(matchups, f, indent=2)
        try:
            txns = http_get_json(league_transactions_url(league_id, w))
        except urllib.error.HTTPError as e:
            # Some years/leagues may not have txns endpoint by week; tolerate 404
            if e.code == 404:
                txns = []
            else:
                raise
        txns_by_week[w] = txns
        with open(outdir / f"transactions_week_{w}.json", "w", encoding="utf-8") as f:
            json.dump(txns, f, indent=2)
        time.sleep(0.05)  # be nice

    # 6) Build ID sets for trimming players
    used_ids: Set[str] = set()

    def collect_ids_from_rosters():
        for r in rosters:
            for pid in (r.get("players") or []):
                if pid:
                    used_ids.add(str(pid))
            # Team DEFs can be strings like "PHI" already
            for pid in (r.get("starters") or []):
                if pid:
                    used_ids.add(str(pid))

    def collect_ids_from_matchups():
        for w, matchups in matchups_by_week.items():
            for m in matchups or []:
                for pid in (m.get("players") or []):
                    if pid:
                        used_ids.add(str(pid))
                for pid in (m.get("starters") or []):
                    if pid:
                        used_ids.add(str(pid))

    def collect_ids_from_picks():
        for p in all_picks or []:
            pid = p.get("player_id")
            if pid:
                used_ids.add(str(pid))

    collect_ids_from_rosters()
    collect_ids_from_matchups()
    collect_ids_from_picks()

    players_min: Dict[str, Dict[str, Any]] = {}
    players_full_count = 0

    if include_players:
        # 7) Download and trim the big players map
        players_all = http_get_json(PLAYERS_URL)
        players_full_count = len(players_all)
        for pid in used_ids:
            pdata = players_all.get(pid)
            if pdata is None and pid.isalpha():
                # Team DEFs are keyed by team abbreviation in many dumps
                pdata = players_all.get(pid.upper())
            if pdata:
                players_min[pid] = {
                    "player_id": pid,
                    "full_name": pdata.get("full_name") or pdata.get("first_name"),
                    "first_name": pdata.get("first_name"),
                    "last_name": pdata.get("last_name"),
                    "position": pdata.get("position"),
                    "team": pdata.get("team"),
                    "status": pdata.get("status"),
                    "injury_status": pdata.get("injury_status"),
                    "age": pdata.get("age"),
                    "depth_chart_order": pdata.get("depth_chart_order"),
                    "fantasy_positions": pdata.get("fantasy_positions"),
                }
        with open(outdir / "players_min.json", "w", encoding="utf-8") as f:
            json.dump(players_min, f, indent=2)

    # 8) Build a tidy summary for ChatGPT ingestion
    summary = build_summary(league, users, rosters, matchups_by_week, players_min, season)
    with open(outdir / "league_state.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    # 9) Emit convenient CSVs
    write_csvs(outdir, summary)

    return {
        "league_id": league_id,
        "season": season,
        "weeks": weeks,
        "players_used": len(players_min),
        "players_full_catalog": players_full_count,
        "outdir": str(outdir.resolve()),
    }


# ----------------------------
# Derived summaries
# ----------------------------

def index_users(users: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    idx = {}
    for u in users or []:
        uid = str(u.get("user_id"))
        idx[uid] = {
            "user_id": uid,
            "username": u.get("username"),
            "display_name": u.get("display_name"),
            "team_name": (u.get("metadata") or {}).get("team_name") or u.get("display_name"),
        }
    return idx


def index_rosters(rosters: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    idx: Dict[int, Dict[str, Any]] = {}
    for r in rosters or []:
        rid = int(r.get("roster_id"))
        idx[rid] = r
    return idx


def build_summary(league: Dict[str, Any],
                  users: List[Dict[str, Any]],
                  rosters: List[Dict[str, Any]],
                  matchups_by_week: Dict[int, Any],
                  players_min: Dict[str, Dict[str, Any]],
                  season: int) -> Dict[str, Any]:
    user_idx = index_users(users)
    roster_idx = index_rosters(rosters)

    # Map roster_id -> owner info
    teams: Dict[int, Dict[str, Any]] = {}
    for r in rosters or []:
        rid = int(r.get("roster_id"))
        owner_id = str(r.get("owner_id"))
        u = user_idx.get(owner_id, {})
        s = r.get("settings") or {}
        teams[rid] = {
            "roster_id": rid,
            "owner_id": owner_id,
            "owner": {
                "display_name": u.get("display_name"),
                "username": u.get("username"),
                "team_name": u.get("team_name"),
            },
            "record": {
                "wins": s.get("wins", 0),
                "losses": s.get("losses", 0),
                "ties": s.get("ties", 0),
            },
            "points_for": float(s.get("fpts", 0)) + float(s.get("fpts_decimal", 0)) / 100.0,
            "points_against": float(s.get("fpts_against", 0)) + float(s.get("fpts_against_decimal", 0)) / 100.0,
            "waiver": {
                "position": s.get("waiver_position"),
                "budget_used": s.get("waiver_budget_used"),
            },
            "starters_current": r.get("starters") or [],
            "players_current": r.get("players") or [],
        }

    # Weekly schedule, results
    schedule: List[Dict[str, Any]] = []
    for week, ms in matchups_by_week.items():
        by_matchup: Dict[int, List[Dict[str, Any]]] = {}
        for m in ms or []:
            by_matchup.setdefault(int(m.get("matchup_id", -1)), []).append(m)
        for mid, pair in by_matchup.items():
            if len(pair) != 2:
                # Handle odd cases (doubleheaders/median etc.) by recording singles
                for side in pair:
                    schedule.append({
                        "week": week,
                        "roster_id": int(side.get("roster_id")),
                        "opponent_roster_id": None,
                        "points": float(side.get("points", 0)),
                        "result": None,
                    })
                continue
            a, b = pair[0], pair[1]
            ra, rb = int(a.get("roster_id")), int(b.get("roster_id"))
            pa, pb = float(a.get("points", 0)), float(b.get("points", 0))
            res_a = "W" if pa > pb else ("L" if pa < pb else "T")
            res_b = "W" if pb > pa else ("L" if pb < pa else "T")
            schedule.append({"week": week, "roster_id": ra, "opponent_roster_id": rb, "points": pa, "result": res_a})
            schedule.append({"week": week, "roster_id": rb, "opponent_roster_id": ra, "points": pb, "result": res_b})

    # Humanized players for current rosters
    def humanize(pid: str) -> Dict[str, Any]:
        meta = players_min.get(str(pid), {})
        return {
            "player_id": str(pid),
            "name": meta.get("full_name") or f"ID:{pid}",
            "position": meta.get("position"),
            "team": meta.get("team"),
            "injury_status": meta.get("injury_status"),
        }

    teams_pretty: Dict[int, Dict[str, Any]] = {}
    for rid, t in teams.items():
        starters_h = [humanize(p) for p in (t.get("starters_current") or [])]
        bench_h = [humanize(p) for p in (t.get("players_current") or []) if p not in (t.get("starters_current") or [])]
        teams_pretty[rid] = {
            **t,
            "starters": starters_h,
            "bench": bench_h,
        }

    # Final summary object
    summary = {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "season": season,
        "league": {
            "league_id": str(league.get("league_id")),
            "name": league.get("name"),
            "status": league.get("status"),
            "settings": {
                "scoring_settings": league.get("scoring_settings"),
                "roster_positions": league.get("roster_positions"),
                "playoff_teams": league.get("settings", {}).get("playoff_teams"),
            },
        },
        "teams": teams_pretty,
        "schedule": schedule,
    }
    return summary


# ----------------------------
# CSV writers
# ----------------------------

def write_csvs(outdir: Path, summary: Dict[str, Any]) -> None:
    teams = summary.get("teams", {})
    schedule = summary.get("schedule", [])

    # teams.csv
    with open(outdir / "teams.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["roster_id", "team_name", "owner", "wins", "losses", "ties", "points_for", "points_against", "waiver_position", "waiver_budget_used"])
        for rid, t in sorted(teams.items(), key=lambda kv: int(kv[0])):
            owner = t.get("owner", {})
            rec = t.get("record", {})
            w.writerow([
                rid,
                owner.get("team_name"),
                owner.get("display_name") or owner.get("username"),
                rec.get("wins", 0),
                rec.get("losses", 0),
                rec.get("ties", 0),
                f"{t.get('points_for', 0):.2f}",
                f"{t.get('points_against', 0):.2f}",
                (t.get("waiver") or {}).get("position"),
                (t.get("waiver") or {}).get("budget_used"),
            ])

    # roster_current.csv
    with open(outdir / "roster_current.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["roster_id", "slot", "player_id", "name", "position", "team", "injury_status", "is_starter"])
        for rid, t in teams.items():
            for idx, p in enumerate(t.get("starters") or []):
                w.writerow([rid, idx + 1, p.get("player_id"), p.get("name"), p.get("position"), p.get("team"), p.get("injury_status"), 1])
            bench = t.get("bench") or []
            for p in bench:
                w.writerow([rid, "", p.get("player_id"), p.get("name"), p.get("position"), p.get("team"), p.get("injury_status"), 0])

    # schedule_weekly.csv
    with open(outdir / "schedule_weekly.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["week", "roster_id", "opponent_roster_id", "points", "result"])
        for row in sorted(schedule, key=lambda r: (int(r.get("week", 0)), int(r.get("roster_id", 0)))):
            w.writerow([row.get("week"), row.get("roster_id"), row.get("opponent_roster_id"), f"{row.get('points', 0):.2f}", row.get("result")])


# ----------------------------
# ZIP helper
# ----------------------------

def zip_dir(folder: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in folder.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(folder))


# ----------------------------
# CLI
# ----------------------------

def parse_weeks(weeks_str: Optional[str]) -> Optional[List[int]]:
    if not weeks_str:
        return None
    out: List[int] = []
    for part in weeks_str.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Export a Sleeper league to tidy JSON/CSV for ChatGPT analysis.")
    ap.add_argument("--league", required=True, help="Sleeper league_id, e.g., 1265837618587762688")
    ap.add_argument("--season", type=int, default=None, help="Season (e.g., 2025). Defaults to league season.")
    ap.add_argument("--weeks", type=str, default=None, help="Weeks to pull (e.g., '1-4' or '1,2,5'). Default=1..current week")
    ap.add_argument("--out", type=str, default="./sleeper_export", help="Output directory (will create if missing)")
    ap.add_argument("--skip-players", action="store_true", help="Skip downloading the full players map (not recommended on first run)")
    ap.add_argument("--zip", dest="do_zip", action="store_true", help="Zip the export folder when done")

    args = ap.parse_args()

    league_id = str(args.league)
    out_base = Path(args.out)
    outdir = out_base / f"league_{league_id}_{args.season or 'auto'}"

    weeks = parse_weeks(args.weeks)

    meta = pull_league_bundle(
        league_id=league_id,
        season=args.season,
        weeks=weeks,
        outdir=outdir,
        include_players=not args.skip_players,
    )

    print("Export complete:\n" + json.dumps(meta, indent=2))

    if args.do_zip:
        zip_path = outdir.with_suffix(".zip")
        zip_dir(outdir, zip_path)
        print(f"Zipped to: {zip_path}")


if __name__ == "__main__":
    main()
