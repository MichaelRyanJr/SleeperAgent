#!/usr/bin/env python3
"""
sleeper_draft_and_keepers.py

Companion script for the SleeperAgent repo.

- Fetches draft results for each configured league from the Sleeper API.
- Writes a compact JSON summary per league to docs/draft_<league_id>.json
- Writes a markdown "mirror" of that JSON to docs/draft_<league_id>.md

For the keeper league, also computes keeper round costs based on the
current season's draft and the existing league_state_<league_id>.json file
that sleeper_sync.py already generates:

- docs/keeper_costs_<league_id>.json
- docs/keeper_costs_<league_id>.md

Finally, it rewrites docs/index.md to add links to the new files alongside
the existing state/teams/schedule/transactions/players_min/manifest/diff links.

This script is designed to be run AFTER sleeper_sync.py in your GitHub
Actions workflow so that league_state_<league_id>.json is already present.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

import requests


SLEEPER_BASE = "https://api.sleeper.app/v1"


@dataclass
class LeagueConfig:
    league_id: str
    label: str
    is_keeper: bool = False


# Known leagues for this repo.
LEAGUES: List[LeagueConfig] = [
    LeagueConfig(
        league_id="1181689020258160640",
        label="Grub from the Fantasy",
        is_keeper=True,
    ),
    LeagueConfig(
        league_id="1265837618587762688",
        label="Ryan Fantasy Football",
        is_keeper=False,
    ),
]


ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"


def _http_get(path: str) -> Any:
    """Small helper around requests.get with a nice error message."""
    url = f"{SLEEPER_BASE}{path}"
    resp = requests.get(url, timeout=30)
    try:
        resp.raise_for_status()
    except Exception as exc:  # defensive
        raise RuntimeError(f"GET {url} failed: {resp.status_code} {resp.text}") from exc
    return resp.json()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_league_state(league_id: str) -> Optional[Dict[str, Any]]:
    """
    Load the aggregated league_state_<league_id>.json file that sleeper_sync.py writes.

    Returns None if the file is missing.
    """
    state_path = DOCS / f"league_state_{league_id}.json"
    if not state_path.exists():
        return None
    with state_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def fetch_draft_for_league(league_id: str) -> Dict[str, Any]:
    """
    Fetch league + draft + picks from Sleeper.

    Returns a dict:
        {
            "league": <league_obj>,
            "draft": <draft_obj>,
            "picks": [ ... ]
        }
    """
    league = _http_get(f"/league/{league_id}")
    draft_id = league.get("draft_id")
    if not draft_id:
        # Fallback: get first draft from /league/<id>/drafts
        drafts = _http_get(f"/league/{league_id}/drafts")
        if not drafts:
            raise RuntimeError(f"No drafts found for league {league_id}")
        draft = drafts[0]
        draft_id = draft["draft_id"]
    else:
        draft = _http_get(f"/draft/{draft_id}")

    picks = _http_get(f"/draft/{draft_id}/picks")
    return {
        "league": league,
        "draft": draft,
        "picks": picks,
    }


def build_team_lookup_from_state(state: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """
    Given league_state JSON, return mapping roster_id -> { owner_display_name, team_name, keepers }.
    """
    teams = {}
    for roster_id_str, team in state.get("teams", {}).items():
        try:
            roster_id = int(roster_id_str)
        except ValueError:
            # Shouldn't happen, but be defensive.
            continue
        owner = team.get("owner", {})
        teams[roster_id] = {
            "owner_display_name": owner.get("display_name"),
            "team_name": owner.get("team_name"),
            "keepers": team.get("keepers", []),
        }
    return teams


def build_draft_summary(
    cfg: LeagueConfig,
    state: Optional[Dict[str, Any]],
    draft_bundle: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the JSON structure we want to write for draft_<league_id>.json"""
    league = draft_bundle["league"]
    draft = draft_bundle["draft"]
    picks = draft_bundle["picks"]

    season = int(league.get("season", 0)) if "season" in league else None
    league_name = None
    generated_at_state = None

    if state is not None:
        league_name = state.get("league", {}).get("name") or league.get("name")
        generated_at_state = state.get("generated_at")

    league_name = league_name or league.get("name") or cfg.label

    teams_by_roster = build_team_lookup_from_state(state) if state is not None else {}

    # Build pick list
    out_picks = []
    for p in picks:
        player_meta = p.get("metadata") or {}
        player_name = player_meta.get("full_name")
        if not player_name:
            first = player_meta.get("first_name") or ""
            last = player_meta.get("last_name") or ""
            player_name = (first + " " + last).strip() or None

        roster_id = p.get("roster_id")
        team_info = teams_by_roster.get(roster_id, {})
        owner_display_name = team_info.get("owner_display_name")
        owner_team_name = team_info.get("team_name")

        out_picks.append(
            {
                "overall_pick": p.get("pick_no"),
                "round": p.get("round"),
                "draft_slot": p.get("draft_slot"),
                "roster_id": roster_id,
                "owner_display_name": owner_display_name,
                "owner_team_name": owner_team_name,
                "player_id": p.get("player_id"),
                "player_name": player_name,
                "position": player_meta.get("position"),
                "nfl_team": player_meta.get("team"),
                "is_keeper": p.get("is_keeper"),
            }
        )

    return {
        "generated_at": _utc_now_iso(),
        "league_id": cfg.league_id,
        "league_name": league_name,
        "season": season,
        "draft_id": draft.get("draft_id"),
        "draft_type": draft.get("type"),
        "num_rounds": draft.get("settings", {}).get("rounds"),
        "num_teams": draft.get("settings", {}).get("teams"),
        "picks": out_picks,
        "source_generated_at": generated_at_state,
    }


