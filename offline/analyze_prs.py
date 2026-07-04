"""
Pipeline for building a diverse, intent-labeled sample of merged PRs from a
GitHub repo (default: nltk/nltk).

Stages (each resumable, run via subcommand or `all`):

  list       Cheaply list every merged PR (title, body, dates, author,
             labels) -> data/prs_list.jsonl
  candidates Stratify prs_list.jsonl by year and pick a candidate pool
             (default 300) -> data/prs_candidates.json
  enrich     Fetch full stats (additions/deletions/changed_files/commits)
             and the changed-file list for each candidate
             -> data/prs_enriched.jsonl
  sample     From the enriched pool, pick a final diverse 100 (stratified
             by year x size bucket) -> data/prs_sample.jsonl
  classify   Call the Anthropic API to tag each of the 100 with an intent
             from a controlled taxonomy -> data/prs_classified.jsonl
  select     Pick one representative PR per intent
             -> data/taxonomy_selection.json
  all        Run every stage in sequence

Requires env vars GITHUB_TOKEN and (for `classify`) ANTHROPIC_API_KEY.
"""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

GITHUB_API = "https://api.github.com"
DATA_DIR = Path(__file__).parent / "data"


def load_dotenv(path=Path(__file__).parent / ".env"):
    """Minimal .env loader: sets os.environ from KEY=VALUE lines, without
    overwriting variables already set in the real environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


load_dotenv()

INTENT_TAXONOMY = [
    "bug_fix",
    "feature_addition",
    "refactor_cleanup",
    "performance",
    "documentation",
    "test_coverage",
    "build_ci_deps",
    "deprecation_removal",
    "api_breaking_change",
    "style_lint_formatting",
    "security_fix",
    "revert",
    "data_corpus_update",
    "other",
]

ISSUE_REF_RE = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def gh_session(token):
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return s


def gh_get(session, url, params=None):
    while True:
        resp = session.get(url, params=params, timeout=30)
        remaining = int(resp.headers.get("X-RateLimit-Remaining", "1"))
        if resp.status_code == 403 and remaining == 0:
            reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(reset - time.time(), 1) + 1
            print(f"  rate limited, sleeping {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            print(f"  {resp.status_code} from {url}, retrying in 5s", file=sys.stderr)
            time.sleep(5)
            continue
        resp.raise_for_status()
        return resp


def gh_paginate(session, url, params=None):
    params = dict(params or {})
    params.setdefault("per_page", 100)
    while url:
        resp = gh_get(session, url, params=params)
        yield resp.json()
        url = resp.links.get("next", {}).get("url")
        params = None  # subsequent requests use the full URL from Link header


# ---------------------------------------------------------------------------
# Stage: list
# ---------------------------------------------------------------------------

def stage_list(session, owner, repo, out_path):
    seen = set()
    if out_path.exists():
        with out_path.open(encoding="utf-8") as f:
            for line in f:
                seen.add(json.loads(line)["number"])
        print(f"resuming, {len(seen)} PRs already listed")

    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls"
    count = 0
    with out_path.open("a", encoding="utf-8") as out:
        for page in gh_paginate(session, url, {"state": "closed", "sort": "created", "direction": "asc"}):
            if not page:
                break
            for pr in page:
                if pr["number"] in seen or pr.get("merged_at") is None:
                    continue
                record = {
                    "number": pr["number"],
                    "title": pr["title"],
                    "body": pr.get("body") or "",
                    "user": pr["user"]["login"] if pr.get("user") else None,
                    "created_at": pr["created_at"],
                    "merged_at": pr["merged_at"],
                    "closed_at": pr["closed_at"],
                    "labels": [l["name"] for l in pr.get("labels", [])],
                    "draft": pr.get("draft", False),
                    "base_ref": pr["base"]["ref"],
                    "head_ref": pr["head"]["ref"],
                    "html_url": pr["html_url"],
                }
                out.write(json.dumps(record) + "\n")
                seen.add(pr["number"])
                count += 1
            print(f"  listed {len(seen)} merged PRs so far...")
    print(f"stage_list: added {count} new PRs, {len(seen)} total -> {out_path}")


# ---------------------------------------------------------------------------
# Stage: candidates
# ---------------------------------------------------------------------------

def stage_candidates(list_path, out_path, pool_size, seed):
    prs = [json.loads(l) for l in list_path.open(encoding="utf-8")]
    by_year = defaultdict(list)
    for pr in prs:
        year = pr["merged_at"][:4]
        by_year[year].append(pr)

    rng = random.Random(seed)
    years = sorted(by_year)
    per_year = max(1, pool_size // len(years))

    candidates = []
    for year in years:
        pool = by_year[year][:]
        rng.shuffle(pool)
        candidates.extend(pool[:per_year])

    # top up to pool_size if under, from leftover PRs not already picked
    if len(candidates) < pool_size:
        chosen = {c["number"] for c in candidates}
        leftover = [pr for pr in prs if pr["number"] not in chosen]
        rng.shuffle(leftover)
        candidates.extend(leftover[: pool_size - len(candidates)])

    candidates = candidates[:pool_size]
    out_path.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    print(f"stage_candidates: picked {len(candidates)} candidates across {len(years)} years -> {out_path}")


# ---------------------------------------------------------------------------
# Stage: enrich
# ---------------------------------------------------------------------------

def fetch_pr_detail(session, owner, repo, number):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}"
    return gh_get(session, url).json()


def fetch_pr_files(session, owner, repo, number):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/files"
    files = []
    for page in gh_paginate(session, url):
        for f in page:
            files.append({
                "filename": f["filename"],
                "status": f["status"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "changes": f["changes"],
            })
    return files


def stage_enrich(session, owner, repo, candidates_path, out_path):
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))

    done = set()
    if out_path.exists():
        with out_path.open(encoding="utf-8") as f:
            for line in f:
                done.add(json.loads(line)["number"])
        print(f"resuming, {len(done)} PRs already enriched")

    with out_path.open("a", encoding="utf-8") as out:
        for i, pr in enumerate(candidates):
            number = pr["number"]
            if number in done:
                continue
            detail = fetch_pr_detail(session, owner, repo, number)
            files = fetch_pr_files(session, owner, repo, number)
            issue_refs = sorted({int(m.group(2)) for m in ISSUE_REF_RE.finditer(pr["body"] or "")})

            record = {
                **pr,
                "additions": detail.get("additions"),
                "deletions": detail.get("deletions"),
                "changed_files": detail.get("changed_files"),
                "commits": detail.get("commits"),
                "comments": detail.get("comments"),
                "review_comments": detail.get("review_comments"),
                "author_association": detail.get("author_association"),
                "merged_by": (detail.get("merged_by") or {}).get("login"),
                "milestone": (detail.get("milestone") or {}).get("title"),
                "linked_issues": issue_refs,
                "files": files,
            }
            out.write(json.dumps(record) + "\n")
            done.add(number)
            if (i + 1) % 10 == 0:
                print(f"  enriched {i + 1}/{len(candidates)}")
    print(f"stage_enrich: {len(done)} PRs enriched -> {out_path}")


# ---------------------------------------------------------------------------
# Stage: sample
# ---------------------------------------------------------------------------

def size_bucket(pr):
    lines = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    if lines <= 10:
        return "tiny"
    if lines <= 100:
        return "small"
    if lines <= 500:
        return "medium"
    return "large"


def has_test_changes(pr):
    return any(f["filename"].startswith("nltk/test/") for f in pr["files"])


def stage_sample(enriched_path, out_path, target, seed):
    all_prs = [json.loads(l) for l in enriched_path.open(encoding="utf-8")]
    prs = [pr for pr in all_prs if has_test_changes(pr)]
    print(f"stage_sample: {len(prs)}/{len(all_prs)} enriched candidates touch a nltk/test/ file "
          f"(the rest have no way to demonstrate FAIL_TO_PASS and are dropped)")
    rng = random.Random(seed)

    buckets = defaultdict(list)
    for pr in prs:
        key = (pr["merged_at"][:4], size_bucket(pr))
        buckets[key].append(pr)

    for v in buckets.values():
        rng.shuffle(v)

    keys = list(buckets)
    rng.shuffle(keys)

    chosen = []
    chosen_numbers = set()
    idx = 0
    # round-robin across (year, size) strata until target reached or exhausted
    while len(chosen) < target and any(buckets[k] for k in keys):
        key = keys[idx % len(keys)]
        idx += 1
        if buckets[key]:
            pr = buckets[key].pop()
            if pr["number"] not in chosen_numbers:
                chosen.append(pr)
                chosen_numbers.add(pr["number"])

    with out_path.open("w", encoding="utf-8") as out:
        for pr in chosen:
            out.write(json.dumps(pr) + "\n")
    print(f"stage_sample: selected {len(chosen)} PRs -> {out_path}")
    dist = defaultdict(int)
    for pr in chosen:
        dist[size_bucket(pr)] += 1
    print(f"  size distribution: {dict(dist)}")


# ---------------------------------------------------------------------------
# Stage: classify
# ---------------------------------------------------------------------------

CLASSIFY_TOOL = {
    "name": "record_pr_intent",
    "description": "Record the classified intent of a pull request.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": INTENT_TAXONOMY},
            "other_label": {
                "type": "string",
                "description": "Short custom label, only set when category is 'other'.",
            },
            "rationale": {
                "type": "string",
                "description": "One sentence justifying the category.",
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["category", "rationale", "confidence"],
    },
}


def build_classify_prompt(pr, cli_mode=False):
    files_summary = "\n".join(
        f"  {f['status']:>8} {f['filename']} (+{f['additions']}/-{f['deletions']})"
        for f in pr["files"][:30]
    )
    if len(pr["files"]) > 30:
        files_summary += f"\n  ... and {len(pr['files']) - 30} more files"

    body = (pr["body"] or "").strip()
    if len(body) > 2000:
        body = body[:2000] + "\n...[truncated]"

    prompt = f"""Classify the intent of this merged pull request from the nltk/nltk repository.

