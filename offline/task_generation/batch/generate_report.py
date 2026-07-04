"""
Generate sample_output_reports/batch-progress-snapshot.html from
report_data.json (produced by extract_report_data.py) + PR metadata.
"""
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent.parent.parent
OUT_PATH = REPO_ROOT / "sample_output_reports" / "batch-progress-snapshot.html"

PR_TITLES = {
    3672: ("fix(security): Denial of service via quadratic complexity in NLTK segmentation metrics", "security_fix"),
    3644: ("fix(security): block IPv6 transition-embedded internal IPv4 in SSRF filter (NAT64 bypass, CWE-918)", "security_fix"),
    3641: ("fix(security): stop quadratic ReDoS in CCG lexicon parsing (CWE-1333)", "security_fix"),
    3544: ("fix(security): block XML entity expansion (XXE) in downloader", "security_fix"),
    3487: ("Warn on unpickling user-provided pickles", "security_fix"),
    3474: ("Support some zipped models", "bug_fix"),
    3460: ("Avoid segfaults in LazyCorpusLoader._unload()", "bug_fix"),
    3411: ("Update download checksums to use SHA256 in built index", "security_fix"),
    3371: ("Add support for mixed rules conversion into Chomsky Normal Form", "feature_activation"),
}

MODEL_ORDER = ["laguna", "gemini-3.5-flash", "kimi-k2.7-code", "gpt-oss-120b", "claude-opus-4.8-high"]
MODEL_LABEL = {
    "laguna": "laguna",
    "gemini-3.5-flash": "gemini-3.5-flash",
    "kimi-k2.7-code": "kimi-k2.7-code",
    "gpt-oss-120b": "gpt-oss-120b",
    "claude-opus-4.8-high": "claude-opus-4.8 (reasoning=high)",
}


def fmt_num(n):
    if n is None:
        return "—"
    return f"{n:,}"


