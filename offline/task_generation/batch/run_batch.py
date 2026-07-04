"""
Batch runner: for N PRs x M models, generate+verify the task (real
FAIL_TO_PASS/PASS_TO_PASS via Docker), build a per-PR network-isolated
single-commit agent image, run mini-swe-agent, grade the submission.

Resumable: skips any (pr, model) combo whose results/pr-<n>__<model>.json
already exists. Safe to kill and restart. Progress goes to batch.log (one
line per event) so it can be watched with `tail -f` or read cold in a new
session with no memory of this one.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
TASK_GEN_DIR = HERE.parent
OFFLINE_DIR = TASK_GEN_DIR.parent
MINI_DIR = TASK_GEN_DIR / "mini_swe_agent_test"

sys.path.insert(0, str(TASK_GEN_DIR))
sys.path.insert(0, str(OFFLINE_DIR))
import generate_task  # noqa: E402
import verify_in_docker  # noqa: E402
from analyze_prs import gh_session, gh_get, GITHUB_API, load_dotenv, DATA_DIR  # noqa: E402

PR_NUMBERS = [3672, 3644, 3641, 3544, 3487, 3474, 3460, 3451, 3411, 3371]
MODELS = {
    "laguna": "openrouter/poolside/laguna-xs-2.1",
    "gemini-3.5-flash": "openrouter/google/gemini-3.5-flash",
    "kimi-k2.7-code": "openrouter/moonshotai/kimi-k2.7-code",
    "gpt-oss-120b": "openrouter/openai/gpt-oss-120b",
}

LOG_PATH = HERE / "batch.log"
RESULTS_DIR = HERE / "results"
RESULTS_DIR.mkdir(exist_ok=True)
GRADING_IMAGE = "nltk-eval:pr-3564"  # full history -- reused for grading/verifying every PR
AGENT_BASE_IMAGE = "nltk-agent-base:latest"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd, cwd=None, timeout=900, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


# ---------------------------------------------------------------------------
# Task generation + verification (reuses task_generation/*.py, already generic)
# ---------------------------------------------------------------------------

def ensure_task(number):
    task_dir = TASK_GEN_DIR / "tasks" / f"pr-{number}"
    task_path = task_dir / "task.json"
    if task_path.exists():
        task = json.loads(task_path.read_text(encoding="utf-8"))
        if task.get("_status") in ("verified", "no_discriminating_tests", "no_test_changes"):
            return task
    log(f"pr-{number}: generating task config")
    generate_task.generate(number)
    log(f"pr-{number}: verifying FAIL_TO_PASS/PASS_TO_PASS in Docker")
    verify_in_docker.verify(number)
    return json.loads(task_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Problem statement: linked issue if one exists, else the PR's own body
# ---------------------------------------------------------------------------

def get_problem_statement(session, number):
    prs = [json.loads(l) for l in (DATA_DIR / "prs_sample.jsonl").open(encoding="utf-8")]
    pr = next(p for p in prs if p["number"] == number)
    linked = pr.get("linked_issues") or []
    if linked:
        issue_num = linked[0]
        issue = gh_get(session, f"{GITHUB_API}/repos/nltk/nltk/issues/{issue_num}").json()
        return f"{issue['title']}\n\n{issue.get('body') or ''}", f"issue #{issue_num}"
    return f"{pr['title']}\n\n{pr.get('body') or ''}", "PR description (no linked issue found)"


# ---------------------------------------------------------------------------
# Per-PR agent-safe image (shallow, single-commit, no other branches)
# ---------------------------------------------------------------------------

def ensure_agent_image(number, base_commit):
    tag = f"nltk-agent-safe:pr-{number}"
    existing = run(["docker", "images", "-q", tag]).stdout.strip()
    if existing:
        return tag
    log(f"pr-{number}: building agent-safe image ({tag}) at commit {base_commit[:10]}")
    result = run(
        [
            "docker", "build", "-f", str(HERE / "Dockerfile.agent-safe"),
            "--build-arg", f"BASE_IMAGE={AGENT_BASE_IMAGE}",
            "--build-arg", f"NLTK_COMMIT={base_commit}",
            "-t", tag, str(HERE),
        ],
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"image build failed for pr-{number}: {result.stderr[-2000:]}")
    return tag


# ---------------------------------------------------------------------------
# Run one (pr, model) through mini-swe-agent
# ---------------------------------------------------------------------------

def run_agent(number, model_key, model_name, image_tag, problem_statement):
    from minisweagent.agents import get_agent
    from minisweagent.config import builtin_config_dir, get_config_from_spec
    from minisweagent.models import get_model
    from minisweagent.run.benchmarks.swebench import get_sb_environment
    from minisweagent.utils.serialize import recursive_merge

    os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")

    instance = {
        "instance_id": f"nltk__nltk-{number}",
        "repo": "nltk/nltk",
        "image_name": image_tag,
        "problem_statement": problem_statement,
    }

    default_config = get_config_from_spec(str(builtin_config_dir / "benchmarks" / "swebench.yaml"))
    override = {
        "environment": {
            "cwd": "/repo",
            "environment_class": "docker",
            "run_args": ["--rm", "--entrypoint", "", "--network", "none"],
            "env": {
                "PAGER": "cat", "MANPAGER": "cat", "LESS": "-R",
                "PIP_PROGRESS_BAR": "off", "TQDM_DISABLE": "1", "BASH_ENV": "/dev/null",
            },
        },
        "model": {"model_name": model_name},
        "agent": {"mode": "yolo", "cost_limit": 3.0},
    }
    config = recursive_merge(default_config, override)

    env = get_sb_environment(config, instance)
    agent = get_agent(get_model(config=config.get("model", {})), env, config.get("agent", {}), default_type="default")
    try:
        info = agent.run(problem_statement)
    finally:
        traj_path = RESULTS_DIR / f"pr-{number}__{model_key}.traj.json"
        try:
            agent.save(traj_path)
        except Exception as e:
            log(f"pr-{number}/{model_key}: trajectory save failed: {e}")
    return info


# ---------------------------------------------------------------------------
# Grade a submitted patch against the task's real FAIL_TO_PASS/PASS_TO_PASS
# ---------------------------------------------------------------------------

def grade(number, patch_text):
    task_dir = TASK_GEN_DIR / "tasks" / f"pr-{number}"
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))

    grading_dir = RESULTS_DIR / f"_grading_pr-{number}"
    grading_dir.mkdir(exist_ok=True)
    (grading_dir / "patch.diff").write_text(patch_text, encoding="utf-8", newline="\n")
    if task.get("test_patch"):
        (grading_dir / "test_patch.diff").write_text(
            (task_dir / task["test_patch"]).read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )
    grading_task = {**task, "patch": "patch.diff", "test_patch": "test_patch.diff" if task.get("test_patch") else None}
    (grading_dir / "task.json").write_text(json.dumps(grading_task, indent=2), encoding="utf-8")

    out_path = grading_dir / "result.json"
    docker_cmd = [
        "docker", "run", "--rm", "--network=none", "--memory=2g", "--cpus=2",
        "-v", f"{grading_dir}:/task:ro",
        "-v", f"{grading_dir}:/results",
        GRADING_IMAGE,
    ]
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    run(docker_cmd, env=env, timeout=300)
    try:
        return json.loads(out_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"resolved": False, "error": "grading_run_failed", "FAIL_TO_PASS": {}, "PASS_TO_PASS": {}}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    load_dotenv(OFFLINE_DIR / ".env")
    session = gh_session(os.environ["GITHUB_TOKEN"])
    os.environ["MSYS_NO_PATHCONV"] = "1"

    total = len(PR_NUMBERS) * len(MODELS)
    done_at_start = sum(1 for n in PR_NUMBERS for m in MODELS if (RESULTS_DIR / f"pr-{n}__{m}.json").exists())
    log(f"=== batch start: {len(PR_NUMBERS)} PRs x {len(MODELS)} models = {total} combos, {done_at_start} already done ===")

    for number in PR_NUMBERS:
        try:
            task = ensure_task(number)
        except Exception as e:
            log(f"pr-{number}: TASK GENERATION FAILED: {e}")
            continue

        if not task.get("FAIL_TO_PASS"):
            log(f"pr-{number}: SKIPPING all models -- no verified FAIL_TO_PASS tests for this task "
                f"(status={task.get('_status')})")
            continue

        problem_statement, source = get_problem_statement(session, number)
        log(f"pr-{number}: problem statement source = {source}, "
            f"FAIL_TO_PASS={len(task['FAIL_TO_PASS'])}, PASS_TO_PASS={len(task['PASS_TO_PASS'])}")

        try:
            image_tag = ensure_agent_image(number, task["base_commit"])
        except Exception as e:
            log(f"pr-{number}: AGENT IMAGE BUILD FAILED: {e}")
            continue

        for model_key, model_name in MODELS.items():
            result_path = RESULTS_DIR / f"pr-{number}__{model_key}.json"
            if result_path.exists():
                log(f"pr-{number}/{model_key}: already done, skipping")
                continue

            log(f"pr-{number}/{model_key}: running mini-swe-agent ({model_name})")
            start = time.time()
            try:
                info = run_agent(number, model_key, model_name, image_tag, problem_statement)
                submission = info.get("submission") or ""
                exit_status = info.get("exit_status")
            except Exception as e:
                log(f"pr-{number}/{model_key}: AGENT RUN FAILED: {e}")
                submission, exit_status = "", f"error: {e}"
            elapsed = time.time() - start

            if submission.strip():
                log(f"pr-{number}/{model_key}: submitted {len(submission)} chars in {elapsed:.0f}s, grading...")
                verdict = grade(number, submission)
            else:
                log(f"pr-{number}/{model_key}: no submission (exit_status={exit_status}), skipping grading")
                verdict = {"resolved": False, "FAIL_TO_PASS": {}, "PASS_TO_PASS": {}}

            ftp = verdict.get("FAIL_TO_PASS", {})
            ptp = verdict.get("PASS_TO_PASS", {})
            ftp_passed = sum(1 for v in ftp.values() if v == "passed")
            ptp_passed = sum(1 for v in ptp.values() if v == "passed")

            result = {
                "pr_number": number,
                "model_key": model_key,
                "model_name": model_name,
                "exit_status": exit_status,
                "elapsed_seconds": round(elapsed, 1),
                "submission_chars": len(submission),
                "resolved": verdict.get("resolved", False),
                "fail_to_pass_total": len(task["FAIL_TO_PASS"]),
                "fail_to_pass_passed": ftp_passed,
                "pass_to_pass_total": len(task["PASS_TO_PASS"]),
                "pass_to_pass_passed": ptp_passed,
                "detail": verdict,
            }
            result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            log(f"pr-{number}/{model_key}: DONE -- resolved={result['resolved']} "
                f"FAIL_TO_PASS {ftp_passed}/{len(task['FAIL_TO_PASS'])} "
                f"PASS_TO_PASS {ptp_passed}/{len(task['PASS_TO_PASS'])}")

    log("=== batch complete ===")


if __name__ == "__main__":
    main()
