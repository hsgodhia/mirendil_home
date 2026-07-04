"""
Aggregate every pr-<N>__<model>.json + its .traj.json into one table:
FAIL_TO_PASS, PASS_TO_PASS, elapsed time, input/output tokens, step count.
Prints as JSON to stdout for the report generator to consume.
"""
import json
import glob
from pathlib import Path

HERE = Path(__file__).parent
RESULTS_DIR = HERE / "results"


def extract_traj_stats(traj_path):
    if not traj_path.exists():
        return None, None, None
    try:
        data = json.loads(traj_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None, None
    msgs = data.get("messages", [])
    input_tokens = 0
    output_tokens = 0
    steps = 0
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        extra = m.get("extra") or {}
        resp = extra.get("response") or {}
        usage = resp.get("usage") if isinstance(resp, dict) else None
        if usage:
            input_tokens += usage.get("prompt_tokens") or 0
            output_tokens += usage.get("completion_tokens") or 0
        if m.get("tool_calls") or (extra.get("actions")):
            steps += 1
    return input_tokens or None, output_tokens or None, steps or None


def main():
    rows = []
    for f in sorted(glob.glob(str(RESULTS_DIR / "pr-*__*.json"))):
        fp = Path(f)
        if "_grading" in fp.name or fp.name.endswith(".traj.json"):
            continue
        d = json.loads(fp.read_text(encoding="utf-8"))
        traj_path = RESULTS_DIR / f"pr-{d['pr_number']}__{d['model_key']}.traj.json"
        in_tok, out_tok, steps = extract_traj_stats(traj_path)
        rows.append({
            "pr_number": d["pr_number"],
            "model_key": d["model_key"],
            "resolved": d["resolved"],
            "exit_status": d["exit_status"],
            "fail_to_pass_passed": d["fail_to_pass_passed"],
            "fail_to_pass_total": d["fail_to_pass_total"],
            "pass_to_pass_passed": d["pass_to_pass_passed"],
            "pass_to_pass_total": d["pass_to_pass_total"],
            "elapsed_seconds": d["elapsed_seconds"],
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "steps": steps,
        })
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
