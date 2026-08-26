#!/usr/bin/env python3
"""Publish Sleeper exports behind stable, season-independent URLs.

The exporter writes per-run folders using the current Sleeper league_id. Sleeper
changes that ID whenever a league is renewed for a new season. BASE_LEAGUES are
treated as permanent anchor IDs, while LEAGUES contains the IDs resolved for the
current season. Publishing copies current data into stable anchor paths so old
links continue to work year after year.
"""

import datetime as dt
import hashlib
import html
import json
import os
import pathlib
import shutil
import sys

DOCS = pathlib.Path("docs")
DOCS.mkdir(exist_ok=True)
ISO = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> str:
    return dt.datetime.utcnow().strftime(ISO)


def newest_run_dir(lid: str) -> pathlib.Path | None:
    candidates = [p for p in DOCS.glob(f"league_{lid}_*") if p.is_dir()]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def sha256_file(p: pathlib.Path, chunk: int = 1 << 16) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def list_rel_files(root: pathlib.Path) -> list[str]:
    if not root.exists():
        return []
    out = [
        str(q.relative_to(root)).replace("\\", "/")
        for q in root.rglob("*")
        if q.is_file()
    ]
    out.sort()
    return out


def build_diff(old_dir: pathlib.Path, new_dir: pathlib.Path) -> dict:
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


def build_manifest(
    stable_dir: pathlib.Path,
    anchor_league_id: str,
    resolved_league_id: str,
) -> dict:
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
                "state.json",
                "teams.json",
                "schedule.json",
                "transactions.json",
                "players_min.json",
            } or rel.startswith("lineups/"),
        })

    generated = utcnow()
    state_p = stable_dir / "state.json"
    if state_p.exists():
        try:
            state = json.loads(state_p.read_text(encoding="utf-8"))
            generated = state.get("generated_at") or generated
        except Exception:
            pass

    return {
        "league_id": resolved_league_id,
        "anchor_league_id": anchor_league_id,
        "dir": stable_dir.name,
        "generated_at": generated,
        "count": len(items),
        "files": items,
    }


def write_manifest(
    stable_dir: pathlib.Path,
    anchor_league_id: str,
    resolved_league_id: str,
) -> None:
    manifest = build_manifest(stable_dir, anchor_league_id, resolved_league_id)
    (stable_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )


def write_html_mirror(public_id: str, stable_dir: pathlib.Path) -> None:
    state_p = stable_dir / "state.json"
    if not state_p.exists():
        return

    raw = state_p.read_text(encoding="utf-8")
    title = f"league_state_{public_id}.json"
    html_doc = "".join([
        '<!doctype html><meta charset="utf-8">',
        f"<title>{html.escape(title)}</title>",
        f"<h1>{html.escape(title)} (mirror)</h1>",
        '<pre style="white-space:pre-wrap;word-break:break-word;">',
        html.escape(raw),
        "</pre>",
    ])
    (DOCS / f"league_state_{public_id}.html").write_text(
        html_doc,
        encoding="utf-8",
    )


def copytree_overwrite(src: pathlib.Path, dst: pathlib.Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def publish_shortcuts(
    stable_dir: pathlib.Path,
    anchor_id: str,
    resolved_id: str,
    primary: bool,
) -> None:
    state_src = stable_dir / "state.json"
    draft_src = stable_dir / "draft_picks.json"

    if state_src.exists():
        shutil.copy2(state_src, DOCS / f"league_state_{anchor_id}.json")
    if draft_src.exists():
        shutil.copy2(draft_src, DOCS / f"draft_picks_{anchor_id}.json")
    write_html_mirror(anchor_id, stable_dir)

    if resolved_id != anchor_id:
        if state_src.exists():
            shutil.copy2(state_src, DOCS / f"league_state_{resolved_id}.json")
        if draft_src.exists():
            shutil.copy2(draft_src, DOCS / f"draft_picks_{resolved_id}.json")
        write_html_mirror(resolved_id, stable_dir)

    if primary and state_src.exists():
        shutil.copy2(state_src, DOCS / "league_state.json")


def main() -> int:
    resolved = os.environ.get("LEAGUES", "").split()
    anchors = os.environ.get("BASE_LEAGUES", "").split()

    if not resolved:
        print("::error::LEAGUES env is empty", file=sys.stderr)
        return 2
    if not anchors:
        anchors = list(resolved)
    if len(anchors) != len(resolved):
        print(
            "::error::BASE_LEAGUES and LEAGUES must contain the same number of IDs",
            file=sys.stderr,
        )
        return 2

    for index, (anchor_id, resolved_id) in enumerate(zip(anchors, resolved)):
        print(f">> Publish anchor {anchor_id} from current league {resolved_id}")
        run_dir = newest_run_dir(resolved_id)
        if not run_dir or not run_dir.exists():
            print(
                f"::error::No per-run folder for resolved league {resolved_id}",
                file=sys.stderr,
            )
            return 2

        stable_dir = DOCS / f"league_{anchor_id}"
        run_diff = run_dir / "diff.json"
        write_diff(stable_dir, run_dir, run_diff)
        copytree_overwrite(run_dir, stable_dir)

        if run_diff.exists():
            shutil.copy2(run_diff, stable_dir / "diff.json")

        write_manifest(stable_dir, anchor_id, resolved_id)
        publish_shortcuts(
            stable_dir,
            anchor_id,
            resolved_id,
            primary=(index == 0),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
