"""Privacy tests use synthetic labels, never real league nicknames."""
import copy
import csv
import hashlib
import html
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools.scrub_nicknames import (install_export_guards, scrub_bytes, scrub_tree,
                                   strip_player_nicknames)


class NicknamePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = [{"roster_id": 6, "players": ["12"], "starters": ["12"],
                         "reserve": ["13"], "settings": {"wins": 3},
                         "metadata": {"p_nick_12": "Synthetic label", "p_nick_NE": "Example",
                                      "team_name": "Team", "allow_pn_scoring": "on"}}]

    def test_roster_data_and_non_nickname_metadata_preserved(self):
        expected = copy.deepcopy(self.fixture)
        expected[0]["metadata"] = {"team_name": "Team", "allow_pn_scoring": "on"}
        self.assertEqual(strip_player_nicknames(self.fixture), expected)
        self.assertIn("p_nick_12", self.fixture[0]["metadata"])

    def test_nested_objects_lists_and_generic_aliases(self):
        self.assertEqual(strip_player_nicknames({"x": [{"nickname": "Example", "name": "Real"}],
                                                "PLAYER_NICKNAME": "Example"}),
                         {"x": [{"name": "Real"}]})

    def test_null_empty_and_numeric_values(self):
        for obj in (None, [], {}, 5, True, "Actual player name"):
            self.assertEqual(strip_player_nicknames(obj), obj)

    def test_serialized_json_metadata(self):
        result = strip_player_nicknames({"content": json.dumps(self.fixture)})
        self.assertNotIn("p_nick_", result["content"])

    def test_json_key_unicode_escape(self):
        result = scrub_bytes(b'{"\\u0070_nick_12":"Example", "round":8}', "snapshot.json")
        self.assertEqual(json.loads(result), {"round": 8})

    def test_valid_json_and_empty_metadata_after_last_key(self):
        result = scrub_bytes(b'{"metadata":{"p_nick_12":"Example"}}', "rosters.json")
        self.assertEqual(json.loads(result), {"metadata": {}})

    def test_html_json_mirror(self):
        source = '<h1>Roster</h1><pre class="data">' + html.escape(json.dumps(self.fixture)) + '</pre>'
        result = scrub_bytes(source.encode(), "mirror.html").decode()
        self.assertNotIn("p_nick_", result)
        self.assertIn("<h1>Roster</h1>", result)

    def test_markdown_json_fence(self):
        source = "# Snapshot\n```json\n" + json.dumps(self.fixture) + "\n```\n"
        result = scrub_bytes(source.encode(), "snapshot.md").decode()
        self.assertNotIn("p_nick_", result)
        self.assertIn("# Snapshot", result)

    def test_csv_nickname_column_removed(self):
        source = b'name,nickname,round\r\nReal,Example,8\r\n'
        rows = list(csv.reader(io.StringIO(scrub_bytes(source, "players.csv").decode())))
        self.assertEqual(rows, [["name", "round"], ["Real", "8"]])

    def test_zip_archive_sanitized_without_extraction(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr("rosters.json", json.dumps(self.fixture))
            z.writestr("keep.txt", "Unchanged")
        result = scrub_bytes(stream.getvalue(), "bundle.zip")
        with zipfile.ZipFile(io.BytesIO(result)) as z:
            self.assertNotIn(b"p_nick_", z.read("rosters.json"))
            self.assertEqual(z.read("keep.txt"), b"Unchanged")

    def test_tar_archive_sanitized_without_extraction(self):
        stream = io.BytesIO()
        raw = json.dumps(self.fixture).encode()
        with tarfile.open(fileobj=stream, mode="w:") as t:
            member = tarfile.TarInfo("rosters.json")
            member.size = len(raw)
            t.addfile(member, io.BytesIO(raw))
        result = scrub_bytes(stream.getvalue(), "bundle.tar")
        with tarfile.open(fileobj=io.BytesIO(result)) as t:
            self.assertNotIn(b"p_nick_", t.extractfile("rosters.json").read())

    def test_unknown_representation_fails_closed(self):
        with self.assertRaises(ValueError):
            scrub_bytes(b'plain p_nick_12: Example', "unknown.dat")

    def test_binary_with_marker_fails_closed(self):
        with self.assertRaises(ValueError):
            scrub_bytes(b'\xffp_nick_12', "unknown.bin")

    def test_unrelated_file_byte_preservation(self):
        raw = b'{"player_id":"12", "team":"CLE"}\n'
        self.assertEqual(scrub_bytes(raw, "player.json"), raw)

    def test_recursive_scrub_and_check_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "_history" / "rosters.json"
            path.parent.mkdir()
            path.write_text(json.dumps(self.fixture))
            with self.assertRaises(ValueError):
                scrub_tree(root, check=True)
            self.assertIn("p_nick_", path.read_text())
            self.assertEqual(scrub_tree(root)["files_scrubbed"], 1)
            self.assertEqual(scrub_tree(root, check=True)["files_scrubbed"], 0)
            self.assertEqual(scrub_tree(root)["files_scrubbed"], 0)

    def test_manifest_hashes_repaired_without_faking_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "rosters.json"
            path.write_text(json.dumps(self.fixture))
            manifest = {"generated_at": "2000-01-01T00:00:00Z", "files": [
                {"path": "rosters.json", "bytes": len(path.read_bytes()), "sha256": "old"}]}
            (root / "manifest.json").write_text(json.dumps(manifest))
            scrub_tree(root)
            output = json.loads((root / "manifest.json").read_text())
            self.assertEqual(output["generated_at"], manifest["generated_at"])
            self.assertEqual(output["files"][0]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_api_guard_migration_compiles_filters_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "sleeper_sync.py"
            target.write_text('import json\ndef http_get_json(resp):\n    return json.loads(resp.read().decode("utf-8"))\n')
            self.assertEqual(install_export_guards(root), ["sleeper_sync.py"])
            self.assertEqual(install_export_guards(root), [])
            namespace = {}
            exec(compile(target.read_text(), str(target), "exec"), namespace)
            self.assertEqual(namespace["http_get_json"](io.BytesIO(json.dumps(self.fixture).encode())),
                             strip_player_nicknames(self.fixture))

    def test_unexpected_source_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sleeper_sync.py").write_text("# unknown exporter")
            with self.assertRaises(ValueError):
                install_export_guards(root)

    def test_existing_api_readers_strip_response_before_storage(self):
        repo = Path(__file__).resolve().parents[1]
        for relative in ("sleeper_sync.py", "tools/enrich_keeper_data.py", "tools/cache_draft_history.py"):
            path = repo / relative
            self.assertTrue(path.exists(), relative)
            spec = importlib.util.spec_from_file_location("privacy_" + path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            response = io.BytesIO(json.dumps(self.fixture).encode())
            with patch.object(module.urllib.request, "urlopen", return_value=response):
                result = module.http_get_json("https://api.sleeper.app/v1/test")
            self.assertEqual(result, strip_player_nicknames(self.fixture), relative)


if __name__ == "__main__":
    unittest.main()
