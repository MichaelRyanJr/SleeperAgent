#!/usr/bin/env python3
"""
Sleeper League Sync (normalized outputs + keeper-aware)
------------------------------------------------------
Pulls essential data for a Sleeper league and emits a compact, ChatGPT‑friendly export.
In addition to the single-file summary, this version writes a *normalized API* per run:

Inside the run folder (e.g., docs/league_<ID>_auto/):
  - state.json            → tidy league snapshot (same content as league_state.json)
  - league_state.json     → backward-compatible name of state.json
  - teams.json            → team-level fields only (record, PF/PA, waiver, keepers)
  - schedule.json         → all matchups in the season to date
  - transactions.json     → flattened list of league transactions with week tags
  - players_min.json      → trimmed player metadata actually referenced by the league
  - lineups/<week>.json   → per-week starters/bench for each roster (humanized)
  - drafts.json           → list of drafts
  - draft_picks.json      → picks across drafts (used for keeper detection)
  - teams.csv, roster_current.csv, schedule_weekly.csv → convenience CSVs

Usage
------
python sleeper_sync.py --league 1181689020258160640 --season 2025 --weeks 1-6 --out ./docs

Notes
- If --season is omitted, we infer it from the league object.
- If --weeks is omitted, we pull 1..current NFL week from /state/nfl.
- Keepers are inferred from draft picks metadata ("is_keeper" or similar flags). If a league
  doesn’t use keeper flags in picks, the keeper tagging simply stays False.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Set

BASE = "https://api.sleeper.app/v1"
NFL_STATE_URL = f"{BASE}/state/nfl"
PLAYERS_URL = f"{BASE}/players/nfl"
USER_AGENT = "sleeper-sync/1.2 (stdlib)"

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


def league_url(league_id: str) -> str: return f"{BASE}/league/{league_id}"

def league_users_url(league_id: str) -> str: return f"{BASE}/league/{league_id}/users"

def league_rosters_url(league_id: str) -> str: return f"{BASE}/league/{league_id}/rosters"

def league_matchups_url(league_id: str, week: int) -> str: return f"{BASE}/league/{league_id}/matchups/{week}"

def league_transactions_url(league_id: str, week: int) -> str: return f"{BASE}/league/{league_id}/transactions/{week}"

def league_drafts_url(league_id: str) -> str: return f"{BASE}/league/{league_id}/drafts"

def draft_picks_url(draft_id: str) -> str: return f"{BASE}/draft/{draft_id}/picks"

# ----------------------------
# Keeper detection
# ----------------------------

def _truthy(x: Any) -> bool:
    if x is True: return True
    if x is False or x is None: return False
    s = str(x).strip().lower()
    return s in {"1", "true", "yes", "y", "t"}


def detect_keepers_from_picks(all_picks: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """Return mapping {user_id -> set(player_id)} for picks flagged as keepers."""
    keepers: Dict[str, Set[str]] = {}
    for p in all_picks or []:
        md = p.get("metadata") or {}
        flags = [
            p.get("is_keeper"), md.get("is_keeper"), md.get("keeper"), md.get("was_keeper"),
            md.get("isKeeper"), (md.get("keeper_status") or "").lower() == "keeper",
        ]
        if any(_truthy(f) for f in flags):
            uid = str(p.get("picked_by") or p.get("owner_id") or "")
            pid = str(p.get("player_id") or "")
            if uid and pid:
                keepers.setdefault(uid, set()).add(pid)
    return keepers

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
    (outdir / "league.json").write_text(json.dumps(league, indent=2), encoding="utf-8")

    nfl_state = http_get_json(NFL_STATE_URL)
    (outdir / "nfl_state.json").write_text(json.dumps(nfl_state, indent=2), encoding="utf-8")

    if season is None:
        try:
            season = int(league.get("season"))
        except Exception:
            from datetime import datetime
            season = datetime.now().year

    # 2) Users & rosters
    users = http_get_json(league_users_url(league_id))
    (outdir / "users.json").write_text(json.dumps(users, indent=2), encoding="utf-8")

    rosters = http_get_json(league_rosters_url(league_id))
    (outdir / "rosters.json").write_text(json.dumps(rosters, indent=2), encoding="utf-8")

    # 3) Drafts & picks (for keepers)
    drafts = http_get_json(league_drafts_url(league_id))
    (outdir / "drafts.json").write_text(json.dumps(drafts, indent=2), encoding="utf-8")

    all_picks: List[Dict[str, Any]] = []
    for d in drafts or []:
        did = str(d.get("draft_id"))
        if did:
            picks = http_get_json(draft_picks_url(did))
            all_picks.extend(picks)
    if all_picks:
        (outdir / "draft_picks.json").write_text(json.dumps(all_picks, indent=2), encoding="utf-8")

    keeper_map = detect_keepers_from_picks(all_picks)

    # 4) Figure out weeks
    if weeks is None:
        current_week = int(nfl_state.get("week") or 17)
        weeks = range(1, current_week + 1)
    weeks = list(sorted(set(int(w) for w in weeks)))

    # 5) Matchups & transactions per week
    matchups_by_week: Dict[int, Any] = {}
    txns_by_week: Dict[int, Any] = {}
    for w in weeks:
        matchups = http_get_json(league_matchups_url(league_id, w))
        matchups_by_week[w] = matchups
        (outdir / f"matchups_week_{w}.json").write_text(json.dumps(matchups, indent=2), encoding="utf-8")
        try:
            txns = http_get_json(league_transactions_url(league_id, w))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                txns = []
            else:
                raise
        txns_by_week[w] = txns
        (outdir / f"transactions_week_{w}.json").write_text(json.dumps(txns, indent=2), encoding="utf-8")
        time.sleep(0.05)

    # 6) Build player-id set for trimming
    used_ids: Set[str] = set()
    for r in rosters:
        for pid in (r.get("players") or []):
            if pid: used_ids.add(str(pid))
        for pid in (r.get("starters") or []):
            if pid: used_ids.add(str(pid))
    for ms in matchups_by_week.values():
        for m in ms or []:
            for pid in (m.get("players") or []):
                if pid: used_ids.add(str(pid))
            for pid in (m.get("starters") or []):
                if pid: used_ids.add(str(pid))
    for p in all_picks or []:
        pid = p.get("player_id")
        if pid: used_ids.add(str(pid))

    players_min: Dict[str, Dict[str, Any]] = {}
    players_full_count = 0
    if include_players:
        players_all = http_get_json(PLAYERS_URL)
        players_full_count = len(players_all)
        for pid in used_ids:
            pdata = players_all.get(pid) or players_all.get(str(pid).upper())
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
        (outdir / "players_min.json").write_text(json.dumps(players_min, indent=2), encoding="utf-8")

    # 7) Build tidy summary (keeper-aware)
    summary = build_summary(league, users, rosters, matchups_by_week, players_min, season, keeper_map)
    # Normalized outputs
    (outdir / "state.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (outdir / "league_state.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Teams-only view
    teams_lite = []
    for rid, t in (summary.get("teams") or {}).items():
        teams_lite.append({
            "roster_id": int(rid),
            "owner": t.get("owner"),
            "record": t.get("record"),
            "points_for": t.get("points_for"),
            "points_against": t.get("points_against"),
            "waiver": t.get("waiver"),
            "keepers": t.get("keepers", []),
        })
    (outdir / "teams.json").write_text(json.dumps(sorted(teams_lite, key=lambda x: x["roster_id"]), indent=2), encoding="utf-8")

    # Schedule-only view
    (outdir / "schedule.json").write_text(json.dumps(summary.get("schedule", []), indent=2), encoding="utf-8")

    # Transactions (flatten + tag week)
    flat_txns: List[Dict[str, Any]] = []
    for w, txns in txns_by_week.items():
        for tx in txns or []:
            flat_txns.append({"week": int(w), **tx})
    (outdir / "transactions.json").write_text(json.dumps(flat_txns, indent=2), encoding="utf-8")

    # Lineups per week (humanized)
    lineups_dir = outdir / "lineups"
    lineups_dir.mkdir(parents=True, exist_ok=True)
    for w, ms in matchups_by_week.items():
        # Map matchup_id -> sides, to get opponents
        by_matchup: Dict[int, List[Dict[str, Any]]] = {}
        for m in ms or []:
            by_matchup.setdefault(int(m.get("matchup_id", -1)), []).append(m)
        entries: List[Dict[str, Any]] = []
        for mid, pair in by_matchup.items():
            # Build a quick opponent map
            opp = {}
            if len(pair) == 2:
                a, b = pair
                opp[int(a.get("roster_id"))] = int(b.get("roster_id"))
                opp[int(b.get("roster_id"))] = int(a.get("roster_id"))
            for side in pair:
                rid = int(side.get("roster_id"))
                starters = [humanize_pid(p, players_min) for p in (side.get("starters") or []) if p and str(p) != "0"]
                bench = []
                all_ps = [p for p in (side.get("players") or []) if p]
                if all_ps:
                    bench = [humanize_pid(p, players_min) for p in all_ps if p not in (side.get("starters") or []) and str(p) != "0"]
                entries.append({
                    "roster_id": rid,
                    "opponent_roster_id": opp.get(rid),
                    "points": float(side.get("points", 0)),
                    "starters": starters,
                    "bench": bench,
                })
        (lineups_dir / f"{int(w)}.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")

    # 8) CSVs
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

def humanize_pid(pid: Any, players_min: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    spid = str(pid)
    if spid == "0" or not spid:
        return None
    meta = players_min.get(spid, {})
    return {
        "player_id": spid,
        "name": meta.get("full_name") or f"ID:{spid}",
        "position": meta.get("position"),
        "team": meta.get("team"),
        "injury_status": meta.get("injury_status"),
    }


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
                  season: int,
                  keeper_map: Dict[str, Set[str]]) -> Dict[str, Any]:
    user_idx = index_users(users)
    roster_idx = index_rosters(rosters)

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

    # Weekly schedule
    schedule: List[Dict[str, Any]] = []
    for week, ms in matchups_by_week.items():
        by_matchup: Dict[int, List[Dict[str, Any]]] = {}
        for m in ms or []:
            by_matchup.setdefault(int(m.get("matchup_id", -1)), []).append(m)
        for mid, pair in by_matchup.items():
            if len(pair) != 2:
                for side in pair:
                    schedule.append({
                        "week": int(week),
                        "roster_id": int(side.get("roster_id")),
                        "opponent_roster_id": None,
                        "points": float(side.get("points", 0)),
                        "result": None,
                    })
                continue
            a, b = pair[0], pair[1]
            ra, rb = int(a.get("roster_id")), int(b.get("roster_id"))
            pa, pb = float(a.get("points", 0)), float(b.get("points", 0))
            if pa == 0.0 and pb == 0.0:
                res_a = res_b = None
            else:
                res_a = "W" if pa > pb else ("L" if pa < pb else "T")
                res_b = "W" if pb > pa else ("L" if pb < pa else "T")
            schedule.append({"week": int(week), "roster_id": ra, "opponent_roster_id": rb, "points": pa, "result": res_a})
            schedule.append({"week": int(week), "roster_id": rb, "opponent_roster_id": ra, "points": pb, "result": res_b})

    # Helper to humanize + keeper-tag
    def humanize(pid: str) -> Dict[str, Any]:
        if str(pid) == "0":
            return None
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
        owner_keeper_ids = keeper_map.get(t.get("owner_id"), set())

        starters_h = [humanize(p) for p in (t.get("starters_current") or [])]
        starters_h = [p for p in starters_h if p is not None]
        bench_h = [humanize(p) for p in (t.get("players_current") or []) if p not in (t.get("starters_current") or [])]
        bench_h = [p for p in bench_h if p is not None]

        for p in starters_h:
            p["keeper"] = p["player_id"] in owner_keeper_ids
        for p in bench_h:
            p["keeper"] = p["player_id"] in owner_keeper_ids

        keepers_list = [p for p in (starters_h + bench_h) if p.get("keeper")]

        teams_pretty[rid] = {
            **t,
            "starters": starters_h,
            "bench": bench_h,
            "keepers": keepers_list,
        }

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

    with open(outdir / "teams.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["roster_id", "team_name", "owner", "wins", "losses", "ties", "points_for", "points_against", "waiver_position", "waiver_budget_used", "keepers"])
        for rid, t in sorted(teams.items(), key=lambda kv: int(kv[0])):
            owner = t.get("owner", {})
            rec = t.get("record", {})
            keepers = ", ".join(p.get("name") for p in t.get("keepers", []))
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
                keepers,
            ])

    with open(outdir / "roster_current.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["roster_id", "slot", "player_id", "name", "position", "team", "injury_status", "is_starter", "keeper"])
        for rid, t in teams.items():
            for idx, p in enumerate(t.get("starters") or []):
                w.writerow([rid, idx + 1, p.get("player_id"), p.get("name"), p.get("position"), p.get("team"), p.get("injury_status"), 1, int(bool(p.get("keeper")))])
            bench = t.get("bench") or []
            for p in bench:
                w.writerow([rid, "", p.get("player_id"), p.get("name"), p.get("position"), p.get("team"), p.get("injury_status"), 0, int(bool(p.get("keeper")))])

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
    ap = argparse.ArgumentParser(description="Export a Sleeper league to tidy JSON/CSV for ChatGPT analysis (keeper-aware, normalized outputs).")
    ap.add_argument("--league", required=True, help="Sleeper league_id, e.g., 1181689020258160640")
    ap.add_argument("--season", type=int, default=None, help="Season (e.g., 2025). Defaults to league season.")
    ap.add_argument("--weeks", type=str, default=None, help="Weeks to pull (e.g., '1-6' or '1,2,5'). Default=1..current week")
    ap.add_argument("--out", type=str, default="./docs", help="Output directory (will create if missing)")
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