Title: {pr['title']}

Description:
{body or '(no description provided)'}

Stats: +{pr['additions']}/-{pr['deletions']} lines, {pr['changed_files']} files changed, {pr['commits']} commits
Labels: {', '.join(pr['labels']) or '(none)'}

Files changed:
{files_summary or '(none)'}

Pick the single best-fitting category from this taxonomy: {', '.join(INTENT_TAXONOMY)}.
Use 'other' only if truly nothing else fits."""

    if cli_mode:
        prompt += """

Reply with ONLY a single JSON object, no markdown fences, no other text:
{"category": "<one of the taxonomy values>", "other_label": "<short custom tag, only if category is other, else empty string>", "rationale": "<one sentence>", "confidence": <number 0-1>}"""
    return prompt


def parse_json_object(text):
    """Extract the first top-level JSON object from text, tolerating markdown fences."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text, start)
            return obj
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
    raise ValueError(f"no JSON object found in: {text[:200]!r}")


CLASSIFY_SYSTEM_PROMPT = (
    "You are a PR intent classifier. Reply with only the requested JSON, no other text."
)

_CLAUDE_BIN = None


def claude_bin():
    """Resolve the claude executable's full path once. On Windows, npm installs
    it as claude.CMD; passing the bare "claude" to subprocess.run (no shell=True)
    fails with WinError 2 because CreateProcess doesn't do PATHEXT resolution
    the way cmd.exe does -- shutil.which() does that resolution for us."""
    global _CLAUDE_BIN
    if _CLAUDE_BIN is None:
        _CLAUDE_BIN = shutil.which("claude")
        if _CLAUDE_BIN is None:
            raise RuntimeError("claude CLI not found on PATH")
    return _CLAUDE_BIN


