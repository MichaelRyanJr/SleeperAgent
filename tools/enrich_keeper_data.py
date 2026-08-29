#!/usr/bin/env python3
"""Add keeper-cost and traded-pick context to a SleeperAgent export.

Outputs in the newest per-run folder for the supplied league:
  - keeper_board.json  Humanized roster-by-roster keeper candidates with prior
                       draft round, acquisition path, and submitted keeper round.
  - traded_picks.json  Current Sleeper traded-pick records for the league.

Keeper-cost policy encoded here matches this league project's rules:
  - Round 1 players are not keepable.
  - Drafted players kept by their drafter use the prior/original draft round.
  - Traded players retain that prior/original draft round.
  - Waiver/free-agent costs are left unknown unless Sleeper already assigned a
    submitted keeper round; the exporter does not invent a house rule.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

BASE = "https://api.sleeper.app/v1"
USER_AGENT = "sleeper-sync/1.3 (keeper-enrichment)"


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


def newest_run_dir(out_base: Path, league_id: str) -> Path:
    candidates = [p for p in out_base.glob(f"league_{league_id}_*") if p.is_dir()]
    if not candidates:
        raise FileNotFoundError(f"No run folder found for league {league_id} under {out_base}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def league_url(league_id: str) -> str:
    return f"{BASE}/league/{league_id}"


def league_drafts_url(league_id: str) -> str:
    return f"{BASE}/league/{league_id}/drafts"


def league_rosters_url(league_id: str) -> str:
    return f"{BASE}/league/{league_id}/rosters"


def draft_picks_url(draft_id: str) -> str:
    return f"{BASE}/draft/{draft_id}/picks"


def league_transactions_url(league_id: str, week: int) -> str:
    return f"{BASE}/league/{league_id}/transactions/{week}"


def league_traded_picks_url(league_id: str) -> str:
    return f"{BASE}/league/{league_id}/traded_picks"


def truthy(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "t"}


def is_keeper_pick(pick: Dict[str, Any]) -> bool:
    md = pick.get("metadata") or {}
    flags = [
        pick.get("is_keeper"),
        md.get("is_keeper"),
        md.get("keeper"),
        md.get("was_keeper"),
        md.get("isKeeper"),
        (md.get("keeper_status") or "").lower() == "keeper",
    ]
    return any(truthy(flag) for flag in flags)


def fetch_draft_picks(league_id: str) -> List[Dict[str, Any]]:
    drafts = http_get_json(league_drafts_url(league_id)) or []
    picks: List[Dict[str, Any]] = []
    for draft in drafts:
        draft_id = str(draft.get("draft_id") or "")
        if not draft_id:
            continue
        for pick in http_get_json(draft_picks_url(draft_id)) or []:
            picks.append({"draft_id": draft_id, **pick})
    return picks


def draft_index(picks: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Map player id to its earliest recorded draft round/pick metadata."""
    out: Dict[str, Dict[str, Any]] = {}
    for pick in picks:
        pid = str(pick.get("player_id") or "")
        if not pid:
            continue
        try:
            round_no = int(pick.get("round"))
        except (TypeError, ValueError):
            continue
        candidate = {
            "round": round_no,
            "pick_no": pick.get("pick_no"),
            "draft_slot": pick.get("draft_slot"),
            "roster_id": pick.get("roster_id"),
            "picked_by": str(pick.get("picked_by") or "") or None,
            "draft_id": pick.get("draft_id"),
        }
        previous = out.get(pid)
        if previous is None or round_no < int(previous["round"]):
            out[pid] = candidate
    return out


