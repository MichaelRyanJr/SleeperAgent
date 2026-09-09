#!/usr/bin/env python3
"""One-time cleanup of branch tips and old Pages bundles; no history rewrite.

Use only from the main Sleeper sync workflow. Normal branch pushes are used;
concurrent edits cause a failure rather than being force-overwritten. Only old
artifacts named github-pages are deleted. No nickname strings enter logs.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.request

from scrub_nicknames import install_export_guards, scrub_tree

REPOSITORY = "MichaelRyanJr/SleeperAgent"
RECEIPT = Path("docs/nickname_privacy_cleanup.json")


def git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if proc.returncode:
        # Do not echo git diffs or API response bodies into public logs.
        raise RuntimeError("Git cleanup operation failed; concurrent branch changes may need review")
    return proc.stdout.strip()


def api(path: str, method: str = "GET"):
    token = os.environ["GH_TOKEN"]
    request = urllib.request.Request(
        "https://api.github.com/repos/" + REPOSITORY + path,
        method=method,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "SleeperAgent-nickname-cleanup"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    return json.loads(data) if data else None


def main() -> int:
    if RECEIPT.exists():
        print("One-time nickname cleanup already recorded; current exports are checked separately.")
        return 0
    if os.environ.get("GITHUB_REPOSITORY", "").lower() != REPOSITORY.lower():
        raise RuntimeError("Cleanup restricted to the requested repository")
    root = Path.cwd()
    helper = (root / "tools/scrub_nicknames.py").read_bytes()
    cutoff = api("/actions/runs/" + os.environ["GITHUB_RUN_ID"])["created_at"]
    branches = []
    for line in git("ls-remote", "--heads", "origin").splitlines():
        sha, ref = line.split("\t", 1)
        if ref != "refs/heads/main":
            branches.append((sha, ref))
    branch_results = []
    for expected_sha, ref in branches:
        # Keep the main working directory intact while sanitizing other tips.
        with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP")) as temp:
            work = Path(temp) / "branch"
            git("fetch", "--no-tags", "--depth=1", "origin", ref)
            git("worktree", "add", "--detach", str(work), "FETCH_HEAD")
            try:
                if git("rev-parse", "HEAD", cwd=work) != expected_sha:
                    raise RuntimeError("Branch changed during cleanup; retry without overwriting it")
                (work / "tools").mkdir(exist_ok=True)
                (work / "tools/scrub_nicknames.py").write_bytes(helper)
                guarded = install_export_guards(work)
                result = scrub_tree(work / "docs")
                scrub_tree(work / "docs", check=True)
                git("add", "-A", cwd=work)
                if git("diff", "--cached", "--name-only", cwd=work):
                    git("-c", "user.name=github-actions[bot]", "-c", "user.email=actions@github.com",
                        "commit", "-m", "privacy: remove player nicknames from branch exports", cwd=work)
                    git("push", "origin", "HEAD:" + ref, cwd=work)
                branch_results.append({"branch": ref.removeprefix("refs/heads/"),
                                       "files_scrubbed": result["files_scrubbed"],
                                       "api_readers_protected": len(guarded)})
            finally:
                git("worktree", "remove", "--force", str(work))
    # Gather all IDs BEFORE deletion so pagination is not shifted by removals.
    artifacts, page = [], 1
    while True:
        batch = api(f"/actions/artifacts?per_page=100&page={page}")["artifacts"]
        artifacts.extend(a["id"] for a in batch
                         if a["name"] == "github-pages" and a["created_at"] < cutoff)
        if len(batch) < 100:
            break
        page += 1
    deleted = 0
    for artifact_id in artifacts:
        try:
            api(f"/actions/artifacts/{artifact_id}", "DELETE")
            deleted += 1
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 410):
                raise
    RECEIPT.write_text(json.dumps({
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": "current branch tips and pre-cleanup GitHub Pages artifacts",
        "branches": branch_results,
        "old_pages_artifacts_deleted": deleted,
        "artifact_cutoff": cutoff,
        "history_rewritten": False,
        "old_commits_and_pull_request_refs_may_still_contain_prior_data": True,
    }, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"branch_tips_cleaned": len(branch_results),
                      "old_pages_artifacts_deleted": deleted, "history_rewritten": False}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("::error::One-time privacy cleanup incomplete; inspect permissions or retry. No history was rewritten.")
        raise SystemExit(1)
