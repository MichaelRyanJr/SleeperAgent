#!/usr/bin/env python3
# Robust publisher: copies newest per-run export into the stable folder and
# ALWAYS writes diff.json + manifest.json locally (no external tools).
import os, sys, json, shutil, pathlib, datetime as dt, html, hashlib

DOCS = pathlib.Path("docs")
DOCS.mkdir(exist_ok=True)
ISO = "%Y-%m-%dT%H:%M:%SZ"

def utcnow():
    return dt.datetime.utcnow().strftime(ISO)

def newest_run_dir(lid: str) -> pathlib.Path | None:
    cands = [p for p in DOCS.glob(f"league_{lid}_*") if p.is_dir()]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None

def sha256_file(p: pathlib.Path, chunk: int = 1 << 16) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()

def list_rel_files(root: pathlib.Path):
    out = []
    if not root.exists(): return out
    for p in root.rglob("*"):
        if p.is_file():
            out.append(str(p.relative_to(root)).replace("\\", "/"))
    out.sort()
    return out

def build_diff(old_dir: pathlib.Path, new_dir: pathlib.Path) -> dict:
    old_set = set(list_rel_files(old_dir))
    new_set = set(list_rel_files(new_dir))
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    common = sorted(old_set & new_set)
    changed, unchanged = [], 0
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
            "unchanged_count": unchanged
        }
    }

def write_diff(old_dir: pathlib.Path, new_dir: pathlib.Path, out_path: pathlib.Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = build_diff(old_dir, new_dir) if old_dir.exists() else {
        "generated_at": utcnow(),
        "files": {"added": list_rel_files(new_dir), "removed": [], "changed": [], "unchanged_count": 0}
    }
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

def build_manifest(stable_dir: pathlib.Path, league_id: str) -> dict:
    items = []
    for rel in list_rel_files(stable_dir):
        full = stable_dir / rel
        st = full.stat()
        items.append({
            "path": rel,
            "bytes": int(st.st_size),
            "sha256": sha256_file(full),
            "mtime": dt.datetime.utcfromtimestamp(st.st_mtime).strftime(ISO),
            "is_core": rel in {"state.json","teams.json","schedule.json","transactions.json","players_min.json"} or rel.startswith("lineups/")
        })
    # use state's generated_at if present
    gen = utcnow()
    state_p = stable_dir / "state.json"
    if state_p.exists():
        try:
            s = json.loads(state_p.read_text("utf-8"))
            gen = s.get("generated_at") or gen
        except Exception:
            pass
    return {
        "league_id": league_id,
        "dir": stable_dir.name,
        "generated_at": gen,
        "count": len(items),
        "files": items
    }

def write_manifest(stable_dir: pathlib.Path, league_id: str):
    manifest = build_manifest(stable_dir, league_id)
    (stable_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

def write_html_mirror(lid: str, stable_dir: pathlib.Path):
    state_p = stable_dir / "state.json"
    if not state_p.exists(): return
    raw = state_p.read_text("utf-8")
    title = f"league_state_{lid}.json"
    doc = (
        '<!doctype html><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        f"<h1>{html.escape(title)} (mirror)</h1>"
        '<pre style="white-space:pre-wrap;word-break:break-word;">'
        + html.escape(raw) + "</pre>"
    )
    (DOCS / f"league_state_{lid}.html").write_text(doc, encoding=_