def submitted_keeper_index(picks: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for pick in picks:
        if not is_keeper_pick(pick):
            continue
        pid = str(pick.get("player_id") or "")
        if not pid:
            continue
        try:
            round_no = int(pick.get("round"))
        except (TypeError, ValueError):
            round_no = None
        out[pid] = {
            "round": round_no,
            "roster_id": pick.get("roster_id"),
            "picked_by": str(pick.get("picked_by") or "") or None,
            "pick_no": pick.get("pick_no"),
        }
    return out


def fetch_transactions(league_id: str, weeks: Iterable[int] = range(1, 19)) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for week in weeks:
        try:
            txns = http_get_json(league_transactions_url(league_id, int(week))) or []
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        for txn in txns:
            out.append({"league_id": league_id, "week": int(week), **txn})
        time.sleep(0.02)
    return out


def latest_acquisition(
    player_id: str,
    roster_targets: Dict[str, int],
    transactions: Iterable[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Find the latest add of player_id to the owner's roster in each league.

    Renewed Sleeper leagues can reassign roster_id values, so roster_targets maps
    each league_id to the roster_id owned by the current manager in that season.
    """
    latest: Optional[Tuple[int, Dict[str, Any]]] = None
    for txn in transactions:
        txn_league_id = str(txn.get("league_id") or "")
        expected_roster_id = roster_targets.get(txn_league_id)
        if expected_roster_id is None:
            continue
        adds = txn.get("adds") or {}
        destination = adds.get(player_id)
        if destination is None:
            destination = adds.get(str(player_id))
        try:
            destination_id = int(destination)
        except (TypeError, ValueError):
            continue
        if destination_id != int(expected_roster_id):
            continue
        try:
            created = int(txn.get("created") or 0)
        except (TypeError, ValueError):
            created = 0
        event = {
            "type": txn.get("type") or "unknown",
            "transaction_id": txn.get("transaction_id"),
            "created": txn.get("created"),
            "league_id": txn_league_id or None,
            "week": txn.get("week"),
        }
        if latest is None or created >= latest[0]:
            latest = (created, event)
    return latest[1] if latest else None


def load_run_transactions(run_dir: Path, league_id: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path in sorted(run_dir.glob("transactions_week_*.json")):
        try:
            week = int(path.stem.rsplit("_", 1)[-1])
        except ValueError:
            continue
        for txn in load_json(path, []) or []:
            out.append({"league_id": league_id, "week": week, **txn})
    return out


def load_or_fetch_previous_history(
    out_base: Path, previous_league_id: str
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Cache completed prior-season data so scheduled syncs do not refetch it."""
    cache_dir = out_base / "_history" / f"league_{previous_league_id}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    league_path = cache_dir / "league.json"
    rosters_path = cache_dir / "rosters.json"
    picks_path = cache_dir / "draft_picks.json"
    txns_path = cache_dir / "transactions.json"

    if all(path.exists() for path in (league_path, rosters_path, picks_path, txns_path)):
        return (
            load_json(league_path, {}),
            load_json(rosters_path, []),
            load_json(picks_path, []),
            load_json(txns_path, []),
        )

    previous_league = http_get_json(league_url(previous_league_id)) or {}
    previous_rosters = http_get_json(league_rosters_url(previous_league_id)) or []
    previous_picks = fetch_draft_picks(previous_league_id)
    previous_transactions = fetch_transactions(previous_league_id)

    league_path.write_text(json.dumps(previous_league, indent=2), encoding="utf-8")
    rosters_path.write_text(json.dumps(previous_rosters, indent=2), encoding="utf-8")
    picks_path.write_text(json.dumps(previous_picks, indent=2), encoding="utf-8")
    txns_path.write_text(json.dumps(previous_transactions, indent=2), encoding="utf-8")
    return previous_league, previous_rosters, previous_picks, previous_transactions


def iso_from_ms(value: Any) -> Optional[str]:
    try:
        return dt.datetime.fromtimestamp(int(value) / 1000, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich a SleeperAgent run with keeper costs and traded picks.")
    parser.add_argument("--league", required=True, help="Current Sleeper league ID")
    parser.add_argument("--out", default="./docs", help="SleeperAgent docs/output directory")
    args = parser.parse_args()

    league_id = str(args.league)
    out_base = Path(args.out)
    run_dir = newest_run_dir(out_base, league_id)

    league = load_json(run_dir / "league.json", {})
    rosters = load_json(run_dir / "rosters.json", [])
    users = load_json(run_dir / "users.json", [])
    players_min = load_json(run_dir / "players_min.json", {})
    current_picks = load_json(run_dir / "draft_picks.json", [])

    traded_picks = http_get_json(league_traded_picks_url(league_id)) or []
    (run_dir / "traded_picks.json").write_text(json.dumps(traded_picks, indent=2), encoding="utf-8")

    user_by_id = {str(u.get("user_id")): u for u in users or []}
    max_keepers = int((league.get("settings") or {}).get("max_keepers") or 0)
    keeper_enabled = max_keepers > 0

    previous_league_id = str(league.get("previous_league_id") or "") or None
    previous_picks: List[Dict[str, Any]] = []
    previous_transactions: List[Dict[str, Any]] = []
    previous_rosters: List[Dict[str, Any]] = []
    previous_season = None

    # Historical draft/transaction enrichment is useful only for keeper leagues.
    # Prior-season data is cached because a completed season is effectively static.
    if keeper_enabled and previous_league_id:
        previous_league, previous_rosters, previous_picks, previous_transactions = (
            load_or_fetch_previous_history(out_base, previous_league_id)
        )
        previous_season = previous_league.get("season")

    current_transactions = load_run_transactions(run_dir, league_id) if keeper_enabled else []
    all_transactions = previous_transactions + current_transactions

    prior_draft = draft_index(previous_picks)
    current_draft = draft_index(current_picks)
    submitted = submitted_keeper_index(current_picks)
    previous_roster_by_owner = {
        str(r.get("owner_id") or ""): int(r.get("roster_id") or 0)
        for r in previous_rosters or []
        if r.get("owner_id") is not None
    }

    teams: List[Dict[str, Any]] = []
    for roster in sorted(rosters or [], key=lambda r: int(r.get("roster_id") or 0)):
        roster_id = int(roster.get("roster_id") or 0)
        owner_id = str(roster.get("owner_id") or "")
        user = user_by_id.get(owner_id, {})
        team_name = (user.get("metadata") or {}).get("team_name") or user.get("display_name")
        player_rows: List[Dict[str, Any]] = []

        for raw_pid in roster.get("players") or []:
            pid = str(raw_pid)
            meta = players_min.get(pid, {})
            prior = prior_draft.get(pid)
            submitted_keeper = submitted.get(pid)
            previous_roster_id = previous_roster_by_owner.get(owner_id)
            roster_targets = {league_id: roster_id}
            if previous_league_id and previous_roster_id is not None:
                roster_targets[previous_league_id] = previous_roster_id
            acquisition = latest_acquisition(pid, roster_targets, all_transactions)

            acquired_via = None
            if acquisition:
                acquired_via = acquisition.get("type")
            elif prior and (
                prior.get("picked_by") == owner_id
                or (previous_roster_id is not None and int(prior.get("roster_id") or -1) == previous_roster_id)
            ):
                acquired_via = "draft"
            elif prior:
                acquired_via = "unknown"
            else:
                acquired_via = "undrafted_or_unknown"

            original_round = int(prior["round"]) if prior else None
            submitted_round = submitted_keeper.get("round") if submitted_keeper else None

            if not keeper_enabled:
                keeper_eligible: Optional[bool] = False
                eligibility_reason = "league_not_keeper_enabled"
                keeper_cost_round = None
                cost_source = None
            elif submitted_round is not None:
                keeper_eligible = submitted_round != 1
                eligibility_reason = "submitted_keeper_round" if keeper_eligible else "round_1_not_keepable"
                keeper_cost_round = submitted_round if keeper_eligible else None
                cost_source = "current_submitted_keeper"
            elif original_round == 1:
                keeper_eligible = False
                eligibility_reason = "round_1_not_keepable"
                keeper_cost_round = None
                cost_source = "previous_draft"
            elif original_round is not None and acquired_via in {"draft", "trade", "unknown"}:
                keeper_eligible = True
                eligibility_reason = "prior_draft_round_available"
                keeper_cost_round = original_round
                cost_source = "previous_draft"
            elif original_round is not None:
                keeper_eligible = None
                eligibility_reason = "waiver_or_free_agent_cost_rule_unknown"
                keeper_cost_round = None
                cost_source = None
            else:
                keeper_eligible = None
                eligibility_reason = "no_prior_draft_round_found"
                keeper_cost_round = None
                cost_source = None

            player_rows.append({
                "player_id": pid,
                "name": meta.get("full_name") or f"ID:{pid}",
                "position": meta.get("position"),
                "team": meta.get("team"),
                "injury_status": meta.get("injury_status"),
                "original_draft_round": original_round,
                "original_draft_pick_no": prior.get("pick_no") if prior else None,
                "original_draft_slot": prior.get("draft_slot") if prior else None,
                "original_drafted_by": prior.get("picked_by") if prior else None,
                "original_draft_roster_id": prior.get("roster_id") if prior else None,
                "acquired_via": acquired_via,
                "acquisition_transaction_id": acquisition.get("transaction_id") if acquisition else None,
                "acquisition_time": iso_from_ms(acquisition.get("created")) if acquisition else None,
                "acquisition_league_id": acquisition.get("league_id") if acquisition else None,
                "keeper_submitted": submitted_keeper is not None,
                "keeper_submitted_round": submitted_round,
                "keeper_eligible": keeper_eligible,
                "eligibility_reason": eligibility_reason,
                "keeper_cost_round": keeper_cost_round,
                "keeper_cost_source": cost_source,
                "current_draft_round": current_draft.get(pid, {}).get("round"),
            })

        teams.append({
            "roster_id": roster_id,
            "owner_id": owner_id,
            "owner": user.get("display_name"),
            "team_name": team_name,
            "players": player_rows,
        })

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "league_id": league_id,
        "season": league.get("season"),
        "previous_league_id": previous_league_id,
        "previous_season": previous_season,
        "max_keepers": max_keepers,
        "rules": {
            "round_1_keepable": False,
            "drafted_player_cost": "previous/original draft round",
            "traded_player_cost": "previous/original draft round",
            "waiver_free_agent_cost": "unknown unless Sleeper has a submitted keeper round",
        },
        "teams": teams,
    }
    (run_dir / "keeper_board.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Wrote {run_dir / 'keeper_board.json'}")
    print(f"Wrote {run_dir / 'traded_picks.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
