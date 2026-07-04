"""
Host-side wrapper: runs verify_task.py inside the nltk-eval container for one
task, then writes the container's `updated_task` (real FAIL_TO_PASS/PASS_TO_PASS,
_status) back onto tasks/pr-<n>/task.json -- the container only ever sees
that folder read-only.
"""
import argparse
import json
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).parent
IMAGE = "nltk-eval:pr-3564"


def verify(number):
    task_dir = HERE / "tasks" / f"pr-{number}"
    results_dir = HERE / "results"
    results_dir.mkdir(exist_ok=True)

    start = time.time()
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{HERE}/verify_task.py:/opt/verify_task.py:ro",
        "-v", f"{task_dir}:/task:ro",
        "-v", f"{results_dir}:/results",
        "--entrypoint", "python",
        IMAGE,
        "/opt/verify_task.py", "--repo", "/repo", "--task-dir", "/task",
        "--out", f"/results/pr-{number}.json",
    ]
    import os
    env = dict(os.environ)
    env["MSYS_NO_PATHCONV"] = "1"
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=900)
    elapsed = time.time() - start

    print(result.stdout)
    if result.returncode != 0:
        print("STDERR:", result.stderr)

    report_path = results_dir / f"pr-{number}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["elapsed_seconds"] = round(elapsed, 1)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    updated_task = report.pop("updated_task", None)
    if updated_task:
        (task_dir / "task.json").write_text(json.dumps(updated_task, indent=2), encoding="utf-8")

    print(f"\nstatus: {report.get('status')} | elapsed: {elapsed:.1f}s")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("number", type=int)
    args = ap.parse_args()
    verify(args.number)
