"""Offline report from collected evidence: CSV/JSON tables and a static HTML page.

Timing rules:
- job execution time = completed_at - started_at (queue excluded), minus "Replay telemetry /" steps
- timings come from the first attempt; jobs that needed a rerun are counted separately
- cache machinery = cache steps (including their post steps) + the aggregator job's net time
- missing data stays missing; nothing is reported as zero
"""

import csv
import html
import json
import statistics
from pathlib import Path

from .decide import event_key, parse_ts

MEASUREMENT_PREFIX = "Replay telemetry /"


def seconds(a: str | None, b: str | None) -> float | None:
    ta, tb = parse_ts(a), parse_ts(b)
    if ta is None or tb is None:
        return None
    return (tb - ta).total_seconds()


def step_seconds(step: dict) -> float:
    return seconds(step.get("started_at"), step.get("completed_at")) or 0.0


def is_cache_step(name: str, cache_steps: list[str]) -> bool:
    base = name[len("Post "):] if name.startswith("Post ") else name
    return base in cache_steps


def job_metrics(job: dict, variant: str, telemetry: dict, settings: dict, aggregator: str) -> dict:
    cache_steps = settings.get("cache_steps", {}).get(variant, [])
    exec_s = seconds(job.get("started_at"), job.get("completed_at"))
    measurement = sum(step_seconds(s) for s in job["steps"] if (s.get("name") or "").startswith(MEASUREMENT_PREFIX))
    cache = sum(step_seconds(s) for s in job["steps"] if is_cache_step(s.get("name") or "", cache_steps))
    net = exec_s - measurement if exec_s is not None else None
    m = {
        "job": job.get("job") or job["name"],
        "conclusion": job["conclusion"],
        "queue_s": seconds(job.get("created_at"), job.get("started_at")),
        "exec_s": exec_s,
        "measurement_s": measurement,
        "net_s": net,
        "cache_s": net if job.get("job") == aggregator else cache,
        "http_429": (job.get("log") or {}).get("http_429"),
        "scan_urls": (job.get("log") or {}).get("scan_urls", []),
        "failure_class": job.get("failure_class"),
    }
    t = telemetry.get(m["job"]) or {}
    if "invocations" in t:
        outcomes = {}
        for inv in t["invocations"]:
            for k, n in inv["outcomes"].items():
                outcomes[k] = outcomes.get(k, 0) + n
        m["tasks"] = outcomes
        deps = t.get("deps", {})
        if "begin" in deps and "end" in deps:
            m["dep_files_added"] = deps["end"]["files"] - deps["begin"]["files"]
            m["dep_bytes_added"] = deps["end"]["bytes"] - deps["begin"]["bytes"]
        outputs = (t.get("job") or {}).get("cacheOutputs") or {}
        if "dependency-gc-due" in outputs:
            m["gc_due"] = outputs["dependency-gc-due"]
    elif t.get("missing"):
        m["telemetry_missing"] = t["missing"]
    else:
        m["telemetry_missing"] = "no artifact"
    return m


def critical_path(jobs: dict[str, float], needs: dict[str, list[str]]) -> float | None:
    memo: dict[str, float] = {}

    def longest(j: str) -> float:
        if j not in memo:
            memo[j] = jobs[j] + max((longest(n) for n in needs.get(j, []) if n in jobs), default=0.0)
        return memo[j]

    return max((longest(j) for j in jobs), default=None)


def load_runs(data_dir: Path) -> list[dict]:
    root = data_dir / "runs"
    return [json.loads(f.read_text()) for f in sorted(root.glob("*/*.json"))] if root.exists() else []


