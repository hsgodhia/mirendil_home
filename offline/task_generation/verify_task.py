"""
Runs INSIDE the nltk-eval Docker container to verify one generated task.

Derives FAIL_TO_PASS (tests that exist only once test_patch is applied) and
PASS_TO_PASS (tests that existed before too, so must keep passing) by diffing
`pytest --collect-only` output between the base commit and base+test_patch --
this handles both function-style and unittest.TestCase-class-style tests
correctly, unlike a regex over `def test_` lines.

Then empirically verifies, in order:
  1. base + test_patch (no source fix)  -> FAIL_TO_PASS must NOT pass,
     PASS_TO_PASS must pass
  2. + patch.diff (the source fix)      -> everything must pass

Any candidate test that doesn't behave as expected at either checkpoint is
dropped and reported, rather than silently included in a broken task.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run(cmd, cwd, timeout=600):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def git_checkout_clean(repo, commit):
    run(["git", "checkout", "--force", commit], repo)
    run(["git", "clean", "-fd"], repo)


def git_apply(repo, patch_path):
    r = run(["git", "apply", "--whitespace=nowarn", patch_path], repo)
    return r.returncode == 0, r.stderr


def touched_test_files(test_patch_path):
    files = []
    for line in Path(test_patch_path).read_text(encoding="utf-8").splitlines():
        if line.startswith("+++ b/"):
            files.append(line[len("+++ b/"):])
    return files


def pytest_collect(repo, test_root, rel_paths):
    """rel_paths are relative to repo root (e.g. nltk/test/unit/test_stem.py).
    Only pass paths that currently exist in the checked-out tree -- pytest
    errors out entirely (collecting nothing) if any arg path is missing,
    which matters for brand-new test files that don't exist at base_commit.
    """
    existing = [p for p in rel_paths if (Path(repo) / p).exists()]
    if not existing:
        return set()
    args_paths = [p[len("nltk/test/"):] for p in existing]
    r = run([sys.executable, "-m", "pytest", "--collect-only", "-q", *args_paths], test_root, timeout=120)
    ids = set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("=", "!")):
            ids.add(line)
    return ids


def pytest_run(test_root, node_ids):
    if not node_ids:
        return {}
    report_path = "/tmp/verify_pytest_report.json"
    run(
        [sys.executable, "-m", "pytest", *node_ids,
         "--json-report", f"--json-report-file={report_path}", "-q", "--no-header"],
        test_root, timeout=600,
    )
    try:
        data = json.loads(Path(report_path).read_text())
    except FileNotFoundError:
        return {n: "ERROR" for n in node_ids}
    return {t["nodeid"]: t["outcome"] for t in data.get("tests", [])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--task-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    task_path = Path(args.task_dir) / "task.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    repo = args.repo
    test_root = f"{repo}/nltk/test"

    report = {"pr_number": task["pr_number"], "log": []}

    def log(msg):
        report["log"].append(msg)
        print(msg, flush=True)

    def finish(status, **extra):
        report["status"] = status
        report["updated_task"] = task  # host applies this back onto the real task.json
        report.update(extra)
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))

    base = task["base_commit"]
    test_patch = task.get("test_patch")
    patch = task.get("patch")

    if not test_patch:
        log("no test_patch in this PR -- can't derive FAIL_TO_PASS; checking patch applies only")
        git_checkout_clean(repo, base)
        ok, err = (True, None)
        if patch:
            ok, err = git_apply(repo, f"{args.task_dir}/{patch}")
        task["FAIL_TO_PASS"] = []
        task["PASS_TO_PASS"] = []
        task["_status"] = "no_test_changes" if ok else "patch_apply_failed"
        finish(task["_status"], patch_applies=ok, patch_apply_error=err)
        return

    test_patch_full = f"{args.task_dir}/{test_patch}"
    touched = touched_test_files(test_patch_full)
    log(f"touched test files: {touched}")

    # Candidates = every test node ID that exists in the FIXED state (test_patch
    # + patch both applied), not the pre-fix state. A test file often imports a
    # name the fix itself introduces (a new function/module for the security
    # fixes in this batch, or new internals like _BOS/_EOS for a bigger rewrite)
    # -- collecting right after test_patch alone would hit an ImportError and
    # yield zero candidates even though the tests are perfectly valid once
    # fixed. The post-fix state is the one guaranteed importable, since it's
    # the real merged code.
    git_checkout_clean(repo, base)
    ok, err = git_apply(repo, test_patch_full)
    if not ok:
        task["_status"] = "test_patch_apply_failed"
        finish("test_patch_apply_failed", error=err)
        return
    if patch:
        ok, err = git_apply(repo, f"{args.task_dir}/{patch}")
        if not ok:
            task["_status"] = "patch_apply_failed"
            finish("patch_apply_failed", error=err)
            return
    candidates = sorted(pytest_collect(repo, test_root, touched))
    log(f"candidates: {len(candidates)} tests collected with test_patch + patch applied (post-fix)")

    if not candidates:
        task["FAIL_TO_PASS"] = []
        task["PASS_TO_PASS"] = []
        task["_status"] = "no_tests_found"
        finish("no_tests_found", note="test_patch touched files but no tests could be collected even post-fix")
        return

    # Phase 1: back to base source (unfixed) + test_patch only. A test whose
    # file fails to even import here (because the fix's new name doesn't
    # exist yet) correctly counts as "not passed" -- pytest_run's json report
    # simply won't have an entry for it, and phase1.get(t) returns None.
    git_checkout_clean(repo, base)
    git_apply(repo, test_patch_full)
    phase1 = pytest_run(test_root, candidates)
    fail_to_pass = [t for t in candidates if phase1.get(t) != "passed"]
    pass_to_pass = [t for t in candidates if phase1.get(t) == "passed"]
    log(f"phase1 (pre-fix): {len(fail_to_pass)} fail (-> FAIL_TO_PASS), "
        f"{len(pass_to_pass)} already pass (-> PASS_TO_PASS)")

    if not fail_to_pass:
        task["FAIL_TO_PASS"] = []
        task["PASS_TO_PASS"] = pass_to_pass
        task["_status"] = "no_discriminating_tests"
        finish("no_discriminating_tests",
               note="every candidate test already passes on the unfixed source -- "
                     "test_patch doesn't actually exercise the bug this PR fixes")
        return

    # Phase 2: + the source patch -- everything must now pass.
    if patch:
        ok, err = git_apply(repo, f"{args.task_dir}/{patch}")
        if not ok:
            task["_status"] = "patch_apply_failed"
            finish("patch_apply_failed", error=err)
            return

    phase2 = pytest_run(test_root, fail_to_pass + pass_to_pass)
    still_bad = [t for t in fail_to_pass + pass_to_pass if phase2.get(t) != "passed"]
    final_fail_to_pass = [t for t in fail_to_pass if t not in still_bad]
    final_pass_to_pass = [t for t in pass_to_pass if t not in still_bad]
    log(f"phase2 (post-fix): {len(still_bad)} still not passing (dropped)")

    task["FAIL_TO_PASS"] = final_fail_to_pass
    task["PASS_TO_PASS"] = final_pass_to_pass
    task["_status"] = "verified" if final_fail_to_pass else "no_valid_tests"

    finish(
        task["_status"],
        FAIL_TO_PASS=final_fail_to_pass,
        PASS_TO_PASS=final_pass_to_pass,
        dropped_still_failing_post_fix=still_bad,
    )


if __name__ == "__main__":
    main()
