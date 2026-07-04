"""
Run all 10 PRs for a single additional model: anthropic/claude-opus-4.8 via
OpenRouter, with reasoning effort forced to "high".

Runs as an independent process alongside (not instead of) run_batch.py's
4-model sweep -- writes to the same results/ directory (each combo owns a
uniquely-named file, so no collision) and the same tasks/ directory (task
generation is resumable/idempotent), but its own log file, since two
processes appending to the same batch.log risk interleaved lines.

--- Reasoning effort: verified, not assumed ---
litellm's *static* model registry doesn't yet recognize
"openrouter/anthropic/claude-opus-4.8" as reasoning-capable
(litellm.supports_reasoning(...) returns False for it), which means the
standard top-level `reasoning_effort` kwarg gets silently dropped for this
model (config sets drop_params=True) -- confirmed empirically: passing
reasoning_effort="high" produced reasoning_tokens=0 in the response.

The reliable fix, also verified empirically (reasoning_tokens=30, matching
a direct curl call to OpenRouter's API with the same body): bypass
litellm's parameter-support gate entirely by passing OpenRouter's native
request shape straight through via `extra_body`, which litellm forwards
unconditionally regardless of what its internal model database knows:

    extra_body={"reasoning": {"effort": "high"}}
"""

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import run_batch as rb  # noqa: E402

MODEL_KEY = "claude-opus-4.8-high"
MODEL_NAME = "openrouter/anthropic/claude-opus-4.8"
MODEL_KWARGS = {"extra_body": {"reasoning": {"effort": "high"}}}

LOG_PATH = HERE / "batch_opus.log"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


rb.log = log  # redirect the imported module's logging to our own log file


def main():
    rb.load_dotenv(rb.OFFLINE_DIR / ".env")
    session = rb.gh_session(__import__("os").environ["GITHUB_TOKEN"])
    __import__("os").environ["MSYS_NO_PATHCONV"] = "1"

    done_at_start = sum(1 for n in rb.PR_NUMBERS if (rb.RESULTS_DIR / f"pr-{n}__{MODEL_KEY}.json").exists())
    log(f"=== opus batch start: {len(rb.PR_NUMBERS)} PRs, model={MODEL_NAME} (reasoning=high), "
        f"{done_at_start} already done ===")

    for number in rb.PR_NUMBERS:
        try:
            task = rb.ensure_task(number)
        except Exception as e:
            log(f"pr-{number}: TASK GENERATION FAILED: {e}")
            continue

        if not task.get("FAIL_TO_PASS"):
            log(f"pr-{number}: SKIPPING -- no verified FAIL_TO_PASS tests for this task "
                f"(status={task.get('_status')})")
            continue

        result_path = rb.RESULTS_DIR / f"pr-{number}__{MODEL_KEY}.json"
        if result_path.exists():
            log(f"pr-{number}/{MODEL_KEY}: already done, skipping")
            continue

        problem_statement, source = rb.get_problem_statement(session, number)
        log(f"pr-{number}: problem statement source = {source}, "
            f"FAIL_TO_PASS={len(task['FAIL_TO_PASS'])}, PASS_TO_PASS={len(task['PASS_TO_PASS'])}")

        try:
            image_tag = rb.ensure_agent_image(number, task["base_commit"])
        except Exception as e:
            log(f"pr-{number}: AGENT IMAGE BUILD FAILED: {e}")
            continue

        timeout_s = rb.compute_timeout(number)
        log(f"pr-{number}/{MODEL_KEY}: running mini-swe-agent ({MODEL_NAME}, reasoning=high), "
            f"timeout={timeout_s:.0f}s (4x completed-peer average)")
        start = time.time()
        try:
            outcome, info = rb.run_agent_with_timeout(
                number, MODEL_KEY, MODEL_NAME, image_tag, problem_statement, timeout_s, MODEL_KWARGS
            )
            if outcome == "timeout":
                log(f"pr-{number}/{MODEL_KEY}: TIMED OUT after {timeout_s:.0f}s -- killed its container, "
                    f"marking failed, moving on")
                submission, exit_status = "", "TimedOut"
            else:
                submission = info.get("submission") or ""
                exit_status = info.get("exit_status")
        except Exception as e:
            log(f"pr-{number}/{MODEL_KEY}: AGENT RUN FAILED: {e}")
            submission, exit_status = "", f"error: {e}"
        elapsed = time.time() - start

        if submission.strip():
            log(f"pr-{number}/{MODEL_KEY}: submitted {len(submission)} chars in {elapsed:.0f}s, grading...")
            verdict = rb.grade(number, submission)
        else:
            log(f"pr-{number}/{MODEL_KEY}: no submission (exit_status={exit_status}), skipping grading")
            verdict = {"resolved": False, "FAIL_TO_PASS": {}, "PASS_TO_PASS": {}}

        ftp = verdict.get("FAIL_TO_PASS", {})
        ptp = verdict.get("PASS_TO_PASS", {})
        ftp_passed = sum(1 for v in ftp.values() if v == "passed")
        ptp_passed = sum(1 for v in ptp.values() if v == "passed")

        result = {
            "pr_number": number,
            "model_key": MODEL_KEY,
            "model_name": MODEL_NAME,
            "model_kwargs": MODEL_KWARGS,
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
        log(f"pr-{number}/{MODEL_KEY}: DONE -- resolved={result['resolved']} "
            f"FAIL_TO_PASS {ftp_passed}/{len(task['FAIL_TO_PASS'])} "
            f"PASS_TO_PASS {ptp_passed}/{len(task['PASS_TO_PASS'])}")

    log("=== opus batch complete ===")


if __name__ == "__main__":
    main()
