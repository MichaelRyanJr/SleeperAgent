"""Offline regression tests for availability, freshness, and sync integration."""
import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import sleeper_sync
from tools.available_players import build_snapshot, load_player_catalog, render_html, write_outputs

NOW = "2026-09-07T02:00:00Z"


def player(name, position="RB", **extra):
    return {"full_name": name, "position": position, "fantasy_positions": [position],
            "team": "CLE", "active": True, "status": "Active", **extra}


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.league = {"league_id": "123", "name": "Test league", "season": "2026", "status": "in_season",
                       "total_rosters": 2, "settings": {"disable_adds": 0},
                       "roster_positions": ["QB", "RB", "WR", "TE", "FLEX", "K", "DEF", "BN"]}
        self.rosters = [{"roster_id": 1, "players": [1], "starters": [1, "0"], "reserve": ["2"], "taxi": ["3"]},
                        {"roster_id": 2, "players": ["4"], "starters": ["4"]}]
        self.drafts = [{"draft_id": "d", "season": "2026", "status": "complete"}]
        self.picks = [{"draft_id": "d", "player_id": "5", "round": 9, "is_keeper": None}]
        self.catalog = {str(n): player("Player " + str(n)) for n in range(1, 8)}
        self.sources = {"players": {"fetched_at": NOW}, "rosters": {"fetched_at": NOW}}

    def build(self):
        return build_snapshot(self.league, self.rosters, self.drafts, self.picks, self.catalog, self.sources, NOW)

    def ids(self, result):
        return {p["player_id"] for p in result["players"]}

    def test_all_roster_fields_excluded_and_ids_normalized(self):
        self.assertEqual(self.ids(self.build()), {"5", "6", "7"})

    def test_dropped_player_not_permanently_excluded_by_completed_draft(self):
        result = self.build()
        self.assertIn("5", self.ids(result))
        self.assertEqual(next(p for p in result["players"] if p["player_id"] == "5")["current_season_draft_round"], 9)
        self.assertFalse(result["is_provisional"])

    def test_in_progress_manual_keeper_assignment_excluded(self):
        self.league["status"] = "drafting"
        self.drafts[0]["status"] = "drafting"
        result = self.build()
        self.assertNotIn("5", self.ids(result))
        self.assertEqual(result["excluded_draft_only_count"], 1)
        self.assertTrue(result["is_provisional"])

    def test_pre_draft_carried_holdings_remain_conservatively_excluded(self):
        self.league["status"] = "pre_draft"
        self.drafts[0]["status"] = "pre_draft"
        result = self.build()
        self.assertEqual(self.ids(result), {"6", "7"})
        self.assertTrue(any("carried-over" in w for w in result["warnings"]))

    def test_inconsistent_draft_status_keeps_provisional_guard(self):
        self.drafts[0]["status"] = "drafting"
        self.assertTrue(self.build()["is_provisional"])
        self.assertNotIn("5", self.ids(self.build()))

    def test_orphan_pick_not_falsely_available(self):
        self.picks[0]["draft_id"] = "missing"
        self.assertNotIn("5", self.ids(self.build()))
        self.assertTrue(self.build()["is_provisional"])

    def test_prior_season_picks_do_not_affect_current_availability(self):
        self.drafts.append({"draft_id": "old", "season": "2025", "status": "drafting"})
        self.picks.append({"draft_id": "old", "player_id": "6", "round": 1})
        result = self.build()
        self.assertIn("6", self.ids(result))
        self.assertFalse(result["is_provisional"])
        self.assertIsNone(next(p for p in result["players"] if p["player_id"] == "6")["current_season_draft_round"])

    def test_retired_hidden_but_kept_in_all_view(self):
        self.catalog["6"].update(team=None, active=False, status="Retired")
        result = self.build()
        self.assertNotIn("6", self.ids(result))
        self.assertIn("6", {p["player_id"] for p in result["all_players"]})

    def test_injured_suspended_and_teamless_active_not_lost(self):
        self.catalog["5"].update(status="Injured Reserve", active=False, injury_status="IR")
        self.catalog["6"].update(status="Suspended", injury_status="Suspended")
        self.catalog["7"].update(team=None)
        self.assertEqual(self.ids(self.build()), {"5", "6", "7"})

    def test_ineligible_idp_not_in_offensive_league(self):
        self.catalog["6"] = player("Defender", "LB")
        self.assertNotIn("6", self.ids(self.build()))
        self.league["roster_positions"].append("IDP_FLEX")
        self.assertIn("6", self.ids(self.build()))

    def test_multiple_fantasy_positions_respected(self):
        self.catalog["6"] = player("Dual player", "DB", fantasy_positions=["DB", "WR"])
        self.assertIn("6", self.ids(self.build()))

    def test_flex_only_eligibility(self):
        self.league["roster_positions"] = ["REC_FLEX", "BN"]
        self.catalog["5"] = player("Receiver", "WR")
        self.catalog["6"] = player("Tight end", "TE")
        self.assertEqual(self.ids(self.build()), {"5", "6"})

    def test_defense_case_normalization_and_zero_placeholders(self):
        self.catalog["KC"] = player("Kansas City", "DEF", team="KC")
        self.rosters[1]["players"].extend(["kc", None, "0"])
        self.assertNotIn("KC", self.ids(self.build()))
        self.assertEqual(self.build()["excluded_rostered_count"], 5)

    def test_null_roster_players_valid_but_missing_roster_list_rejected(self):
        self.rosters[1] = {"roster_id": 2, "players": None}
        self.assertIn("4", self.ids(self.build()))
        self.rosters = None
        with self.assertRaises(ValueError):
            self.build()

    def test_partial_roster_response_fails(self):
        self.rosters.pop()
        with self.assertRaises(ValueError):
            self.build()

    def test_duplicate_roster_and_invalid_player_list_fail(self):
        self.rosters[1]["roster_id"] = 1
        with self.assertRaises(ValueError):
            self.build()
        self.rosters[1]["roster_id"] = 2
        self.rosters[1]["players"] = "not a list"
        with self.assertRaises(ValueError):
            self.build()

    def test_missing_catalog_does_not_claim_no_free_agents(self):
        self.catalog = None
        result = self.build()
        self.assertFalse(result["valid"])
        self.assertTrue(result["is_provisional"])
        self.assertEqual(result["player_count"], 0)

    def test_missing_drafts_or_positions_fails(self):
        self.drafts = None
        with self.assertRaises(ValueError):
            self.build()
        self.drafts = []
        self.league["roster_positions"] = None
        with self.assertRaises(ValueError):
            self.build()

    def test_unknown_claim_status_even_when_adds_enabled(self):
        p = self.build()["players"][0]
        self.assertEqual(p["waiver_status"], "unknown")
        self.assertIsNone(p["can_add_now"])
        self.assertIsNone(p["claim_deadline"])
        self.league["settings"]["disable_adds"] = 1
        self.assertIs(self.build()["players"][0]["can_add_now"], False)

    def test_json_csv_html_consistent_and_escape_untrusted_names(self):
        self.catalog["6"]["full_name"] = '<script>alert("x")</script>'
        self.catalog["7"]["full_name"] = '=1+1'
        result = self.build()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_outputs(root, result)
            default = json.loads((root / "available_players.json").read_text())
            all_view = json.loads((root / "available_players_all.json").read_text())
            self.assertEqual(default["player_count"], len(default["players"]))
            self.assertEqual(all_view["player_count"], len(all_view["players"]))
            self.assertNotIn("all_players", default)
            with (root / "available_players.csv").open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), default["player_count"])
            self.assertEqual(rows[0]["generated_at"], NOW)
            self.assertTrue(any(row["name"] == "'=1+1" for row in rows))
            page = (root / "available_players.html").read_text()
            self.assertNotIn('<script>alert("x")</script>', page)
            self.assertIn("&lt;script&gt;", page)
            self.assertIn("STALE SNAPSHOT", page)
            self.assertIn('2026-09-07T03:00:00Z', page)