def build_rows(plan: dict, data_dir: Path) -> tuple[list[dict], list[dict]]:
    settings = plan["settings"]
    aggregator = plan["jobs"].get("aggregator")
    needs = {jid: j["needs"] for jid, j in plan["workflow"]["jobs"].items()}
    warmup = settings.get("warmup_events", 3)
    by_attempt = {}
    for rec in load_runs(data_dir):
        by_attempt.setdefault((rec["variant"], rec["event"]), {})[rec["attempt"]] = rec

    job_rows, run_rows = [], []
    scored_main_seen = 0
    for e in plan["events"]:
        if e["type"] == "gate":
            continue
        key = event_key(e)
        flags = []
        if e["campaign"] == "smoke":
            flags.append("smoke")
        elif e["type"] in ("seed", "main"):
            if scored_main_seen < warmup:
                flags.append("warm-up")
            scored_main_seen += 1
        for v in plan["variants"]:
            attempts = by_attempt.get((v, key), {})
            if not attempts:
                run_rows.append({"event": key, "event_type": e["type"], "variant": v, "missing": "not collected", "flags": flags})
                continue
            first = attempts[min(attempts)]
            final = attempts[max(attempts)]
            metrics = [job_metrics(j, v, first.get("telemetry", {}), settings, aggregator) for j in first["jobs"] if j["conclusion"] != "skipped"]
            rerun_jobs = sorted({m["job"] for m in metrics if m["conclusion"] != "success"})
            for m in metrics:
                job_rows.append({"event": key, "event_type": e["type"], "variant": v, "run_id": first["run_id"], "attempt": first["attempt"], **m})
            ok = {m["job"]: m["net_s"] for m in metrics if m["conclusion"] == "success" and m["net_s"] is not None}
            row_flags = list(flags) + (["rerun"] if rerun_jobs else [])
            gc_due = sum(1 for m in metrics if str(m.get("gc_due")) == "true")
            run_rows.append({
                "event": key,
                "event_type": e["type"],
                "variant": v,
                "run_id": first["run_id"],
                "attempts": len(attempts),
                "final_conclusion": final["run"]["conclusion"],
                "total_exec_s": sum(ok.values()) if not rerun_jobs else None,
                "critical_path_s": critical_path(ok, needs) if not rerun_jobs else None,
                "cache_s": sum(m["cache_s"] for m in metrics if m["conclusion"] == "success") if not rerun_jobs else None,
                "measurement_s": sum(m["measurement_s"] for m in metrics),
                "jobs": len(metrics),
                "rerun_jobs": rerun_jobs,
                "failure_classes": sorted({f["class"] for a in attempts.values() for f in a.get("failures", [])}),
                "tasks": _sum_tasks(metrics),
                "dep_files_added": _sum_opt(m.get("dep_files_added") for m in metrics),
                "http_429": _sum_opt(m.get("http_429") for m in metrics),
                "gc_due_jobs": gc_due,
                "scan_urls": [u for m in metrics for u in m["scan_urls"]],
                "flags": row_flags,
            })
    return job_rows, run_rows


def _sum_tasks(metrics: list[dict]) -> dict:
    out = {}
    for m in metrics:
        for k, n in (m.get("tasks") or {}).items():
            out[k] = out.get(k, 0) + n
    return out


def _sum_opt(values) -> int | None:
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def summarize(plan: dict, run_rows: list[dict]) -> dict:
    variants = list(plan["variants"])
    groups = {}
    for group in ("main", "pr"):
        types = ("seed", "main") if group == "main" else ("pr",)
        for metric in ("total_exec_s", "critical_path_s", "cache_s"):
            for v in variants:
                vals = [r[metric] for r in run_rows if r["variant"] == v and r["event_type"] in types and not r["flags"] and r.get(metric) is not None]
                groups.setdefault(group, {}).setdefault(metric, {})[v] = {"n": len(vals), "median": statistics.median(vals) if vals else None}
    return {"groups": groups, "variants": variants}


def write_report(plan: dict, data_dir: Path, out_dir: Path) -> dict:
    job_rows, run_rows = build_rows(plan, data_dir)
    summary = summarize(plan, run_rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "jobs.csv", job_rows)
    _write_csv(out_dir / "runs.csv", run_rows)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out_dir / "index.html").write_text(render_html(plan, run_rows, summary, data_dir))
    return summary


def _write_csv(path: Path, rows: list[dict]) -> None:
    keys = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in r.items()})


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v / 60:.1f} min" if v >= 60 else f"{v:.0f} s"
    return html.escape(str(v))


