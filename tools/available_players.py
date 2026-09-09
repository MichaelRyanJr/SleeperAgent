#!/usr/bin/env python3
"""Derive unrostered players from Sleeper snapshots, not private waiver claims.

No rankings are implied. The default view hides inactive/unaffiliated catalog
entries; the all view retains them. League ownership refreshes every sync;
player metadata uses a shared, timestamped 24-hour catalog cache.
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable

PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
CATALOG_FIELDS = (
    "full_name", "first_name", "last_name", "position", "fantasy_positions",
    "team", "status", "injury_status", "active", "age", "years_exp",
    "depth_chart_order", "depth_chart_position", "number", "news_updated",
)
SLOT_POSITIONS = {
    "FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "REC_FLEX": {"WR", "TE"}, "WRRB_FLEX": {"WR", "RB"},
    "IDP_FLEX": {"DL", "LB", "DB"}, "DL": {"DL", "DE", "DT", "NT"},
    "LB": {"LB", "ILB", "OLB", "MLB"}, "DB": {"DB", "CB", "S", "FS", "SS"},
}
NOTICE = (
    "Unrostered does not mean immediately claimable. The public API does not "
    "provide an authoritative per-player waiver/free-agent lock, claim deadline, "
    "or private pending claim list. Confirm acquisition in the Sleeper app."
)


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def player_id(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    result = str(value).strip()
    return "" if result.lower() in {"", "0", "none", "null"} else result.upper()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_catalog(data: Any) -> None:
    if not isinstance(data, dict) or not data or any(not isinstance(p, dict) for p in data.values()):
        raise ValueError("Missing or malformed full Sleeper player catalog")


def load_player_catalog(fetch: Callable[[str], Any], cache_path: Path,
                        now: str | None = None) -> tuple[dict, dict]:
    """Reuse a shared catalog for 24 hours. Never relabel stale metadata as fresh."""
    clock_override = now
    now = now or utcnow()
    current = parse_time(now)
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        validate_catalog(cached["players"])
        age = (current - parse_time(cached["fetched_at"])).total_seconds()
        if cached.get("schema_version") == 1 and 0 <= age < 86400:
            return cached["players"], {"url": PLAYERS_URL, "fetched_at": cached["fetched_at"],
                                       "cache_hit": True, "max_age_seconds": 86400}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raw = fetch(PLAYERS_URL)
    validate_catalog(raw)
    catalog = {player_id(pid): {key: p.get(key) for key in CATALOG_FIELDS}
               for pid, p in raw.items() if player_id(pid)}
    validate_catalog(catalog)
    fetched_at = clock_override or utcnow()
    write_json(cache_path, {"schema_version": 1, "fetched_at": fetched_at, "players": catalog})
    return catalog, {"url": PLAYERS_URL, "fetched_at": fetched_at,
                     "cache_hit": False, "max_age_seconds": 86400}


def validate_rosters(league: dict, rosters: Any) -> set[str]:
    """Refuse a partial roster response rather than declaring owned players free."""
    if not isinstance(rosters, list) or not rosters:
        raise ValueError("Missing roster list; availability cannot be established")
    roster_ids, owned = set(), set()
    expected = league.get("total_rosters") or (league.get("settings") or {}).get("num_teams")
    if expected and len(rosters) != int(expected):
        raise ValueError(f"Expected {expected} rosters, received {len(rosters)}")
    for roster in rosters:
        if not isinstance(roster, dict) or roster.get("roster_id") is None or "players" not in roster:
            raise ValueError("Roster lacks its ID or players field")
        rid = str(roster["roster_id"])
        if rid in roster_ids:
            raise ValueError("Duplicate roster ID")
        roster_ids.add(rid)
        for field in ("players", "starters", "reserve", "taxi"):
            values = roster.get(field)
            if values is None:
                continue
            if not isinstance(values, list):
                raise ValueError(f"Invalid {field} list on roster {rid}")
            owned.update(player_id(v) for v in values if player_id(v))
    return owned


def eligible_positions(league: dict) -> set[str]:
    slots = league.get("roster_positions")
    if not isinstance(slots, list) or not slots:
        raise ValueError("Missing roster positions; cannot determine fantasy eligibility")
    result = set()
    for slot in slots:
        if slot not in {"BN", "IR", "TAXI"}:
            result.update(SLOT_POSITIONS.get(slot, {slot}))
    return result


def candidate_visible(p: dict) -> bool:
    status = str(p.get("status") or "").lower()
    if status in {"retired", "inactive"}:
        return False
    return bool(p.get("team") or p.get("active") is True or status in {
        "active", "injured reserve", "ir", "pup", "suspended", "practice squad",
    })


def build_snapshot(league: dict, rosters: list, drafts: list, picks: list,
                   catalog: dict | None, sources: dict, generated_at: str | None = None) -> dict:
    """Use current rosters; additionally exclude draft assignments until settled.

    Completed historical picks must NOT permanently exclude a later-dropped
    player. During pre-draft/drafting, conservative roster union also excludes
    prior-season holdings; the snapshot explicitly says this is provisional.
    """
    generated_at = generated_at or utcnow()
    owned = validate_rosters(league, rosters)
    allowed = eligible_positions(league)
    if not isinstance(drafts, list) or not isinstance(picks, list):
        raise ValueError("Missing draft metadata/picks")
    current_drafts = {}
    for d in drafts:
        if not isinstance(d, dict) or not d.get("draft_id"):
            raise ValueError("Malformed draft metadata")
        if str(d.get("season", league.get("season"))) == str(league.get("season")):
            current_drafts[str(d["draft_id"])] = d
    league_status = str(league.get("status") or "unknown")
    unresolved = league_status not in {"in_season", "complete"} or any(
        d.get("status") != "complete" for d in current_drafts.values())
    draft_ids = {str(d["draft_id"]) for d in drafts}
    reserved, drafted_rounds = set(), {}
    warnings = [NOTICE]
    orphan_picks = False
    for pick in picks:
        if not isinstance(pick, dict):
            raise ValueError("Malformed draft pick")
        pid = player_id(pick.get("player_id"))
        if not pid:
            continue
        did = str(pick.get("draft_id") or "")
        if did in draft_ids and did not in current_drafts:
            continue
        d = current_drafts.get(did)
        if d is None:
            orphan_picks = True
        if d is None or d.get("status") != "complete" or league_status not in {"in_season", "complete"}:
            reserved.add(pid)  # Includes manual slots with is_keeper=null.
        try:
            rnd = int(pick.get("round"))
            if rnd > 0:
                drafted_rounds[pid] = min(rnd, drafted_rounds.get(pid, rnd))
        except (TypeError, ValueError):
            pass
    unresolved = unresolved or orphan_picks
    if unresolved:
        warnings.append("Provisional draft-period pool: roster holdings and current draft assignments "
                        "are excluded. Old carried-over roster players may still be excluded; this is "
                        "not a verified final post-draft waiver list.")
    if orphan_picks:
        warnings.append("Some picks lack matching draft metadata and are conservatively excluded.")
    disabled = (league.get("settings") or {}).get("disable_adds")
    if disabled in (1, True, "1"):
        warnings.append("Sleeper reports that league adds are disabled.")
    blocked = owned | reserved
    rows = []
    if catalog is None:
        warnings.append("Player catalog was skipped; empty output is NOT evidence of an empty waiver pool.")
    else:
        validate_catalog(catalog)
        for raw_pid, p in catalog.items():
            pid = player_id(raw_pid)
            positions = p.get("fantasy_positions") or [p.get("position")]
            if not isinstance(positions, list):
                raise ValueError(f"Invalid fantasy positions for {pid}")
            matches = sorted(allowed.intersection(positions))
            if not pid or pid in blocked or not matches:
                continue
            name = p.get("full_name") or " ".join(str(p.get(k) or "") for k in ("first_name", "last_name")).strip() or f"ID:{pid}"
            rows.append({
                "player_id": pid, "name": name, "position": p.get("position"),
                "fantasy_positions": positions, "eligible_positions": matches,
                "team": p.get("team"), "age": p.get("age"), "years_exp": p.get("years_exp"),
                "status": p.get("status"), "injury_status": p.get("injury_status"),
                "active": p.get("active"), "depth_chart_order": p.get("depth_chart_order"),
                "depth_chart_position": p.get("depth_chart_position"), "news_updated": p.get("news_updated"),
                "in_default_view": candidate_visible(p), "availability": "unrostered_at_snapshot",
                "waiver_status": "unknown", "claim_deadline": None,
                "can_add_now": False if disabled in (1, True, "1") else None,
                "current_season_draft_round": drafted_rounds.get(pid),
            })
    rows.sort(key=lambda p: (str(p["position"] or ""), not bool(p["team"]), p["name"].casefold(), p["player_id"]))
    visible = [p for p in rows if p["in_default_view"]]
    return {
        "schema_version": 1, "generated_at": generated_at,
        "stale_after": (parse_time(generated_at) + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "league_id": str(league["league_id"]), "league_name": league.get("name"),
        "season": str(league.get("season")), "league_status": league_status,
        "valid": catalog is not None, "is_provisional": unresolved or catalog is None,
        "availability_basis": "rosters_plus_unsettled_draft_assignments" if unresolved else "current_rosters",
        "sources": sources, "warnings": warnings, "eligible_positions": sorted(allowed),
        "excluded_rostered_count": len(owned), "excluded_draft_only_count": len(reserved - owned),
        "excluded_owned_ids_missing_from_catalog": sorted(owned - {player_id(v) for v in (catalog or {})}),
        "catalog_player_count": len(catalog or {}), "default_player_count": len(visible),
        "all_player_count": len(rows), "player_count": len(visible),
        "position_counts": dict(sorted(Counter(p["position"] or "UNKNOWN" for p in visible).items())),
        "view": "active_or_nfl_affiliated_candidates", "players": visible, "all_players": rows,
    }


def render_html(snapshot: dict) -> str:
    esc = lambda v: html.escape("" if v is None else str(v), quote=True)
    title = f"Available players - {snapshot.get('league_name') or snapshot['league_id']}"
    status = "UNAVAILABLE" if not snapshot["valid"] else "PROVISIONAL" if snapshot["is_provisional"] else "UNROSTERED SNAPSHOT"
    out = ["<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">",
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           f"<title>{esc(title)}</title>",
           '<style>body{font:16px/1.5 system-ui;margin:2em;max-width:1200px}table{border-collapse:collapse;width:100%}th,td{padding:.45em;text-align:left;border-bottom:1px solid #ccc}th{position:sticky;top:0;background:white}input,select{font:inherit;padding:.35em}small{display:block}.notice{border:2px solid;padding:1em}#stale{font-weight:bold}</style>',
           f"<h1>{esc(title)}</h1><p><strong>{status}</strong> | Season {esc(snapshot['season'])} | League {esc(snapshot['league_id'])}</p>",
           f"<p>Snapshot: <time>{esc(snapshot['generated_at'])}</time><small>Catalog metadata fetched: {esc(snapshot['sources'].get('players', {}).get('fetched_at'))}</small></p>",
           '<p id="stale" role="status"></p><div class="notice">',
           *(f"<p>{esc(warning)}</p>" for warning in snapshot["warnings"]),
           '</div><p><a href="available_players.json">JSON</a> | <a href="available_players.csv">CSV</a> | <a href="available_players_all.json">All eligible unrostered catalog entries (includes inactive players)</a></p>',
           '<p>Alphabetical within position, not fantasy rankings. Injuries and depth charts are Sleeper metadata, not live news.</p>',
           '<label>Search <input id="q" type="search" placeholder="Name, NFL team, player ID"></label> ',
           '<label>Position <select id="pos"><option value="">All positions</option>',
           *(f'<option value="{esc(p)}">{esc(p)}</option>' for p in snapshot["eligible_positions"]),
           '</select></label><p id="count"></p><div style="overflow:auto"><table><thead><tr><th>Player</th><th>Position</th><th>NFL team</th><th>Age</th><th>Injury</th><th>Status</th><th>Depth order</th><th>Draft round</th></tr></thead><tbody>']
    for p in snapshot["players"]:
        out.append(f'<tr data-pos="{esc(" ".join(p["eligible_positions"]))}"><td>{esc(p["name"])}<small>{esc(p["player_id"])}</small></td>' +
                   "".join(f"<td>{esc(p.get(k))}</td>" for k in ("position", "team", "age", "injury_status", "status", "depth_chart_order", "current_season_draft_round")) + "</tr>")
    out.append('</tbody></table></div><script>\n' +
               'const expiry = ' + json.dumps(snapshot["stale_after"]) + ';\n' +
               '''const rows = Array.from(document.querySelectorAll('tbody tr'));
function filter(){const q=document.getElementById('q').value.toLowerCase();const p=document.getElementById('pos').value;let count=0;rows.forEach(r=>{r.hidden=!(r.textContent.toLowerCase().includes(q)&&(!p||r.dataset.pos.split(' ').includes(p)));if(!r.hidden)count++;});document.getElementById('count').textContent=count+' of '+rows.length+' players shown';}
function age(){document.getElementById('stale').textContent=Date.now()>Date.parse(expiry)?'STALE SNAPSHOT: more than one hour old. Recheck the next sync or Sleeper before acting.':'';}
document.getElementById('q').addEventListener('input',filter);document.getElementById('pos').addEventListener('change',filter);filter();age();setInterval(age,60000);
</script></html>''')
    return "\n".join(out)


def write_outputs(outdir: Path, snapshot: dict) -> None:
    """Write default + complete JSON, flat timestamped CSV, and a static HTML table."""
    public = {k: v for k, v in snapshot.items() if k != "all_players"}
    write_json(outdir / "available_players.json", public)
    all_rows = snapshot["all_players"]
    write_json(outdir / "available_players_all.json", {
        **public, "view": "all_eligible_unrostered_including_inactive", "players": all_rows,
        "player_count": len(all_rows),
        "position_counts": dict(sorted(Counter(p["position"] or "UNKNOWN" for p in all_rows).items())),
    })
    fields = ["generated_at", "league_id", "season", "is_provisional", "player_id", "name",
              "position", "fantasy_positions", "team", "age", "status", "injury_status",
              "depth_chart_order", "current_season_draft_round", "availability", "waiver_status",
              "can_add_now", "player_metadata_fetched_at"]
    with (outdir / "available_players.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for p in public["players"]:
            row = {**p, **{k: snapshot[k] for k in ("generated_at", "league_id", "season", "is_provisional")},
                   "fantasy_positions": "/".join(p["fantasy_positions"]),
                   "player_metadata_fetched_at": snapshot["sources"].get("players", {}).get("fetched_at")}
            # Guard text from being interpreted as spreadsheet formulas.
            writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for k, v in row.items()})
    (outdir / "available_players.html").write_text(render_html(snapshot), encoding="utf-8")
