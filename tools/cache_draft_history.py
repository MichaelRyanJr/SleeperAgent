#!/usr/bin/env python3
"""Cache historical Sleeper drafts for keeper-league analysis.

For a current keeper league, follow Sleeper's previous_league_id chain and cache
completed historical league/draft data under:

    docs/_history/league_<league_id>/league.json
    docs/_history/league_<league_id>/drafts.json
    docs/_history/league_<league_id>/draft_picks.json

The newest current-season run also receives draft_history_index.json so callers
can discover every cached season without knowing historical Sleeper league IDs.

This history is analysis-only. Keeper-cost inference remains based on the
immediately previous season in enrich_keeper_data.py.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

BASE = "https://api.sleeper.app/v1"
USER_AGENT = "sleeper-sync/1.5 (draft-history)"


def http_get_json(url: str, retry: int = 3, backoff: float = 0.75) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, retry + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < retry:
                time.sleep(backoff * attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retry:
                time.sleep(backoff * attempt)
                continue
            raise


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def newest_run_dir(out_base: Path, league_id: str) -> Path:
    candidates = [p for p in out_base.glob(f"league_{league_id}_*") if p.is_dir()]
    if not candidates:
        raise FileNotFoundError(
            f"No run folder found for league {league_id} under {out_base}"
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def fetch_league(league_id: str) -> Dict[str, Any]:
    return http_get_json(f"{BASE}/league/{league_id}") or {}


def fetch_drafts(league_id: str) -> List[Dict[str, Any]]:
    return http_get_json(f"{BASE}/league/{league_id}/drafts") or []


def fetch_draft_picks(drafts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    picks: List[Dict[str, Any]] = []
    for draft in drafts:
        draft_id = str(draft.get("draft_id") or "")
        if not draft_id:
            continue
        draft_picks = http_get_json(f"{BASE}/draft/{draft_id}/picks") or []
        picks.extend({"draft_id": draft_id, **pick} for pick in draft_picks)
        time.sleep(0.02)
    return picks


def cache_one_season(out_base: Path, league_id: str) -> Dict[str, Any]:
    cache_dir = out_base / "_history" / f"league_{league_id}"
    cache_dir.mkdir(parents=True, exist_ok=True)

    league_path = cache_dir / "league.json"
    drafts_path = cache_dir / "drafts.json"
    picks_path = cache_dir / "draft_picks.json"

    league = load_json(league_path, None)
    if league is None:
        league = fetch_league(league_id)
        league_path.write_text(json.dumps(league, indent=2), encoding="utf-8")

    drafts = load_json(drafts_path, None)
    if drafts is None:
        drafts = fetch_drafts(league_id)
        drafts_path.write_text(json.dumps(drafts, indent=2), encoding="utf-8")

    picks = load_json(picks_path, None)
    if picks is None:
        picks = fetch_draft_picks(drafts)
        picks_path.write_text(json.dumps(picks, indent=2), encoding="utf-8")

    return {
        "season": str(league.get("season") or "") or None,
        "league_id": league_id,
        "previous_league_id": (
            str(league.get("previous_league_id") or "") or None
        ),
        "draft_count": len(drafts),
        "pick_count": len(picks),
        "cache_dir": f"docs/_history/league_{league_id}",
        "draft_picks_file": f"docs/_history/league_{league_id}/draft_picks.json",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cache all historical draft seasons for a keeper league."
    )
    parser.add_argument("--league", required=True, help="Current Sleeper league ID")
    parser.add_argument("--out", default="./docs", help="SleeperAgent docs directory")
    parser.add_argument(
        "--max-seasons",
        type=int,
        default=10,
        help="Safety limit for previous_league_id traversal (default: 10)",
    )
    args = parser.parse_args()

    league_id = str(args.league)
    out_base = Path(args.out)
    run_dir = newest_run_dir(out_base, league_id)
    current_league = load_json(run_dir / "league.json", {})

    max_keepers = int((current_league.get("settings") or {}).get("max_keepers") or 0)
    if max_keepers <= 0:
        print(f"League {league_id} is not keeper-enabled; skipping draft history cache")
        return 0

    history: List[Dict[str, Any]] = []
    seen = set()
    historical_id: Optional[str] = (
        str(current_league.get("previous_league_id") or "") or None
    )

    while historical_id and historical_id not in seen and len(history) < args.max_seasons:
        seen.add(historical_id)
        entry = cache_one_season(out_base, historical_id)
        history.append(entry)
        historical_id = entry.get("previous_league_id")

    index = {
        "current_season": current_league.get("season"),
        "current_league_id": league_id,
        "historical_seasons_cached": len(history),
        "max_seasons_safety_limit": args.max_seasons,
        "history": history,
    }
    (run_dir / "draft_history_index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )

    seasons = ", ".join(
        str(entry.get("season") or entry.get("league_id")) for entry in history
    ) or "none"
    print(f"Cached keeper draft history: {seasons}")
    print(f"Wrote {run_dir / 'draft_history_index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
