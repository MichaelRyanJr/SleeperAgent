#!/usr/bin/env python3
"""Remove player nickname fields without changing football or league data.

The normal sync uses this as a fail-closed gate before commit/Pages upload.
--install-guards is an idempotent migration for the existing API readers.
Neither mode rewrites Git history. Never log removed keys or their values.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import re
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

KEYS = {"nickname", "nicknames", "player_nickname", "player_nicknames"}
MARKER = re.compile(r"p_nick_", re.I)
MAX_MEMBER_BYTES = 64 * 1024 * 1024


def is_nickname_key(key: Any) -> bool:
    name = str(key).strip().lower()
    return name.startswith("p_nick_") or name in KEYS


def strip_player_nicknames(value: Any) -> Any:
    """Return a clean copy, including nested/serialized JSON metadata."""
    if isinstance(value, dict):
        return {k: strip_player_nicknames(v) for k, v in value.items()
                if not is_nickname_key(k)}
    if isinstance(value, list):
        return [strip_player_nicknames(v) for v in value]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return value
        clean = strip_player_nicknames(parsed)
        if clean != parsed:
            return json.dumps(clean, ensure_ascii=True)
    return value


def _clean_json(text: str) -> str:
    data = json.loads(text)
    clean = strip_player_nicknames(data)
    return json.dumps(clean, indent=2, ensure_ascii=True) + "\n" if clean != data else text


def scrub_bytes(data: bytes, name: str, depth: int = 0) -> bytes:
    """Sanitize structured text/archive members, never extracting member paths."""
    if depth > 8 or len(data) > MAX_MEMBER_BYTES:
        raise ValueError("Privacy scan size/depth limit exceeded")
    lower = name.lower()
    if lower.endswith(".zip"):
        src, dst = io.BytesIO(data), io.BytesIO()
        changed = False
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
            if sum(m.file_size for m in zin.infolist()) > MAX_MEMBER_BYTES:
                raise ValueError("Archive expanded size limit exceeded")
            for member in zin.infolist():
                raw = zin.read(member)
                clean = scrub_bytes(raw, member.filename, depth + 1) if not member.is_dir() else raw
                changed |= clean != raw
                zout.writestr(member, clean)
        return dst.getvalue() if changed else data
    if lower.endswith((".tar", ".tar.gz", ".tgz")):
        src, dst = io.BytesIO(data), io.BytesIO()
        changed = False
        mode = "w:gz" if lower.endswith((".tar.gz", ".tgz")) else "w:"
        with tarfile.open(fileobj=src, mode="r:*") as tin, tarfile.open(fileobj=dst, mode=mode) as tout:
            members = tin.getmembers()
            if sum(m.size for m in members) > MAX_MEMBER_BYTES:
                raise ValueError("Archive expanded size limit exceeded")
            for member in members:
                if member.isfile():
                    with tin.extractfile(member) as stream:
                        raw = stream.read()
                    clean = scrub_bytes(raw, member.name, depth + 1)
                    changed |= clean != raw
                    member.size = len(clean)
                    tout.addfile(member, io.BytesIO(clean))
                else:
                    tout.addfile(member)
        return dst.getvalue() if changed else data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        if MARKER.search(data.decode("latin-1")):
            raise ValueError("Unrecognized binary export contains nickname fields")
        return data
    clean = text
    if lower.endswith(".json"):
        clean = _clean_json(text)
    elif lower.endswith(".csv"):
        rows = list(csv.reader(io.StringIO(text)))
        if rows:
            keep = [i for i, key in enumerate(rows[0]) if not is_nickname_key(key)]
            output = [[strip_player_nicknames(row[i]) if i < len(row) else "" for i in keep]
                      for row in rows]
            if output != rows:
                stream = io.StringIO(newline="")
                csv.writer(stream).writerows(output)
                clean = stream.getvalue()
    elif lower.endswith((".html", ".htm")):
        def replace_pre(match: re.Match) -> str:
            inner = html.unescape(match.group(2))
            if not inner.lstrip().startswith(("{", "[")):
                return match.group(0)
            result = _clean_json(inner)
            return match.group(1) + html.escape(result) + match.group(3) if result != inner else match.group(0)
        clean = re.sub(r"(<pre\b[^>]*>)(.*?)(</pre>)", replace_pre, clean, flags=re.I | re.S)
        def replace_script(match: re.Match) -> str:
            inner = match.group(2)
            result = _clean_json(inner)
            result = result.replace("</", "<\\/")
            return match.group(1) + result + match.group(3) if result != inner else match.group(0)
        clean = re.sub(r'(<script\b[^>]*type=[\"\']application/json[\"\'][^>]*>)(.*?)(</script>)',
                       replace_script, clean, flags=re.I | re.S)
    elif lower.endswith((".md", ".txt")):
        if text.lstrip().startswith(("{", "[")):
            clean = _clean_json(text)
        else:
            def replace_fence(match: re.Match) -> str:
                return match.group(1) + _clean_json(match.group(2)) + match.group(3)
            clean = re.sub(r"(```json[^\n]*\n)(.*?)(\n```)", replace_fence, text, flags=re.I | re.S)
    if MARKER.search(html.unescape(clean)):
        raise ValueError("Unrecognized nickname representation; refusing to publish")
    return clean.encode("utf-8")


def refresh_manifests(root: Path) -> None:
    """Repair affected file hashes; retain original source/snapshot timestamps."""
    for path in sorted(root.rglob("*manifest*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            continue
        changed = False
        for item in data["files"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            target = (path.parent / item["path"]).resolve()
            if not target.is_relative_to(root.resolve()) or not target.is_file() or target == path.resolve():
                continue
            raw = target.read_bytes()
            for key, value in (("bytes", len(raw)), ("sha256", hashlib.sha256(raw).hexdigest())):
                if key in item and item[key] != value:
                    item[key] = value
                    changed = True
        if changed:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def scrub_tree(root: Path, check: bool = False) -> dict:
    """Process current and archived exports. No removed values are logged."""
    scanned, changed = 0, 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlink found in public export tree")
        if not path.is_file():
            continue
        scanned += 1
        raw = path.read_bytes()
        try:
            clean = scrub_bytes(raw, path.name)
        except (ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise ValueError(f"Privacy validation failed for export {path.relative_to(root)}") from exc
        if clean != raw:
            changed += 1
            if not check:
                temporary = path.with_name(path.name + ".privacy-tmp")
                temporary.write_bytes(clean)
                temporary.replace(path)
    if check and changed:
        raise ValueError(f"Privacy check failed: {changed} exports contain nickname fields")
    if not check:
        refresh_manifests(root)
    return {"files_scanned": scanned, "files_scrubbed": changed, "history_rewritten": False}


def install_export_guards(root: Path) -> list[str]:
    """One-time narrow migration: remove nicknames at every API JSON decode.

