"""Keeper policy and full CLI-stage regressions, offline and deterministic."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.keeper_policy import annotate_board, keeper_enabled
from tools import enrich_keeper_data, apply_keeper_rules, build_index


def league(season=2026, kind=1):
    return {"league_id": "test", "season": str(season), "settings": {"type": kind, "max_keepers": 7}}


def board(rounds=(2, 3)):
    return {"max_keepers": 7, "previous_season": "2025", "rules": {}, "teams": [
        {"players": [{"player_id": str(i), "keeper_submitted": True,
                      "keeper_submitted_round": r, "current_draft_round": r,
                      "keeper_cost_round": 14, "original_draft_round": 14}
                     for i, r in enumerate(rounds)]}]}


class KeeperPolicyTests(unittest.TestCase):
    def test_old_policy_is_historical_and_new_policy_separate(self):
        data = annotate_board(board(), league())
        self.assertEqual(data["keeper_cost_season"], 2026)
        self.assertEqual(data["original_draft_season"], "2025")
        self.assertFalse(data["teams"][0]["submitted_keeper_rule_check"]["valid_so_far"])
        self.assertEqual(data["next_season_planning"]["max_keepers"], 8)
        self.assertEqual(data["next_season_planning"]["target_season"], 2027)
        for p in data["teams"][0]["players"]:
            self.assertTrue(p["next_season_keeper"]["eligible_by_draft_round"])
            self.assertIsNone(p["next_season_keeper"]["keeper_cost_round"])
            self.assertEqual(p["keeper_cost_round"], 14)

    def test_2027_eight_allowed_early_rounds_without_old_cost_formula(self):
        data = annotate_board(board([2, 3, 4, 5, 6, 7, 8, 9]), league(2027))
        check = data["teams"][0]["submitted_keeper_rule_check"]
        self.assertTrue(check["valid_so_far"])
        self.assertTrue(check["complete"])
        self.assertEqual(data["teams"][0]["players"][0]["keeper_cost_round"], 2)
        self.assertIsNone(data["teams"][0]["players"][0]["keeper_base_round"])
        self.assertIsNone(data["rules"]["round_2_or_3_constraint"])

    def test_too_many_and_first_round_invalid(self):
        for rounds, reason in [([4]*9, "too_many_keepers"), ([1], "round_1_not_keepable")]:
            data = annotate_board(board(rounds), league(2027))
            check = data["teams"][0]["submitted_keeper_rule_check"]
            self.assertFalse(check["valid_so_far"])
            self.assertFalse(check["complete"])
            self.assertEqual(check["invalid_reason"], reason)

    def test_missing_round_unknown_not_undrafted(self):
        data = annotate_board(board([None]), league())
        p = data["teams"][0]["players"][0]
        self.assertIsNone(p["next_season_keeper"]["eligible_by_draft_round"])
        self.assertIsNone(data["teams"][0]["submitted_keeper_rule_check"]["valid_so_far"])

    def test_current_round_not_prior_round_controls_next_year(self):
        data = annotate_board(board([1]), league())
        p = data["teams"][0]["players"][0]
        self.assertFalse(p["next_season_keeper"]["eligible_by_draft_round"])
        self.assertEqual(p["original_draft_round"], 14)

    def test_redraft_and_dynasty_not_keeper_even_with_limit(self):
        for kind in [0, "0", 2, None]:
            with self.subTest(kind=kind):
                self.assertFalse(keeper_enabled(league(kind=kind)))
                data = annotate_board(board(), league(kind=kind))
                self.assertEqual(data["rules"], {})
                self.assertFalse(data["next_season_planning"]["enabled"])
                self.assertIsNone(data["teams"][0]["submitted_keeper_rule_check"]["valid_so_far"])
                self.assertIsNone(data["teams"][0]["players"][0]["keeper_cost_round"])

    def test_both_cli_stages_offline(self):
        for kind in [0, 1]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                run = Path(tmp) / "league_test_2026"
                run.mkdir()
                inputs = {"league": league(kind=kind), "rosters": [{"roster_id": 1, "owner_id": "u", "players": ["p"]}],
                          "users": [], "players_min": {"p": {"full_name": "Player"}},
                          "draft_picks": [{"player_id": "p", "round": 1}]}
                for name, value in inputs.items():
                    (run / f"{name}.json").write_text(json.dumps(value))
                args = ["program", "--league", "test", "--out", tmp]
                with patch("sys.argv", args), patch.object(enrich_keeper_data, "http_get_json", return_value=[]):
                    self.assertEqual(enrich_keeper_data.main(), 0)
                    self.assertEqual(apply_keeper_rules.main(), 0)
                data = json.loads((run / "keeper_board.json").read_text())
                self.assertEqual(data["keeper_enabled"], kind == 1)
                self.assertFalse(data["teams"][0]["players"][0]["next_season_keeper"]["eligible_by_draft_round"])
                if kind == 1:
                    self.assertIn("round_2_or_3_constraint", data["rules"])

    def test_markdown_hub_rebuilt_from_current_stable_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)
            path = docs / "league_anchor"
            path.mkdir()
            (path / "state.json").write_text(json.dumps({"season": 2026, "generated_at": "2026-11-10T10:00:00Z", "league": {"name": "League", "league_id": "current"}}))
            with patch.object(build_index, "DOCS", docs):
                build_index.main()
            md = (docs / "index.md").read_text()
            self.assertIn("Season 2026", md)
            self.assertIn("current Sleeper ID current", md)
            self.assertIn("2026-11-10T10:00:00Z", md)
            self.assertTrue((docs / "index.html").exists())
