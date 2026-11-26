"""Collects immutable evidence for completed run attempts into the data directory.

Layout (all JSON, one file per run attempt, never rewritten):
  runs/<variant>/<run_id>-a<attempt>.json
  usage/<variant>.jsonl     cache/artifact usage samples
  actions.jsonl             mutation intents and results
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from . import classify
from .decide import _Ctx, Env, event_key, iso, parse_ts, runs_for

USAGE_SAMPLE_INTERVAL = timedelta(minutes=55)


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def run_path(data_dir: Path, variant: str, run_id: int, attempt: int) -> Path:
    return data_dir / "runs" / variant / f"{run_id}-a{attempt}.json"


def load_evidence(data_dir: Path) -> dict:
    out = {}
    root = data_dir / "runs"
    if not root.exists():
        return out
    for f in root.glob("*/*.json"):
        rec = json.loads(f.read_text())
        out[(rec["variant"], rec["run_id"], rec["attempt"])] = {"failures": rec.get("failures", []), "event": rec.get("event")}
    return out


def job_ids_by_name(plan: dict) -> dict[str, str]:
    return {j["name"]: jid for jid, j in plan["workflow"]["jobs"].items()}


def parse_telemetry(files: dict[str, bytes]) -> dict:
    out = {"job": None, "deps": {}, "invocations": []}
    for name, content in sorted(files.items()):
        text = content.decode(errors="replace")
        base = name.rsplit("/", 1)[-1]
        try:
            if base == "job.json":
                out["job"] = json.loads(text)
            elif base.startswith("deps-") and base.endswith(".json"):
                rec = json.loads(text)
                out["deps"][rec["phase"]] = rec
            elif base.startswith("tasks-") and base.endswith(".jsonl"):
                lines = [json.loads(line) for line in text.splitlines() if line.strip()]
                if lines:
                    header, tasks = lines[0], lines[1:]
                    counts = {}
                    for t in tasks:
                        counts[t["outcome"]] = counts.get(t["outcome"], 0) + 1
                    out["invocations"].append({**header, "outcomes": counts, "task_count": len(tasks), "tasks": tasks})
        except (ValueError, KeyError) as e:
            out.setdefault("errors", []).append(f"{base}: {e}")
    out["invocations"].sort(key=lambda i: i.get("startedAt", 0))
    return out


def collect_attempt(plan: dict, api, variant: str, e: dict, run: dict, attempt: int, now: datetime) -> dict:
    repo = plan["variants"][variant]["repo"]
    patterns = plan["settings"].get("failure_patterns", {})
    names = job_ids_by_name(plan)
    attempt_run = api.get_run_attempt(repo, run["id"], attempt)
    jobs = api.list_attempt_jobs(repo, run["id"], attempt)
    artifacts = [a for a in api.list_run_artifacts(repo, run["id"]) if a["name"].startswith("replay-telemetry-") and a["name"].endswith(f"-{attempt}")]
    telemetry = {}
    for a in artifacts:
        job_id = a["name"][len("replay-telemetry-") : -len(f"-{attempt}")]
        if a.get("expired"):
            telemetry[job_id] = {"missing": "expired"}
            continue
        try:
            telemetry[job_id] = parse_telemetry(api.download_artifact_files(repo, a["id"]))
        except Exception as ex:  # noqa: BLE001 - recorded as missing evidence
            telemetry[job_id] = {"missing": f"download failed: {ex}"}
    job_records = []
    failures = []
    for j in jobs:
        rec = {
            "api_id": j["id"],
            "job": names.get(j["name"]),
            "name": j["name"],
            "status": j["status"],
            "conclusion": j["conclusion"],
            "created_at": j.get("created_at"),
            "started_at": j.get("started_at"),
            "completed_at": j.get("completed_at"),
            "runner_name": j.get("runner_name"),
            "labels": j.get("labels", []),
            "steps": [{k: s.get(k) for k in ("number", "name", "status", "conclusion", "started_at", "completed_at")} for s in j.get("steps") or []],
        }
        if j["conclusion"] not in (None, "skipped"):
            try:
                rec["log"] = classify.extract(api.job_log(repo, j["id"]))
            except Exception as ex:  # noqa: BLE001
                rec["log"] = {"missing": f"log download failed: {ex}"}
        if j["conclusion"] in ("failure", "cancelled", "timed_out"):
            cls = classify.classify(j["conclusion"], rec.get("log", {}), patterns) if "missing" not in rec.get("log", {}) else "unknown"
            rec["failure_class"] = cls
            failures.append({"job": rec["job"] or j["name"], "conclusion": j["conclusion"], "class": cls})
        job_records.append(rec)
    return {
        "schema": 1,
        "collector_version": classify.COLLECTOR_VERSION,
        "collected_at": iso(now),
        "variant": variant,
        "event": event_key(e),
        "event_type": e["type"],
        "upstream": e.get("upstream"),
        "sha": e["sha"][variant],
        "run_id": run["id"],
        "attempt": attempt,
        "run": {k: attempt_run.get(k) for k in ("event", "status", "conclusion", "created_at", "updated_at", "run_started_at", "run_attempt", "html_url", "head_sha", "head_branch")},
        "jobs": job_records,
        "telemetry": telemetry,
        "failures": failures,
    }


def collect(plan: dict, api, snap: dict, data_dir: Path, now: datetime, budget: int = 6) -> list[str]:
    """Collects completed, not yet collected run attempts of plan events (oldest first)."""
    ctx = _Ctx(plan, snap, {}, [], Env(now=now))
    notes = []
    for e in plan["events"]:
        if e["type"] == "gate":
            continue
        for v in plan["variants"]:
            for run in runs_for(ctx, v, e):
                for attempt in range(1, run["run_attempt"] + 1):
                    complete = attempt < run["run_attempt"] or run["status"] == "completed"
                    path = run_path(data_dir, v, run["id"], attempt)
                    if not complete or path.exists():
                        continue
                    if budget <= 0:
                        notes.append("collection budget exhausted; continuing next tick")
                        return notes
                    budget -= 1
                    write_atomic(path, collect_attempt(plan, api, v, e, run, attempt, now))
                    notes.append(f"collected {v} {event_key(e)} run {run['id']} attempt {attempt}")
    return notes


def record_usage(plan: dict, snap: dict, data_dir: Path, now: datetime) -> None:
    for v in plan["variants"]:
        path = data_dir / "usage" / f"{v}.jsonl"
        sample = {"cache_count": snap[v]["cache_count"], "cache_bytes": snap[v]["cache_bytes"], "artifact_count": snap[v]["artifact_count"]}
        prev = read_jsonl(path)
        last = prev[-1] if prev else None
        if last and {k: last[k] for k in sample} == sample and now - parse_ts(last["at"]) < USAGE_SAMPLE_INTERVAL:
            continue
        append_jsonl(path, {"at": iso(now), **sample})