def render_html(plan: dict, run_rows: list[dict], summary: dict, data_dir: Path) -> str:
    variants = summary["variants"]
    rep = set(plan["settings"].get("representative_events", []))
    first_sweep = next((r["event"] for r in run_rows if r.get("gc_due_jobs") and "smoke" not in r.get("flags", [])), None)
    if first_sweep:
        rep.add(first_sweep)
    rows = []
    by_event = {}
    for r in run_rows:
        by_event.setdefault(r["event"], {})[r["variant"]] = r
    for key, vr in by_event.items():
        cells = [f"<td>{html.escape(key)}</td>"]
        flags = sorted({f for r in vr.values() for f in r.get("flags", [])})
        for metric in ("total_exec_s", "critical_path_s", "cache_s"):
            vals = [vr.get(v, {}).get(metric) for v in variants]
            cells += [f"<td class=n>{_fmt(x)}</td>" for x in vals]
            delta = vals[1] - vals[0] if None not in vals else None
            cells.append(f"<td class=n>{'—' if delta is None else ('+' if delta >= 0 else '−') + _fmt(abs(delta))}</td>")
        for v in variants:
            t = vr.get(v, {}).get("tasks") or {}
            cells.append(f"<td class=n>{t.get('from-cache', '—')}/{t.get('executed', '—')}</td>")
        cells.append(f"<td>{html.escape(', '.join(flags))}</td>")
        scans = ""
        if key in rep:
            scans = " ".join(f"<a href='{html.escape(u)}'>{v[0]}</a>" for v in variants for u in vr.get(v, {}).get("scan_urls", []))
        cells.append(f"<td>{scans}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    med = []
    for group, metrics in summary["groups"].items():
        for metric, per in metrics.items():
            med.append(f"<tr><td>{group}</td><td>{metric}</td>" + "".join(f"<td class=n>{_fmt(per[v]['median'])} (n={per[v]['n']})</td>" for v in variants) + "</tr>")
    usage = []
    for v in variants:
        p = data_dir / "usage" / f"{v}.jsonl"
        samples = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
        peak = max((s["cache_bytes"] for s in samples), default=None)
        usage.append(f"<tr><td>{v}</td><td class=n>{'—' if peak is None else f'{peak / 1e9:.2f} GB'}</td><td class=n>{len(samples)}</td></tr>")
    head = "".join(f"<th>{m} {v}</th>" for m in ("exec", "crit", "cache") for v in [*variants, "Δ"])
    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>Replay report</title>
<style>
:root {{ --bg:#fff; --fg:#1b1b1b; --muted:#666; --line:#ddd; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161616; --fg:#e8e8e8; --muted:#999; --line:#333; }} }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.4 system-ui, sans-serif; margin:16px; }}
table {{ border-collapse:collapse; margin:12px 0; }} td, th {{ border-bottom:1px solid var(--line); padding:3px 8px; text-align:left; }}
td.n {{ text-align:right; font-variant-numeric:tabular-nums; }} .muted {{ color:var(--muted); }} .wrap {{ overflow-x:auto; }}
</style></head><body>
<h1>Replay report: {html.escape(plan['profile'])}</h1>
<p class=muted>Variants: {', '.join(variants)} (Δ = {variants[1]} − {variants[0]}). Execution time excludes queue time and replay telemetry steps.
Rows with flags are excluded from medians. Tasks column: from-cache/executed. Missing values are shown as —.</p>
<h2>Medians</h2><table><tr><th>group</th><th>metric</th>{''.join(f'<th>{v}</th>' for v in variants)}</tr>{''.join(med)}</table>
<h2>Events</h2><div class=wrap><table><tr><th>event</th>{head}{''.join(f'<th>tasks {v}</th>' for v in variants)}<th>flags</th><th>scans</th></tr>
{''.join(rows)}</table></div>
<h2>Cache storage</h2><table><tr><th>variant</th><th>peak cache size</th><th>samples</th></tr>{''.join(usage)}</table>
</body></html>
"""
