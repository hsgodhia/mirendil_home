"""
SWE-bench-style evaluation harness: applies a model-generated patch to a
frozen NLTK checkout, runs the task's FAIL_TO_PASS / PASS_TO_PASS tests,
and emits a structured verdict.
"""

import argparse
import json
import subprocess
import sys


def run(cmd, cwd):
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=600
    )


def git_apply(repo, patch_path, label):
    result = run(["git", "apply", "--whitespace=nowarn", patch_path], repo)
    if result.returncode != 0:
        print(f"[{label}] git apply failed:\n{result.stderr}", file=sys.stderr)
        return False
    return True


def pytest_run_one_invocation(test_root, node_ids):
    report_path = "/tmp/pytest_report.json"
    run(
        [
            sys.executable, "-m", "pytest",
            *node_ids,
            "--json-report", f"--json-report-file={report_path}",
            "-q", "--no-header",
        ],
        test_root,
    )
    try:
        with open(report_path) as f:
            report = json.load(f)
    except FileNotFoundError:
        return {}
    return {test["nodeid"]: test["outcome"] for test in report.get("tests", [])}


def pytest_run(test_root, node_ids):
    # NLTK's pytest.ini lives in nltk/test/, so pytest anchors node IDs
    # relative to that directory (e.g. "unit/test_tnt.py::test_x"), not to
    # the repo root. task.json's ids must already be in that form.
    if not node_ids:
        return {}

    # A collection ImportError in one file (e.g. a patch that doesn't define
    # a name a test module imports) makes pytest abort the *entire* session
    # when that file's tests were explicitly named on the command line --
    # --continue-on-collection-errors does NOT help here, it only covers
    # directory sweeps. Invoking pytest once per file isolates the blast
    # radius so one broken file can't hide the real pass/fail status of
    # every other file in the same grading run.
    by_file = {}
    for node_id in node_ids:
        file_part = node_id.split("::", 1)[0]
        by_file.setdefault(file_part, []).append(node_id)

    outcomes = {}
    for file_node_ids in by_file.values():
        outcomes.update(pytest_run_one_invocation(test_root, file_node_ids))
    return outcomes


def resolve(node_id, outcomes):
    # A bare "file.py::test_func" id (no parametrize brackets) is
    # considered passed only if every parametrized variant passed. A bare
    # "file.py" module id is passed only if every test in that module passed.
    direct = outcomes.get(node_id)
    if direct is not None:
        return direct
    variants = [
        v for k, v in outcomes.items()
        if k.startswith(node_id + "[") or k.startswith(node_id + "::")
    ]
    if not variants:
        return "MISSING"
    return "passed" if all(v == "passed" for v in variants) else "failed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.task) as f:
        task = json.load(f)

    task_dir = "/".join(args.task.split("/")[:-1])

    # Reset to the task's frozen base commit before applying anything.
    run(["git", "checkout", "--force", task["base_commit"]], args.repo)
    run(["git", "clean", "-fd"], args.repo)

    if task.get("test_patch"):
        git_apply(args.repo, f"{task_dir}/{task['test_patch']}", "test_patch")

    if task.get("patch"):
        applied = git_apply(args.repo, f"{task_dir}/{task['patch']}", "patch")
        if not applied:
            result = {"resolved": False, "reason": "patch_apply_failed"}
            with open(args.out, "w") as f:
                json.dump(result, f, indent=2)
            sys.exit(1)

    fail_to_pass = task.get("FAIL_TO_PASS", [])
    pass_to_pass = task.get("PASS_TO_PASS", [])
    test_root = f"{args.repo}/nltk/test"
    outcomes = pytest_run(test_root, fail_to_pass + pass_to_pass)

    fail_to_pass_results = {t: resolve(t, outcomes) for t in fail_to_pass}
    pass_to_pass_results = {t: resolve(t, outcomes) for t in pass_to_pass}

    result = {
        "resolved": all(v == "passed" for v in fail_to_pass_results.values())
        and all(v == "passed" for v in pass_to_pass_results.values()),
        "FAIL_TO_PASS": fail_to_pass_results,
        "PASS_TO_PASS": pass_to_pass_results,
    }
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))
    sys.exit(0 if result["resolved"] else 1)


if __name__ == "__main__":
    main()
