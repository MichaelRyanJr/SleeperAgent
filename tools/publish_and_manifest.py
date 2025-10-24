#!/usr/bin/env python3
"""
Robust publisher: copies the newest per-run export into the stable folder and
ALWAYS writes diff.json + manifest.json locally (no external tool calls).
Also creates an HTML mirror of state.json at docs/league_state_<id>.html.
"""

import os
import sys
import json
import html
import hashlib
import shutil
import pathlib
import datetime as dt

# Constants
DOCS = pathlib.Path("docs")
DOCS.mkdir(exist_ok=True)
ISO = "%Y-%m-%dT%H:%M:%SZ"


# -------------------- helpers --------------------

def utcnow() -> str:
    return dt.datetime.utcnow().strftime(ISO)


def newest_run_dir(lid: str) -> pathlib.Path | None:
    """
    Returns the most recent per-run folder for a league, e.g. docs/league_<id>_auto/
    or any timestamped variant created by the exporter.
    """
    candidates = [p for p in DOCS.glob(f"league_{lid}_*") if p.is_dir()]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def sha256_file(p: pathlib.Path, chunk: int = 1 << 16) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def list_rel_files(root: pathlib.Path) -> list[str]:
    out: list[str] = []
    if not root.exists():
        return out
    for q in root.rglob("*"):
        if q.is_file():
            out.append(str(q.relative_to(root)).replace("\\", "/"))
    out.sort()
    return out


def build_diff(old_dir: pathlib.Path, new_dir: pathlib.Path) -> dict:
    """
    File-level diff: compares by SHA256 for files present in both trees.
    """
    old_set = set(list_rel_files(old_dir))
    new_set = set(list_rel_files(new_dir))

    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    common = sorted(old_set & new_set)

    changed: list[str] = []
    unchanged = 0
    for rel in common:
        if sha256_file(old_dir / rel) != sha256_file(new_dir / rel):
            changed.append(rel)
        else:
            unchanged += 1

    return {
        "generated_at": utcnow(),
        "files": {
            "added": added,
            "removed": removed,
            "changed": changed,
            "unchanged_count": unchanged,
        },
    }


def write_diff(old_dir: pathlib.Path, new_dir: pathlib.Path, out_path: pathlib.Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if old_dir.exists():
        data = build_diff(old_dir, new_dir)
    else:
        # First publish for this league: treat all files as "added".
        data = {
            "generated_at": utcnow(),
            "files": {
                "added": list_rel_files(new_dir),
                "removed": [],
                "changed": [],
                "unchanged_count": 0,
            },
        }
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def build_manifest(stable_dir: pathlib.Path, league_id: str) -> dict:
    """
    Manifest includes bytes, sha256, mtime per file. generated_at prefers state's field.
    """
    items: list[dict] = []
    for rel in list_rel_files(stable_dir):
        full = stable_dir / rel
        st = full.stat()
        items.append({
            "path": rel,
            "bytes": int(st.st_size),
            "sha256": sha256_file(full),
            "mtime": dt.datetime.utcfromtimestamp(st.st_mtime).strftime(ISO),
            "is_core": rel in {
                "state.json", "teams.json", "schedule.json",
                "transactions.json", "players_min.json"
            } or rel.startswith("lineups/"),
        })

    generated = utcnow()
    state_p = stable_dir / "state.json"
    if state_p.exists():
        try:
            s = json.loads(state_p.read_text(encoding="utf-8"))
            generated = s.get("generated_at") or generated
        except Exception:
            pass

    return {
        "league_id": league_id,
        "dir": stable_dir.name,
        "generated_at": generated,
        "count": len(items),
        "files": items,
    }


def write_manifest(stable_dir: pathlib.Path, league_id: str) -> None:
    manifest = build_manifest(stable_dir, league_id)
    (stable_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def write_html_mirror(lid: str, stable_dir: pathlib.Path) -> None:
    """
    Create docs/league_state_<lid>.html that renders the stable state.json.
    """
    state_p = stable_dir / "state.json"
    if not state_p.exists():
        return

    raw = state_p.read_text(encoding="utf-8")
    title = "league_state_{}.json".format(lid)

    parts = [
        '<!doctype html><meta charset="utf-8">',
        "<title>{}</title>".format(html.escape(title)),
        "<h1>{} (mirror)</h1>".format(html.escape(title)),
        '<pre style="white-space:pre-wrap;word-break:break-word;">',
        html.escape(raw),
        "</pre>",
    ]
    html_doc = "".join(parts)

    out_path = DOCS / "league_state_{}.html".format(lid)
    out_path.write_text(html_doc, encoding="utf-8")


def copytree_overwrite(src: pathlib.Path, dst: pathlib.Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


# -------------------- main --------------------

def main() -> int:
    leagues = os.environ.get("LEAGUES", "").split()
    if not leagues:
        print("::error:: LEAGUES env is empty", file=sys.stderr)
        return 2

    for lid in leagues:
        print(f">> Publish {lid}")
        run_dir = newest_run_dir(lid)
        if not run_dir or not run_dir.exists():
            print(f"::error:: No per-run folder for {lid}", file=sys.stderr)
            return 2

        stable_dir = DOCS / f"league_{lid}"

        # 1) Compute diff BEFORE copying (compare old stable vs new run)
        run_diff = run_dir / "diff.json"
        write_diff(stable_dir, run_dir, run_diff)

        # 2) Copy run -> stable (includes run diff.json and all outputs)
        copytree_overwrite(run_dir, stable_dir)

        # 3) Ensure diff.json ends up in stable (defensive)
        try:
            if run_diff.exists():
                shutil.copy2(run_diff, stable_dir / "diff.json")
        except Exception:
            pass

        # 4) Back-compat shortcuts at docs root
        state_src = stable_dir / "state.json"
        if state_src.exists():
            shutil.copy2(state_src, DOCS / f"league_state_{lid}.json")
        dp_src = stable_dir / "draft_picks.json"
        if dp_src.exists():
            shutil.copy2(dp_src, DOCS / f"draft_picks_{lid}.json")

        # 5) HTML mirror
        write_html_mirror(lid, stable_dir)

        # 6) Manifest in stable
        write_manifest(stable_dir, lid)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
