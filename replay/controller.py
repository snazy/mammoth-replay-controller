"""One controller tick: snapshot, collect, decide, execute, write status."""

import json
from datetime import datetime
from pathlib import Path

from . import collect as collect_mod
from .decide import Decision, Env, decide, iso, take_snapshot
from .github import GitHubError

MAX_ACTIONS_PER_TICK = 6


def log_action(data_dir: Path, now: datetime, action: dict, phase: str, **extra) -> None:
    rec = {"at": iso(now), "phase": phase, **{k: v for k, v in action.items() if k not in ("body",)}, **extra}
    collect_mod.append_jsonl(data_dir / "actions.jsonl", rec)


def execute(plan: dict, api, actions: list[dict], data_dir: Path, now: datetime) -> list[str]:
    """Executes actions in order; stops at the first failure. Returns notes."""
    notes = []
    for action in actions[:MAX_ACTIONS_PER_TICK]:
        repo = action["repo"]
        t = action["type"]
        if t == "update_ref":
            current = api.get_ref(repo, action["ref"])
            if current != action["expected"]:
                notes.append(f"skipped {t} {action['variant']}: {action['ref']} moved to {str(current)[:10]}; reconciling next tick")
                break
        log_action(data_dir, now, action, "intent")
        try:
            result = None
            if t == "create_ref":
                api.create_ref(repo, action["ref"], action["sha"])
            elif t == "update_ref":
                api.update_ref(repo, action["ref"], action["sha"])
            elif t == "create_pr":
                result = api.create_pr(repo, action["head"], action["base"], action["title"], action["body"])
            elif t == "close_pr":
                api.close_pr(repo, action["number"])
            elif t == "rerun_failed":
                api.rerun_failed_jobs(repo, action["run_id"])
            elif t == "rerun_job":
                job_name = plan["workflow"]["jobs"][action["job"]]["name"]
                jobs = [j for j in api.list_attempt_jobs(repo, action["run_id"], action["attempt"]) if j["name"] == job_name]
                if len(jobs) != 1:
                    raise GitHubError(f"expected one job named {job_name!r} in run {action['run_id']}, found {len(jobs)}")
                api.rerun_job(repo, jobs[0]["id"])
                result = jobs[0]["id"]
            elif t == "delete_caches_and_artifacts":
                caches = api.list_caches(repo)
                for c in caches:
                    api.delete_cache(repo, c["id"])
                artifacts = api.list_artifacts(repo)
                for a in artifacts:
                    api.delete_artifact(repo, a["id"])
                result = {"caches": len(caches), "artifacts": len(artifacts)}
            else:
                raise ValueError(f"unknown action {t}")
        except GitHubError as e:
            log_action(data_dir, now, action, "error", error=str(e)[:500])
            notes.append(f"{t} {action['variant']} {action['event']} failed: {str(e)[:300]}")
            break
        log_action(data_dir, now, action, "done", result=result)
        notes.append(f"{t} {action['variant']} {action['event']} done")
    return notes


def tick(plan: dict, api, data_dir: Path, env: Env) -> tuple[Decision, list[str]]:
    data_dir.mkdir(parents=True, exist_ok=True)
    snap = take_snapshot(plan, api)
    notes = collect_mod.collect(plan, api, snap, data_dir, env.now)
    collect_mod.record_usage(plan, snap, data_dir, env.now)
    evidence = collect_mod.load_evidence(data_dir)
    log = collect_mod.read_jsonl(data_dir / "actions.jsonl")
    d = decide(plan, snap, evidence, log, env)
    if d.exceptions:
        notes.append("exceptions present; no mutations")
    elif env.paused:
        notes.append(f"paused; {len(d.actions)} action(s) not executed")
    elif d.actions:
        notes.extend(execute(plan, api, d.actions, data_dir, env.now))
    write_status(plan, d, env, notes, data_dir)
    return d, notes


def write_status(plan: dict, d: Decision, env: Env, notes: list[str], data_dir: Path) -> None:
    done = sum(1 for flags in d.progress.values() if all(flags.values()))
    status = {
        "at": iso(env.now),
        "profile": plan["profile"],
        "paused": env.paused,
        "finished": d.finished,
        "current": d.current,
        "not_before": iso(d.not_before) if d.not_before else None,
        "applied_events": done,
        "total_events": len(d.progress),
        "exceptions": d.exceptions,
        "waiting": d.waiting,
        "planned_actions": [{k: v for k, v in a.items() if k != "body"} for a in d.actions],
        "notes": notes,
    }
    collect_mod.write_atomic(data_dir / "status.json", status)
    lines = [f"# Replay status ({plan['profile']})", "", f"As of {status['at']}."]
    if d.finished:
        lines.append("All events completed.")
    if env.paused:
        lines.append("**Paused** (REPLAY_PAUSED is set); evidence collection continues.")
    lines.append(f"Applied in all variants: {done} of {len(d.progress)} CI events. Current: `{d.current}`.")
    if d.not_before:
        lines.append(f"Next mutation not before {status['not_before']}.")
    for title, items in (("Exceptions (mutations stopped)", d.exceptions), ("Waiting", d.waiting), ("This tick", notes)):
        if items:
            lines += ["", f"## {title}", ""] + [f"- {i}" for i in items]
    (data_dir / "status.md").write_text("\n".join(lines) + "\n")


def load_plan(path: Path) -> dict:
    return json.loads(path.read_text())
