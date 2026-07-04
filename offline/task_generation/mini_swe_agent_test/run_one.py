"""
Run mini-swe-agent on one hand-built instance dict, bypassing the CLI's
datasets.load_dataset() path resolution (which only accepts Hub dataset ids
or data_files=, not a bare local file path -- not worth fighting for a
single-instance smoke test).
"""
import json
import os
import sys
import time
from pathlib import Path

from minisweagent.agents import get_agent
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.models import get_model
from minisweagent.run.benchmarks.swebench import get_sb_environment
from minisweagent.utils.serialize import recursive_merge

HERE = Path(__file__).parent

def load_dotenv(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

load_dotenv(HERE.parent.parent / ".env")
os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")  # gpt-oss-120b:free has no litellm pricing entry

instance = json.loads((HERE / "dataset.jsonl").read_text(encoding="utf-8").splitlines()[0])

default_config = get_config_from_spec(str(builtin_config_dir / "benchmarks" / "swebench.yaml"))
local_config = get_config_from_spec(str(HERE / "swebench_local.yaml"))
config = recursive_merge(default_config, local_config)
config.setdefault("agent", {})["mode"] = "yolo"  # no interactive confirmation
config["agent"]["cost_limit"] = 2.0

print(f"instance_id: {instance['instance_id']}")
print(f"image: {instance['image_name']}")
print(f"model: {config['model']['model_name']}")

start = time.time()
env = get_sb_environment(config, instance)
agent = get_agent(get_model(config=config.get("model", {})), env, config.get("agent", {}), default_type="default")
try:
    info = agent.run(instance["problem_statement"])
finally:
    agent.save(HERE / "trajectory.json")
elapsed = time.time() - start

result = {
    "instance_id": instance["instance_id"],
    "exit_status": info.get("exit_status"),
    "submission": info.get("submission"),
    "elapsed_seconds": round(elapsed, 1),
}
(HERE / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(f"\n=== exit_status: {result['exit_status']} | elapsed: {elapsed:.1f}s ===")
print(f"submission length: {len(result['submission'] or '')} chars")
