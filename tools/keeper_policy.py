"""Season-scoped keeper annotations; never infer next-season keeper costs.

The 2027 selection policy was clarified by the owner on 2026-11-10.
Historical cost fields remain about the export season's draft, not next year.
"""


def keeper_enabled(league):
    settings = league.get("settings") or {}
    # Sleeper type: 0 redraft, 1 keeper, 2 dynasty. A stale max_keepers is
    # not evidence that a redraft or dynasty league uses this keeper policy.
    return str(settings.get("type")) == "1" and int(settings.get("max_keepers") or 0) > 0


def annotate_board(board, league):
    enabled = keeper_enabled(league)
    season = int(league["season"])
    board["keeper_enabled"] = enabled
    board["league_type"] = (league.get("settings") or {}).get("type")
    board["keeper_cost_season"] = season
    board["original_draft_season"] = board.get("previous_season")
    board["current_draft_season"] = season
    board["keeper_cost_scope"] = "export_season_draft_only_not_next_season"
    if not enabled:
        board["rules"] = {}
        board["max_keepers"] = 0
    elif season >= 2027:
        # New selection rules, but costs were not reconfirmed for this season.
        board["max_keepers"] = 8
        board["rules"] = {"round_1_keepable": False, "max_keepers": 8,
                          "round_2_or_3_constraint": None,
                          "cost_rules_status": "not_confirmed_for_2027_onward"}
    else:
        board["rules"]["round_2_or_3_constraint"] = {
            "max_keepers_from_rounds_2_and_3_combined": 1,
            "applies_through_draft_season": 2026,
        }
    target = season + 1
    board["next_season_planning"] = {
        "target_season": target,
        "enabled": enabled,
        "max_keepers": 8 if enabled and target >= 2027 else None,
        "round_1_keepable": False if enabled and target >= 2027 else None,
        "round_2_or_3_combined_limit": None,
        "selection_rule_source": "owner clarification 2026-11-10" if enabled and target >= 2027 else None,
        "cost_rules_status": "unconfirmed" if enabled else "not_applicable",
        "limitations": ["Current draft rounds are evidence, not confirmed future keeper costs.",
                        "Missing draft evidence does not prove a player was undrafted.",
                        "Current submitted keepers belong to the export season, not next season."],
    }
    for team in board.get("teams") or []:
        rows = team.get("players") or []
        submitted = [p for p in rows if p.get("keeper_submitted")]
        rounds = [int(p["keeper_submitted_round"]) for p in submitted
                  if p.get("keeper_submitted_round") is not None]
        max_keepers = board["max_keepers"]
        reason = ("round_1_not_keepable" if 1 in rounds else
                  "too_many_keepers" if len(submitted) > max_keepers else
                  "more_than_one_round_2_or_3_keeper" if season <= 2026 and sum(r in (2, 3) for r in rounds) > 1 else None)
        team["submitted_keeper_rule_check"] = {
            "season": season, "applicable": enabled,
            "submitted_keeper_count": len(submitted), "max_keepers": max_keepers,
            "assigned_rounds": sorted(rounds),
            "round_2_or_3_keeper_count": sum(r in (2, 3) for r in rounds),
            "valid_so_far": (False if reason else None if len(rounds) != len(submitted) else True) if enabled else None,
            "invalid_reason": reason if enabled else None,
            "complete": enabled and len(submitted) == max_keepers and len(rounds) == len(submitted) and not reason,
        }
        for player in rows:
            if not enabled or season >= 2027:
                # Do not carry historical formulas forward into a new policy.
                for key in ("keeper_cost_round", "base_keeper_cost_round", "keeper_base_round"):
                    player[key] = None
                player["keeper_eligible"] = None if enabled else False
                player["eligibility_reason"] = "future_cost_policy_unconfirmed" if enabled else "league_not_keeper_enabled"
                player["keeper_cost_source"] = None
                player["keeper_cost_round_is_provisional"] = False
                player["keeper_round_may_move_for_collision"] = False
                if enabled and player.get("keeper_submitted_round") is not None:
                    player["keeper_cost_round"] = int(player["keeper_submitted_round"])
                    player["keeper_cost_source"] = "current_submitted_keeper"
            current_round = player.get("current_draft_round")
            known = enabled and target >= 2027 and current_round is not None
            player["next_season_keeper"] = {
                "target_season": target,
                "draft_evidence_season": season,
                "draft_round": current_round,
                "eligible_by_draft_round": (int(current_round) != 1) if known else False if not enabled else None,
                "reason": "league_not_keeper_enabled" if not enabled else "round_1_not_keepable" if known and int(current_round) == 1 else "eligible_round_cost_unconfirmed" if known else "missing_draft_evidence",
                "keeper_cost_round": None,
            }
    return board
