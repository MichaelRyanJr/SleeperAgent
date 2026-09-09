#!/usr/bin/env python3
"""Apply league-specific keeper cost rules to keeper_board.json.

Rules confirmed by the league commissioner:
- A player drafted in the prior season retains that original draft round even if
  later dropped, claimed on waivers, re-added, or traded.
- A player who went undrafted has a base keeper cost of Round 14.
- Keeper-round collisions cascade toward earlier rounds: if the base round is
  occupied, use the next earlier available round (14 -> 13 -> 12 -> ...).
- Round 1 drafted players are not keepable.

For keepers already submitted in Sleeper, keeper_submitted_round is treated as
an authoritative final round. For unsubmitted candidates, keeper_cost_round is
the base cost and keeper_cost_round_is_provisional is true because the final
round can move earlier depending on the other keepers selected.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict


def newest_run_dir(out_base: Path, league_id: str) -> Path:
    candidates = [p for p in out_base.glob(f"league_{league_id}_*") if p.is_dir()]
    if not candidates:
        raise FileNotFoundError(f"No run folder found for league {league_id} under {out_base}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply clarified keeper rules to keeper_board.json")
    parser.add_argument("--league", required=True, help="Current Sleeper league ID")
    parser.add_argument("--out", default="./docs", help="SleeperAgent docs/output directory")
    args = parser.parse_args()

    league_id = str(args.league)
    run_dir = newest_run_dir(Path(args.out), league_id)
    path = run_dir / "keeper_board.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}")

    board = load_json(path)
    max_keepers = int(board.get("max_keepers") or 0)
    if max_keepers <= 0:
        return 0

    board["rules"] = {
        "round_1_keepable": False,
        "drafted_player_base_cost": "previous/original draft round, regardless of later drop, waiver claim, re-add, or trade",
        "undrafted_player_base_cost_round": 14,
        "round_collision_rule": "If a keeper's base round is already occupied, move that keeper one round earlier repeatedly until an unused round is available.",
        "submitted_keeper_round_authoritative": True,
    }

    for team in board.get("teams") or []:
        for player in team.get("players") or []:
            original_round = player.get("original_draft_round")
            submitted_round = player.get("keeper_submitted_round")

            if original_round == 1:
                player["keeper_eligible"] = False
                player["eligibility_reason"] = "round_1_not_keepable"
                player["base_keeper_cost_round"] = None
                player["keeper_cost_round"] = None
                player["keeper_cost_source"] = "previous_draft"
                player["keeper_cost_round_is_provisional"] = False
                continue

            if original_round is not None:
                base_round = int(original_round)
                base_source = "previous_draft"
                reason = "original_draft_round_retained"
            else:
                base_round = 14
                base_source = "undrafted_base_rule"
                reason = "undrafted_base_round_14"

            player["keeper_eligible"] = True
            player["eligibility_reason"] = reason
            player["base_keeper_cost_round"] = base_round

            if submitted_round is not None:
                player["keeper_cost_round"] = int(submitted_round)
                player["keeper_cost_source"] = "current_submitted_keeper"
                player["keeper_cost_round_is_provisional"] = False
            else:
                player["keeper_cost_round"] = base_round
                player["keeper_cost_source"] = base_source
                player["keeper_cost_round_is_provisional"] = True

    board["rules_updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(board, indent=2), encoding="utf-8")
    print(f"Applied clarified keeper rules to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