def fmt_time(s):
    if s is None:
        return "—"
    if s < 60:
        return f"{s:.0f}s"
    m = int(s // 60)
    sec = int(s % 60)
    return f"{m}m {sec:02d}s"


def fmt_compact(n):
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


def fmt_pct(x):
    if x is None:
        return "—"
    return f"{x * 100:.0f}%"


# Series colors: fixed slot per model, consistent across every small-multiple
# chart in the "averages" section (dataviz skill: categorical, fixed order).
SERIES_CLASS = {m: f"s{i + 1}" for i, m in enumerate(MODEL_ORDER)}


def compute_model_averages(rows):
    agg = {m: {"steps": [], "in_tok": [], "out_tok": [], "time": [], "ftp_rate": [], "ptp_rate": []} for m in MODEL_ORDER}
    for r in rows:
        m = r["model_key"]
        if r["steps"] is not None:
            agg[m]["steps"].append(r["steps"])
        if r["input_tokens"] is not None:
            agg[m]["in_tok"].append(r["input_tokens"])
        if r["output_tokens"] is not None:
            agg[m]["out_tok"].append(r["output_tokens"])
        if r["elapsed_seconds"] is not None:
            agg[m]["time"].append(r["elapsed_seconds"])
        if r["fail_to_pass_total"]:
            agg[m]["ftp_rate"].append(r["fail_to_pass_passed"] / r["fail_to_pass_total"])
        if r["pass_to_pass_total"]:
            agg[m]["ptp_rate"].append(r["pass_to_pass_passed"] / r["pass_to_pass_total"])

    def avg(lst):
        return sum(lst) / len(lst) if lst else None

    return {m: {k: avg(v) for k, v in a.items()} for m, a in agg.items()}


def render_metric_chart(title, note, values, formatter):
    """One small-multiple bar chart: one bar per model, same fixed series color per model."""
    numeric = [v for v in values.values() if v is not None]
    peak = max(numeric) if numeric else 1
    rows_html = []
    for m in MODEL_ORDER:
        v = values[m]
        pct = (v / peak * 100) if (v is not None and peak) else 0
        rows_html.append(f'''
      <div class="metric-row">
        <div class="metric-label">{MODEL_LABEL[m]}</div>
        <div class="metric-track"><div class="metric-fill {SERIES_CLASS[m]}" style="width:{pct:.1f}%"></div></div>
        <div class="metric-value">{formatter(v)}</div>
      </div>''')
    return f'''
    <div class="metric-chart">
      <h3>{title}</h3>
      {"".join(rows_html)}
      <p class="metric-note">{note}</p>
    </div>'''


def main():
    rows = json.loads((HERE / "report_data.json").read_text(encoding="utf-8"))
    by_pr = defaultdict(dict)
    for r in rows:
        by_pr[r["pr_number"]][r["model_key"]] = r

    pr_numbers = sorted(by_pr.keys(), key=lambda n: -n)  # most recent first, matches earlier reports

    # histogram data
    resolved_counts = {m: 0 for m in MODEL_ORDER}
    total_counts = {m: 0 for m in MODEL_ORDER}
    for r in rows:
        total_counts[r["model_key"]] += 1
        if r["resolved"]:
            resolved_counts[r["model_key"]] += 1

    bar_rows = []
    for m in MODEL_ORDER:
        resolved = resolved_counts[m]
        total = total_counts[m]
        not_resolved = total - resolved
        if resolved:
            bar_rows.append(f'''
      <div class="bar-row">
        <div class="bar-label">{MODEL_LABEL[m]}</div>
        <div class="bar-track">
          <div class="bar-seg pass" style="flex:{resolved}"><span>{resolved} resolved</span></div>
          {f'<div class="bar-seg fail" style="flex:{not_resolved}"><span>{not_resolved}</span></div>' if not_resolved else ''}
        </div>
        <div class="bar-total">{resolved} / {total}</div>
      </div>''')
        else:
            bar_rows.append(f'''
      <div class="bar-row">
        <div class="bar-label">{MODEL_LABEL[m]}</div>
        <div class="bar-track">
          <div class="bar-seg fail" style="flex:{total}"><span>0 resolved / {total} attempted</span></div>
        </div>
        <div class="bar-total">0 / {total}</div>
      </div>''')
    histogram_html = "".join(bar_rows)

    # per-PR tables
    pr_sections = []
    for n in pr_numbers:
        title, category = PR_TITLES.get(n, (f"PR #{n}", "?"))
        model_rows = by_pr[n]
        trs = []
        for m in MODEL_ORDER:
            r = model_rows.get(m)
            if r is None:
                trs.append(f'<tr><td class="model">{MODEL_LABEL[m]}</td><td colspan="6"><span class="note">not run</span></td></tr>')
                continue
            ftp = f'{r["fail_to_pass_passed"]} / {r["fail_to_pass_total"]}'
            ptp = f'{r["pass_to_pass_passed"]} / {r["pass_to_pass_total"]}' if r["pass_to_pass_total"] else "— (0)"
            chip = '<span class="status-chip pass">Yes</span>' if r["resolved"] else '<span class="status-chip fail">No</span>'
            note = "" if r["exit_status"] == "Submitted" else f' <span class="note">({r["exit_status"]})</span>'
            trs.append(
                f'<tr><td class="model">{MODEL_LABEL[m]}{note}</td>'
                f'<td class="num">{ftp}</td><td class="num">{ptp}</td>'
                f'<td class="num">{fmt_time(r["elapsed_seconds"])}</td>'
                f'<td class="num">{fmt_num(r["input_tokens"])}</td>'
                f'<td class="num">{fmt_num(r["output_tokens"])}</td>'
                f'<td class="num">{fmt_num(r["steps"])}</td>'
                f'<td>{chip}</td></tr>'
            )
        pr_sections.append(f'''
    <div class="pr-group">
      <div class="pr-head">
        <span class="title">#{n} — {title}</span>
        <span class="num">{category}</span>
      </div>
      <table>
        <tr><th>Model</th><th>FAIL_TO_PASS</th><th>PASS_TO_PASS</th><th>Time</th><th>Input tok</th><th>Output tok</th><th>Steps</th><th>Resolved</th></tr>
        {"".join(trs)}
      </table>
    </div>''')
    pr_sections_html = "".join(pr_sections)

    total_combos = len(rows)
    total_prs = len(pr_numbers)

    # per-model averages across all tasks, one small-multiple bar chart per metric
    avgs = compute_model_averages(rows)
    metrics_html = "".join([
        render_metric_chart(
            "Steps (avg)", "Mean tool-call/action count per run, across all 9 PRs.",
            {m: avgs[m]["steps"] for m in MODEL_ORDER}, lambda v: fmt_num(round(v)) if v is not None else "—",
        ),
        render_metric_chart(
            "Time to answer (avg)", "Mean wall-clock time per run, including timed-out runs.",
            {m: avgs[m]["time"] for m in MODEL_ORDER}, fmt_time,
        ),
        render_metric_chart(
            "Input tokens (avg)", "Mean cumulative prompt tokens per run (grows with steps and context re-sent each turn).",
            {m: avgs[m]["in_tok"] for m in MODEL_ORDER}, fmt_compact,
        ),
        render_metric_chart(
            "Output tokens (avg)", "Mean cumulative completion tokens per run.",
            {m: avgs[m]["out_tok"] for m in MODEL_ORDER}, fmt_compact,
        ),
        render_metric_chart(
            "FAIL_TO_PASS rate (avg)", "Mean share of each PR's regression tests turned from failing to passing.",
            {m: avgs[m]["ftp_rate"] for m in MODEL_ORDER}, fmt_pct,
        ),
        render_metric_chart(
            "PASS_TO_PASS rate (avg)", "Mean share of pre-existing tests kept passing (5 of 9 PRs have any).",
            {m: avgs[m]["ptp_rate"] for m in MODEL_ORDER}, fmt_pct,
        ),
    ])
    model_legend_html = "".join(
        f'<span><i class="{SERIES_CLASS[m]}"></i>{MODEL_LABEL[m]}</span>' for m in MODEL_ORDER
    )

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Batch progress: {total_prs} PRs × {len(MODEL_ORDER)} models</title>
<style>
  :root {{
    --bg: #f3f4f1; --surface: #ffffff; --surface-2: #eceee9;
    --ink: #1c2320; --muted: #5b655e; --rule: #daddd4; --accent: #35578c;
    --pass: #2f7a4f; --pass-bg: #ebf5ee; --fail: #b3392f; --fail-bg: #fbebe9;
    --shadow: 0 1px 2px rgba(28,35,32,0.06), 0 8px 24px rgba(28,35,32,0.05);
    --series-1: #2a78d6; --series-2: #1baf7a; --series-3: #eda100;
    --series-4: #008300; --series-5: #4a3aa7;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #14171a; --surface: #1b1f22; --surface-2: #21262a;
      --ink: #e8eae6; --muted: #98a29b; --rule: #2c3236; --accent: #7ea3d6;
      --pass: #379c65; --pass-bg: #163524; --fail: #d9584a; --fail-bg: #3a201d;
      --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 12px 32px rgba(0,0,0,0.35);
      --series-1: #3987e5; --series-2: #199e70; --series-3: #c98500;
      --series-4: #008300; --series-5: #9085e9;
    }}
  }}
  :root[data-theme="dark"] {{
    --bg: #14171a; --surface: #1b1f22; --surface-2: #21262a;
    --ink: #e8eae6; --muted: #98a29b; --rule: #2c3236; --accent: #7ea3d6;
    --pass: #379c65; --pass-bg: #163524; --fail: #d9584a; --fail-bg: #3a201d;
    --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 12px 32px rgba(0,0,0,0.35);
    --series-1: #3987e5; --series-2: #199e70; --series-3: #c98500;
    --series-4: #008300; --series-5: #9085e9;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 16px; line-height: 1.55; -webkit-font-smoothing: antialiased;
  }}
  .page {{ max-width: 1040px; margin: 0 auto; padding: 56px 24px 96px; }}
  .eyebrow {{
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
    font-size: 12.5px; letter-spacing: 0.09em; text-transform: uppercase;
    color: var(--muted); margin: 0 0 10px;
  }}
  h1 {{
    font-family: Constantia, Charter, Georgia, "Times New Roman", serif;
    font-size: 32px; font-weight: 600; line-height: 1.22; letter-spacing: -0.01em;
    margin: 0 0 10px; text-wrap: balance;
  }}
  .dek {{ color: var(--muted); font-size: 16.5px; margin: 0 0 24px; max-width: 72ch; }}
  .meta-row {{
    display: flex; flex-wrap: wrap; gap: 8px 22px; padding: 16px 0 32px;
    border-bottom: 1px solid var(--rule); font-size: 14px; color: var(--muted);
  }}
  .meta-row dt {{ font-weight: 600; color: var(--ink); display: inline; }}
  .meta-row div {{ white-space: nowrap; }}
  .meta-row code {{
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
    font-size: 13px; background: var(--surface-2); padding: 1px 6px; border-radius: 3px;
  }}
  section {{ margin-top: 44px; }}
  h2 {{
    font-family: Constantia, Charter, Georgia, "Times New Roman", serif;
    font-size: 21px; font-weight: 600; margin: 0 0 6px;
  }}
  .h2-note {{ color: var(--muted); font-size: 14.5px; margin: 0 0 20px; max-width: 72ch; }}
  .chart {{
    background: var(--surface); border: 1px solid var(--rule); border-radius: 8px;
    padding: 22px 24px; box-shadow: var(--shadow);
  }}
  .bar-row {{ display: grid; grid-template-columns: 170px 1fr 90px; align-items: center; gap: 14px; margin-bottom: 14px; }}
  .bar-row:last-child {{ margin-bottom: 0; }}
  .bar-label {{
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
    font-size: 13px; color: var(--ink); text-align: right;
  }}
  .bar-track {{ display: flex; height: 22px; border-radius: 4px; overflow: hidden; background: var(--surface-2); }}
  .bar-seg {{ display: flex; align-items: center; justify-content: center; height: 100%; }}
  .bar-seg.pass {{ background: var(--pass); }}
  .bar-seg.fail {{ background: var(--fail); }}
  .bar-seg span {{
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
    font-size: 11px; font-weight: 700; color: #fff;
  }}
  .bar-total {{
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
    font-variant-numeric: tabular-nums; font-size: 13px; color: var(--muted);
  }}
  .legend {{ display: flex; gap: 20px; margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--rule); font-size: 13px; color: var(--muted); }}
  .legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
  .legend i {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
  .legend i.pass {{ background: var(--pass); }}
  .legend i.fail {{ background: var(--fail); }}
  .pr-group {{
    background: var(--surface); border: 1px solid var(--rule); border-radius: 8px;
    margin-bottom: 16px; overflow: hidden; box-shadow: var(--shadow);
  }}
  .pr-head {{
    padding: 14px 20px; border-bottom: 1px solid var(--rule);
    display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  }}
  .pr-head .title {{ font-weight: 600; font-size: 15px; }}
  .pr-head .num {{
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
    color: var(--muted); font-size: 13px;
  }}
  .pr-group table {{ display: block; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 9px 16px; border-bottom: 1px solid var(--rule); white-space: nowrap; }}
  tr:last-child td {{ border-bottom: none; }}
  th {{ font-size: 10.5px; letter-spacing: 0.03em; text-transform: uppercase; color: var(--muted); font-weight: 600; }}
  td.model {{ font-weight: 600; white-space: normal; }}
  td.num {{ font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace; font-variant-numeric: tabular-nums; }}
  .status-chip {{
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
    font-size: 11px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase;
    padding: 3px 9px; border-radius: 100px; white-space: nowrap;
  }}
  .status-chip.pass {{ color: var(--pass); background: var(--pass-bg); }}
  .status-chip.fail {{ color: var(--fail); background: var(--fail-bg); }}
  .note {{ color: var(--muted); font-size: 11.5px; }}
  .model-legend {{ display: flex; flex-wrap: wrap; gap: 8px 20px; margin-bottom: 20px; font-size: 13px; color: var(--muted); }}
  .model-legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
  .model-legend i {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
  .s1 {{ background: var(--series-1); }}
  .s2 {{ background: var(--series-2); }}
  .s3 {{ background: var(--series-3); }}
  .s4 {{ background: var(--series-4); }}
  .s5 {{ background: var(--series-5); }}
  .metrics-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }}
  .metric-chart {{
    background: var(--surface); border: 1px solid var(--rule); border-radius: 8px;
    padding: 18px 20px 14px; box-shadow: var(--shadow);
  }}
  .metric-chart h3 {{ font-size: 13.5px; font-weight: 600; margin: 0 0 14px; }}
  .metric-row {{ display: grid; grid-template-columns: 150px 1fr 66px; align-items: center; gap: 10px; margin-bottom: 9px; }}
  .metric-row:last-child {{ margin-bottom: 0; }}
  .metric-label {{
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
    font-size: 11.5px; color: var(--muted); text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .metric-track {{ height: 15px; border-radius: 4px; background: var(--surface-2); overflow: hidden; }}
  .metric-fill {{ height: 100%; border-radius: 4px; }}
  .metric-value {{
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
    font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink);
  }}
  .metric-note {{ color: var(--muted); font-size: 11.5px; margin: 12px 0 0; padding-top: 10px; border-top: 1px solid var(--rule); }}
  footer {{ margin-top: 56px; padding-top: 20px; border-top: 1px solid var(--rule); font-size: 13px; color: var(--muted); }}
  @media (max-width: 720px) {{ .metrics-grid {{ grid-template-columns: 1fr; }} }}
  @media (max-width: 640px) {{ .bar-row {{ grid-template-columns: 90px 1fr 70px; }} .metric-row {{ grid-template-columns: 100px 1fr 60px; }} h1 {{ font-size: 27px; }} }}
</style>
</head>
<body>
<div class="page">

  <p class="eyebrow">Batch evaluation · complete</p>
  <h1>{total_prs} PRs × {len(MODEL_ORDER)} models — full results</h1>
  <p class="dek">mini-swe-agent attempting real, previously-unseen NLTK bug fixes, each graded against its PR's actual FAIL_TO_PASS/PASS_TO_PASS test suite in an isolated (single-commit, network-blocked) agent environment. Time, token usage, and step count pulled from each run's actual trajectory, not estimated.</p>

  <dl class="meta-row">
    <div><dt>Repo</dt> <code>nltk/nltk</code></div>
    <div><dt>PRs</dt> {total_prs} (1 of the original 10 excluded — no discriminating test could be derived)</div>
    <div><dt>Combos</dt> {total_combos} / {total_prs * len(MODEL_ORDER)}</div>
  </dl>

  <section>
    <h2>Resolved rate per model</h2>
    <p class="h2-note">Stacked by outcome, out of all {total_prs} PRs each model attempted.</p>
    <div class="chart">
      {histogram_html}
      <div class="legend">
        <span><i class="pass"></i> Resolved</span>
        <span><i class="fail"></i> Not resolved</span>
      </div>
    </div>
  </section>

  <section>
    <h2>Averages per model, across all {total_prs} PRs</h2>
    <p class="h2-note">Same model, same color, in every panel below — so a bar's identity carries across metrics. FAIL_TO_PASS/PASS_TO_PASS are averaged as a per-task rate (passed/total), not raw counts, since PRs carry very different test-suite sizes.</p>
    <div class="model-legend">{model_legend_html}</div>
    <div class="metrics-grid">
      {metrics_html}
    </div>
  </section>

  <section>
    <h2>Per-PR, per-model breakdown</h2>
    <p class="h2-note">FAIL_TO_PASS = the PR's real regression tests (must go from failing to passing). PASS_TO_PASS = unrelated pre-existing tests that must not regress. Time/tokens/steps are pulled from each run's actual trajectory.</p>
    {pr_sections_html}
  </section>

  <section>
    <h2>What the numbers show</h2>
    <p class="h2-note" style="max-width:74ch">
      <code>claude-opus-4.8</code> (reasoning=high) and <code>kimi-k2.7-code</code> lead at 4/9 resolved; <code>laguna</code> and <code>gemini-3.5-flash</code> follow at 3/9; <code>gpt-oss-120b</code> failed to submit a usable patch on any of the 9 (always <code>RepeatedFormatError</code>, a protocol incompatibility, not model weakness). Token and step counts vary enormously by PR difficulty, not just model — see #3371, where a timed-out run burned over 4M cumulative input tokens and 138 steps looping without ever submitting, versus a 15-step, ~95K-token clean resolve on the same PR from another model.
    </p>
  </section>

  <footer>
    Graded via nltk-eval:pr-3564 image · agent environment: shallow single-commit, network-isolated per PR · reasoning effort verified via extra_body passthrough for claude-opus-4.8
  </footer>

</div>
</body>
</html>
'''
    OUT_PATH.write_text(html, encoding="utf-8", newline="\n")
    print(f"wrote {OUT_PATH} ({len(html)} chars, {total_combos} combos, {total_prs} PRs)")


if __name__ == "__main__":
    main()