Each installed helper is self-contained so the existing script/module entry
points work without changing their import paths. Unexpected source fails closed.
"""
    helper = '''def _omit_player_nicknames(data):
    """Privacy boundary: custom player nicknames are never exported."""
    return {key: value for key, value in data.items()
            if not str(key).strip().lower().startswith("p_nick_")
            and str(key).strip().lower() not in
            {"nickname", "nicknames", "player_nickname", "player_nicknames"}}


'''
    old = 'return json.loads(resp.read().decode("utf-8"))'
    new = 'return json.loads(resp.read().decode("utf-8"), object_hook=_omit_player_nicknames)'
    modified = []
    for relative in ("sleeper_sync.py", "tools/enrich_keeper_data.py", "tools/cache_draft_history.py"):
        path = root / relative
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "def _omit_player_nicknames(" in text and new in text:
            continue
        if text.count(old) != 1 or text.count("def http_get_json(") != 1:
            raise ValueError("Unexpected API reader source; privacy migration aborted: " + relative)
        result = text.replace("def http_get_json(", helper + "def http_get_json(", 1).replace(old, new, 1)
        compile(result, relative, "exec")
        path.write_text(result, encoding="utf-8")
        modified.append(relative)
    return modified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("docs"))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--install-guards", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    args = parser.parse_args()
    try:
        if args.install_guards:
            print("API readers protected:", len(install_export_guards(args.repo_root)))
        print(json.dumps(scrub_tree(args.root, args.check), sort_keys=True))
    except Exception:
        # Parser exceptions can contain private strings. Never print a traceback.
        print("::error::Nickname privacy validation failed; no export should be published.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
