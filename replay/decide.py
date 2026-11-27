"""Remote snapshot and the pure decision function.

The controller keeps no state between ticks beyond what is on GitHub and in the data directory.
`decide(plan, snapshot, evidence, log, env)` derives the replay position from remote refs, PRs
and workflow runs, and returns the actions to take next, or an exception that pauses mutations.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

ACTIVE_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}
RERUNNABLE = {"failure", "cancelled", "timed_out"}
PR_MARKER = "<!-- replay-event: {key} -->"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_key(e: dict) -> str:
    return f"{e['campaign']}/{e['id']}"


# --- snapshot --------------------------------------------------------------------------------


def take_snapshot(plan: dict, api) -> dict:
    """Reads the remote state the decision needs. Read-only."""
    snap = {}
    for variant, cfg in plan["variants"].items():
        repo = cfg["repo"]
        heads = api.matching_refs(repo, "heads")
        tags = api.matching_refs(repo, "tags/replay-staging")
        prs = [p for p in api.list_prs(repo) if p["same_repo"] and p["head_ref"].startswith("replay/")]
        usage = api.cache_usage(repo)
        snap[variant] = {
            "main": heads.get("refs/heads/main"),
            "branches": {r[len("refs/heads/"):]: s for r, s in heads.items()},
            "staging": tags,
            "prs": prs,
            "runs": api.list_workflow_runs(repo, plan["workflow"]["file"]),
            "cache_count": usage.get("active_caches_count", 0),
            "cache_bytes": usage.get("active_caches_size_in_bytes", 0),
            "artifact_count": len(api.list_artifacts(repo)),
        }
    return snap


# --- decision --------------------------------------------------------------------------------


@dataclass
class Env:
    now: datetime
    paused: bool = False
    released_gates: frozenset[str] = frozenset()


@dataclass
class Decision:
    actions: list[dict] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    current: str | None = None
    not_before: datetime | None = None
    finished: bool = False
    progress: dict = field(default_factory=dict)


class _Ctx:
    def __init__(self, plan, snap, evidence, log, env):
        self.plan = plan
        self.snap = snap
        self.evidence = evidence
        self.log = log
        self.env = env
        self.settings = plan["settings"]
        self.variants = list(plan["variants"])
        self.events = plan["events"]
        self.chain = {}
        for i, e in enumerate(self.events):
            if e["type"] in ("seed", "main"):
                for v in self.variants:
                    self.chain.setdefault(v, {})[e["sha"][v]] = i


def runs_for(ctx: _Ctx, variant: str, e: dict) -> list[dict]:
    sha = e["sha"][variant]
    want = "pull_request" if e["type"] == "pr" else "push"
    runs = [r for r in ctx.snap[variant]["runs"] if r["head_sha"] == sha and r["event"] == want]
    if e["type"] == "pr":
        runs = [r for r in runs if r.get("head_branch") == e["branch"]]
    else:
        runs = [r for r in runs if r.get("head_branch") == "main"]
    return runs


def pr_for(ctx: _Ctx, variant: str, e: dict) -> dict | None:
    prs = [p for p in ctx.snap[variant]["prs"] if p["head_ref"] == e["branch"]]
    return prs[0] if len(prs) == 1 else (None if not prs else {"duplicate": True})


def applied(ctx: _Ctx, variant: str, idx: int) -> bool:
    e = ctx.events[idx]
    if e["type"] in ("seed", "main"):
        head = ctx.snap[variant]["main"]
        return head is not None and head in ctx.chain[variant] and ctx.chain[variant][head] >= idx
    if e["type"] == "pr":
        return pr_for(ctx, variant, e) is not None
    return False


def last_mutation(ctx: _Ctx, variant: str, key: str) -> datetime | None:
    times = [parse_ts(r["at"]) for r in ctx.log if r.get("variant") == variant and r.get("event") == key and r.get("phase") == "intent"]
    return max(times) if times else None


def job_rerun_made(ctx: _Ctx, variant: str, run_id: int) -> bool:
    """Whether the event's planned job rerun was already requested for this run.

    Not derived from the attempt number: a rerun of failed jobs (e.g. after an HTTP 429) also
    raises it. A request that failed with an error doesn't count; one whose response was lost does.
    """
    rows = [r for r in ctx.log if r.get("type") == "rerun_job" and r.get("variant") == variant and r.get("run_id") == run_id]
    intents = sum(1 for r in rows if r.get("phase") == "intent")
    errors = sum(1 for r in rows if r.get("phase") == "error")
    return intents > errors


def ci_status(ctx: _Ctx, variant: str, e: dict, d: Decision) -> tuple[str, datetime | None]:
    """Status of an applied CI event in one variant: running | done | waiting | exception."""
    key = event_key(e)
    repo = ctx.plan["variants"][variant]["repo"]
    now = ctx.env.now
    if e["type"] == "pr":
        pr = pr_for(ctx, variant, e)
        if pr.get("duplicate"):
            d.exceptions.append(f"{variant}: several PRs for branch {e['branch']}")
            return "exception", None
        if pr["head_sha"] != e["sha"][variant]:
            d.exceptions.append(f"{variant}: PR #{pr['number']} head {pr['head_sha'][:10]} differs from plan {e['sha'][variant][:10]}")
            return "exception", None
    runs = runs_for(ctx, variant, e)
    if len(runs) > 1:
        d.exceptions.append(f"{variant}: {len(runs)} runs for {key}: " + ", ".join(str(r["id"]) for r in runs))
        return "exception", None
    if not runs:
        since = last_mutation(ctx, variant, key)
        limit = timedelta(minutes=ctx.settings["run_visibility_timeout_minutes"])
        if since is not None and now - since > limit:
            d.exceptions.append(f"{variant}: no CI run for {key} {int((now - since).total_seconds() // 60)} min after applying it")
            return "exception", None
        d.waiting.append(f"{variant}: waiting for the CI run of {key} to appear")
        return "running", None
    run = runs[0]
    if run["status"] in ACTIVE_STATUSES:
        started = parse_ts(run.get("run_started_at") or run["created_at"])
        if now - started > timedelta(minutes=ctx.settings["run_max_duration_minutes"]):
            d.exceptions.append(f"{variant}: run {run['html_url']} attempt {run['run_attempt']} still {run['status']} after {int((now - started).total_seconds() // 3600)} h")
            return "exception", None
        d.waiting.append(f"{variant}: run {run['id']} attempt {run['run_attempt']} {run['status']}")
        return "running", None
    if run["status"] != "completed":
        d.exceptions.append(f"{variant}: run {run['html_url']} has unexpected status {run['status']}")
        return "exception", None
    conclusion = run["conclusion"]
    attempt = run["run_attempt"]
    if conclusion == "success":
        if e.get("rerun_job") and not job_rerun_made(ctx, variant, run["id"]):
            d.actions.append({"variant": variant, "event": key, "type": "rerun_job", "repo": repo, "run_id": run["id"], "attempt": attempt, "job": e["rerun_job"]})
            return "running", None
        if e["type"] == "pr":
            pr = pr_for(ctx, variant, e)
            if pr["state"] != "closed":
                d.actions.append({"variant": variant, "event": key, "type": "close_pr", "repo": repo, "number": pr["number"]})
                return "running", None
        return "done", parse_ts(run["updated_at"])
    if conclusion in RERUNNABLE:
        ev = ctx.evidence.get((variant, run["id"], attempt))
        if ev is None:
            d.waiting.append(f"{variant}: collecting evidence of failed run {run['id']} attempt {attempt}")
            return "running", None
        unknown = [f["job"] for f in ev.get("failures", []) if f.get("class") == "unknown"]
        if unknown and ctx.settings.get("on_unknown_failure", "pause") == "pause":
            d.exceptions.append(
                f"{variant}: run {run['html_url']} attempt {attempt} has unclassified failures in {', '.join(unknown)}. "
                "Inspect it; rerun the failed jobs manually to continue, or adjust failure patterns."
            )
            return "exception", None
        if attempt <= campaign_setting(ctx.plan, e, "max_reruns"):
            # the next attempt is rerun number `attempt`
            not_before = parse_ts(run["updated_at"]) + rerun_wait(ctx.plan, e, attempt)
            if now < not_before:
                d.waiting.append(f"{variant}: rerun {attempt} of run {run['id']} not before {iso(not_before)}")
                return "running", None
            d.actions.append({"variant": variant, "event": key, "type": "rerun_failed", "repo": repo, "run_id": run["id"], "attempt": attempt})
            return "running", None
        d.exceptions.append(f"{variant}: run {run['html_url']} still {conclusion} after {attempt} attempts (rerun limit reached)")
        return "exception", None
    d.exceptions.append(f"{variant}: run {run['html_url']} attempt {attempt} concluded {conclusion}")
    return "exception", None


def apply_actions(ctx: _Ctx, variant: str, e: dict) -> list[dict]:
    key = event_key(e)
    repo = ctx.plan["variants"][variant]["repo"]
    sha = e["sha"][variant]
    if e["type"] in ("seed", "main"):
        head = ctx.snap[variant]["main"]
        # main normally starts at the plan's root commit; an absent main is still created
        if head is None:
            return [{"variant": variant, "event": key, "type": "create_ref", "repo": repo, "ref": "heads/main", "sha": sha}]
        return [{"variant": variant, "event": key, "type": "update_ref", "repo": repo, "ref": "heads/main", "sha": sha, "expected": head}]
    branch_sha = ctx.snap[variant]["branches"].get(e["branch"])
    actions = []
    if branch_sha is None:
        actions.append({"variant": variant, "event": key, "type": "create_ref", "repo": repo, "ref": f"heads/{e['branch']}", "sha": sha})
    elif branch_sha != sha:
        return [{"variant": variant, "event": key, "type": "exception", "message": f"{variant}: branch {e['branch']} at {branch_sha[:10]}, plan expects {sha[:10]}"}]
    actions.append({
        "variant": variant, "event": key, "type": "create_pr", "repo": repo, "head": e["branch"], "base": "main",
        "title": e.get("title") or f"Replay {key}",
        "body": f"{PR_MARKER.format(key=key)}\nReplay of upstream commit {e['upstream']}. Managed by the replay controller; do not edit.",
    })
    return actions


def campaign_setting(plan: dict, e: dict, name: str, default=None):
    """A controller setting for event e: its campaign's settings override the profile's defaults."""
    campaign = plan.get("campaign_settings", {}).get(e["campaign"], {})
    return campaign.get(name, plan["settings"].get(name, default))


def spacing_minutes(plan: dict, e: dict) -> float:
    """Minimum wait before event e, after the previous event finished."""
    return campaign_setting(plan, e, "min_event_spacing_minutes")


def rerun_wait(plan: dict, e: dict, rerun: int) -> timedelta:
    """Wait after a failed attempt before rerun number `rerun` (1-based).

    The first rerun happens at the next tick; with rerun_backoff_minutes set, each further rerun
    waits twice as long as the previous one, to ride out throttling or a short outage.
    """
    base = campaign_setting(plan, e, "rerun_backoff_minutes", 0)
    return timedelta(minutes=base * 2 ** (rerun - 2)) if base and rerun >= 2 else timedelta(0)


def decide(plan: dict, snap: dict, evidence: dict, log: list[dict], env: Env) -> Decision:
    ctx = _Ctx(plan, snap, evidence, log, env)
    d = Decision()

    for v in ctx.variants:
        expected = plan["staging_refs"][v]
        actual = {r: s for r, s in snap[v]["staging"].items()}
        missing = [r for r, s in expected.items() if actual.get(r) != s]
        if missing:
            d.exceptions.append(f"{v}: staging refs missing or different: {', '.join(missing)}")
        head = snap[v]["main"]
        if head is not None and head not in ctx.chain[v] and head != plan.get("root"):
            d.exceptions.append(f"{v}: main is at {head[:10]}, which is not a plan commit")
    if d.exceptions:
        return d

    ci_idx = [i for i, e in enumerate(ctx.events) if e["type"] != "gate"]
    furthest = None
    for i in ci_idx:
        if any(applied(ctx, v, i) for v in ctx.variants):
            furthest = i
    for i, e in enumerate(ctx.events):
        if e["type"] != "gate":
            d.progress[event_key(e)] = {v: applied(ctx, v, i) for v in ctx.variants}

    last_done_at = None
    if furthest is None:
        current = 0
    else:
        e = ctx.events[furthest]
        flags = [applied(ctx, v, furthest) for v in ctx.variants]
        if not all(flags):
            current = furthest
        else:
            sub = Decision()
            results = [ci_status(ctx, v, e, sub) for v in ctx.variants]
            if all(s == "done" for s, _ in results):
                last_done_at = max(t for _, t in results)
                current = furthest + 1
            else:
                current = furthest
    # Gates that a later applied event already passed are done.
    passed_gate = False
    while current < len(ctx.events) and ctx.events[current]["type"] == "gate":
        g = ctx.events[current]
        status = gate_status(ctx, g, d)
        if status != "done":
            d.current = event_key(g)
            return d
        passed_gate = True
        current += 1
    # The spacing separates events within a campaign; a released gate already separates the
    # campaigns, so the first event after it starts right away.
    if passed_gate:
        last_done_at = None

    if current >= len(ctx.events):
        d.finished = True
        d.current = None
        return d

    e = ctx.events[current]
    key = event_key(e)
    d.current = key
    flags = {v: applied(ctx, v, current) for v in ctx.variants}

    if not any(flags.values()):
        if last_done_at is not None:
            not_before = last_done_at + timedelta(minutes=spacing_minutes(ctx.plan, e))
            d.not_before = not_before
            if env.now < not_before:
                d.waiting.append(f"{key}: not before {iso(not_before)}")
                return d
        order = ctx.variants if current % 2 == 0 else list(reversed(ctx.variants))
        for v in order:
            d.actions.extend(apply_actions(ctx, v, e))
    else:
        for v in ctx.variants:
            if not flags[v]:
                d.actions.extend(apply_actions(ctx, v, e))
            else:
                ci_status(ctx, v, e, d)

    for a in list(d.actions):
        if a["type"] == "exception":
            d.exceptions.append(a["message"])
            d.actions.remove(a)
    if d.exceptions:
        d.actions = []
    return d


def gate_status(ctx: _Ctx, g: dict, d: Decision) -> str:
    key = event_key(g)
    idx = ctx.events.index(g)
    later = [i for i in range(idx + 1, len(ctx.events)) if ctx.events[i]["type"] != "gate"]
    if any(applied(ctx, v, i) for v in ctx.variants for i in later):
        return "done"
    if key not in ctx.env.released_gates:
        d.waiting.append(f"{key}: waiting for release (add it to REPLAY_RELEASED_GATES)")
        return "waiting"
    # all earlier runs must be collected before anything is deleted
    for i in range(idx):
        e = ctx.events[i]
        if e["type"] == "gate":
            continue
        for v in ctx.variants:
            for r in runs_for(ctx, v, e):
                for attempt in range(1, r["run_attempt"] + 1):
                    if (v, r["id"], attempt) not in ctx.evidence:
                        d.waiting.append(f"{key}: waiting for collection of {v} run {r['id']} attempt {attempt}")
                        return "waiting"
    if g.get("action") == "delete-caches-and-artifacts":
        pending = False
        for v in ctx.variants:
            s = ctx.snap[v]
            if s["cache_count"] or s["artifact_count"]:
                pending = True
                d.actions.append({"variant": v, "event": key, "type": "delete_caches_and_artifacts", "repo": ctx.plan["variants"][v]["repo"]})
        if pending:
            return "running"
    return "done"
