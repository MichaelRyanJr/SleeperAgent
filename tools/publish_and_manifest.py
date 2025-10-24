#!/usr/bin/env python3
import os, sys, json, shutil, pathlib, datetime as dt, subprocess

DOCS = pathlib.Path("docs")
DOCS.mkdir(exist_ok=True)

def newest_run_dir(lid: str) -> pathlib.Path | None:
    # matches docs/league_<ID>_* (your exporter creates _auto or timestamped dirs)
    candidates = [p for p in DOCS.glob(f"league_{lid}_*") if p.is_dir()]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None

def list_rel_files(root: pathlib.Path) -> list[str]:
    out = []
    for p in root.rglob("*"):
        if p.is_file():
            out.append(str(p.relative_to(root)).replace("\\", "/"))
    return sorted(out)

def write_baseline_diff(run_dir: pathlib.Path) -> None:
    added = list_rel_files(run_dir)
    obj = {
        "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": {"added": added, "removed": [], "changed": [], "unchanged_count": 0},
    }
    (run_dir / "diff.json").write_text(json.dumps(obj, indent=2), encoding="utf-8")

def run_cmd(args: list[str]) -> None:
    subprocess.run(args, check=False)

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

        # Diff BEFORE copying to stable
        if stable_dir.exists():
            run_cmd([
                sys.executable, "tools/postprocess.py",
                "--diff", "--old", str(stable_dir), "--new", str(run_dir),
                "--out", str(run_dir / "diff.json"),
            ])
        else:
            write_baseline_diff(run_dir)

        # Copy contents of run_dir -> stable_dir
        if stable_dir.exists():
            shutil.rmtree(stable_dir)
        shutil.copytree(run_dir, stable_dir)

        # Back-compat shortcuts
        state_src = stable_dir / "state.json"
        if state_src.exists():
            shutil.copy2(state_src, DOCS / f"league_state_{lid}.json")
        dp_src = stable_dir / "draft_picks.json"
        if dp_src.exists():
            shutil.copy2(dp_src, DOCS / f"draft_picks_{lid}.json")

        # Manifest
        run_cmd([
            sys.executable, "tools/postprocess.py",
            "--manifest", str(stable_dir), "--league-id", lid
        ])

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