def build_keeper_costs(
    cfg: LeagueConfig,
    state: Dict[str, Any],
    draft_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build keeper_costs_<league_id>.json structure based on:

    - `state["teams"][roster_id]["keepers"]` for who is a keeper
    - draft_summary["picks"] for which round each player was drafted

    Keeper rule (from user):
      - The round a player was drafted THIS YEAR is their keeper round cost.
      - Traded players keep the same round they were originally drafted in.
      - Round 1 picks cannot be kept.
    """
    if state is None:
        raise ValueError("keeper_costs requires league_state_<league_id>.json to exist")

    # Build player_id -> draft_round map
    player_to_round: Dict[str, Optional[int]] = {}
    for p in draft_summary.get("picks", []):
        pid = p.get("player_id")
        rnd = p.get("round")
        if pid is None or rnd is None:
            continue
        # If a player somehow appears multiple times, keep the EARLIEST round (smallest number).
        if pid not in player_to_round or (isinstance(rnd, int) and rnd < player_to_round[pid]):
            player_to_round[pid] = rnd

    league_name = state.get("league", {}).get("name") or cfg.label
    season = state.get("season")

    teams_out: Dict[str, Any] = {}
    for roster_id_str, team in state.get("teams", {}).items():
        owner = team.get("owner", {})
        keepers_in = team.get("keepers", []) or []

        keepers_out = []
        for player in keepers_in:
            pid = player.get("player_id")
            draft_round = player_to_round.get(pid)

            if draft_round is None:
                keepable = False
                reason = "not_drafted_this_year"
            elif draft_round == 1:
                keepable = False
                reason = "round_1_not_keepable"
            else:
                keepable = True
                reason = "ok"

            keepers_out.append(
                {
                    "player_id": pid,
                    "name": player.get("name"),
                    "position": player.get("position"),
                    "nfl_team": player.get("team"),
                    "draft_round": draft_round,
                    "keepable": keepable,
                    "reason": reason,
                }
            )

        teams_out[roster_id_str] = {
            "roster_id": team.get("roster_id"),
            "owner_id": team.get("owner_id"),
            "owner_display_name": owner.get("display_name"),
            "owner_team_name": owner.get("team_name"),
            "keepers": keepers_out,
        }

    return {
        "generated_at": _utc_now_iso(),
        "league_id": cfg.league_id,
        "league_name": league_name,
        "season": season,
        "rules": {
            "keeper_round_based_on": "current season draft round",
            "round_1_keepable": False,
            "note": "If a player was drafted this season in round N, they can be kept next season at round N; trades do not change the round cost.",
        },
        "teams": teams_out,
    }


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")
    tmp.replace(path)


def write_mirror(md_path: Path, json_filename: str, data: Dict[str, Any]) -> None:
    """
    Write a markdown "mirror" file that matches the style of the existing
    league_state_* mirrors:

    # filename.json (mirror)
        {
          ...
        }
    """
    json_text = json.dumps(data, indent=2, sort_keys=False)
    indented = "\n".join("    " + line for line in json_text.splitlines())

    md_path.parent.mkdir(parents=True, exist_ok=True)
    with md_path.open("w", encoding="utf-8") as f:
        f.write(f"# {json_filename} (mirror)\n\n")
        f.write(indented)
        f.write("\n")


def update_index_md() -> None:
    """
    Rebuild docs/index.md so that each league line also includes links to:

    - draft_<league_id>.json
    - keeper_costs_<league_id>.json (for keeper leagues)
    """
    built_at = _utc_now_iso()
    lines: List[str] = []
    lines.append("# SleeperAgent export")
    lines.append("")
    lines.append(f"built_at: {built_at}")
    lines.append("")

    for cfg in LEAGUES:
        state = load_league_state(cfg.league_id)
        league_name = cfg.label
        generated_at = None

        if state is not None:
            league_name = state.get("league", {}).get("name") or cfg.label
            generated_at = state.get("generated_at")

        # Default if state wasn't present
        generated_at = generated_at or "unknown"

        league_id = cfg.league_id

        # Paths relative to docs/ (what GitHub Pages serves)
        state_link = f"league_{league_id}/state.json"
        mirror_link = f"league_state_{league_id}.html"
        teams_link = f"league_{league_id}/teams.json"
        schedule_link = f"league_{league_id}/schedule.json"
        transactions_link = f"league_{league_id}/transactions.json"
        players_min_link = f"league_{league_id}/players_min.json"
        manifest_link = f"league_{league_id}/manifest.json"
        diff_link = f"league_{league_id}/diff.json"
        draft_link = f"draft_{league_id}.json"
        keeper_costs_link = f"keeper_costs_{league_id}.json"

        parts = [
            f"[state.json]({state_link})",
            f"[HTML mirror]({mirror_link})",
            f"[teams]({teams_link})",
            f"[schedule]({schedule_link})",
            f"[transactions]({transactions_link})",
            f"[players_min]({players_min_link})",
            f"[manifest]({manifest_link})",
            f"[diff]({diff_link})",
            f"[draft]({draft_link})",
        ]
        if cfg.is_keeper:
            parts.append(f"[keeper_costs]({keeper_costs_link})")

        parts_str = " | ".join(parts)
        lines.append(
            f"- {league_name} (ID {league_id}) — {parts_str} — generated_at: {generated_at}"
        )

    index_md = "\n".join(lines) + "\n"
    index_path = DOCS / "index.md"
    index_path.write_text(index_md, encoding="utf-8")


def main() -> None:
    DOCS.mkdir(exist_ok=True)

    for cfg in LEAGUES:
        league_id = cfg.league_id
        state = load_league_state(league_id)

        # Fetch draft & picks from Sleeper
        draft_bundle = fetch_draft_for_league(league_id)
        draft_summary = build_draft_summary(cfg, state, draft_bundle)

        # Write draft JSON + mirror
        draft_json_name = f"draft_{league_id}.json"
        draft_json_path = DOCS / draft_json_name
        draft_md_path = DOCS / f"draft_{league_id}.md"
        write_json(draft_json_path, draft_summary)
        write_mirror(draft_md_path, draft_json_name, draft_summary)

        # Keeper costs only for keeper leagues
        if cfg.is_keeper:
            if state is None:
                raise RuntimeError(
                    f"Expected league_state_{league_id}.json to exist for keeper league"
                )
            keeper_costs = build_keeper_costs(cfg, state, draft_summary)
            keeper_json_name = f"keeper_costs_{league_id}.json"
            keeper_json_path = DOCS / keeper_json_name
            keeper_md_path = DOCS / f"keeper_costs_{league_id}.md"
            write_json(keeper_json_path, keeper_costs)
            write_mirror(keeper_md_path, keeper_json_name, keeper_costs)

    # Rebuild hub index
    update_index_md()


if __name__ == "__main__":
    main()
