#!/usr/bin/env python3
import os, sys, json, shutil, pathlib, datetime as dt, subprocess, html

DOCS = pathlib.Path("docs")
DOCS.mkdir(exist_ok=True)

def newest_run_dir(lid: str) -> pathlib.Path | None:
    candidates = [p for p in DOCS.glob(f"league_{lid}_*") if p.is_dir()]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None

def list_rel_files(root: pathlib.Path) -> list[str]:
    out = []
    for p in root.rglob("*"):
        if p.is_file():
            out.append(str(p.relative_to(root)).replace("\\", "/"))
    return sorted(out)

def write_baseline_diff(run_dir: pathlib.Path) -> pathlib.Path:
    added = list_rel_files(run_dir)
    obj = {
        "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": {"added": added, "removed": [], "changed": [], "unchanged_count": 0},
    }
    outp = run_dir / "diff.json"
    outp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return outp

def safe_run(args: list[str]) -> int:
    try:
        return subprocess.run(args, check=True).returncode
    except subprocess.CalledProcessError as e:
        print(f"::warning:: command failed: {' '.join(args)} (rc={e.returncode})", file=sys.stderr)
        return e.returncode

def write_html_mirror(lid: str, stable_dir: pathlib.Path) -> None:
    state_p = stable_dir / "state.json"
    if not state_p.exists():
        return
    try:
        raw = state_p.read_text(encoding="utf-8")
    except Exception:
        return
    title = f"league_state_{lid}.json"
    doc = (
        '<!doctype html><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        f"<h1>{html.escape(title)} (mirror)</h1>"
        '<pre style="white-space:pre-wrap;word-break:break-word;">'
        + html.escape(raw)
        + "</pre>"
    )
    (DOCS / f"league_state_{lid}.html").write_text(doc, encoding="utf-8")

def fallback_manifest(stable_dir: pathlib.Path, league_id: str) -> None:
    # Minimal local manifest if tools/postprocess.py fails
    items = []
    for p in stable_dir.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(stable_dir)).replace("\\", "/")
            items.append({"path": rel, "bytes": p.stat().st_size})
    obj = {
        "league_id": league_id,
        "dir": stable_dir.name,
        "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(items),
        "files": items,
    }
    (stable_dir / "manifest.json").write_text(json.dumps(obj, indent=2), encoding="utf-8")

def main() -> int:
    leagues = os.environ.get("LEAGUES", "").split()
    if not leagues:
        print("::error:: LEAGUES env is empty", file=sys.stderr)
        return 2

    for lid in leagues:
        print(f">> Publish {lid}")
        run_dir = newest_run_dir(lid)
        stable_dir = DOCS / f"league_{lid}"
        if not run_dir or not run_dir.is_dir():
            print(f"::error:: No per-run folder for {lid}", file=sys.stderr)
            return 2

        # 1) Diff BEFORE copying
        diff_run = run_dir / "diff.json"
        if stable_dir.exists():
            rc = safe_run([
                sys.executable, "tools/postprocess.py",
                "--diff", "--old", str(stable_dir), "--new", str(run_dir),
                "--out", str(diff_run),
            ])
            if rc != 0 and not diff_run.exists():
                write_baseline_diff(run_dir)
        else:
            write_baseline_diff(run_dir)

        # 2) Copy contents of run_dir -> stable_dir
        if stable_dir.exists():
            shutil.rmtree(stable_dir)
        shutil.copytree(run_dir, stable_dir)

        # Ensure diff.json ends up in stable (defensive)
        try:
            if diff_run.exists():
                shutil.copy2(diff_run, stable_dir / "diff.json")
        except Exception as e:
            print(f"::warning:: copy diff.json failed: {e}", file=sys.stderr)

        # 3) Back-compat shortcuts
        state_src = stable_dir / "state.json"
        if state_src.exists():
            shutil.copy2(state_src, DOCS / f"league_state_{lid}.json")
        dp_src = stable_dir / "draft_picks.json"
        if dp_src.exists():
            shutil.copy2(dp_src, DOCS / f"draft_picks_{lid}.json")

        # 4) HTML mirror
        write_html_mirror(lid, stable_dir)

        # 5) Manifest (with fallback)
        rc = safe_run([
            sys.executable, "tools/postprocess.py",
            "--manifest", str(stable_dir), "--league-id", lid
        ])
        if rc != 0 or not (stable_dir / "manifest.json").exists():
            fallback_manifest(stable_dir, lid)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
