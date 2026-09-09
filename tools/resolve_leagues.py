#!/usr/bin/env python3
"""Resolve stable Sleeper league anchors to the current NFL season.

Sleeper creates a new league_id when a league is renewed for a new season.
This helper follows each league's previous_league_id chain using one member's
Sleeper account, then exports the resolved IDs to GitHub Actions via GITHUB_ENV.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = "https://api.sleeper.app/v1"
USER_AGENT = "sleeper-agent-league-resolver/1.0"


def get_json(url: str, retries: int = 3) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError):
            if attempt >= retries:
                raise
            time.sleep(0.75 * attempt)
    raise RuntimeError("unreachable")


def get_user_id(username: str) -> str:
    encoded = urllib.parse.quote(username, safe="")
    user = get_json(f"{BASE}/user/{encoded}")
    user_id = str((user or {}).get("user_id") or "")
    if not user_id:
        raise RuntimeError(f"Could not resolve Sleeper user {username!r}")
    return user_id


def get_target_season() -> int:
    override = os.environ.get("TARGET_SEASON", "").strip()
    if override:
        return int(override)
    state = get_json(f"{BASE}/state/nfl")
    season = state.get("season")
    if season is None:
        raise RuntimeError("Sleeper NFL state did not include a season")
    return int(season)


def user_leagues(user_id: str, season: int) -> list[dict[str, Any]]:
    data = get_json(f"{BASE}/user/{user_id}/leagues/nfl/{season}")
    return list(data or [])


def resolve_league(base_league_id: str, user_id: str, target_season: int) -> dict[str, Any]:
    league = get_json(f"{BASE}/league/{base_league_id}")
    current_id = str(league.get("league_id") or base_league_id)
    current_name = str(league.get("name") or "")
    current_season = int(league.get("season") or target_season)

    history = [{
        "season": current_season,
        "league_id": current_id,
        "name": current_name,
        "match": "anchor",
    }]

    if current_season >= target_season:
        return {
            "anchor_league_id": base_league_id,
            "resolved_league_id": current_id,
            "resolved_season": current_season,
            "history": history,
        }

    for season in range(current_season + 1, target_season + 1):
        leagues = user_leagues(user_id, season)

        linked = [
            item for item in leagues
            if str(item.get("previous_league_id") or "") == current_id
        ]

        match_type = "previous_league_id"
        if len(linked) == 1:
            successor = linked[0]
        elif len(linked) > 1:
            raise RuntimeError(
                f"Multiple {season} leagues point back to {current_id}; "
                "cannot choose safely."
            )
        else:
            same_name = [
                item for item in leagues
                if current_name and str(item.get("name") or "") == current_name
            ]
            if len(same_name) == 1:
                successor = same_name[0]
                match_type = "exact_name_fallback"
                print(
                    f"::warning::No previous_league_id match for {current_id} "
                    f"in {season}; using exact name match {current_name!r}.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"::warning::No renewed league found for {current_name!r} "
                    f"({current_id}) in {season}; keeping the latest resolved league.",
                    file=sys.stderr,
                )
                break

        current_id = str(successor.get("league_id"))
        current_name = str(successor.get("name") or current_name)
        current_season = int(successor.get("season") or season)
        history.append({
            "season": current_season,
            "league_id": current_id,
            "name": current_name,
            "match": match_type,
        })

    return {
        "anchor_league_id": base_league_id,
        "resolved_league_id": current_id,
        "resolved_season": current_season,
        "history": history,
    }


def main() -> int:
    anchors = os.environ.get("BASE_LEAGUES", "").split()
    username = os.environ.get("SLEEPER_USERNAME", "").strip()

    if not anchors:
        print("::error::BASE_LEAGUES is empty", file=sys.stderr)
        return 2
    if not username:
        print("::error::SLEEPER_USERNAME is empty", file=sys.stderr)
        return 2

    user_id = get_user_id(username)
    target_season = get_target_season()
    resolutions = [
        resolve_league(anchor, user_id, target_season)
        for anchor in anchors
    ]
    resolved_ids = [r["resolved_league_id"] for r in resolutions]

    env_path = os.environ.get("GITHUB_ENV")
    if env_path:
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"LEAGUES={' '.join(resolved_ids)}\n")
            f.write(f"RESOLVED_SEASON={target_season}\n")

    print(json.dumps({
        "target_season": target_season,
        "username": username,
        "resolutions": resolutions,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