def classify_one_cli(pr, model):
    """Run a single, fresh headless `claude -p` session (Pro/Max subscription
    auth, not API-billed) to classify one PR.

    --safe-mode disables CLAUDE.md/memory/skills/plugins/hooks (this is what
    was dragging ~20K tokens of unrelated project context into every call)
    while leaving OAuth/subscription auth intact -- unlike --bare, which also
    strips context but forces API-key-only auth. --tools "" drops all tool
    schemas since classification needs none, and --system-prompt replaces
    (not appends to) the default prompt.
    """
    prompt = build_classify_prompt(pr, cli_mode=True)
    # Pass the (multi-line) prompt via stdin, not as a CLI argument: the
    # resolved binary is npm's claude.CMD, and Windows batch files mangle
    # embedded newlines in argv, silently truncating/dropping the prompt.
    result = subprocess.run(
        [
            claude_bin(), "-p",
            "--output-format", "json",
            "--model", model,
            "--safe-mode",
            "--tools", "",
            "--no-session-persistence",
            "--system-prompt", CLASSIFY_SYSTEM_PROMPT,
        ],
        input=prompt,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exited {result.returncode}: {result.stderr[:500]}")
    wrapper = json.loads(result.stdout)
    if wrapper.get("is_error"):
        raise RuntimeError(f"claude CLI reported an error: {wrapper}")
    return parse_json_object(wrapper["result"])


def classify_one_api(client, pr, model):
    resp = client.messages.create(
        model=model,
        max_tokens=500,
        tools=[CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "record_pr_intent"},
        messages=[{"role": "user", "content": build_classify_prompt(pr)}],
    )
    tool_use = next(b for b in resp.content if b.type == "tool_use")
    return tool_use.input


def stage_classify(sample_path, out_path, model, engine):
    done = {}
    if out_path.exists():
        with out_path.open(encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                done[rec["number"]] = rec
        print(f"resuming, {len(done)} PRs already classified")

    prs = [json.loads(l) for l in sample_path.open(encoding="utf-8")]

    client = None
    if engine == "api":
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY is not set", file=sys.stderr)
            sys.exit(1)
        client = anthropic.Anthropic(api_key=api_key)

    with out_path.open("a", encoding="utf-8") as out:
        for i, pr in enumerate(prs):
            if pr["number"] in done:
                continue
            try:
                if engine == "cli":
                    classification = classify_one_cli(pr, model)
                else:
                    classification = classify_one_api(client, pr, model)
            except Exception as e:
                print(f"  [{i + 1}/{len(prs)}] PR #{pr['number']}: FAILED ({e})", file=sys.stderr)
                continue

            record = {**pr, "intent": classification}
            out.write(json.dumps(record) + "\n")
            out.flush()
            print(f"  [{i + 1}/{len(prs)}] PR #{pr['number']}: {classification['category']}")
    print(f"stage_classify: classified {len(prs)} PRs -> {out_path}")


# ---------------------------------------------------------------------------
# Stage: select
# ---------------------------------------------------------------------------

def stage_select(classified_path, out_path):
    prs = [json.loads(l) for l in classified_path.open(encoding="utf-8")]

    by_intent = defaultdict(list)
    for pr in prs:
        cat = pr["intent"]["category"]
        label = pr["intent"].get("other_label") if cat == "other" else cat
        by_intent[label or "other"].append(pr)

    selection = {}
    for intent, group in by_intent.items():
        # representative = highest-confidence classification for that intent
        best = max(group, key=lambda p: p["intent"].get("confidence", 0))
        selection[intent] = {
            "number": best["number"],
            "title": best["title"],
            "html_url": best["html_url"],
            "confidence": best["intent"]["confidence"],
            "rationale": best["intent"]["rationale"],
            "candidates_in_group": len(group),
        }

    out_path.write_text(json.dumps(selection, indent=2), encoding="utf-8")
    print(f"stage_select: {len(selection)} distinct intents -> {out_path}")
    for intent, pr in sorted(selection.items()):
        print(f"  {intent:<24} #{pr['number']:<6} {pr['title'][:70]}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["list", "candidates", "enrich", "sample", "classify", "select", "all"])
    ap.add_argument("--owner", default="nltk")
    ap.add_argument("--repo", default="nltk")
    ap.add_argument("--pool-size", type=int, default=300, help="candidate pool size before enrichment")
    ap.add_argument("--target", type=int, default=100, help="final diverse sample size")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--engine", choices=["cli", "api"], default="cli",
                     help="cli = shell out to the claude CLI (Pro/Max subscription auth, no API billing); "
                          "api = call the Anthropic API directly (needs ANTHROPIC_API_KEY with credit)")
    ap.add_argument("--model", default=None, help="defaults to 'haiku' for --engine cli, "
                                                    "'claude-haiku-4-5-20251001' for --engine api")
    args = ap.parse_args()
    if args.model is None:
        args.model = "haiku" if args.engine == "cli" else "claude-haiku-4-5-20251001"

    DATA_DIR.mkdir(exist_ok=True)
    list_path = DATA_DIR / "prs_list.jsonl"
    candidates_path = DATA_DIR / "prs_candidates.json"
    enriched_path = DATA_DIR / "prs_enriched.jsonl"
    sample_path = DATA_DIR / "prs_sample.jsonl"
    classified_path = DATA_DIR / "prs_classified.jsonl"
    selection_path = DATA_DIR / "taxonomy_selection.json"

    token = os.environ.get("GITHUB_TOKEN")
    session = gh_session(token) if token else None

    stages_needing_github = {"list", "enrich"}
    if args.stage in stages_needing_github or args.stage == "all":
        if not token:
            print("ERROR: GITHUB_TOKEN is not set", file=sys.stderr)
            sys.exit(1)

    if args.stage in ("list", "all"):
        stage_list(session, args.owner, args.repo, list_path)
    if args.stage in ("candidates", "all"):
        stage_candidates(list_path, candidates_path, args.pool_size, args.seed)
    if args.stage in ("enrich", "all"):
        stage_enrich(session, args.owner, args.repo, candidates_path, enriched_path)
    if args.stage in ("sample", "all"):
        stage_sample(enriched_path, sample_path, args.target, args.seed)
    if args.stage in ("classify", "all"):
        stage_classify(sample_path, classified_path, args.model, args.engine)
    if args.stage in ("select", "all"):
        stage_select(classified_path, selection_path)


if __name__ == "__main__":
    main()
