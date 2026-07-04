"""
Generate a SWE-bench-style task (base_commit + patch.diff + test_patch.diff)
for one nltk/nltk PR, using the classified sample from ../data/.

Splits the PR's full diff (base_commit..merge_commit) by filename:
  nltk/test/**  -> test_patch.diff
  everything else -> patch.diff

base_commit is the merge commit's first parent -- the true state of
`develop` right before this PR landed (see offline/analyze_prs.py docstring
history for why this, not pr.base.sha, is the correct anchor).

FAIL_TO_PASS / PASS_TO_PASS are NOT computed here -- that requires running
pytest --collect-only against the real installed package at two different
commits, which only makes sense inside the Docker eval environment. This
script only produces the skeleton task.json + diffs; verify_task.py (run
inside the container) fills in the test lists and validates them.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from analyze_prs import gh_session, gh_get, GITHUB_API, load_dotenv, DATA_DIR as OFFLINE_DATA_DIR  # noqa: E402

HERE = Path(__file__).parent
CLONE_DIR = HERE / ".nltk-clone"
TASKS_DIR = HERE / "tasks"


def git(*args, cwd=CLONE_DIR, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check
    )


def load_pr_record(number):
    path = OFFLINE_DATA_DIR / "prs_classified.jsonl"
    if not path.exists():
        path = OFFLINE_DATA_DIR / "prs_sample.jsonl"
    for line in path.open(encoding="utf-8"):
        rec = json.loads(line)
        if rec["number"] == number:
            return rec
    raise ValueError(f"PR #{number} not found in {path}")


def fetch_merge_commit_sha(session, owner, repo, number):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}"
    detail = gh_get(session, url).json()
    sha = detail.get("merge_commit_sha")
    if not sha:
        raise ValueError(f"PR #{number} has no merge_commit_sha (not merged?)")
    return sha


def commit_exists_locally(sha):
    return git("cat-file", "-t", sha, check=False).returncode == 0


def resolve_base_and_head(session, owner, repo, number, merge_commit_sha):
    """Two paths, in order of preference:

    1. merge_commit_sha is reachable locally -> base_commit = its first
       parent (the tip of develop right before this exact merge). Best case:
       most accurate "immediately before merge" state.

    2. merge_commit_sha is gone (NLTK's repo has old history rewrites --
       branches like 2.0.5a/aline-patches/etc. suggest this happened; GitHub
       replies "not our ref" for these). Fall back to GitHub's permanent
       refs/pull/<n>/head (survives branch deletion/rewrites) plus the PR's
       commit list, so base_commit = parent of the PR's *first* commit --
       correct even for rebase-merged PRs where every individual commit
       ended up directly on develop.
    """
    if commit_exists_locally(merge_commit_sha):
        parents = git("log", "-1", "--format=%P", merge_commit_sha).stdout.split()
        if len(parents) >= 1:
            return parents[0], merge_commit_sha  # (base_commit, end_commit_to_diff_against)

    git("fetch", "origin", f"refs/pull/{number}/head:refs/pr-heads/{number}")
    head_commit = git("rev-parse", f"refs/pr-heads/{number}").stdout.strip()

    commits = gh_get(session, f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/commits").json()
    first_commit_sha = commits[0]["sha"]
    parents = git("log", "-1", "--format=%P", first_commit_sha).stdout.split()
    if not parents:
        raise ValueError(f"PR #{number}'s first commit {first_commit_sha} has no parent")
    return parents[0], head_commit


def generate(number, owner="nltk", repo="nltk"):
    load_dotenv(HERE.parent / ".env")
    import os
    token = os.environ["GITHUB_TOKEN"]
    session = gh_session(token)

    pr = load_pr_record(number)
    merge_commit = fetch_merge_commit_sha(session, owner, repo, number)
    base_commit, end_commit = resolve_base_and_head(session, owner, repo, number, merge_commit)

    diff = git("diff", base_commit, end_commit).stdout

    # Split by file: each hunk starts with "diff --git a/<path> b/<path>"
    sections = []
    current = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            sections.append(current)
            current = []
        current.append(line)
    if current:
        sections.append(current)

    test_lines, patch_lines = [], []
    for section in sections:
        header = section[0]
        # "diff --git a/<path> b/<path>" -- path has no spaces in this repo
        path = header.split(" b/", 1)[1].strip()
        target = test_lines if path.startswith("nltk/test/") else patch_lines
        target.extend(section)

    task_dir = TASKS_DIR / f"pr-{number}"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "patch.diff").write_text("".join(patch_lines), encoding="utf-8", newline="\n")
    (task_dir / "test_patch.diff").write_text("".join(test_lines), encoding="utf-8", newline="\n")

    task = {
        "repo": f"{owner}/{repo}",
        "pr_number": number,
        "pr_title": pr["title"],
        "pr_url": pr["html_url"],
        "intent": pr.get("intent"),
        "base_commit": base_commit,
        "merge_commit": merge_commit,
        "diff_end_commit": end_commit,  # == merge_commit unless the fallback path (rewritten history) was used
        "patch": "patch.diff" if patch_lines else None,
        "test_patch": "test_patch.diff" if test_lines else None,
        "FAIL_TO_PASS": [],
        "PASS_TO_PASS": [],
        "_status": "generated",  # updated to "verified" / "verify_failed" by verify_task.py
    }
    (task_dir / "task.json").write_text(json.dumps(task, indent=2), encoding="utf-8")

    print(f"PR #{number}: base_commit={base_commit[:10]} merge_commit={merge_commit[:10]}")
    print(f"  patch.diff: {len(patch_lines)} lines, test_patch.diff: {len(test_lines)} lines")
    print(f"  -> {task_dir}")
    return task_dir


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("number", type=int)
    args = ap.parse_args()
    generate(args.number)