class CatalogTests(unittest.TestCase):
    def test_cache_shared_and_old_timestamp_preserved(self):
        fetch = Mock(return_value={"1": player("A")})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            _, first = load_player_catalog(fetch, path, NOW)
            _, second = load_player_catalog(fetch, path, "2026-09-07T03:00:00Z")
            self.assertEqual(fetch.call_count, 1)
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(second["fetched_at"], NOW)
            load_player_catalog(fetch, path, "2026-09-08T02:00:00Z")
            self.assertEqual(fetch.call_count, 2)

    def test_corrupt_cache_refetched(self):
        fetch = Mock(return_value={"1": player("A")})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text("invalid json")
            self.assertEqual(load_player_catalog(fetch, path, NOW)[0]["1"]["full_name"], "A")

    def test_api_failure_does_not_relabel_expired_cache(self):
        fetch = Mock(return_value={"1": player("A")})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            load_player_catalog(fetch, path, NOW)
            before = path.read_bytes()
            fetch.side_effect = RuntimeError("offline")
            with self.assertRaises(RuntimeError):
                load_player_catalog(fetch, path, "2026-09-09T02:00:00Z")
            self.assertEqual(before, path.read_bytes())

    def test_empty_catalog_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                load_player_catalog(Mock(return_value={}), Path(tmp) / "catalog.json", NOW)


class SyncIntegrationTests(unittest.TestCase):
    def test_full_pull_writes_exports_and_clears_stale_picks(self):
        league = {"league_id": "123", "name": "Test", "season": "2026", "status": "in_season",
                  "total_rosters": 1, "roster_positions": ["QB", "RB", "WR", "TE", "K", "DEF", "BN"], "settings": {}}
        responses = {
            sleeper_sync.league_url("123"): league,
            sleeper_sync.NFL_STATE_URL: {"week": 1},
            sleeper_sync.league_users_url("123"): [{"user_id": "u", "display_name": "Team"}],
            sleeper_sync.league_rosters_url("123"): [{"roster_id": 1, "owner_id": "u", "players": ["1"], "starters": ["1"], "settings": {}}],
            sleeper_sync.league_drafts_url("123"): [],
            sleeper_sync.PLAYERS_URL: {"1": player("Owned"), "2": player("Available")},
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(sleeper_sync, "http_get_json", side_effect=lambda url: copy.deepcopy(responses[url])) as fetch:
            out = Path(tmp) / "league_123_auto"
            out.mkdir()
            (out / "draft_picks.json").write_text('[{"player_id":"stale"}]')
            result = sleeper_sync.pull_league_bundle("123", 2026, [], out)
            self.assertEqual(result["available_players"], 1)
            self.assertEqual(json.loads((out / "draft_picks.json").read_text()), [])
            self.assertEqual(json.loads((out / "available_players.json").read_text())["players"][0]["name"], "Available")
            self.assertEqual(json.loads((out / "state.json").read_text())["available_players"]["player_count"], 1)
            self.assertEqual(set(json.loads((out / "players_min.json").read_text())), {"1"})
            sleeper_sync.pull_league_bundle("123", 2026, [], out)
            self.assertEqual(sum(c.args[0] == sleeper_sync.PLAYERS_URL for c in fetch.call_args_list), 1)
            sleeper_sync.pull_league_bundle("123", 2026, [], out, include_players=False)
            self.assertFalse(json.loads((out / "available_players.json").read_text())["valid"])


if __name__ == "__main__":
    unittest.main()
